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

Restart semantics
-----------------
The pool is process-local, so a restart orphans whatever was still in flight.
Those tasks are **not** resumable and must not pretend otherwise:

* their real ``GenerateRequest`` lives in ``_pending_payloads`` during the
  lifetime of the process (it holds the API keys, and only the
  credential-stripped snapshot is written to the ``tasks`` row), so nothing
  can re-run them after a restart;
* :meth:`TaskManager.restore_from_db` therefore rewrites every ``pending`` /
  ``running`` row it finds into the terminal ``interrupted`` status, with a
  ``TASK_INTERRUPTED`` error payload telling the user to resubmit.

Leaving them as ``pending``/``running`` instead is a permanent zombie: the row
has no worker and no payload, yet ``active_count()`` keeps counting it and the
UI keeps polling a task that will never move. Rehydrating rows verbatim was
exactly that bug. The rewrite is persisted, so a second boot does not re-mark
anything (and an already-interrupted row is left untouched).

The manager is process-local on purpose. For multi-worker deployments put a
real broker (Celery/RQ/Arq) behind the same ``submit/status/result`` contract -
the routes only depend on that interface. Such a broker is also what would be
needed to make in-flight tasks resumable across a restart.
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
from app.db.models import GenerationRecord, TaskModel, iso_utc
from app.schemas.generation import GenerateRequest
from app.services.generation import generation_service
from app.services.metrics import metrics

logger = get_logger(__name__)

#: Stage -> progress floor, used when a stage is entered without an explicit
#: percentage (keeps the UI bar monotonic).
STAGE_PROGRESS = {"queued": 0, "llm": 15, "tts": 60, "persist": 92, "done": 100}
#: Terminal, i.e. the UI stops polling. ``interrupted`` is terminal on purpose:
#: the worker that owned the task died with the process, so it will never
#: change state again (see the module docstring).
TERMINAL_STATUSES = {"succeeded", "failed", "canceled", "interrupted"}
#: Terminal state applied to a task whose worker did not survive a restart.
INTERRUPTED_STATUS = "interrupted"
#: Statuses that can only ever be transient within one process lifetime.
ACTIVE_STATUSES = {"pending", "running"}

#: User-facing explanation for a task orphaned by a restart. Deliberately
#: actionable: the payload (and its keys) is gone, so resubmitting is the only
#: way forward, and the message must say so instead of leaving a dead progress
#: bar on screen.
INTERRUPTED_MESSAGE = "服务已重启，该任务已中断，无法继续执行，请重新提交。"
INTERRUPTED_SUGGESTION = (
    "任务在服务重启时被中断，未完成的进度无法恢复（密钥不会落盘，因此无法自动续跑）。"
    "请返回生成页重新提交任务。"
)


