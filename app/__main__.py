"""Entrypoint: long polling (по умолчанию) или webhook-режим."""

from __future__ import annotations

import asyncio
import contextlib

from app.bot import build_dispatcher, make_bot, register_middlewares
from app.config import get_config
from app.db.session import make_engine, make_session_factory
from app.services.notifications import notify_admins
from app.utils.logging import get_logger, setup_logging

log = get_logger("main")


def build_internal_api(session_factory, bot):
    from app.internal_api import build_internal_app

    return build_internal_app(session_factory, bot)


async def main() -> None:
    cfg = get_config()
    cfg.ensure_runtime()
    setup_logging(cfg.log_level)

    engine = make_engine(cfg)
    session_factory = make_session_factory(engine)

    bot = make_bot()
    dp = build_dispatcher()
    register_middlewares(dp, session_factory)

    # кнопки меню (список команд) без ручного ввода /start
    from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault

    user_commands = [BotCommand(command="start", description="Открыть меню")]
    admin_commands = user_commands + [BotCommand(command="admin", description="Админ-панель")]
    with contextlib.suppress(Exception):
        await bot.set_my_commands(user_commands, scope=BotCommandScopeDefault())
    for admin_id in cfg.admin_ids:
        with contextlib.suppress(Exception):
            await bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(chat_id=admin_id))

    # глобальный error-handler
    from aiogram.types import ErrorEvent, Update

    from app.handlers.admin import alert_exception

    @dp.error()
    async def on_error(event: ErrorEvent) -> bool:
        await alert_exception(bot, event.update, event.exception)
        update = event.update
        if isinstance(update, Update) and update.message and update.message.from_user:
            with contextlib.suppress(Exception):
                await bot.send_message(update.message.chat.id, "😔 Произошла ошибка. Мы уже разбираемся.")
        return True

    scheduler = None
    try:
        from app.tasks.scheduler import setup_scheduler

        scheduler = setup_scheduler(session_factory, bot)
        scheduler.start()
        log.info("scheduler.started")

        if cfg.webhook_enabled:
            from aiohttp import web as aioweb

            from app.web.server import build_app, set_dependencies

            set_dependencies(session_factory, bot)
            webhook_path = "/webhook/telegram"
            base = (cfg.webhook_base_url or "").rstrip("/")
            await bot.set_webhook(f"{base}{webhook_path}", drop_pending_updates=True, secret_token=cfg.bot_token[:32])

            app = build_app()

            async def telegram_webhook(request: aioweb.Request) -> aioweb.Response:
                data = await request.json()
                await dp.feed_webhook_update(bot, data)
                return aioweb.json_response({"ok": True})

            app.router.add_post(webhook_path, telegram_webhook)

            runner = aioweb.AppRunner(app)
            await runner.setup()
            site = aioweb.TCPSite(runner, host="0.0.0.0", port=cfg.web_port)
            await site.start()
            log.info("webhook.mode", port=cfg.web_port, path=webhook_path)
            await asyncio.Event().wait()  # работать вечно
        else:
            await notify_admins(bot, "🚀 Бот запущен (polling)")
            await bot.delete_webhook(drop_pending_updates=True)
            log.info("polling.started")

            # внутренний API для сайта (ТЗ v2): 127.0.0.1:<port>, токен из .env
            internal_runner = None
            if cfg.internal_api_token and not cfg.webhook_enabled:
                from aiohttp import web as aioweb

                from app.internal_api import build_internal_app

                internal_app = build_internal_app(session_factory, bot)
                internal_runner = aioweb.AppRunner(internal_app)
                await internal_runner.setup()
                internal_site = aioweb.TCPSite(internal_runner, host="127.0.0.1", port=cfg.internal_api_port)
                await internal_site.start()
                log.info("internal_api.started", port=cfg.internal_api_port)

            try:
                await dp.start_polling(bot, handle_signals=True)
            finally:
                if internal_runner is not None:
                    await internal_runner.cleanup()
    finally:
        if scheduler is not None:
            scheduler.shutdown(wait=False)
        await bot.session.close()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
