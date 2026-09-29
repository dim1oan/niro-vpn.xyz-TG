"""Фабрика бота и диспетчера."""

from __future__ import annotations

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.base import BaseStorage
from aiogram.fsm.storage.memory import MemoryStorage

from app.config import get_config


def make_bot() -> Bot:
    import os

    cfg = get_config()
    session = None
    proxy = os.getenv("HTTPS_PROXY") or os.getenv("https_proxy") or os.getenv("HTTP_PROXY") or os.getenv("http_proxy")
    if proxy:
        from aiogram.client.session.aiohttp import AiohttpSession

        session = AiohttpSession(proxy=proxy)
    return Bot(
        token=cfg.bot_token,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


def make_storage() -> BaseStorage:
    cfg = get_config()
    if cfg.redis_url:
        try:
            from aiogram.fsm.storage.redis import RedisStorage

            return RedisStorage.from_url(cfg.redis_url)
        except ImportError:
            pass
    return MemoryStorage()


def build_dispatcher() -> Dispatcher:
    from app.handlers import (
        admin,
        buy,
        guides,
        keys,
        payments_manual,
        payments_stars,
        profile,
        site,
        start,
        support,
        trial,
    )

    dp = Dispatcher(storage=make_storage())

    # порядок важен: платёжные колбэки до общих
    for r in (
        payments_stars.router,
        payments_manual.router,
        start.router,
        site.router,
        buy.router,
        trial.router,
        keys.router,
        profile.router,
        guides.router,
        support.router,
        admin.router,
    ):
        dp.include_router(r)
    return dp


def register_middlewares(dp: Dispatcher, session_factory) -> None:
    from app.middlewares.db import DbSessionMiddleware
    from app.middlewares.user import UserContextMiddleware
    from app.utils.rate_limit import ThrottlingMiddleware

    for observer in (dp.message, dp.callback_query):
        observer.middleware(DbSessionMiddleware(session_factory))
        observer.middleware(UserContextMiddleware())
    dp.message.middleware(ThrottlingMiddleware())