#: Sentinel pushed onto the queue by :meth:`TaskManager.stop` to wake idle
#: workers so they can notice the shutdown flag and exit on their own, instead
#: of being cancelled mid-await (which cannot unwind a DB write cleanly).
_SHUTDOWN = "\x00__shutdown__"


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
        # Timestamps are marked UTC so the browser renders them in local time
        # instead of misreading naive UTC values as local.
        return {
            "task_id": self.task_id,
            "status": self.status,
            "stage": self.stage,
            "progress": self.progress,
            "message": self.message,
            "record_id": self.record_id,
            "created_at": iso_utc(self.created_at),
            "started_at": iso_utc(self.started_at),
            "finished_at": iso_utc(self.finished_at),
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
        #: Set while shutting down, so workers skip their blocking DB write.
        #: See the finally block in _worker(): awaiting persistence while being
        #: cancelled is what made stop() wait forever.
        self._shutting_down = False
        #: Payloads awaiting execution. They contain credentials, so they live
        #: in memory only and are dropped the moment the task finishes.
        self._pending_payloads: dict[str, GenerateRequest] = {}

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    async def start(self) -> None:
        if self._started:
            return
        # Clear any flag left over from a previous stop(), so a restart behaves
        # exactly like a first start.
        self._shutting_down = False
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
        # Workers consult this to wind down voluntarily. The old implementation
        # cancelled them instead, which is what hung shutdown: a worker killed
        # while parked inside `session_scope()` still has to run its teardown
        # (`await session.commit()`), and an aiosqlite commit does not observe
        # task cancellation -- it blocks on its worker thread. `gather()` then
        # waited forever, freezing the whole process (and, in CI, the suite).
        self._shutting_down = True
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
            self._cleanup_task = None

        # Wake every idle worker so it can see the flag. A worker blocked on
        # `queue.get()` is not awaiting anything cancellable-sensitive; putting
        # a sentinel in lets it return on its own.
        assert self._queue is not None
        for _ in self._workers:
            try:
                self._queue.put_nowait(_SHUTDOWN)
            except asyncio.QueueFull:  # pragma: no cover - pathological
                break

        if self._workers:
            # Bounded as a last resort: if any worker still cannot finish (a
            # provider call that ignores cancellation), shutdown proceeds
            # rather than hanging the process forever.
            try:
                await asyncio.wait_for(
                    asyncio.gather(*self._workers, return_exceptions=True),
                    timeout=10.0,
                )
            except (asyncio.TimeoutError, asyncio.CancelledError):
                logger.warning(
                    "task manager stop timed out; %s worker(s) did not finish",
                    len(self._workers),
                )
                # Cancel whatever is left, but do NOT await it -- that is the
                # operation that can never complete.
                for worker in self._workers:
                    worker.cancel()
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
        if state.status in ACTIVE_STATUSES:
            raise AppError(
                ErrorCode.TASK_NOT_FINISHED,
                "任务尚未完成，请稍后再试。",
                detail={"task_id": task_id, "status": state.status, "stage": state.stage},
                http_status=409,
            )
        if state.status == INTERRUPTED_STATUS:
            # Must be checked *before* the generic "result is missing" branch
            # below: an interrupted task legitimately has no result, and
            # reporting that as INTERNAL_ERROR/500 would tell the user the
            # server broke instead of "your task was interrupted, resubmit".
            raise AppError(
                ErrorCode.TASK_INTERRUPTED,
                (state.error or {}).get("message", INTERRUPTED_MESSAGE),
                detail={
                    "task_id": task_id,
                    "status": state.status,
                    "suggestion": INTERRUPTED_SUGGESTION,
                    "retryable": True,
                },
                http_status=409,
                retryable=True,
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
        # Only genuinely in-flight work counts as active; ``interrupted`` is
        # terminal (see ACTIVE_STATUSES), otherwise the health endpoint would
        # report a phantom load forever.
        return sum(1 for state in self._tasks.values() if state.status in ACTIVE_STATUSES)

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
            except asyncio.CancelledError:  # pragma: no cover - hard cancel
                return

            # Shutdown sentinel from stop(): exit cleanly *between* items,
            # where there is no half-finished DB write to unwind.
            if task_id == _SHUTDOWN:
                self._queue.task_done()
                logger.debug("worker %s exiting on shutdown", index)
                return

            # Also stop picking up new work once shutdown has been requested.
            if self._shutting_down:
                self._queue.task_done()
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
                # Shutdown is the one case where this must NOT await.
                #
                # `stop()` cancels the workers and then gathers them. If a
                # worker is cancelled while parked inside this finally block,
                # the `await self._persist(state)` never completes and the
                # gather waits forever -- the whole process hangs at constant
                # CPU. That is exactly the intermittent full-suite freeze seen
                # on this host (reproducible at test_tasks.py::TestRestore,
                # the last test that submits a task). Skipping the durable
                # write during shutdown is harmless: the row already exists
                # (it is written on submit) and the state is re-derived on the
                # next boot by restore_from_db(), which rewrites any
                # pending/running row to the terminal 'interrupted' status.
                if not self._shutting_down:
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

    @staticmethod
    def _interrupted_error(request_id: str | None) -> dict[str, Any]:
        """Error payload stored on a task orphaned by a restart.

        Shaped like every other :class:`AppError` envelope so the frontend can
        render it with the generic error renderer, with an actionable
        ``detail.suggestion`` (there is no way to resume — only to resubmit).
        """
        return AppError(
            ErrorCode.TASK_INTERRUPTED,
            INTERRUPTED_MESSAGE,
            detail={"suggestion": INTERRUPTED_SUGGESTION, "retryable": True},
            retryable=True,
        ).to_payload(request_id)

    @staticmethod
    def _interrupted_history_row(
        row: TaskModel, finished_at: datetime
    ) -> GenerationRecord | None:
        """Build the history row for a task lost to a restart.

        Task rows are never shown in the UI, so without this an interrupted
        attempt is invisible: the user sees nothing in history and has nothing
        to retry. Everything needed to rebuild the request comes from
        ``request_payload``, which is the credential-stripped snapshot -- there
        is deliberately no key in it, and none is written here.

        Returns ``None`` when the original request asked not to keep history, or
        when the payload is missing/unusable.
        """
        payload = row.request_payload or {}
        if not isinstance(payload, dict):
            return None

        options = payload.get("options") or {}
        if isinstance(options, dict) and options.get("auto_save") is False:
            return None

        llm = payload.get("llm") or {}
        tts = payload.get("tts") or {}
        if not isinstance(llm, dict) or not isinstance(tts, dict):
            return None

        text = payload.get("text")
        if not isinstance(text, str) or not text:
            return None

        return GenerationRecord(
            input_text=text,
            reply_text="",
            llm_provider=str(llm.get("provider") or ""),
            llm_model=str(llm.get("model") or ""),
            tts_provider=str(tts.get("provider") or ""),
            tts_voice=str(tts.get("voice") or ""),
            tts_model=str(tts.get("model") or ""),
            audio_format=str(tts.get("format") or ""),
            audio_bytes=0,
            audio_path="",
            status=INTERRUPTED_STATUS,
            error_code=ErrorCode.TASK_INTERRUPTED.value,
            error_message=INTERRUPTED_MESSAGE,
            request_id=row.request_id,
            created_at=_as_aware_utc(row.created_at) or finished_at,
        )

    async def restore_from_db(self) -> None:
        """Rehydrate tasks after a restart, closing out the orphaned ones.

        Finished rows come back as they were. ``pending``/``running`` rows
        cannot: their worker is gone with the previous process and their real
        payload (which holds the API keys) was never persisted, so they are
        rewritten to the terminal ``interrupted`` state — in memory *and* in
        the table, so the next boot finds an already-terminal row and leaves it
        alone.

        Best effort: a database problem must not stop the app from booting.
        """
        try:
            from sqlalchemy import select

            async with session_scope() as session:
                rows = (
                    await session.execute(
                        select(TaskModel).order_by(TaskModel.created_at.desc()).limit(100)
                    )
                ).scalars().all()
                # Only rows that were still in flight are rewritten; anything
                # already terminal (including a row interrupted on an earlier
                # boot) is restored verbatim, which is what makes this
                # idempotent.
                orphans = [row for row in rows if row.status in ACTIVE_STATUSES]
                finished_at = datetime.now(timezone.utc)
                for row in orphans:
                    # Started tasks keep a progress value worth showing; queued
                    # ones never started, so they stay at their 0.
                    row.status = INTERRUPTED_STATUS
                    row.stage = "done"
                    row.message = INTERRUPTED_MESSAGE[:255]
                    row.error = self._interrupted_error(row.request_id)
                    if row.finished_at is None:
                        row.finished_at = finished_at
                    # A task without a start time never began executing, so its
                    # elapsed time must stay unknown instead of being invented.
                    if row.started_at is not None:
                        row.elapsed_ms = max(
                            0,
                            int(
                                (
                                    _as_aware_utc(row.finished_at)
                                    - _as_aware_utc(row.started_at)
                                ).total_seconds()
                                * 1000
                            ),
                        )
                    # Also surface it in history, so the user can see that the
                    # attempt was lost and retry it. Without this the task row is
                    # the only trace, and task rows are never shown in the UI.
                    # Safe against duplicates: this branch runs only for rows
                    # that were pending/running, and it rewrites them to a
                    # terminal state in the same transaction.
                    history_row = self._interrupted_history_row(row, finished_at)
                    if history_row is not None:
                        session.add(history_row)
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
            if orphans:
                logger.warning(
                    "marked interrupted tasks count=%s (worker lost in a restart)", len(orphans)
                )
            logger.info("restored tasks from database count=%s", len(rows))
        except Exception as exc:  # noqa: BLE001
            logger.warning("task restore skipped error=%s", exc)


task_manager = TaskManager()