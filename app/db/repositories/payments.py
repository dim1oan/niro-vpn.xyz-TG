"""Репозитории платежей и промокодов."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditLog, Payment, PaymentProvider, PaymentStatus, PromoCode, PromoType, PromoUse


class PaymentRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, payment_id: int) -> Payment | None:
        return await self.session.get(Payment, payment_id)

    async def get_by_external(self, external_id: str) -> Payment | None:
        return await self.session.scalar(select(Payment).where(Payment.external_id == external_id))

    async def cancel_other_pending(self, user_id: int, keep_id: int | None = None) -> int:
        """Отменяет прочие pending платежи пользователя (один pending за раз)."""
        q = select(Payment).where(
            Payment.user_id == user_id,
            Payment.status == PaymentStatus.PENDING,
        )
        if keep_id is not None:
            q = q.where(Payment.id != keep_id)
        count = 0
        for pay in (await self.session.scalars(q)).all():
            pay.status = PaymentStatus.CANCELED
            count += 1
        return count

    async def pending_older_than(self, minutes: int, provider: PaymentProvider | None = None) -> list[Payment]:
        cutoff = datetime.now(UTC) - timedelta(minutes=minutes)
        q = select(Payment).where(Payment.status == PaymentStatus.PENDING, Payment.created_at <= cutoff)
        if provider is not None:
            q = q.where(Payment.provider == provider)
        return list((await self.session.scalars(q)).all())

    async def failed_provisioning(self) -> list[Payment]:
        return list(
            (await self.session.scalars(
                select(Payment).where(
                    Payment.status == PaymentStatus.PAID,
                    Payment.provision_failed.is_(True),
                    Payment.provision_attempts < 3,
                )
            )).all()
        )

    async def revenue_sum(self, since: datetime) -> int:
        total = await self.session.scalar(
            select(func.coalesce(func.sum(Payment.amount_kopeks), 0)).where(
                Payment.status == PaymentStatus.PAID,
                Payment.paid_at >= since,
                Payment.provider != PaymentProvider.BALANCE,
            )
        )
        return int(total or 0)

    async def recent_manual_pending(self) -> list[Payment]:
        rows = await self.session.scalars(
            select(Payment)
            .where(Payment.provider == PaymentProvider.MANUAL, Payment.status == PaymentStatus.PENDING)
            .order_by(Payment.created_at.desc())
            .limit(20)
        )
        return list(rows)

    async def mark_paid(self, payment: Payment, external_id: str | None = None) -> None:
        payment.status = PaymentStatus.PAID
        payment.paid_at = datetime.now(UTC)
        if external_id:
            payment.external_id = external_id
        self.session.add(payment)


class PromoRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, promo_id: int) -> PromoCode | None:
        return await self.session.get(PromoCode, promo_id)

    async def get_by_code(self, code: str) -> PromoCode | None:
        return await self.session.scalar(
            select(PromoCode).where(func.lower(PromoCode.code) == code.lower())
        )

    async def list_all(self, limit: int = 50) -> list[PromoCode]:
        rows = await self.session.scalars(
            select(PromoCode).order_by(PromoCode.created_at.desc()).limit(limit)
        )
        return list(rows)

    async def create(
        self, *, code: str, type_: PromoType, value: int, max_uses: int = 0, days_valid: int | None = None
    ) -> PromoCode:
        promo = PromoCode(
            code=code.strip(),
            type=type_,
            value=value,
            max_uses=max_uses,
            expires_at=datetime.now(UTC) + timedelta(days=days_valid) if days_valid else None,
        )
        self.session.add(promo)
        await self.session.flush()
        return promo

    async def register_use(self, promo_id: int, user_id: int, payment_id: int) -> None:
        self.session.add(PromoUse(promo_id=promo_id, user_id=user_id, payment_id=payment_id))
        promo = await self.get(promo_id)
        if promo:
            promo.used_count += 1

    async def audit(self, actor_telegram_id: int | None, action: str) -> None:
        self.session.add(AuditLog(actor_telegram_id=actor_telegram_id, action=action, entity="promo_code"))
