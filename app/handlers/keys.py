"""Мои ключи: список, карточка, ссылка/QR/продление/перевыпуск/сброс трафика."""

from __future__ import annotations

from datetime import timedelta

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_config
from app.db.models import PaymentProvider, Subscription, SubStatus
from app.db.repositories.plans import PlanRepo
from app.db.repositories.subs import SubRepo
from app.handlers.buy import build_vless_url
from app.handlers.common import safe_edit
from app.keyboards.callbacks import KeysCB, MenuCB
from app.keyboards.user import key_card_kb, renew_pay_kb, renew_plans_kb
from app.locales.ru import t
from app.services.billing import BillingService
from app.services.payments.yookassa import YooKassaClient
from app.services.promo import format_price
from app.services.provisioning import ProvisioningError, ProvisioningService
from app.utils.qr import make_qr_png
from app.utils.time import fmt_dt, human_bytes, human_days_left, utcnow

router = Router(name="keys")

STATUS_TEXT = {SubStatus.ACTIVE: "активна", SubStatus.EXPIRED: "истекла", SubStatus.DISABLED: "отключена", SubStatus.DELETED: "удалена"}
STATUS_EMOJI = {SubStatus.ACTIVE: "🟢", SubStatus.EXPIRED: "🔴", SubStatus.DISABLED: "⛔", SubStatus.DELETED: "🗑"}


@router.callback_query(MenuCB.filter(F.action == "keys"))
@router.callback_query(KeysCB.filter(F.action == "list"))
async def cb_keys_list(cb: CallbackQuery, session: AsyncSession, user) -> None:
    subs = await SubRepo(session).list_user(user.id)
    if cb.message is None:
        await cb.answer()
        return
    if not subs:
        await safe_edit(cb.message, t("keys_empty"))
        await cb.answer()
        return

    lines = ["<b>🔑 Ваши ключи</b>", ""]
    for i, sub in enumerate(subs, 1):
        status = STATUS_EMOJI.get(sub.status, "⚪")
        left = human_days_left(sub.expires_at) if sub.expires_at > utcnow() else "истекла"
        traffic = human_bytes(sub.traffic_used_bytes) if sub.last_synced_at else "—"
        lines.append(
            f"{i}. {status} <b>{sub.server.country_flag} {sub.server.country_name}</b> · {sub.plan.title}\n"
            f"   📅 {fmt_dt(sub.expires_at, get_config().tz)} ({left}) · 📊 {traffic}"
        )
    lines.append("\nВыберите ключ:")
    from aiogram.utils.keyboard import InlineKeyboardBuilder

    ikb = InlineKeyboardBuilder()
    for i, sub in enumerate(subs, 1):
        ikb.button(text=f"{i}. {sub.server.country_flag} {sub.plan.title}", callback_data=KeysCB(action="card", sub_id=sub.id))
    ikb.button(text="⬅️ В меню", callback_data=MenuCB(action="back"))
    ikb.adjust(1)
    await safe_edit(cb.message, "\n".join(lines), ikb.as_markup())
    await cb.answer()


def _card_text(sub: Subscription) -> str:
    traffic = human_bytes(sub.traffic_used_bytes) if sub.last_synced_at else "—"
    return (
        f"🔑 <b>{sub.server.country_flag} {sub.server.country_name}</b> — {sub.plan.title}\n\n"
        f"📅 Действует до: <b>{fmt_dt(sub.expires_at, get_config().tz)}</b>\n"
        f"📊 Трафик за период: {traffic}\n"
        f"{STATUS_EMOJI.get(sub.status, '⚪')} Статус: {STATUS_TEXT.get(sub.status, '—')}"
    )


@router.callback_query(KeysCB.filter(F.action == "card"))
async def cb_key_card(cb: CallbackQuery, session: AsyncSession, user, callback_data: KeysCB) -> None:
    sub = await SubRepo(session).by_id_for_user(callback_data.sub_id, user.id)
    if sub is None or cb.message is None:
        await cb.answer("Ключ не найден", show_alert=True)
        return
    await safe_edit(cb.message, _card_text(sub), key_card_kb(sub.id))
    await cb.answer()


@router.callback_query(KeysCB.filter(F.action == "link"))
async def cb_show_link(cb: CallbackQuery, session: AsyncSession, user, callback_data: KeysCB) -> None:
    sub = await SubRepo(session).by_id_for_user(callback_data.sub_id, user.id)
    if sub is None or cb.message is None:
        await cb.answer("Ключ не найден", show_alert=True)
        return
    url = await build_vless_url(sub)
    text = t("link_shown", url=url)
    if sub.server.sub_url:
        from app.services.xui.links import build_subscription_link

        text += "\n\n" + t("sub_shown", sub_url=build_subscription_link(sub.server.sub_url, sub.xui_sub_id))
    from aiogram.utils.keyboard import InlineKeyboardBuilder as IK

    ikb = IK()
    ikb.button(text="⬅️ К карточке", callback_data=KeysCB(action="card", sub_id=sub.id))
    await safe_edit(cb.message, text, ikb.as_markup(), disable_web_page_preview=True)
    await cb.answer()


