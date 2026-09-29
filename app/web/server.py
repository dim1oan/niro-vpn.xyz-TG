"""aiohttp-приложение: вебхуки ЮKassa + healthz (webhook-режим и для polling тоже)."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from aiohttp import web
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_config
from app.handlers.payments_yookassa import handle_payment_succeeded, is_yookassa_ip
from app.utils.logging import get_logger

if TYPE_CHECKING:
    from aiogram import Bot

log = get_logger("web")

_session_factory: async_sessionmaker[AsyncSession] | None = None
_bot: Bot | None = None


def set_dependencies(session_factory: async_sessionmaker[AsyncSession], bot: Bot) -> None:
    global _session_factory, _bot
    _session_factory = session_factory
    _bot = bot


async def healthz(_: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def yookassa_webhook(request: web.Request) -> web.Response:
    remote = request.remote or ""
    if not is_yookassa_ip(remote):
        log.warning("yookassa.webhook_bad_ip", ip=remote)
        raise web.HTTPForbidden(text="forbidden")
    try:
        event = await request.json()
    except Exception as err:
        raise web.HTTPBadRequest(text="invalid json") from err

    if event.get("event") != "payment.succeeded":
        return web.json_response({"ok": True})

    if _session_factory is None or _bot is None:
        raise web.HTTPServiceUnavailable(text="bot not ready")

    try:
        ok = await handle_payment_succeeded(_session_factory, {**event, "bot": _bot})
    except Exception as e:  # noqa: BLE001
        log.error("yookassa.webhook_error", err=str(e))
        return web.json_response({"ok": False}, status=500)
    return web.json_response({"ok": bool(ok)})


def build_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/healthz", healthz)
    app.router.add_post("/webhooks/yookassa", yookassa_webhook)
    return app


async def run_webhook_app(bot: Bot, session_factory) -> None:
    """Запуск aiohttp в отдельной задаче (для webhook-режима)."""
    cfg = get_config()
    set_dependencies(session_factory, bot)
    runner = web.AppRunner(build_app())
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=cfg.web_port)
    await site.start()
    log.info("web.started", port=cfg.web_port)
    await asyncio.Event().wait()
