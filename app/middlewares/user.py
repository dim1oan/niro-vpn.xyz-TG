"""Middleware пользователя: get_or_create, проверка бана, last_seen."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.users import UserRepo


class UserContextMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user = data.get("event_from_user")
        session: AsyncSession | None = data.get("session")
        if tg_user is None or session is None or tg_user.is_bot:
            return await handler(event, data)

        repo = UserRepo(session)
        user, created = await repo.get_or_create(
            telegram_id=tg_user.id,
            username=tg_user.username,
            first_name=tg_user.first_name,
            language_code=tg_user.language_code,
        )
        user.last_seen_at = datetime.now(UTC)
        await session.flush()
        data["user"] = user
        data["user_repo"] = repo
        data["is_new_user"] = created

        if user.is_banned:
            # молча игнорируем забаненных (кроме /start — там сообщение)
            if isinstance(event, CallbackQuery):
                await event.answer("Доступ ограничен", show_alert=True)
                return None
            if isinstance(event, Message) and event.text and event.text.startswith("/start"):
                pass  # пусть увидят бан-сообщение в start.py
            else:
                return None

        return await handler(event, data)
