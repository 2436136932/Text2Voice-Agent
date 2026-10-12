"""Uvicorn entrypoint: ``python -m app.main`` (or ``python main.py``)."""

from __future__ import annotations

import uvicorn

from app.core.config import settings


def main() -> None:
    """Run the development/production server.

    In production prefer ``uvicorn app.main:app --workers N`` behind a reverse
    proxy; this entrypoint is convenient for local runs and service managers
    (systemd, NSSM, supervisord).
    """
    uvicorn.run(
        "app.api.app:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug and not settings.is_production,
        workers=1 if settings.debug else max(1, settings.workers),
        log_level=settings.log_level.lower(),
        access_log=False,  # our middleware logs redacted access lines
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":
    main()