"""Shared FastAPI dependencies: DB session, rate limiting, pagination."""

from __future__ import annotations

from typing import Annotated, AsyncIterator

from fastapi import Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.rate_limit import limiter
from app.db.base import get_session

SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def rate_limit_default(request: Request) -> float:
    """Generic per-IP budget applied to every API route."""
    identity = getattr(request.state, "client_identity", None) or "unknown"
    return await limiter.check(
        bucket="default",
        identity=identity,
        capacity=settings.rate_limit_requests,
        window_seconds=settings.rate_limit_window_seconds,
    )


async def rate_limit_generate(request: Request) -> float:
    """Stricter budget for endpoints that spend money upstream."""
    identity = getattr(request.state, "client_identity", None) or "unknown"
    return await limiter.check(
        bucket="generate",
        identity=identity,
        capacity=settings.rate_limit_generate_requests,
        window_seconds=settings.rate_limit_generate_window_seconds,
    )


RateLimitDefault = Annotated[float, Depends(rate_limit_default)]
RateLimitGenerate = Annotated[float, Depends(rate_limit_generate)]


class Pagination:
    """Validated pagination parameters."""

    __slots__ = ("page", "page_size")

    def __init__(self, page: int, page_size: int) -> None:
        self.page = page
        self.page_size = page_size

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size


def pagination_params(
    page: Annotated[int, Query(ge=1, le=10_000, description="页码，从 1 开始")] = 1,
    page_size: Annotated[int, Query(ge=1, le=100, description="每页条数，最大 100")] = 20,
) -> Pagination:
    """FastAPI dependency returning a validated :class:`Pagination`."""
    return Pagination(page=page, page_size=page_size)


PaginationDep = Annotated[Pagination, Depends(pagination_params)]


async def session_iterator() -> AsyncIterator[AsyncSession]:  # pragma: no cover
    """Typing helper kept for symmetry with ``SessionDep``."""
    async for session in get_session():
        yield session