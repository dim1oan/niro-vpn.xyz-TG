"""Ручная оплата: реквизиты → чек → карточка админу → подтверждение/отклонение."""

from __future__ import annotations

import secrets

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_config
from app.db.models import PaymentProvider, PaymentStatus, Plan, Server
from app.db.repositories.payments import PaymentRepo
from app.db.repositories.servers import ServerRepo
from app.db.repositories.users import UserRepo
from app.handlers.common import safe_edit
from app.keyboards.admin import manual_pay_review
from app.keyboards.callbacks import ManualPayCB
from app.locales.ru import t
from app.services.billing import BillingService
from app.services.notifications import esc, notify_user
from app.services.promo import format_price
from app.utils.logging import get_logger

router = Router(name="manual")
log = get_logger("manual")


class ManualFSM(StatesGroup):
    waiting_receipt = State()


async def start_manual_payment(
    cb: CallbackQuery,
    session: AsyncSession,
    user,
    plan: Plan,
    server: Server | None,
    draft=None,
    *,
    state: FSMContext | None = None,
    renew_sub_id: int | None = None,
) -> None:
    cfg = get_config()
    if server is None:
        server = await ServerRepo(session).pick_best()
    if server is None or cb.message is None:
        await cb.answer("Нет доступных серверов", show_alert=True)
        return
    billing = BillingService(session)
    payment, amount = await billing.create_payment(
        user=user, plan=plan, provider=PaymentProvider.MANUAL, promo_code=draft.promo if draft else None, server=server
    )
    code = f"VPN-{payment.id}-{secrets.token_hex(2).upper()}"
    payment.payload = {**payment.payload, "manual_code": code}
    if renew_sub_id is not None:
        payment.payload = {**payment.payload, "renew_sub_id": renew_sub_id}
    await session.flush()

    from app.keyboards.user import to_menu_kb

    await safe_edit(
        cb.message,
        t(
            "manual_instructions",
            title=plan.title,
            amount=amount / 100,
            code=code,
            details=esc(cfg.manual_pay_details or "Реквизиты не настроены"),
        ),
        reply_markup=to_menu_kb(),
        disable_web_page_preview=True,
    )
    if state is not None:
        await state.set_state(ManualFSM.waiting_receipt)
        await state.update_data(payment_id=payment.id)
    await cb.answer()


@router.message(ManualFSM.waiting_receipt, F.photo | F.document)
async def on_receipt(message: Message, state: FSMContext, session: AsyncSession, user) -> None:
    data = await state.get_data()
    payment_id = int(data.get("payment_id", 0))
    await state.clear()
    repo = PaymentRepo(session)
    payment = await repo.get(payment_id)
    if payment is None or payment.status != PaymentStatus.PENDING:
        await message.answer(t("error_user"))
        return

    file_id = message.photo[-1].file_id if message.photo else (message.document.file_id if message.document else "")
    payment.receipt_file_id = file_id
    session.add(payment)
    await session.flush()

    plan = payment.plan
    caption = (
        f"🧾 <b>Чек по ручной оплате</b>\n"
        f"Пользователь: {esc(user.display_name())} (<code>{user.telegram_id}</code>)\n"
        f"Тариф: {esc(plan.title)}\n"
        f"Сумма: <b>{format_price(payment.amount_kopeks)}</b>\n"
        f"Код платежа: <code>{payment.payload.get('manual_code', '—')}</code>\n"
        f"Платёж №{payment.id}"
    )
    chat_id = get_config().admin_chat()
    try:
        if message.photo:
            await message.bot.send_photo(chat_id, photo=file_id, caption=caption, reply_markup=manual_pay_review(payment.id), parse_mode="HTML")
        else:
            await message.bot.send_document(chat_id, document=file_id, caption=caption, reply_markup=manual_pay_review(payment.id), parse_mode="HTML")
    except Exception as e:  # noqa: BLE001
        log.error("manual.notify_admin_failed", err=str(e))
    await message.answer(t("manual_receipt_ok"))

    from app.handlers.start import send_main_menu

    await send_main_menu(message.bot, message.chat.id, user)


@router.message(ManualFSM.waiting_receipt)
async def receipt_needs_photo(message: Message) -> None:
    await message.answer(t("manual_receipt_need_photo"))


