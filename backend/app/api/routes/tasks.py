"""Asynchronous task routes: submit, poll, fetch result, cancel."""

from __future__ import annotations

from fastapi import APIRouter, Request, status

from app.api.deps import RateLimitDefault, RateLimitGenerate
from app.schemas.generation import (
    GenerateRequest,
    GenerateResponse,
    TaskStatusResponse,
    TaskSubmitResponse,
)
from app.services.tasks import task_manager

router = APIRouter(prefix="/tasks", tags=["异步任务 Tasks"])


@router.post(
    "",
    response_model=TaskSubmitResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="提交异步生成任务",
    description=(
        "提交后立即返回 `task_id`（HTTP 202），后台工作协程依次执行"
        " LLM 与 TTS 两步调用。使用 `GET /tasks/{task_id}` 轮询进度，"
        "完成后通过 `GET /tasks/{task_id}/result` 获取完整结果。\n\n"
        "**密钥安全**：密钥只保留在任务执行期间的内存中，任务结束后立即释放；"
        "数据库中只保存去掉密钥的请求快照。"
    ),
    responses={
        202: {"description": "任务已受理"},
        422: {"description": "请求参数校验失败"},
        429: {"description": "触发限流"},
        503: {"description": "任务队列已满"},
    },
)
async def submit_task(
    payload: GenerateRequest,
    request: Request,
    _: RateLimitGenerate,
) -> TaskSubmitResponse:
    request_id = getattr(request.state, "request_id", None)
    state = await task_manager.submit(payload, request_id=request_id)
    return TaskSubmitResponse(
        task_id=state.task_id,
        status=state.status,  # type: ignore[arg-type]
        stage=state.stage,  # type: ignore[arg-type]
        progress=state.progress,
        message=state.message,
    )


@router.get(
    "",
    summary="列出当前进程内的任务（调试用）",
    description="返回最近提交的任务状态快照，便于本地排查；生产环境可通过网关屏蔽。",
)
async def list_tasks(_: RateLimitDefault, limit: int = 50) -> dict:
    return {"items": task_manager.list_tasks(limit=max(1, min(200, limit)))}


@router.get(
    "/{task_id}",
    response_model=TaskStatusResponse,
    summary="查询任务状态与进度",
    description=(
        "`stage` 取值：`queued` → `llm` → `tts` → `persist` → `done`，"
        "配合 `progress`(0-100) 与 `message` 驱动前端进度条。\n\n"
        "`status` 中只有 `pending` / `running` 是**未结束**状态，出现其它取值即可停止轮询；"
        "其中 `interrupted` 表示服务重启导致任务中断，需重新提交。"
    ),
    responses={404: {"description": "任务不存在或已过期"}},
)
async def get_task(task_id: str, _: RateLimitDefault) -> TaskStatusResponse:
    state = task_manager.get(task_id)
    payload = state.to_dict()
    return TaskStatusResponse(**payload)


@router.get(
    "/{task_id}/result",
    response_model=GenerateResponse,
    summary="获取任务结果",
    description=(
        "任务未完成时返回 409 `TASK_NOT_FINISHED`；"
        "任务因服务重启而中断时返回 409 `TASK_INTERRUPTED`（需重新提交）。"
    ),
    responses={
        404: {"description": "任务不存在"},
        409: {"description": "任务尚未完成、已取消或已中断"},
        500: {"description": "任务执行失败"},
    },
)
async def get_task_result(task_id: str, _: RateLimitDefault) -> GenerateResponse:
    return GenerateResponse(**task_manager.result(task_id))


@router.delete(
    "/{task_id}",
    response_model=TaskStatusResponse,
    summary="取消任务",
    description=(
        "仅对 `pending` / `running` 状态的任务有效。"
        "已中断（`interrupted`）等已结束的任务返回 409 `TASK_ALREADY_FINISHED`。"
    ),
    responses={409: {"description": "任务已结束，无法取消"}},
)
async def cancel_task(task_id: str, _: RateLimitDefault) -> TaskStatusResponse:
    state = await task_manager.cancel(task_id)
    return TaskStatusResponse(**state.to_dict())