"""Репозиторий пользователей."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete as sa_delete
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditLog, Referral, User


class UserRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_telegram_id(self, telegram_id: int) -> User | None:
        return await self.session.scalar(select(User).where(User.telegram_id == telegram_id))

    async def get(self, user_id: int) -> User | None:
        return await self.session.get(User, user_id)

    async def get_or_create(
        self,
        telegram_id: int,
        username: str | None = None,
        first_name: str | None = None,
        language_code: str | None = "ru",
    ) -> tuple[User, bool]:
        user = await self.get_by_telegram_id(telegram_id)
        if user is not None:
            changed = False
            if user.username != username and username is not None:
                user.username = username
                changed = True
            if user.first_name != first_name and first_name is not None:
                user.first_name = first_name
                changed = True
            return user, changed
        user = User(
            telegram_id=telegram_id,
            username=username,
            first_name=first_name,
            language_code=language_code or "ru",
            last_seen_at=datetime.now(UTC),
        )
        self.session.add(user)
        await self.session.flush()
        return user, True

    async def set_referrer(self, user: User, referrer_id: int) -> bool:
        """Ставит реферера один раз. Возвращает True если установлен."""
        if user.id == referrer_id or user.referrer_id is not None:
            return False
        referrer = await self.get(referrer_id)
        if referrer is None:
            return False
        user.referrer_id = referrer_id
        self.session.add(
            Referral(referrer_id=referrer_id, referee_id=user.id, reward_kopeks=0, paid=False)
        )
        return True

    async def touch_last_seen(self, user_id: int) -> None:
        user = await self.get(user_id)
        if user:
            user.last_seen_at = datetime.now(UTC)

    async def add_balance(self, user_id: int, amount_kopeks: int) -> None:
        user = await self.get(user_id)
        if user is not None:
            user.balance_kopeks += amount_kopeks
            self.session.add(user)

    async def count_all(self) -> int:
        return int(await self.session.scalar(select(func.count()).select_from(User)) or 0)

    async def count_new_since(self, since: datetime) -> int:
        return int(
            await self.session.scalar(
                select(func.count()).select_from(User).where(User.created_at >= since)
            )
            or 0
        )

    async def banned_ids(self) -> list[int]:
        rows = await self.session.scalars(select(User.telegram_id).where(User.is_banned.is_(True)))
        return list(rows)

    async def search(self, query: str) -> list[User]:
        q = select(User).limit(10)
        if query.isdigit():
            n = int(query)
            q = q.where(or_(User.id == n, User.telegram_id == n))
        else:
            like = f"%{query.lstrip('@').lower()}%"
            q = q.where(func.lower(func.coalesce(User.username, "")).like(like))
        return list((await self.session.scalars(q)).all())

    async def list_with_active_subs(self, limit: int = 30) -> list[User]:
        """Пользователи, у которых есть активные подписки, с подгруженными подписками."""
        from app.db.models import Subscription, SubStatus

        now = datetime.now(UTC)
        rows = await self.session.scalars(
            select(User)
            .join(Subscription, Subscription.user_id == User.id)
            .where(Subscription.status == SubStatus.ACTIVE, Subscription.expires_at > now)
            .distinct()
            .order_by(User.id.desc())
            .limit(limit)
        )
        users = list(rows)
        for u in users:
            await self.session.refresh(u, attribute_names=["subscriptions"])
        return users

    async def active_subs_of(self, user_id: int) -> list:
        from app.db.models import Subscription, SubStatus

        now = datetime.now(UTC)
        return list(
            (await self.session.scalars(
                select(Subscription)
                .where(
                    Subscription.user_id == user_id,
                    Subscription.status == SubStatus.ACTIVE,
                    Subscription.expires_at > now,
                )
                .order_by(Subscription.expires_at.desc())
            )).all()
        )

    async def referral_count(self, referrer_id: int) -> int:
        return int(
            await self.session.scalar(
                select(func.count()).select_from(Referral).where(Referral.referrer_id == referrer_id)
            )
            or 0
        )

    async def referrals_of(self, referrer_id: int) -> list[Referral]:
        rows = await self.session.scalars(
            select(Referral).where(Referral.referrer_id == referrer_id).order_by(Referral.created_at.desc())
        )
        return list(rows)

    # --- рассылка ---

    async def broadcast_targets(self, segment: str) -> list[int]:
        from app.db.models import Payment, PaymentStatus, Subscription, SubStatus

        q = select(User.telegram_id).where(
            User.is_banned.is_(False), User.telegram_id.is_not(None)
        )
        if segment == "active":
            q = q.join(Subscription, Subscription.user_id == User.id).where(
                Subscription.status == SubStatus.ACTIVE,
                Subscription.expires_at > datetime.now(UTC),
            ).distinct()
        elif segment == "expired":
            active_ids = (
                select(Subscription.user_id)
                .where(Subscription.status == SubStatus.ACTIVE,
                       Subscription.expires_at > datetime.now(UTC))
                .scalar_subquery()
            )
            q = q.where(User.id.not_in(active_ids))
        elif segment == "trial":
            q = q.where(User.trial_used.is_(True)).join(Subscription, Subscription.user_id == User.id).distinct()
        elif segment == "no_purchase":
            paid_ids = (
                select(Payment.user_id).where(Payment.status == PaymentStatus.PAID).scalar_subquery()
            )
            q = q.where(User.id.not_in(paid_ids))
        rows = await self.session.scalars(q.order_by(User.id))
        return list(rows)

    async def audit(self, actor_telegram_id: int | None, action: str, entity: str | None = None,
                    entity_id: int | None = None, payload: dict | None = None) -> None:
        self.session.add(AuditLog(actor_telegram_id=actor_telegram_id, action=action,
                                  entity=entity, entity_id=entity_id, payload=payload or {}))


def gen_ref_code() -> str:
    return secrets.token_hex(4)


async def purge_user(session: AsyncSession, user_id: int) -> None:
    """Полное удаление пользователя (GDPR-хелпер)."""
    await session.execute(sa_delete(Referral).where((Referral.referrer_id == user_id) | (Referral.referee_id == user_id)))
    user = await session.get(User, user_id)
    if user:
        await session.delete(user)


def account_age_days(telegram_id: int, now: datetime | None = None) -> int:
    """Эвристика возраста аккаунта Telegram по ID (snowflake-подобная оценка).

    Telegram не даёт дату регистрации; оцениваем по порядку выдачи ID.
    """
    now = now or datetime.now(UTC)
    # ~1.5 млн новых ID в день на пике роста 2020-2022, консервативно
    approx_created = datetime(2013, 8, 1, tzinfo=UTC)
    delta_per_id = timedelta(seconds=60 * 60 * 24 / 1_500_000)
    estimated = approx_created + timedelta(seconds=telegram_id * delta_per_id.total_seconds())
    age = (now - estimated).days
    return max(age, 0)
