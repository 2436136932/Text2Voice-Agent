"""Uvicorn entrypoint: ``python -m app.main`` (or ``python main.py``)."""

from __future__ import annotations

import uvicorn

from app.core.config import settings


def main() -> None:
    """Run the development/production server.

    Single-process only: the task queue, rate limiter and metrics are
    in-process state, so ``workers > 1`` would split them and lose tasks.
    See ``app.api.app._assert_single_process`` for the full reasoning.

    This entrypoint is convenient for local runs and service managers
    (systemd, NSSM, supervisord). To serve more concurrency, raise
    ``T2V_TASK_WORKERS`` rather than the process count.
    """
    workers = 1 if settings.debug else max(1, settings.workers)
    if workers > 1:
        raise SystemExit(
            f"T2V_WORKERS={workers} 不受支持：任务队列/限流器/指标均为进程内状态，"
            "多进程会导致任务丢失与重复执行。\n"
            "请设置 T2V_WORKERS=1（默认），需要更高并发时调大 T2V_TASK_WORKERS。"
        )
    uvicorn.run(
        "app.api.app:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug and not settings.is_production,
        workers=workers,
        log_level=settings.log_level.lower(),
        access_log=False,  # our middleware logs redacted access lines
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":
    main()