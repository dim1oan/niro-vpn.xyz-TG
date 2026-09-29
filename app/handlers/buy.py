"""Сценарий покупки: страна → тариф → промокод → подтверждение → способ оплаты."""

from __future__ import annotations

from dataclasses import dataclass

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_config
from app.db.models import PaymentProvider, Plan, Server
from app.db.repositories.payments import PromoRepo
from app.db.repositories.plans import PlanRepo
from app.db.repositories.servers import ServerRepo
from app.handlers.common import plan_price_text, safe_edit, safe_edit_id
from app.keyboards.callbacks import BuyCB, MenuCB
from app.keyboards.user import (
    confirm_purchase_kb,
    payment_methods_kb,
    plans_keyboard,
    success_kb,
)
from app.locales.ru import t
from app.services.billing import BillingService
from app.services.payments.yookassa import YooKassaClient
from app.services.promo import apply_promo, format_price
from app.utils.logging import get_logger

router = Router(name="buy")
log = get_logger("buy")


class BuyFSM(StatesGroup):
    promo = State()


@dataclass
class DraftPurchase:
    plan_id: int
    country_code: str
    promo: str = ""
    discount_kopeks: int = 0


# черновики покупок в памяти процесса (один инстанс бота)
_drafts: dict[int, DraftPurchase] = {}


def get_draft(telegram_id: int) -> DraftPurchase | None:
    return _drafts.get(telegram_id)


def set_draft(telegram_id: int, draft: DraftPurchase) -> None:
    _drafts[telegram_id] = draft


async def _load_plan_server(session: AsyncSession, draft: DraftPurchase) -> tuple[Plan, Server] | tuple[None, None]:
    plan = await PlanRepo(session).get(draft.plan_id)
    server = await ServerRepo(session).get_by_code(draft.country_code)
    return (plan, server) if plan and server else (None, None)


async def _refresh_discount(session: AsyncSession, draft: DraftPurchase) -> None:
    plan, _ = await _load_plan_server(session, draft)
    draft.discount_kopeks = 0
    if plan and draft.promo:
        _, result = await apply_promo(PromoRepo(session), draft.promo, plan.price_kopeks)
        if result.ok:
            draft.discount_kopeks = result.discount_kopeks


# ---------- выбор страны ----------

