"""Реферальные начисления: X% от оплаты приглашённого (баланс убран — фиксируем только факт)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_config
from app.db.models import Payment, Referral, User
from app.utils.logging import get_logger

log = get_logger("referral")


async def credit_referral(session: AsyncSession, payment: Payment) -> int:
    """Фиксирует вознаграждение рефереру за оплаченный платёж. Возвращает сумму."""
    cfg = get_config()
    if payment.provider == "balance" or payment.status != "paid":
        return 0
    referee: User | None = await session.get(User, payment.user_id)
    if referee is None or referee.referrer_id is None:
        return 0
    referral = await session.scalar(
        select(Referral).where(Referral.referee_id == payment.user_id).limit(1)
    )
    if referral is None:
        referral = Referral(referrer_id=referee.referrer_id, referee_id=payment.user_id)
        session.add(referral)

    if cfg.referral_fixed_kopeks > 0:
        reward = cfg.referral_fixed_kopeks
    else:
        reward = payment.amount_kopeks * cfg.referral_percent // 100
    if reward <= 0:
        return 0

    referrer = await session.get(User, referee.referrer_id)
    if referrer is None or referrer.is_banned:
        return 0

    referral.reward_kopeks += reward
    referral.paid = True
    session.add(referral)
    log.info("credited", referrer_id=referrer.id, amount=reward, payment_id=payment.id)
    return reward
