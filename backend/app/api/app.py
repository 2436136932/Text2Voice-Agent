"""Application factory: middleware, routers, lifespan and OpenAPI metadata."""

from __future__ import annotations

import asyncio
import contextlib
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import text

from app.adapters.base import shutdown_http_client
from app.adapters.registry import registry
from app.api.errors import register_exception_handlers
from app.api.middleware import RequestContextMiddleware
from app.api.router import api_router
from app.core.config import settings
from app.core.errors import DEFAULT_STATUS, ErrorCode
from app.core.logging import configure_logging, get_logger
from app.db.base import dispose_db, get_sessionmaker, init_db
from app.schemas.generation import ErrorResponse, HealthResponse
from app.services.history import history_service
from app.services.media import media_store
from app.services.tasks import task_manager

logger = get_logger(__name__)


async def _retention_loop() -> None:
    """Daily housekeeping: purge records past the retention window.

    Covers both generated audio (history) and the scratch uploads under
    ``data/media`` - the latter are reference clips that have already served
    their purpose once a voice is cloned.
    """
    if settings.audio_retention_days <= 0 and settings.media_retention_days <= 0:
        return
    while True:
        try:
            await asyncio.sleep(24 * 3600)
            if settings.audio_retention_days > 0:
                async with get_sessionmaker()() as session:
                    removed = await history_service.purge_older_than(
                        session, days=settings.audio_retention_days
                    )
                    await session.commit()
                if removed:
                    logger.info("retention job removed records=%s", removed)
            if settings.media_retention_days > 0:
                purged = media_store.purge_older_than(days=settings.media_retention_days)
                if purged:
                    logger.info("retention job purged media=%s", purged)
        except asyncio.CancelledError:  # pragma: no cover
            return
        except Exception:  # noqa: BLE001
            logger.exception("retention job failed")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup/shutdown: DB schema, task workers, HTTP pool, retention job."""
    configure_logging()
    logger.info(
        "starting %s v%s environment=%s debug=%s",
        settings.app_name, settings.version, settings.environment, settings.debug,
    )

    await init_db()
    await task_manager.start()
    await task_manager.restore_from_db()
    retention_task = asyncio.create_task(_retention_loop(), name="t2v-retention")

    logger.info(
        "ready providers llm=%s tts=%s docs=%s",
        len(registry.llm_ids()),
        len(registry.tts_ids()),
        "disabled" if settings.disable_docs else f"{settings.api_prefix}/docs",
    )
    try:
        yield
    finally:
        logger.info("shutting down")
        retention_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await retention_task
        await task_manager.stop()
        await shutdown_http_client()
        await dispose_db()
        logger.info("shutdown complete")


DESCRIPTION = """
**Text2Voice Agent** —— 把大模型的回复变成可以直接播放的语音。

### 核心链路
`用户文本` → **LLM 适配层**（OpenAI / Anthropic / 通义千问 / DeepSeek / Gemini / 智谱 / Moonshot / Ollama）
→ `回复文本` → **TTS 适配层**（OpenAI TTS / ElevenLabs / Azure Speech / 火山引擎 / MiniMax / Edge / 本地模拟）
→ `音频文件` → 历史记录。

### 密钥安全（重要）
* 所有 `api_key` 字段均为 `SecretStr`：不会出现在日志、`repr()` 或 OpenAPI 示例中；
* 密钥**仅单次转发**给上游服务商，不写入数据库、不写入磁盘；
* 数据库中只保存不可逆的 SHA-256 指纹（`key_fingerprint`），用于审计；
* 请求日志经过统一脱敏过滤器（`sk-*`、`Bearer`、UUID、32 位十六进制等一律打码）。

### 两种调用方式
| 模式 | 接口 | 适用场景 |
| --- | --- | --- |
| 同步 | `POST /generate` | 脚本、一次性调用，直接拿结果 |
| 异步 | `POST /tasks` + `GET /tasks/{id}` | Web 界面，需要进度反馈 |

### 声音克隆
上传参考音频 → 解析/裁剪 → 交给服务商克隆 → 新音色自动出现在音色列表。

1. `GET /capabilities` —— 先探测本机能力（是否装了 ffmpeg、哪些服务商支持克隆）；
2. `POST /media/inspect` —— 上传参考音频/视频，拿到 `media_id` 与时长；
3. `POST /media/trim` —— 可选，裁出 10–30 秒的高质量片段（WAV 原生裁剪，其他格式需 ffmpeg）；
4. `POST /voices/clone` —— 真正发起克隆，返回 `voice_id`；
5. `GET /providers` —— 新音色已并入对应服务商的 `voices` 列表（`cloned=true`），可直接用于生成。

> **没有 ffmpeg 也能用**：仅 WAV 的裁剪与音轨提取受限，克隆主流程不受影响。
> 安装命令：`winget install Gyan.FFmpeg`。