@router.callback_query(MenuCB.filter(F.action == "buy"))
@router.callback_query(BuyCB.filter(F.step == "start"))
async def cb_choose_country(cb: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    """Выбор страны убран: один ключ = все серверы (Латвия + Нидерланды)."""
    await state.clear()
    servers = await ServerRepo(session).list_active()
    if not servers or cb.message is None:
        await cb.answer("Нет доступных серверов, зайдите позже", show_alert=True)
        return
    server = servers[0]
    plans = await PlanRepo(session).list_active()
    text = "🌍 Доступные страны: 🇱🇻 Латвия + 🇳🇱 Нидерланды\n\n" + t("choose_plan")
    await safe_edit(cb.message, text, plans_keyboard(plans, server.code, back_to_menu=True))
    await cb.answer()


@router.callback_query(BuyCB.filter(F.step == "country"))
async def cb_country_picked(cb: CallbackQuery, session: AsyncSession, callback_data: BuyCB) -> None:
    server = await ServerRepo(session).get_by_code(callback_data.country or "")
    if server is None or cb.message is None:
        await cb.answer("Сервер недоступен", show_alert=True)
        return
    plans = await PlanRepo(session).list_active()
    text = t("country_only", flag=server.country_flag, name=server.country_name) + "\n\n" + t("choose_plan")
    await safe_edit(cb.message, text, plans_keyboard(plans, server.code))
    await cb.answer()


@router.callback_query(BuyCB.filter(F.step == "plans"))
async def cb_back_to_plans(cb: CallbackQuery, session: AsyncSession, callback_data: BuyCB) -> None:
    """«Назад к тарифам»: показать список тарифов выбранной страны."""
    server = await ServerRepo(session).get_by_code(callback_data.country or "")
    if server is None or cb.message is None:
        await cb.answer("Сервер недоступен", show_alert=True)
        return
    plans = await PlanRepo(session).list_active()
    text = t("country_only", flag=server.country_flag, name=server.country_name) + "\n\n" + t("choose_plan")
    await safe_edit(cb.message, text, plans_keyboard(plans, server.code))
    await cb.answer()


@router.callback_query(BuyCB.filter(F.step == "plan"))
async def cb_plan_picked(cb: CallbackQuery, session: AsyncSession, user, callback_data: BuyCB) -> None:
    plan = await PlanRepo(session).get(callback_data.plan_id)
    server = await ServerRepo(session).get_by_code(callback_data.country or "")
    if plan is None or server is None or cb.message is None:
        await cb.answer("Тариф не найден", show_alert=True)
        return
    set_draft(user.telegram_id, DraftPurchase(plan_id=plan.id, country_code=server.code))
    price = plan_price_text(plan)
    text = (
        f"{t('plan_card', title=plan.title, days=plan.duration_days, devices=plan.device_limit, price=price)}\n\n"
        f"Страна: {server.country_flag} {server.country_name}"
    )
    from app.db.repositories.subs import SubRepo
    from app.utils.time import fmt_dt

    active_subs = await SubRepo(session).list_user(user.id, only_active=True)
    if active_subs:
        earliest = min(s.expires_at for s in active_subs)
        text += "\n\n" + t("already_have_sub", expires=fmt_dt(earliest, get_config().tz))
    await safe_edit(cb.message, text, confirm_purchase_kb(plan.id, server.code))
    await cb.answer()


# ---------- промокод ----------

@router.callback_query(BuyCB.filter(F.step == "promo"))
async def cb_promo_input(cb: CallbackQuery, user, state: FSMContext, callback_data: BuyCB) -> None:
    draft = get_draft(user.telegram_id) or DraftPurchase(plan_id=callback_data.plan_id, country_code=callback_data.country or "")
    set_draft(user.telegram_id, draft)
    await state.set_state(BuyFSM.promo)
    if cb.message is not None:
        sent = await cb.message.answer(t("enter_promo"))
        await state.update_data(promo_msg_id=sent.message_id)
    await cb.answer()


@router.message(BuyFSM.promo, F.text)
async def on_promo_text(message: Message, session: AsyncSession, user, state: FSMContext) -> None:
    code = (message.text or "").strip()
    data = await state.get_data()
    await state.clear()
    draft = get_draft(user.telegram_id)
    if draft is None:
        await message.answer(t("menu_title"))
        return
    if code not in {"-", ""}:
        draft.promo = code
        await _refresh_discount(session, draft)
        if draft.discount_kopeks > 0:
            await message.answer(t("promo_applied", discount=format_price(draft.discount_kopeks)))
        else:
            draft.promo = ""
            await message.answer(t("promo_invalid"))
    await cb_show_confirm_message(message.bot, message.chat.id, data.get("promo_msg_id"), session, user.telegram_id)


async def cb_show_confirm_message(bot, chat_id: int, message_id: int | None, session: AsyncSession, telegram_id: int) -> None:
    draft = get_draft(telegram_id)
    plan, server = await _load_plan_server(session, draft) if draft else (None, None)
    if plan is None or server is None or draft is None:
        if message_id:
            try:
                await safe_edit_id(bot, chat_id, message_id, t("menu_title"))
            except Exception:
                pass
        return
    markup = confirm_purchase_kb(plan.id, server.code)
    if message_id:
        await safe_edit_id(bot, chat_id, message_id, _confirm_text(plan, server, draft), markup)
    else:
        await bot.send_message(chat_id, _confirm_text(plan, server, draft), reply_markup=markup)


# ---------- экран оплаты ----------

def _confirm_text(plan: Plan, server: Server, draft: DraftPurchase | None = None) -> str:
    discount = draft.discount_kopeks if draft else 0
    price_rub = (plan.price_kopeks - discount) / 100
    promo_line = f"\n🎟 Промокод: −{format_price(discount)}" if discount else ""
    return t(
        "confirm_purchase",
        title=plan.title,
        days=plan.duration_days,
        devices=plan.device_limit,
        flag=server.country_flag,
        country=server.country_name,
        price_rub=price_rub,
        promo_line=promo_line,
    )


@router.callback_query(BuyCB.filter(F.step == "confirm"))
async def cb_confirm(cb: CallbackQuery, session: AsyncSession, user, callback_data: BuyCB) -> None:
    draft = get_draft(user.telegram_id)
    if callback_data.plan_id and (draft is None or draft.plan_id != callback_data.plan_id):
        draft = DraftPurchase(plan_id=callback_data.plan_id, country_code=callback_data.country or "")
        set_draft(user.telegram_id, draft)
        await _refresh_discount(session, draft)
    plan, server = await _load_plan_server(session, draft) if draft else (None, None)
    if plan is None or server is None or cb.message is None or draft is None:
        await cb.answer("Ошибка заказа", show_alert=True)
        return
    cfg = get_config()
    yk_enabled = YooKassaClient().enabled
    markup = payment_methods_kb(
        plan.id,
        server.code,
        stars_enabled=cfg.stars_enabled and bool(plan.price_stars),
        manual_enabled=cfg.manual_pay_enabled,
        yookassa_enabled=yk_enabled,
        sbp_enabled=yk_enabled,
        balance_enabled=user.balance_kopeks > 0,
        balance_kopeks=user.balance_kopeks,
    )
    await safe_edit(cb.message, _confirm_text(plan, server, draft), markup)
    await cb.answer()


# ---------- выбранный провайдер ----------

@router.callback_query(BuyCB.filter(F.step == "pay"))
async def cb_pay(cb: CallbackQuery, session: AsyncSession, user, callback_data: BuyCB, **kwargs) -> None:
    raw_provider = callback_data.provider or ""
    sbp = raw_provider == PaymentProvider.YOOKASSA_SBP.value
    provider = PaymentProvider.YOOKASSA if sbp else PaymentProvider(raw_provider)
    draft = get_draft(user.telegram_id)
    plan, server = (
        await _load_plan_server(session, draft) if draft else (None, None)
    )
    if plan is None or server is None or cb.message is None or draft is None:
        await cb.answer("Ошибка платежа", show_alert=True)
        return
    billing = BillingService(session)

    if provider == PaymentProvider.YOOKASSA:
        payment, amount = await billing.create_payment(
            user=user, plan=plan, provider=provider, promo_code=draft.promo, server=server
        )
        cfg = get_config()
        yk = YooKassaClient()
        username = (await cb.bot.me()).username
        result = await yk.create_payment(
            amount_kopeks=amount,
            description=f"{cfg.brand_name}: {plan.title}",
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
        title = "⚡ Подтвердите платёж в приложении банка (СБП):" if sbp else f"Счёт №{payment.id} создан. Оплатите по кнопке:"
        await safe_edit(cb.message, title, ikb.as_markup())
        await cb.answer()
        return

    if provider == PaymentProvider.STARS:
        from app.handlers.payments_stars import send_stars_invoice

        payment, _ = await billing.create_payment(
            user=user, plan=plan, provider=provider, promo_code=draft.promo, server=server
        )
        await send_stars_invoice(cb.bot, chat_id=cb.message.chat.id, plan=plan, payment_id=payment.id)
        await cb.answer()
        return

    if provider == PaymentProvider.MANUAL:
        from app.handlers.payments_manual import start_manual_payment

        state_ctx: FSMContext | None = kwargs.get("state")
        await start_manual_payment(cb, session, user, plan, server, draft, state=state_ctx)
        return

    if provider == PaymentProvider.BALANCE:
        payment, error = await billing.pay_from_balance(user, plan, promo_code=draft.promo)
        if payment is None:
            need = plan.price_kopeks - (user.balance_kopeks if user.balance_kopeks > 0 else 0)
            await cb.answer(f"Недостаточно средств. Нужно ещё {format_price(need)}", show_alert=True)
            return
        payment.payload = {**payment.payload, "server_code": server.code}
        await session.flush()
        try:
            sub = await billing.grant_for_payment(server, payment)
        except Exception:  # noqa: BLE001
            await cb.answer("Ошибка выдачи, попробуйте позже", show_alert=True)
            return
        from app.services.referral import credit_referral

        await credit_referral(session, payment)
        await show_success(cb.message, session, sub)
        await cb.answer()
        return


async def show_success(message: Message, session: AsyncSession, sub) -> None:
    url = await build_vless_url(sub)
    parts = [t("success_intro")]
    if sub.server.sub_url:
        from app.services.xui.links import build_subscription_link

        sub_url = build_subscription_link(sub.server.sub_url, sub.xui_sub_id)
        parts.append(t("success_sub_url", sub_url=sub_url))
    parts.append(f"\n<code>{url}</code>")
    parts.append(t("success_footer"))
    await message.answer("".join(parts), reply_markup=success_kb(), disable_web_page_preview=True)


_inbound_cache: dict[int, object] = {}


async def build_vless_url(sub, session=None) -> str:
    """Единая подписка (base64) со всеми серверами. IP скрыты за доменами nl/lv."""
    import base64

    from sqlalchemy import select as _sel

    from app.services.xui.factory import get_client
    from app.services.xui.links import build_vless_link, make_label

    cfg = get_config()
    if session is not None:
        servers = (await session.execute(
            _sel(Server).where(Server.is_active == True).order_by(Server.id)  # noqa: E712
        )).scalars().all()
    else:
        from app.db.session import make_engine, make_session_factory, session_scope
        async with session_scope(make_session_factory(make_engine(get_config()))) as s:
            servers = (await s.execute(
                _sel(Server).where(Server.is_active == True).order_by(Server.id)  # noqa: E712
            )).scalars().all()
    links = []
    for srv in servers:
        key = (srv.id, srv.inbound_id)
        inbound = _inbound_cache.get(key)
        if inbound is None:
            client = get_client(srv)
            inbound = await client.get_inbound(srv.inbound_id)
            _inbound_cache[key] = inbound
        label = make_label(cfg.brand_name, srv.country_flag, srv.country_name)
        links.append(build_vless_link(
            uuid=sub.xui_client_uuid, inbound=inbound,
            server_host=srv.server_host, label=label
        ))
    return base64.b64encode("\n".join(links).encode()).decode()
