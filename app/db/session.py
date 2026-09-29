"""Асинхронный движок и фабрика сессий.

SQLite живёт в одном файле с сайтом: WAL, busy_timeout 30с и короткие
транзакции — чтобы два процесса (бот + сайт) не блокировали друг друга.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import Config


def make_engine(cfg: Config) -> AsyncEngine:
    url = cfg.database_url
    if url.startswith("sqlite"):
        # гарантируем существование директории под файл БД
        m = re.search(r"///(.+)$", url)
        if m and (path := Path(m.group(1))).parent != Path("."):
            path.parent.mkdir(parents=True, exist_ok=True)
        engine = create_async_engine(
            url,
            echo=False,
            connect_args={"timeout": 30},
        )
    else:
        engine = create_async_engine(
            url, echo=False, pool_size=10, max_overflow=20, pool_pre_ping=True
        )

    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, _record) -> None:  # noqa: ANN001
        if not url.startswith("sqlite"):
            return
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA busy_timeout=30000")
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
        except Exception:  # noqa: BLE001 — WAL уже включён
            pass
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def session_scope(factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    """Сессия с авто-коммитом/откатом — для фоновых задач и вебхуков."""
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