### 错误约定
所有非 2xx 响应统一为：`{"code", "message", "detail", "request_id"}`。
面向用户的错误会在 `detail.suggestion` 中给出**中文操作建议**（例如该换成哪个模型、
该装什么软件），前端可直接展示该字段。
完整错误码字典见 `GET /meta/error-codes`。
"""

TAGS_METADATA = [
    {"name": "服务商 Providers", "description": "服务商能力目录与密钥校验（不落库）"},
    {"name": "生成 Generation", "description": "同步生成：文本 → 大模型回复 → 语音"},
    {"name": "异步任务 Tasks", "description": "提交、轮询进度、获取结果、取消"},
    {"name": "历史记录 History", "description": "分页查询、详情、删除、统计与导出"},
    {"name": "音频 Audio", "description": "音频流播放与下载（支持 Range）"},
    {"name": "媒体 Media", "description": "参考音频/视频上传、解析、裁剪与预览（WAV 原生支持，其他格式需 ffmpeg）"},
    {"name": "声音克隆 Voices", "description": "用参考音频克隆音色、列出与删除「我的音色」（密钥不落库）"},
    {"name": "系统设置 Settings", "description": "用户偏好设置（不含任何密钥）"},
    {"name": "系统 System", "description": "健康检查与环境能力探测（ffmpeg / 声音克隆能力）"},
    {"name": "元信息 Meta", "description": "错误码字典、运行指标、非敏感配置"},
]


def _custom_openapi(app: FastAPI) -> dict:
    """Inject the shared error envelope into every documented response."""
    if app.openapi_schema:
        return app.openapi_schema

    schema = get_openapi(
        title=settings.app_name,
        version=settings.version,
        description=DESCRIPTION,
        routes=app.routes,
        tags=TAGS_METADATA,
    )
    schema.setdefault("components", {}).setdefault("schemas", {})
    error_schema = ErrorResponse.model_json_schema(ref_template="#/components/schemas/{model}")
    for name, definition in (error_schema.pop("$defs", {}) or {}).items():
        schema["components"]["schemas"].setdefault(name, definition)
    schema["components"]["schemas"]["ErrorResponse"] = error_schema

    for path, methods in schema.get("paths", {}).items():
        for method, operation in methods.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            responses = operation.setdefault("responses", {})
            for status_code in ("400", "401", "422", "429", "500", "502", "503", "504"):
                entry = responses.get(status_code)
                if entry and "content" not in entry:
                    entry["content"] = {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/ErrorResponse"}
                        }
                    }
    schema["info"]["x-error-codes"] = {
        code.value: DEFAULT_STATUS.get(code, 500) for code in ErrorCode
    }
    app.openapi_schema = schema
    return schema


def create_app() -> FastAPI:
    """Build the FastAPI application."""
    configure_logging()

    app = FastAPI(
        title=settings.app_name,
        version=settings.version,
        description=DESCRIPTION,
        openapi_tags=TAGS_METADATA,
        lifespan=lifespan,
        docs_url=None if settings.disable_docs else "/docs",
        redoc_url=None if settings.disable_docs else "/redoc",
        openapi_url=None if settings.disable_docs else "/openapi.json",
        contact={"name": "Text2Voice Agent", "url": "https://github.com/"},
        license_info={"name": "MIT"},
        swagger_ui_parameters={
            "docExpansion": "list",
            "defaultModelsExpandDepth": 1,
            "displayRequestDuration": True,
            "filter": True,
            "tryItOutEnabled": True,
            "persistAuthorization": False,
        },
    )

    # -- middleware (order matters: outermost first) ------------------------
    app.add_middleware(RequestContextMiddleware)
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=False,  # credentials travel in the body, not cookies
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["Content-Type", "Authorization", "X-Request-ID", "Accept"],
            expose_headers=["X-Request-ID", "X-Response-Time", "Content-Disposition"],
            max_age=600,
        )

    register_exception_handlers(app)

    # -- routes -------------------------------------------------------------
    app.include_router(api_router, prefix=settings.api_prefix)

    # Root-level health alias for process supervisors and the frontend indicator.
    @app.get(
        "/health",
        response_model=HealthResponse,
        tags=["系统 System"],
        summary="健康检查（根路径别名）",
    )
    async def root_health() -> HealthResponse:
        import time

        from app.services.metrics import metrics

        database = "ok"
        try:
            async with get_sessionmaker()() as session:
                await session.execute(text("SELECT 1"))
        except Exception as exc:  # noqa: BLE001
            database = f"error: {type(exc).__name__}"
        return HealthResponse(
            status="ok" if database == "ok" else "degraded",
            version=settings.version,
            uptime_seconds=metrics.uptime_seconds,
            environment=settings.environment,
            database=database,
            providers={"llm": len(registry.llm_ids()), "tts": len(registry.tts_ids())},
        )

    @app.get("/", include_in_schema=False)
    async def index() -> JSONResponse:
        return JSONResponse(
            {
                "name": settings.app_name,
                "version": settings.version,
                "api_prefix": settings.api_prefix,
                "docs": None if settings.disable_docs else "/docs",
                "health": "/health",
                "providers": f"{settings.api_prefix}/providers",
            }
        )

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> RedirectResponse:
        return RedirectResponse(url="/docs", status_code=307)

    app.openapi = lambda: _custom_openapi(app)  # type: ignore[method-assign]
    return app


app = create_app()