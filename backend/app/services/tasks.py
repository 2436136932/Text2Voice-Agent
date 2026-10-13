"""Asynchronous task manager for long-running generations.

Why a task queue at all?
------------------------
A generation is two sequential third-party calls (LLM + TTS) and routinely
takes 3-15 seconds. Holding the HTTP connection open is fine for the
synchronous ``POST /generate`` route, but the UI wants *progress*, so the
platform also exposes an async mode:

``POST /tasks`` -> 202 with a ``task_id``
``GET  /tasks/{id}`` -> poll status/stage/progress
``GET  /tasks/{id}/result`` -> the finished payload

Design
------
* a bounded :class:`asyncio.Queue` feeds N worker coroutines (``task_workers``);
* every state change is mirrored to the ``tasks`` table, so status survives a
  page reload and is auditable;
* the in-memory copy serves the hot polling path (no DB hit per poll);
* submissions are rejected with ``TASK_QUEUE_FULL`` when the backlog exceeds
  ``task_queue_size``, which is the backpressure signal the UI shows;
* finished tasks are garbage collected after ``task_retention_minutes``.

The manager is process-local on purpose. For multi-worker deployments put a
real broker (Celery/RQ/Arq) behind the same ``submit/status/result`` contract -
the routes only depend on that interface.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger
from app.db.base import session_scope
from app.db.models import TaskModel
from app.schemas.generation import GenerateRequest
from app.services.generation import generation_service
from app.services.metrics import metrics

logger = get_logger(__name__)

#: Stage -> progress floor, used when a stage is entered without an explicit
#: percentage (keeps the UI bar monotonic).
STAGE_PROGRESS = {"queued": 0, "llm": 15, "tts": 60, "persist": 92, "done": 100}
TERMINAL_STATUSES = {"succeeded", "failed", "canceled"}


def _as_aware_utc(value: datetime | None) -> datetime | None:
    """Return ``value`` as a timezone-aware UTC datetime.

    SQLite does not preserve tzinfo, so datetimes loaded back from the task
    table are naive. Comparing one of those against ``datetime.now(timezone.utc)``
    raises ``TypeError: can't compare offset-naive and offset-aware datetimes``,
    which used to crash the retention sweep on every run. Treat naive values as
    UTC (that is how they were written) and normalise anything else to UTC.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@dataclass
class TaskState:
    """In-memory mirror of a task row (hot path for polling)."""

    task_id: str
    status: str = "pending"
    stage: str = "queued"
    progress: int = 0
    message: str = "任务已提交"
    record_id: int | None = None
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: datetime | None = None
    finished_at: datetime | None = None
    request_payload: dict[str, Any] | None = None
    request_id: str | None = None
    handle: asyncio.Task | None = None

    @property
    def elapsed_ms(self) -> int | None:
        if self.started_at is None:
            return None
        # Both sides must be tz-aware: restored rows are naive (SQLite drops
        # tzinfo) while ``now`` is aware, and subtracting the two raises.
        start = _as_aware_utc(self.started_at)
        end = _as_aware_utc(self.finished_at) or datetime.now(timezone.utc)
        if start is None:
            return None
        return max(0, int((end - start).total_seconds() * 1000))

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "stage": self.stage,
            "progress": self.progress,
            "message": self.message,
            "record_id": self.record_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "elapsed_ms": self.elapsed_ms,
            "error": self.error,
        }


