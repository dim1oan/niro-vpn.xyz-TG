"""Простой in-memory throttling (антифлуд): 1 сообщение / 0.5 c на пользователя.

Для нескольких воркеров подключается Redis-хранилище через REDIS_URL.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message

DEFAULT_RATE = 0.5


class ThrottlingMiddleware(BaseMiddleware):
    def __init__(self, rate: float = DEFAULT_RATE) -> None:
        self.rate = rate
        self._last: dict[int, float] = {}

    async def __call__(
        self,
        handler: Callable[[Any, dict[str, Any]], Awaitable[Any]],
        event: Message | CallbackQuery,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None:
            return await handler(event, data)

        now = time.monotonic()
        key = user.id
        last = self._last.get(key, 0.0)
        if now - last < self.rate:
            if isinstance(event, CallbackQuery):
                await event.answer("Слишком часто, подождите секунду", show_alert=False)
            return None  # глушим флуд
        # чистим хвосты, чтобы словарь не рос бесконечно
        if len(self._last) > 10_000:
            cutoff = now - 60
            self._last = {k: v for k, v in self._last.items() if v > cutoff}
        self._last[key] = now
        return await handler(event, data)
