"""Биллинг: создание платежей, идемпотентная выдача, зачисление баланса."""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_config
from app.db.models import Payment, PaymentProvider, PaymentStatus, Plan, Subscription, User
from app.db.repositories.payments import PaymentRepo, PromoRepo
from app.db.repositories.plans import PlanRepo
from app.services.payments.base import assert_plan_price
from app.services.promo import apply_promo
from app.utils.logging import get_logger

if TYPE_CHECKING:
    from app.db.models import Server

log = get_logger("billing")


class BillingService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.cfg = get_config()

    async def create_payment(
        self,
        *,
        user: User,
        plan: Plan,
        provider: PaymentProvider,
        promo_code: str | None = None,
        server: Server | None = None,
    ) -> tuple[Payment, int]:
        """Создаёт pending-платёж (сумма — из БД), отменяя прочие pending юзера.

        Возвращает (платёж, сумма к оплате с учётом скидки).
        """
        repo = PaymentRepo(self.session)
        await repo.cancel_other_pending(user.id)

        amount = plan.price_kopeks
        discount = 0
        promo = None
        bonus_days = 0
        if promo_code and provider != PaymentProvider.BALANCE:
            promo_repo = PromoRepo(self.session)
            promo, result = await apply_promo(promo_repo, promo_code, plan.price_kopeks)
            if result.ok:
                discount = result.discount_kopeks
                amount = plan.price_kopeks - discount
                bonus_days = result.bonus_days

        payment = Payment(
            user_id=user.id,
            plan_id=plan.id,
            provider=provider,
            amount_kopeks=amount,
            currency="RUB",
            status=PaymentStatus.PENDING,
            external_id=f"pending:{provider}:{secrets.token_hex(8)}",
            payload={
                "plan_code": plan.code,
                "server_code": server.code if server else None,
                **({"bonus_days": bonus_days} if bonus_days else {}),
            },
            discount_kopeks=discount,
            promo_code_id=promo.id if promo else None,
        )
        self.session.add(payment)
        await self.session.flush()
        log.info("payment.created", payment_id=payment.id, provider=provider.value, amount=amount)
        return payment, amount

    async def settle(
        self, payment_id: int, *, external_id: str | None = None, expected_amount: int | None = None
    ) -> Payment | None:
        """Переводит платёж в paid (идемпотентно) и возвращает его.

        Идемпотентность: уникальный индекс external_id + проверка статуса под блокировкой строки.
        """

        repo = PaymentRepo(self.session)
        payment = await repo.get(payment_id)
        if payment is None or payment.status == PaymentStatus.PAID:
            return payment
        plan = await PlanRepo(self.session).get(payment.plan_id)
        user = await self.session.get(User, payment.user_id)
        if plan is None or user is None:
            return None

        if expected_amount is not None and not assert_plan_price(plan, expected_amount):
            log.error("settle.amount_mismatch", payment_id=payment.id)
            return None

        # блокировка строки для гонок вебхук+реконсиляция
        if not self.cfg.is_sqlite:
            await self.session.execute(
                select(Payment).where(Payment.id == payment.id).with_for_update()
            )

        existing_paid = await repo.get_by_external(external_id) if external_id else None
        if existing_paid is not None and existing_paid.id != payment.id:
            log.warning("settle.duplicate_external", external_id=external_id)
            return existing_paid

        await repo.mark_paid(payment, external_id=external_id)
        if payment.promo_code_id:
            await PromoRepo(self.session).register_use(payment.promo_code_id, payment.user_id, payment.id)
            log.info("promo.registered_use", promo_id=payment.promo_code_id, payment_id=payment.id)
        await self.session.flush()
        return payment

    async def pay_from_balance(self, user: User, plan: Plan, promo_code: str | None = None) -> tuple[Payment | None, str]:
        """Оплата внутренним балансом. Возвращает (payment|None, error|'')."""
        amount_needed = plan.price_kopeks
        discount = 0
        if promo_code:
            promo, result = await apply_promo(PromoRepo(self.session), promo_code, plan.price_kopeks)
            if result.ok:
                discount = result.discount_kopeks
                amount_needed -= discount
        if user.balance_kopeks < amount_needed:
            return None, "insufficient"
        from app.utils.time import utcnow

        payment = Payment(
            user_id=user.id,
            plan_id=plan.id,
            provider=PaymentProvider.BALANCE,
            amount_kopeks=amount_needed,
            currency="RUB",
            status=PaymentStatus.PAID,
            paid_at=utcnow(),
            external_id=f"bal:{secrets.token_hex(12)}",
            discount_kopeks=discount,
        )
        user.balance_kopeks -= amount_needed
        self.session.add_all([user, payment])
        await self.session.flush()
        return payment, ""

    async def refund_to_balance(self, session: AsyncSession, payment: Payment) -> None:
        """Возврат средств на внутренний баланс пользователя."""
        user = await session.get(User, payment.user_id)
        if user is not None:
            user.balance_kopeks += payment.amount_kopeks
            session.add(user)

    async def grant_for_payment(self, server: Server, payment: Payment) -> Subscription:
        """Выдача доступа по платежу через provisioning (см. services/provisioning.py)."""
        from app.services.provisioning import ProvisioningService

        plan = await PlanRepo(self.session).get(payment.plan_id)
        user = await self.session.get(User, payment.user_id)
        if plan is None or user is None:
            raise ValueError(f"plan/user missing for payment {payment.id}")

        renew_sub_id = payment.payload.get("renew_sub_id")
        bonus_days = int(payment.payload.get("bonus_days", 0) or 0)
        service = ProvisioningService(self.session)
        if renew_sub_id:
            sub = await self.session.get(Subscription, renew_sub_id)
            if sub is None:
                raise ValueError(f"renew sub {renew_sub_id} missing for payment {payment.id}")
            return await service.renew(sub, plan.duration_days + bonus_days)
        return await service.grant_access(user, plan, server, payment, bonus_days=bonus_days)