@router.callback_query(ManualPayCB.filter(F.action == "approve"))
async def cb_approve(cb: CallbackQuery, callback_data: ManualPayCB, session: AsyncSession) -> None:
    cfg = get_config()
    if str(cb.from_user.id) not in {str(i) for i in cfg.admin_ids}:
        await cb.answer("Недостаточно прав", show_alert=True)
        return
    billing = BillingService(session)
    payment = await PaymentRepo(session).get(callback_data.payment_id)
    if payment is None or payment.status != PaymentStatus.PENDING:
        await cb.answer("Платёж уже обработан", show_alert=True)
        return
    # сервер: из payload (выбор страны на сайте), иначе лучший свободный
    server_code = (payment.payload or {}).get("server_code")
    server = await ServerRepo(session).get_by_code(server_code) if server_code else None
    if server is None:
        server = await ServerRepo(session).pick_best()
    if server is None:
        await cb.answer("Нет доступных серверов!", show_alert=True)
        return
    try:
        sub = await billing.grant_for_payment(server, payment)
    except Exception as e:  # noqa: BLE001
        log.error("manual.approve_provision_failed", err=str(e), payment_id=payment.id)
        await cb.message.edit_caption(caption="⚠️ Оплата подтверждена, но выдача не удалась — retry-задача доделает.", reply_markup=None)
        return
    from app.services.referral import credit_referral

    await credit_referral(session, payment)
    payment.payload = {**payment.payload, "approved_by": f"tg-admin:{cb.from_user.id}"}
    session.add(payment)
    await UserRepo(session).audit(cb.from_user.id, "payment.approve.manual", "payment", payment.id)

    if cb.message is not None:
        try:
            await cb.message.edit_caption(caption=f"✅ Подтверждено ({cb.from_user.full_name})", reply_markup=None)
        except Exception:
            pass
    # отправляем юзеру подтверждение, ключ и меню
    from app.db.repositories.subs import SubRepo
    from app.handlers.start import send_main_menu

    target = await UserRepo(session).get(payment.user_id)
    if target is None:
        await cb.answer("Подтверждено")
        return
    if target.telegram_id is None:
        # веб-юзер: ключ увидит на сайте (страница платежа обновится поллингом)
        await cb.answer("Подтверждено — ключ выдан на сайте")
        return
    await notify_user(cb.bot, target.telegram_id, t("manual_approved"))
    fresh = await SubRepo(session).get(sub.id)
    if fresh is not None:
        url_text = await _success_text(fresh)
        await notify_user(cb.bot, target.telegram_id, url_text)
    await send_main_menu(cb.bot, target.telegram_id, target)
    await cb.answer("Подтверждено")


async def _success_text(sub) -> str:
    from app.handlers.buy import build_vless_url
    from app.services.xui.links import build_subscription_link

    cfg = get_config()
    url = await build_vless_url(sub)
    parts = [t("success_intro")]
    if sub.server.sub_url:
        parts.append(t("success_sub_url", sub_url=build_subscription_link(sub.server.sub_url, sub.xui_sub_id)))
    parts.append(f"\n<code>{url}</code>")
    del cfg
    return "".join(parts)


@router.callback_query(ManualPayCB.filter(F.action == "reject"))
async def cb_reject(cb: CallbackQuery, callback_data: ManualPayCB, session: AsyncSession, state: FSMContext) -> None:
    cfg = get_config()
    if str(cb.from_user.id) not in {str(i) for i in cfg.admin_ids}:
        await cb.answer("Недостаточно прав", show_alert=True)
        return
    payment = await PaymentRepo(session).get(callback_data.payment_id)
    if payment is None or payment.status != PaymentStatus.PENDING:
        await cb.answer("Платёж уже обработан", show_alert=True)
        return
    payment.status = PaymentStatus.CANCELED
    payment.payload = {**payment.payload, "rejected_by": f"tg-admin:{cb.from_user.id}"}
    session.add(payment)
    await UserRepo(session).audit(cb.from_user.id, "payment.reject.manual", "payment", payment.id, {"reason": "без причины"})
    if cb.message is not None:
        try:
            await cb.message.edit_caption(caption=f"❌ Отклонено ({cb.from_user.full_name})", reply_markup=None)
        except Exception:
            pass
    target = await UserRepo(session).get(payment.user_id)
    if target is not None:
        if target.telegram_id is None:
            await cb.answer("Отклонено — статус обновится на сайте")
            return
        await notify_user(cb.bot, target.telegram_id, t("manual_rejected", reason="не указана"))
    await cb.answer("Отклонено")
