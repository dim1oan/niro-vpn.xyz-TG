"""Telegram Stars (XTR): invoice → pre_checkout → successful_payment → refund."""

from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.types import LabeledPrice, Message, PreCheckoutQuery
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_config
from app.db.models import PaymentProvider, PaymentStatus
from app.db.repositories.payments import PaymentRepo
from app.db.repositories.plans import PlanRepo
from app.db.repositories.servers import ServerRepo
from app.handlers.buy import show_success
from app.locales.ru import t
from app.services.billing import BillingService
from app.services.notifications import notify_admins
from app.services.payments.base import parse_payload
from app.utils.logging import get_logger

router = Router(name="stars")
log = get_logger("stars")


async def send_stars_invoice(bot, *, chat_id: int, plan, payment_id: int) -> None:
    cfg = get_config()
    await bot.send_invoice(
        chat_id=chat_id,
        title=f"{cfg.brand_name}: {plan.title}",
        description=f"Доступ на {plan.duration_days} дн, до {plan.device_limit} устройств",
        payload=payment_payload(payment_id),
        currency="XTR",
        prices=[LabeledPrice(label=plan.title, amount=plan.price_stars)],
        provider_token="",  # обязателен пустой для Stars
    )


def payment_payload(payment_id: int) -> str:
    from app.services.payments.base import payment_payload as _p

    return _p(payment_id)


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery, session: AsyncSession) -> None:
    payment_id = parse_payload(query.invoice_payload)
    if payment_id is None:
        await query.answer(ok=False, error_message="Некорректный платёж")
        return
    payment = await PaymentRepo(session).get(payment_id)
    if payment is None or payment.status not in (PaymentStatus.PENDING, PaymentStatus.PAID):
        await query.answer(ok=False, error_message="Счёт не найден или отменён")
        return
    plan = await PlanRepo(session).get(payment.plan_id)
    if plan is None or query.total_amount != plan.price_stars or query.currency != "XTR":
        await query.answer(ok=False, error_message="Сумма не совпадает с тарифом")
        return
    await query.answer(ok=True)


@router.message(F.successful_payment)
async def on_successful_payment(message: Message, session: AsyncSession, user) -> None:
    sp = message.successful_payment
    assert sp is not None
    payment_id = parse_payload(sp.invoice_payload)
    if payment_id is None:
        log.error("stars.bad_payload", payload=sp.invoice_payload)
        return

    billing = BillingService(session)
    # telegram_payment_charge_id в external_id + уникальный индекс = защита от повторного зачисления
    payment = await billing.settle(
        payment_id,
        external_id=sp.telegram_payment_charge_id,
        expected_amount=None,
    )
    if payment is None:
        log.error("stars.settle_failed", payment_id=payment_id)
        return

    server = await ServerRepo(session).pick_best()
    if server is None:
        await message.answer(t("error_user"))
        await notify_admins(message.bot, f"⚠️ Stars оплата {payment.id} без доступного сервера!")
        return

    try:
        sub = await billing.grant_for_payment(server, payment)
    except Exception as e:  # noqa: BLE001
        log.error("stars.provision_failed", err=str(e), payment_id=payment.id)
        await message.answer("✅ Оплата получена! Доступ будет выдан в течение нескольких минут.")
        await notify_admins(message.bot, f"⚠️ provision failed (Stars): payment {payment.id}: <pre>{html.escape(str(e))}</pre>")
        return

    from app.handlers.start import send_main_menu
    from app.services.referral import credit_referral

    await credit_referral(session, payment)

    await show_success(message, session, sub)
    await send_main_menu(message.bot, message.chat.id, user)


@router.message(F.text.regexp(r"^/refund\s+(\S+)$"))
async def cmd_refund(message: Message, session: AsyncSession) -> None:
    """Админский /refund <charge_id>."""
    cfg = get_config()
    if str(message.from_user.id) not in {str(i) for i in cfg.admin_ids}:
        return
    charge_id = message.text.split(maxsplit=1)[1].strip()
    payment = await PaymentRepo(session).get_by_external(charge_id)
    if payment is None or payment.provider != PaymentProvider.STARS:
        await message.answer("Платёж с таким charge_id не найден.")
        return
    try:
        await message.bot.refund_star_payment(user_id=payment.user_id, telegram_payment_charge_id=charge_id)
    except Exception as e:  # noqa: BLE001
        await message.answer(f"Ошибка возврата: {e}")
        return
    payment.status = PaymentStatus.REFUNDED
    session.add(payment)
    await session.flush()
    await message.answer(f"✅ Возврат по платежу №{payment.id} выполнен.")
