"""Уведомления пользователю и админам."""

from __future__ import annotations

import asyncio
import html

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError

from app.config import get_config
from app.utils.logging import get_logger

log = get_logger("notify")


def esc(text: str) -> str:
    return html.escape(str(text), quote=False)


async def notify_admins(bot: Bot, text: str, *, parse_mode="HTML") -> bool:
    cfg = get_config()
    chat_id = cfg.admin_chat()
    if not chat_id:
        return False
    try:
        await bot.send_message(chat_id, text, parse_mode=parse_mode)
        return True
    except TelegramForbiddenError:
        log.warning("admin_chat_forbidden", chat_id=chat_id)
    except TelegramAPIError as e:
        log.error("admin_notify_failed", err=str(e))
    return False


async def notify_user(bot: Bot, telegram_id: int | None, text: str, *, parse_mode="HTML") -> bool:
    if telegram_id is None:  # веб-пользователь без Telegram
        return False
    try:
        await bot.send_message(telegram_id, text, parse_mode=parse_mode)
        return True
    except TelegramForbiddenError:
        log.info("user_blocked_bot", user_id=telegram_id)
    except TelegramAPIError as e:
        log.warning("user_notify_failed", user_id=telegram_id, err=str(e))
    return False


async def broadcast_send(
    bot: Bot,
    targets: list[int],
    send_one,  # async callable(bot, tg_id) -> bool
    *,
    batch_size: int = 25,
    batch_delay: float = 1.05,
    on_blocked=None,  # async callable(tg_id)
) -> tuple[int, int]:
    """Батч-рассылка с rate-limit ≤25 msg/sec; возвращает (ok, failed)."""
    ok = failed = 0
    for i in range(0, len(targets), batch_size):
        batch = targets[i : i + batch_size]
        for tg_id in batch:
            try:
                success = await send_one(bot, tg_id)
                if success:
                    ok += 1
                else:
                    failed += 1
            except TelegramForbiddenError:
                failed += 1
                if on_blocked:
                    await on_blocked(tg_id)
            except TelegramAPIError:
                failed += 1
        if i + batch_size < len(targets):
            await asyncio.sleep(batch_delay)
    return ok, failed
