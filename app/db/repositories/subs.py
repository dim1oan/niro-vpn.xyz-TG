"""Репозиторий подписок."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete as sa_delete
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import random_token
from app.db.models import Payment, Server, Subscription, SubStatus, User


def gen_xui_email(telegram_id: int | None) -> str:
    """tg<id>-<rand>; для веб-пользователей без Telegram — web-<rand>."""
    prefix = f"tg{telegram_id}" if telegram_id is not None else "web"
    return f"{prefix}-{random_token(8)}"


class SubRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, sub_id: int) -> Subscription | None:
        return await self.session.get(Subscription, sub_id)

    async def by_id_for_user(self, sub_id: int, user_id: int) -> Subscription | None:
        return await self.session.scalar(
            select(Subscription).where(Subscription.id == sub_id, Subscription.user_id == user_id)
        )

    async def get_by_email(self, email: str) -> Subscription | None:
        return await self.session.scalar(select(Subscription).where(Subscription.xui_email == email))

    async def list_user(self, user_id: int, *, only_active: bool = False) -> list[Subscription]:
        q = (
            select(Subscription)
            .where(Subscription.user_id == user_id)
            .order_by(Subscription.status, Subscription.expires_at.desc())
        )
        if only_active:
            q = q.where(
                Subscription.status.in_([SubStatus.ACTIVE]),
                Subscription.expires_at > datetime.now(UTC),
            )
        return list((await self.session.scalars(q)).all())

    async def active_count(self) -> int:
        now = datetime.now(UTC)
        return int(
            await self.session.scalar(
                select(func.count())
                .select_from(Subscription)
                .where(Subscription.status == SubStatus.ACTIVE, Subscription.expires_at > now)
            )
            or 0
        )

    async def has_active(self, user_id: int) -> bool:
        now = datetime.now(UTC)
        return bool(
            await self.session.scalar(
                select(func.count())
                .select_from(Subscription)
                .where(
                    Subscription.user_id == user_id,
                    Subscription.status.in_([SubStatus.ACTIVE]),
                    Subscription.expires_at > now,
                )
                .limit(1)
            )
        )

    async def expiring_between(self, start: datetime, end: datetime) -> list[Subscription]:
        rows = await self.session.scalars(
            select(Subscription).where(
                Subscription.status == SubStatus.ACTIVE,
                Subscription.expires_at >= start,
                Subscription.expires_at <= end,
                or_(Subscription.notified_3d.is_(False), Subscription.notified_1d.is_(False)),
            )
        )
        return list(rows)

    async def to_expire(self, now: datetime) -> list[Subscription]:
        rows = await self.session.scalars(
            select(Subscription).where(
                Subscription.status == SubStatus.ACTIVE,
                Subscription.expires_at <= now,
            )
        )
        return list(rows)

    async def expired_before(self, cutoff: datetime) -> list[Subscription]:
        rows = await self.session.scalars(
            select(Subscription).where(
                Subscription.expires_at < cutoff,
                Subscription.status.in_([SubStatus.EXPIRED, SubStatus.DISABLED]),
            )
        )
        return list(rows)

    async def all_syncable(self) -> list[tuple[Subscription, Server]]:
        rows = await self.session.execute(
            select(Subscription, Server)
            .join(Server, Server.id == Subscription.server_id)
            .where(Subscription.status.in_([SubStatus.ACTIVE, SubStatus.EXPIRED]))
        )
        return [(sub, srv) for sub, srv in rows]

    async def create(
        self,
        *,
        user: User,
        server: Server,
        plan_id: int,
        uuid: str,
        email: str,
        sub_id: str,
        duration_days: int,
        base_expires_at: datetime | None = None,
    ) -> Subscription:
        now = datetime.now(UTC)
        started = now if base_expires_at is None or base_expires_at <= now else base_expires_at - timedelta(days=0)
        expires = (base_expires_at if base_expires_at and base_expires_at > now else now) + timedelta(days=duration_days)
        sub = Subscription(
            user_id=user.id,
            server_id=server.id,
            plan_id=plan_id,
            xui_client_uuid=uuid,
            xui_email=email,
            xui_sub_id=sub_id,
            xui_inbound_id=server.inbound_id,
            status=SubStatus.ACTIVE,
            started_at=started,
            expires_at=expires,
        )
        self.session.add(sub)
        await self.session.flush()
        return sub


def compute_renewal(
    current_expires_at: datetime, duration_days: int, *, now: datetime | None = None
) -> tuple[datetime, bool]:
    """Расчёт новой даты окончания при продлении.

    Активная подписка — expires_at += duration. Истёкшая — от текущего момента.
    Возвращает (новая дата, была ли активной).
    """
    now = now or datetime.now(UTC)
    was_active = current_expires_at > now
    base = current_expires_at if was_active else now
    return base + timedelta(days=duration_days), was_active


EMAIL_RE = re.compile(r"^(tg\d+|web)-[0-9a-f]+$")


async def payments_of(session: AsyncSession, user_id: int, limit: int = 10) -> list[Payment]:
    rows = await session.scalars(
        select(Payment)
        .where(Payment.user_id == user_id)
        .order_by(Payment.created_at.desc())
        .limit(limit)
    )
    return list(rows)


async def purge_subscription(session: AsyncSession, sub_id: int) -> None:
    from app.db.models import Payment

    await session.execute(sa_delete(Payment).where(Payment.subscription_id == sub_id))
    sub = await session.get(Subscription, sub_id)
    if sub:
        await session.delete(sub)
