"""Коды активации сайта (activation_codes): выдача ботом, идемпотентно.

Формат и правила — по ТЗ v2: 10 символов [A-Z2-9] без похожих (O/0/I/1),
один активный код на юзера, TTL ACTIVATION_CODE_DAYS, забаненным не выдаётся.
"""

from __future__ import annotations

import secrets
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_config
from app.db.models import ActivationCode, User
from app.utils.time import utcnow

# алфавит без O/0/I/1 (10 символов кода)
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def gen_activation_code() -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(10))


class SiteCodeRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_active(self, user_id: int) -> ActivationCode | None:
        return await self.session.scalar(
            select(ActivationCode)
            .where(
                ActivationCode.user_id == user_id,
                ActivationCode.used_at.is_(None),
                ActivationCode.expires_at > utcnow(),
            )
            .order_by(ActivationCode.created_at.desc())
            .limit(1)
        )

    async def invalidate_active(self, user_id: int) -> int:
        """Гасит неиспользованные коды юзера (expires_at = now). Возвращает кол-во."""
        res = await self.session.execute(
            update(ActivationCode)
            .where(
                ActivationCode.user_id == user_id,
                ActivationCode.used_at.is_(None),
                ActivationCode.expires_at > utcnow(),
            )
            .values(expires_at=utcnow())
        )
        return int(res.rowcount or 0)

    async def create(self, user_id: int) -> ActivationCode:
        cfg = get_config()
        row = ActivationCode(
            code=gen_activation_code(),
            user_id=user_id,
            expires_at=utcnow() + timedelta(days=cfg.activation_code_days),
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def ensure_code(self, user: User) -> ActivationCode:
        """Активный код юзера; если нет — создаёт. Идемпотентно для повторных /start."""
        if user.is_banned:
            raise PermissionError("banned")
        existing = await self.get_active(user.id)
        if existing is not None:
            return existing
        return await self.create(user.id)

    async def reissue(self, user: User) -> ActivationCode | None:
        """Перевыпуск: гасит старый, выдаёт новый. Забаненным — None."""
        if user.is_banned:
            return None
        await self.invalidate_active(user.id)
        return await self.create(user.id)