class TaskManager:
    """Bounded worker pool executing generation tasks."""

    def __init__(self) -> None:
        self._tasks: dict[str, TaskState] = {}
        self._queue: asyncio.Queue[str] | None = None
        self._workers: list[asyncio.Task] = []
        self._cleanup_task: asyncio.Task | None = None
        self._started = False
        self._lock = asyncio.Lock()
        #: Payloads awaiting execution. They contain credentials, so they live
        #: in memory only and are dropped the moment the task finishes.
        self._pending_payloads: dict[str, GenerateRequest] = {}

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    async def start(self) -> None:
        if self._started:
            return
        self._queue = asyncio.Queue(maxsize=settings.task_queue_size)
        for index in range(max(1, settings.task_workers)):
            self._workers.append(
                asyncio.create_task(self._worker(index), name=f"t2v-worker-{index}")
            )
        self._cleanup_task = asyncio.create_task(self._cleanup_loop(), name="t2v-task-cleanup")
        self._started = True
        logger.info(
            "task manager started workers=%s queue_size=%s",
            settings.task_workers, settings.task_queue_size,
        )

    async def stop(self) -> None:
        self._started = False
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
            self._cleanup_task = None
        for worker in self._workers:
            worker.cancel()
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        self._queue = None
        logger.info("task manager stopped")

    @property
    def started(self) -> bool:
        return self._started

    # ------------------------------------------------------------------
    # submission
    # ------------------------------------------------------------------
    async def submit(
        self, request: GenerateRequest, *, request_id: str | None = None
    ) -> TaskState:
        if not self._started or self._queue is None:
            raise AppError(
                ErrorCode.SERVICE_UNAVAILABLE,
                "异步任务队列尚未就绪。",
                http_status=503,
                retryable=True,
            )
        if self._queue.full():
            raise AppError(
                ErrorCode.TASK_QUEUE_FULL,
                f"任务队列已满（{settings.task_queue_size}），请稍后重试。",
                detail={"queue_size": settings.task_queue_size},
                http_status=503,
                retryable=True,
            )

        task_id = uuid.uuid4().hex
        state = TaskState(
            task_id=task_id,
            request_payload=request.safe_dump(),
            request_id=request_id,
        )
        async with self._lock:
            self._tasks[task_id] = state

        # Durable row first: a crash between here and execution still leaves
        # an auditable "pending" task rather than a silent loss.
        async with session_scope() as session:
            session.add(
                TaskModel(
                    task_id=task_id,
                    status="pending",
                    stage="queued",
                    progress=0,
                    message="任务已提交",
                    request_payload=state.request_payload,
                    request_id=request_id,
                    created_at=state.created_at,
                )
            )

        # The request object (and therefore the keys) is held only in memory
        # for the lifetime of the task.
        self._pending_payloads[task_id] = request
        await self._queue.put(task_id)
        metrics.incr("tasks_submitted")
        logger.info(
            "task submitted task_id=%s llm=%s tts=%s request_id=%s",
            task_id, request.llm.provider, request.tts.provider, request_id,
        )
        return state

    # ------------------------------------------------------------------
    # queries
    # ------------------------------------------------------------------
    def get(self, task_id: str) -> TaskState:
        state = self._tasks.get(task_id)
        if state is None:
            raise AppError(
                ErrorCode.TASK_NOT_FOUND,
                f"任务 {task_id} 不存在或已过期。",
                detail={"task_id": task_id},
                http_status=404,
            )
        return state

    def result(self, task_id: str) -> dict[str, Any]:
        state = self.get(task_id)
        if state.status in {"pending", "running"}:
            raise AppError(
                ErrorCode.TASK_NOT_FINISHED,
                "任务尚未完成，请稍后再试。",
                detail={"task_id": task_id, "status": state.status, "stage": state.stage},
                http_status=409,
            )
        if state.status == "failed":
            raise AppError(
                ErrorCode.INTERNAL_ERROR,
                (state.error or {}).get("message", "任务执行失败。"),
                detail={"task_id": task_id, "error": state.error},
                http_status=500,
            )
        if state.status == "canceled":
            raise AppError(
                ErrorCode.TASK_CANCELED,
                "任务已被取消。",
                detail={"task_id": task_id},
                http_status=409,
            )
        if state.result is None:
            raise AppError(
                ErrorCode.INTERNAL_ERROR,
                "任务结果丢失。",
                detail={"task_id": task_id},
                http_status=500,
            )
        return state.result

    async def cancel(self, task_id: str) -> TaskState:
        state = self.get(task_id)
        if state.status in TERMINAL_STATUSES:
            raise AppError(
                ErrorCode.TASK_ALREADY_FINISHED,
                f"任务已处于 {state.status} 状态，无法取消。",
                detail={"task_id": task_id, "status": state.status},
                http_status=409,
            )
        if state.handle is not None and not state.handle.done():
            state.handle.cancel()
        state.status = "canceled"
        state.stage = "done"
        state.message = "任务已取消"
        state.finished_at = datetime.now(timezone.utc)
        await self._persist(state)
        logger.info("task canceled task_id=%s", task_id)
        return state

    def active_count(self) -> int:
        return sum(1 for state in self._tasks.values() if state.status in {"pending", "running"})

    def list_tasks(self, *, limit: int = 50) -> list[dict[str, Any]]:
        # Normalise before sorting: restored rows carry naive timestamps, and
        # mixing them with tz-aware ones raises TypeError inside sorted().
        ordered = sorted(
            self._tasks.values(),
            key=lambda s: _as_aware_utc(s.created_at) or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        return [state.to_dict() for state in ordered[:limit]]

    # ------------------------------------------------------------------
    # worker internals
    # ------------------------------------------------------------------
    async def _worker(self, index: int) -> None:
        assert self._queue is not None
        logger.debug("worker %s ready", index)
        while True:
            try:
                task_id = await self._queue.get()
            except asyncio.CancelledError:  # pragma: no cover - shutdown
                return

            state = self._tasks.get(task_id)
            if state is None or state.status == "canceled":
                self._queue.task_done()
                continue

            payload = self._pending_payloads.pop(task_id, None)
            if payload is None:
                await self._fail(
                    state,
                    AppError(ErrorCode.INTERNAL_ERROR, "任务负载丢失，请重新提交。"),
                )
                self._queue.task_done()
                continue

            state.status = "running"
            state.started_at = datetime.now(timezone.utc)
            state.stage = "llm"
            state.progress = 5
            state.message = "正在调用大模型…"
            metrics.incr("tasks_active")
            await self._persist(state)

            try:
                state.handle = asyncio.current_task()
                async with session_scope() as session:
                    response = await generation_service.generate(
                        session,
                        payload,
                        on_progress=self._make_progress_callback(state),
                        request_id=state.request_id,
                    )
                state.result = response.model_dump(mode="json")
                state.record_id = response.record_id
                state.status = "succeeded"
                state.stage = "done"
                state.progress = 100
                state.message = "生成完成"
            except asyncio.CancelledError:
                state.status = "canceled"
                state.stage = "done"
                state.message = "任务已取消"
                logger.info("task canceled during execution task_id=%s", task_id)
            except AppError as exc:
                await self._fail(state, exc)
            except Exception as exc:  # noqa: BLE001 - never kill the worker
                logger.exception("task crashed task_id=%s", task_id)
                await self._fail(
                    state,
                    AppError(
                        ErrorCode.INTERNAL_ERROR,
                        f"任务执行异常：{type(exc).__name__}",
                        detail={"task_id": task_id},
                    ),
                )
            finally:
                metrics.incr("tasks_active", -1)
                if state.finished_at is None:
                    state.finished_at = datetime.now(timezone.utc)
                self._pending_payloads.pop(task_id, None)
                state.handle = None
                await self._persist(state)
                self._queue.task_done()

    def _make_progress_callback(self, state: TaskState):
        """Throttled progress reporter: DB writes only on stage transitions."""
        last_stage = {"value": state.stage}

        def callback(stage: str, percent: int, message: str) -> None:
            state.stage = stage
            state.progress = max(state.progress, percent)
            state.message = message
            if stage != last_stage["value"]:
                last_stage["value"] = stage
                # Schedule a durable write without blocking the pipeline.
                asyncio.create_task(self._persist(state))

        return callback

    async def _fail(self, state: TaskState, error: AppError) -> None:
        state.status = "failed"
        state.stage = "done"
        state.error = error.to_payload(state.request_id)
        state.message = error.message
        state.finished_at = datetime.now(timezone.utc)
        metrics.incr("generations_failed")
        logger.error(
            "task failed task_id=%s code=%s message=%s",
            state.task_id, error.code.value, error.message,
        )

    async def _persist(self, state: TaskState) -> None:
        """Mirror the in-memory state into the ``tasks`` table."""
        try:
            async with session_scope() as session:
                row = await session.get(TaskModel, state.task_id)
                if row is None:
                    row = TaskModel(task_id=state.task_id)
                    session.add(row)
                row.status = state.status
                row.stage = state.stage
                row.progress = state.progress
                row.message = state.message[:255]
                row.record_id = state.record_id
                row.result = state.result
                row.error = state.error
                row.started_at = state.started_at
                row.finished_at = state.finished_at
                row.elapsed_ms = state.elapsed_ms
        except Exception as exc:  # noqa: BLE001 - persistence must not break tasks
            logger.warning("failed to persist task state task_id=%s error=%s", state.task_id, exc)

    async def _cleanup_loop(self) -> None:
        """Drop finished tasks after the retention window."""
        while True:
            try:
                await asyncio.sleep(60)
                cutoff = datetime.now(timezone.utc) - timedelta(
                    minutes=max(1, settings.task_retention_minutes)
                )
                async with self._lock:
                    stale = [
                        task_id
                        for task_id, state in self._tasks.items()
                        if state.status in TERMINAL_STATUSES
                        and (
                            _as_aware_utc(state.finished_at or state.created_at) or cutoff
                        )
                        < cutoff
                    ]
                    for task_id in stale:
                        self._tasks.pop(task_id, None)
                        self._pending_payloads.pop(task_id, None)
                if stale:
                    logger.info("cleaned up finished tasks count=%s", len(stale))
            except asyncio.CancelledError:  # pragma: no cover - shutdown
                return
            except Exception:  # noqa: BLE001
                logger.exception("task cleanup loop error")

    async def restore_from_db(self) -> None:
        """Rehydrate finished tasks after a restart (best effort)."""
        try:
            from sqlalchemy import select

            async with session_scope() as session:
                rows = (
                    await session.execute(
                        select(TaskModel).order_by(TaskModel.created_at.desc()).limit(100)
                    )
                ).scalars().all()
            for row in rows:
                self._tasks[row.task_id] = TaskState(
                    task_id=row.task_id,
                    status=row.status,
                    stage=row.stage,
                    progress=row.progress,
                    message=row.message,
                    record_id=row.record_id,
                    result=row.result,
                    error=row.error,
                    # SQLite hands back naive datetimes; store them tz-aware so
                    # every later comparison (retention sweep, elapsed time) is
                    # mixing like with like.
                    created_at=_as_aware_utc(row.created_at) or datetime.now(timezone.utc),
                    started_at=_as_aware_utc(row.started_at),
                    finished_at=_as_aware_utc(row.finished_at),
                    request_id=row.request_id,
                )
            logger.info("restored tasks from database count=%s", len(rows))
        except Exception as exc:  # noqa: BLE001
            logger.warning("task restore skipped error=%s", exc)


task_manager = TaskManager()