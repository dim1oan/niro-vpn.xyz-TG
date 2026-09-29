"""Внутренний HTTP API бота (ТЗ v2, ф-2): уведомления с сайта о ручных оплатах.

Только 127.0.0.1, авторизация по X-Internal-Token. aiohttp — уже в зависимостях.
Сайт шлёт POST /internal/notify/manual-payment {"payment_id": N} сразу после
загрузки чека; бот отправляет админ-чату фото чека + кнопки Подтвердить/Отклонить.
Идемпотентность: payload.admin_notified_at.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from aiohttp import web
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_config
from app.db.models import Payment, PaymentProvider, PaymentStatus
from app.db.repositories.payments import PaymentRepo
from app.services.promo import format_price
from app.utils.logging import get_logger

if TYPE_CHECKING:
    from aiogram import Bot

log = get_logger("internal_api")


class InternalApiError(Exception):
    pass


def _unauthorized() -> web.Response:
    return web.json_response({"ok": False, "error": "forbidden"}, status=403)


async def _load_payment(db: AsyncSession, payment_id: int) -> tuple[Payment | None, str | None]:
    """Возвращает (payment, error). error=None если можно уведомлять."""
    payment = await PaymentRepo(db).get(payment_id)
    if payment is None:
        return None, "not_found"
    if payment.provider != PaymentProvider.MANUAL:
        return payment, "not_manual"
    if payment.status != PaymentStatus.PENDING:
        return payment, "not_pending"
    return payment, ""


def _caption(payment: Payment) -> str:
    from html import escape

    user = payment.user
    who = escape(user.display_name())
    tg = user.telegram_id if user.telegram_id is not None else "веб"
    email = escape(user.email or "—")
    return (
        f"🧾 <b>Ручная оплата с сайта</b>\n"
        f"Юзер: {who} (<code>{tg}</code>, {email})\n"
        f"Тариф: {escape(payment.plan.title)} · Сумма: <b>{format_price(payment.amount_kopeks)}</b>\n"
        f"Код платежа: <code>{escape(str(payment.payload.get('manual_code', '—')))}</code>\n"
        f"Платёж №{payment.id}"
        + (f"\n🔗 <a href='{escape(get_config().site_base_url)}/admin/users/{user.id}'>Карточка юзера</a>" if get_config().site_base_url else "")
    )


async def notify_manual_payment(session_factory, bot: Bot, payment_id: int) -> tuple[int, str]:
    """Шлёт админам уведомление о веб-оплате. Возвращает (http_status, error)."""
    cfg = get_config()
    async with session_factory() as db:
        payment, error = await _load_payment(db, payment_id)
        if error == "not_found":
            return 404, "payment not found"
        if error == "not_pending":
            return 409, "already processed"
        if payment is not None and payment.payload.get("admin_notified_at"):
            return 409, "already notified"
        if payment is None or not payment.payload.get("receipt_path"):
            return 404, "no receipt"

        chat_id = cfg.admin_chat()
        if not chat_id:
            return 500, "no admin chat"

        from pathlib import Path

        from aiogram.types import FSInputFile

        receipt = Path(payment.payload["receipt_path"])
        try:
            if receipt.exists():
                await bot.send_document(chat_id, document=FSInputFile(receipt), caption=_caption(payment))
            else:
                await bot.send_message(chat_id, _caption(payment) + "\n⚠️ файл чека не найден на диске")
            from aiogram.types import InlineKeyboardMarkup
            from aiogram.utils.keyboard import InlineKeyboardBuilder

            from app.keyboards.callbacks import ManualPayCB

            ikb = InlineKeyboardBuilder()
            ikb.button(text="✅ Подтвердить", callback_data=ManualPayCB(action="approve", payment_id=payment.id))
            ikb.button(text="❌ Отклонить", callback_data=ManualPayCB(action="reject", payment_id=payment.id))
            ikb.adjust(2)
            markup: InlineKeyboardMarkup = ikb.as_markup()
            await bot.send_message(chat_id, f"↩️ Платёж №{payment.id} — решение:", reply_markup=markup)
            payment.payload = {**payment.payload, "admin_notified_at": datetime.now(UTC).isoformat()}
            db.add(payment)
            await db.commit()
            log.info("notify.manual_sent", payment_id=payment.id)
            return 204, ""
        except Exception as e:  # noqa: BLE001
            log.error("notify.manual_failed", payment_id=payment_id, err=str(e))
            return 500, str(e)


def build_internal_app(session_factory, bot: Bot) -> web.Application:
    app = web.Application()

    async def notify_manual(request: web.Request) -> web.Response:
        cfg = get_config()
        if not cfg.internal_api_token or request.headers.get("X-Internal-Token") != cfg.internal_api_token:
            return _unauthorized()
        try:
            body = await request.json()
            payment_id = int((body or {}).get("payment_id", 0))
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "invalid body"}, status=400)
        if not payment_id:
            return web.json_response({"error": "payment_id required"}, status=400)
        status, msg = await notify_manual_payment(session_factory, bot, payment_id)
        if status == 204:
            return web.Response(status=204)
        return web.json_response({"error": msg}, status=status)

    app.router.add_post("/internal/notify/manual-payment", notify_manual)
    return app


def _unauthorized() -> web.Response:
    return web.json_response({"error": "unauthorized"}, status=403)