@router.callback_query(KeysCB.filter(F.action == "qr"))
async def cb_qr(cb: CallbackQuery, session: AsyncSession, user, callback_data: KeysCB) -> None:
    sub = await SubRepo(session).by_id_for_user(callback_data.sub_id, user.id)
    if sub is None or cb.message is None:
        await cb.answer("Ключ не найден", show_alert=True)
        return
    url = await build_vless_url(sub)
    png = make_qr_png(url)
    ikb = InlineKeyboardBuilder()
    ikb.button(text="⬅️ К карточке", callback_data=KeysCB(action="card", sub_id=sub.id))
    ikb.button(text="🏠 Главное меню", callback_data=MenuCB(action="back"))
    ikb.adjust(1)
    await cb.message.answer_photo(
        BufferedInputFile(png.getvalue(), filename="vpn-qr.png"),
        caption="📷 Отсканируйте QR в клиенте",
        reply_markup=ikb.as_markup(),
    )
    await cb.answer()


# ---------- продление ----------

@router.callback_query(KeysCB.filter(F.action == "renew"))
async def cb_renew_menu(cb: CallbackQuery, session: AsyncSession, user, callback_data: KeysCB) -> None:
    sub = await SubRepo(session).by_id_for_user(callback_data.sub_id, user.id)
    plans = await PlanRepo(session).list_active()
    if sub is None or cb.message is None:
        await cb.answer("Ключ не найден", show_alert=True)
        return
    await safe_edit(cb.message, t("renew_choose"), renew_plans_kb(plans, sub.id))
    await cb.answer()


@router.callback_query(KeysCB.filter(F.action == "renew_do"))
async def cb_renew_do(cb: CallbackQuery, session: AsyncSession, user, callback_data: KeysCB) -> None:
    """Продление: выбираем способ оплаты (баланс убран — сразу к оплате)."""
    plan = await PlanRepo(session).get(callback_data.plan_id)
    sub = await SubRepo(session).by_id_for_user(callback_data.sub_id, user.id)
    if plan is None or sub is None or cb.message is None:
        await cb.answer("Ошибка", show_alert=True)
        return
    cfg = get_config()
    from app.services.payments.yookassa import YooKassaClient

    markup = renew_pay_kb(
        sub.id,
        plan.id,
        stars_enabled=cfg.stars_enabled and bool(plan.price_stars),
        manual_enabled=cfg.manual_pay_enabled,
        yookassa_enabled=YooKassaClient().enabled,
        sbp_enabled=YooKassaClient().enabled,
        balance_enabled=user.balance_kopeks > 0,
        balance_kopeks=user.balance_kopeks,
    )
    await safe_edit(cb.message, t("renew_choose_pay"), markup)
    await cb.answer()


@router.callback_query(KeysCB.filter(F.action == "renew_pay"))
async def cb_renew_pay(cb: CallbackQuery, session: AsyncSession, user, callback_data: KeysCB, **kwargs) -> None:
    """Создаёт платёж на продление и перенаправляет на выбранный способ оплаты."""
    provider = PaymentProvider(callback_data.provider or "")
    plan = await PlanRepo(session).get(callback_data.plan_id)
    sub = await SubRepo(session).by_id_for_user(callback_data.sub_id, user.id)
    if plan is None or sub is None or cb.message is None:
        await cb.answer("Ошибка", show_alert=True)
        return
    raw_provider = callback_data.provider or ""
    sbp = raw_provider == PaymentProvider.YOOKASSA_SBP.value
    provider = PaymentProvider.YOOKASSA if sbp else PaymentProvider(raw_provider)
    server = sub.server
    billing = BillingService(session)

    if provider == PaymentProvider.BALANCE:
        payment, error = await billing.pay_from_balance(user, plan)
        if payment is None:
            need = plan.price_kopeks - (user.balance_kopeks if user.balance_kopeks > 0 else 0)
            await cb.answer(f"Недостаточно средств. Нужно ещё {format_price(need)}", show_alert=True)
            return
        payment.payload = {**payment.payload, "renew_sub_id": sub.id, "server_code": server.code}
        await session.flush()
        try:
            await billing.grant_for_payment(server, payment)
        except Exception:  # noqa: BLE001
            await cb.answer("Ошибка выдачи, попробуйте позже", show_alert=True)
            return
        from app.services.referral import credit_referral

        await credit_referral(session, payment)
        from app.handlers.buy import build_vless_url

        url = await build_vless_url(sub)
        ikb = InlineKeyboardBuilder()
        ikb.button(text="🔑 К карточке ключа", callback_data=KeysCB(action="card", sub_id=sub.id))
        ikb.button(text="🏠 Главное меню", callback_data=MenuCB(action="back"))
        await safe_edit(
            cb.message,
            f"✅ Подписка продлена на {plan.duration_days} дн. — до <b>{fmt_dt(sub.expires_at, get_config().tz)}</b>\n\n"
            f"Ваша ссылка (не изменилась):\n<code>{url}</code>",
            ikb.as_markup(),
            disable_web_page_preview=True,
        )
        await cb.answer()
        return

    payment, amount = await billing.create_payment(
        user=user, plan=plan, provider=provider, promo_code=None, server=server
    )
    payment.payload = {**payment.payload, "renew_sub_id": sub.id}
    await session.flush()

    if provider == PaymentProvider.YOOKASSA:
        cfg = get_config()
        yk = YooKassaClient()
        username = (await cb.bot.me()).username
        result = await yk.create_payment(
            amount_kopeks=amount,
            description=f"{cfg.brand_name}: продление {plan.title}",
            payment_db_id=payment.id,
            return_url=f"https://t.me/{username}",
            metadata={"payment_id": str(payment.id)},
            sbp=sbp,
        )
        payment.payload = {**payment.payload, "yookassa_id": result.get("id")}
        await session.flush()
        ikb = InlineKeyboardBuilder()
        ikb.button(text="💳 Перейти к оплате", url=result["confirmation_url"])
        ikb.button(text="🏠 Главное меню", callback_data=MenuCB(action="back"))
        ikb.adjust(1)
        title = "⚡ Подтвердите платёж в приложении банка (СБП):" if sbp else f"Счёт №{payment.id} на продление создан. Оплатите по кнопке:"
        await safe_edit(cb.message, title, ikb.as_markup())
        await cb.answer()
        return

    if provider == PaymentProvider.STARS:
        from app.handlers.payments_stars import send_stars_invoice

        await send_stars_invoice(cb.bot, chat_id=cb.message.chat.id, plan=plan, payment_id=payment.id)
        await cb.answer()
        return

    if provider == PaymentProvider.MANUAL:
        from app.handlers.payments_manual import start_manual_payment

        state_ctx: FSMContext | None = kwargs.get("state")
        await start_manual_payment(cb, session, user, plan, server, None, state=state_ctx, renew_sub_id=sub.id)
        return


