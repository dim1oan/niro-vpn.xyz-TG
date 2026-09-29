"""Общие хелперы хендлеров: проверка подписки на канал, рендер карточек."""

from __future__ import annotations

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import Message
from aiogram.types import User as TgUser

from app.config import get_config
from app.db.models import Plan, Subscription, SubStatus
from app.utils.time import fmt_dt, human_bytes, human_days_left

STATUS_TEXT = {
    SubStatus.ACTIVE: "активна",
    SubStatus.EXPIRED: "истекла",
    SubStatus.DISABLED: "отключена",
    SubStatus.DELETED: "удалена",
}
STATUS_EMOJI = {
    SubStatus.ACTIVE: "🟢",
    SubStatus.EXPIRED: "🔴",
    SubStatus.DISABLED: "⛔",
    SubStatus.DELETED: "🗑",
}


async def is_channel_member(bot: Bot, telegram_id: int) -> bool:
    cfg = get_config()
    if not cfg.required_channel_id:
        return True
    try:
        member = await bot.get_chat_member(cfg.required_channel_id, telegram_id)
        return member.status in ("member", "administrator", "creator")
    except TelegramAPIError:
        return False


def channel_link() -> str:
    cfg = get_config()
    chat = str(cfg.required_channel_id)
    if chat.startswith("-100"):
        return f"https://t.me/c/{chat[4:]}"  # приватный канал — ссылку не построить красиво
    return f"https://t.me/{chat.lstrip('@')}"


def plan_price_text(plan: Plan) -> str:
    rub = plan.price_kopeks / 100
    stars = f" или {plan.price_stars}⭐" if plan.price_stars else ""
    return f"{rub:.0f} ₽{stars}"


async def safe_edit(
    message: Message,
    text: str,
    reply_markup=None,
    *,
    disable_web_page_preview: bool = False,
) -> None:
    """Редактирует сообщение; если контент не изменился — молча оставляет как есть,
    при прочих сбоях отправляет новое сообщение (без дублей на «not modified»)."""
    try:
        await message.edit_text(
            text, reply_markup=reply_markup, disable_web_page_preview=disable_web_page_preview
        )
        return
    except TelegramBadRequest as e:
        if "message is not modified" in str(e).lower():
            return
    except TelegramAPIError:
        pass
    await message.answer(
        text, reply_markup=reply_markup, disable_web_page_preview=disable_web_page_preview
    )


async def safe_edit_id(
    bot,
    chat_id: int,
    message_id: int,
    text: str,
    reply_markup=None,
    *,
    disable_web_page_preview: bool = False,
) -> None:
    """Редактирует сообщение по chat_id/message_id (для FSM-сценариев, где под рукой
    только Message пользователя). Игнорирует «message is not modified»."""
    try:
        await bot.edit_message_text(
            text,
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=reply_markup,
            disable_web_page_preview=disable_web_page_preview,
        )
    except TelegramBadRequest as e:
        if "message is not modified" in str(e).lower():
            return
    except TelegramAPIError:
        pass


def sub_card(sub: Subscription) -> str:
    status = sub.status
    traffic = human_bytes(sub.traffic_used_bytes) if sub.last_synced_at else "—"
    return (
        f"🔑 <b>{sub.server.country_flag} {sub.server.country_name}</b> — {sub.plan.title}\n"
        f"📅 До {fmt_dt(sub.expires_at, get_config().tz)} ({human_days_left(sub.expires_at)})\n"
        f"📊 Трафик за период: {traffic}\n"
        f"{STATUS_EMOJI.get(status, '⚪')} Статус: {STATUS_TEXT.get(status, status.value)}"
    )


def ref_code(user: TgUser | None, db_user_id: int) -> str:
    return f"ref{db_user_id}"