# ---------- перевыпуск UUID ----------

REISSUE_COOLDOWN = timedelta(hours=24)


@router.callback_query(KeysCB.filter(F.action == "reissue"))
async def cb_reissue(cb: CallbackQuery, session: AsyncSession, user, callback_data: KeysCB) -> None:
    sub = await SubRepo(session).by_id_for_user(callback_data.sub_id, user.id)
    if sub is None or cb.message is None:
        await cb.answer("Ключ не найден", show_alert=True)
        return
    now = utcnow()
    if sub.last_reissue_at and now - sub.last_reissue_at < REISSUE_COOLDOWN:
        next_at = sub.last_reissue_at + REISSUE_COOLDOWN
        await cb.answer(t("reissue_limit", time=fmt_dt(next_at, get_config().tz)), show_alert=True)
        return
    service = ProvisioningService(session)
    try:
        await service.reissue(sub)
    except ProvisioningError:
        await cb.answer("Ошибка панели, попробуйте позже", show_alert=True)
        return
    sub.last_reissue_at = now
    session.add(sub)
    url = await build_vless_url(sub)
    ikb = InlineKeyboardBuilder()
    ikb.button(text="⬅️ К карточке", callback_data=KeysCB(action="card", sub_id=sub.id))
    ikb.adjust(1)
    await cb.answer("🔄 Перевыпущено")
    await safe_edit(cb.message, t("reissued", url=url), ikb.as_markup(), disable_web_page_preview=True)


# ---------- сброс трафика ----------

@router.callback_query(KeysCB.filter(F.action == "reset_traffic"))
async def cb_reset_traffic(cb: CallbackQuery, session: AsyncSession, user, callback_data: KeysCB) -> None:
    sub = await SubRepo(session).by_id_for_user(callback_data.sub_id, user.id)
    if sub is None:
        await cb.answer("Ключ не найден", show_alert=True)
        return
    from app.services.xui.factory import get_client

    client = get_client(sub.server)
    try:
        await client.reset_client_traffic(sub.xui_email)
    except Exception:
        await cb.answer("Ошибка панели, попробуйте позже", show_alert=True)
        return
    sub.traffic_used_bytes = 0
    sub.last_synced_at = utcnow()
    session.add(sub)
    await cb.answer(t("reset_done"), show_alert=True)


@router.callback_query(KeysCB.filter(F.action == "guides"))
async def cb_guides_from_key(cb: CallbackQuery) -> None:
    from app.handlers.guides import send_guides_index

    if cb.message is not None:
        await send_guides_index(cb.message)
    await cb.answer()


async def render_key_message(message: Message, session: AsyncSession, telegram_user_id: int, sub_id: int) -> None:
    """Хелпер для тестов/переиспользования."""
    sub = await SubRepo(session).by_id_for_user(sub_id, telegram_user_id)
    if sub is not None:
        await message.answer(_card_text(sub), reply_markup=key_card_kb(sub.id))
