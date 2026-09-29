"""Админ-панель: /admin, статистика, пользователи, подписки, серверы, тарифы,
промокоды, рассылка, платежи, логи."""

from __future__ import annotations

import asyncio
import csv
import html
import io
from datetime import timedelta

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_config
from app.db.models import (
    AuditLog,
    Payment,
    PaymentProvider,
    PaymentStatus,
    Plan,
    PromoType,
    Server,
    Subscription,
    SubStatus,
    User,
)
from app.db.repositories.payments import PaymentRepo, PromoRepo
from app.db.repositories.servers import ServerRepo
from app.db.repositories.subs import SubRepo
from app.db.repositories.users import UserRepo
from app.handlers.buy import build_vless_url
from app.handlers.common import safe_edit
from app.keyboards.admin import (
    admin_back,
    admin_menu,
    admin_server_card_kb,
    admin_servers_list_kb,
    admin_user_card,
    admin_users_list,
)
from app.keyboards.callbacks import AdminCB
from app.services.notifications import esc, notify_admins, notify_user
from app.services.promo import format_price
from app.utils.logging import get_logger
from app.utils.time import fmt_dt, human_bytes, utcnow

router = Router(name="admin")
log = get_logger("admin")

# последнее сообщение бота в админ-панели (для единообразного редактирования)
_admin_msg: dict[int, int] = {}


def _track(cb: CallbackQuery) -> None:
    if cb.message is not None:
        _admin_msg[cb.from_user.id] = cb.message.message_id


async def _admin_edit(cb: CallbackQuery, text: str, reply_markup=None) -> None:
    _track(cb)
    if cb.message is not None:
        await safe_edit(cb.message, text, reply_markup)
    await cb.answer()


class AdminFSM(StatesGroup):
    broadcast_text = State()
    grant_days = State()
    server_inbound = State()


def is_admin(telegram_id: int | None) -> bool:
    if telegram_id is None:
        return False
    return str(telegram_id) in {str(i) for i in get_config().admin_ids}


@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext) -> None:
    await state.clear()
    if not is_admin(message.from_user.id):
        return
    sent = await message.answer("🛠 <b>Админ-панель</b>", reply_markup=admin_menu())
    _admin_msg[message.from_user.id] = sent.message_id


def admin_guard(handler):
    """Пускает только админов. Пробрасывает хендлеру только те kwargs,
    что есть в его сигнатуре (aiogram инжектит лишний контекст из-за *args/**kwargs).
    Пользователя берём из самого события: после functools.wraps aiogram
    не передаёт в обёртку event_from_user."""
    import functools
    import inspect

    sig = inspect.signature(handler)
    accepts_varkw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
    known = {p.name for p in sig.parameters.values()}

    @functools.wraps(handler)
    async def wrapper(event, *args, **kwargs):
        tg_user = getattr(event, "from_user", None) or kwargs.get("event_from_user")
        if not is_admin(getattr(tg_user, "id", None)):
            cb = event if isinstance(event, CallbackQuery) else None
            if cb:
                await cb.answer("Недостаточно прав", show_alert=True)
            return None
        if not accepts_varkw:
            kwargs = {k: v for k, v in kwargs.items() if k in known}
        return await handler(event, *args, **kwargs)

    return wrapper


# ---------- статистика ----------

async def _stats_text(session: AsyncSession) -> str:
    now = utcnow()
    day_ago = now - timedelta(days=1)
    week_ago = now - timedelta(days=7)
    month_ago = now - timedelta(days=30)
    repo_p = PaymentRepo(session)
    users_total = await UserRepo(session).count_all()
    users_new = await UserRepo(session).count_new_since(day_ago)
    active_subs = await SubRepo(session).active_count()

    trial_users = int(await session.scalar(select(func.count()).select_from(User).where(User.trial_used.is_(True))) or 0)
    trial_paid = int(
        await session.scalar(
            select(func.count()).select_from(Payment).join(User, User.id == Payment.user_id)
            .where(Payment.status == PaymentStatus.PAID, Payment.provider != PaymentProvider.BALANCE, User.trial_used.is_(True))
        ) or 0
    )

    revenue_day = await repo_p.revenue_sum(day_ago)
    revenue_week = await repo_p.revenue_sum(week_ago)
    revenue_month = await repo_p.revenue_sum(month_ago)

    # MRR: активные подписки → цена плана / (дней/30)
    mrr_kopeks = 0.0
    plan_prices = {p.id: p for p in (await session.scalars(select(Plan))).all()}
    for sub in (await session.scalars(
        select(Subscription).where(Subscription.status == SubStatus.ACTIVE)
    )).all():
        plan = plan_prices.get(sub.plan_id)
        if plan and plan.price_kopeks > 0:
            mrr_kopeks += plan.price_kopeks / max(plan.duration_days, 1) * 30

    by_plan_rows = await session.execute(
        select(Plan.title, func.count(Subscription.id))
        .join(Subscription, Subscription.plan_id == Plan.id)
        .group_by(Plan.title)
    )
    by_plan = "\n".join(f"  • {esc(t)} — {c}" for t, c in by_plan_rows.all()) or "—"

    by_server_rows = await session.execute(
        select(Server.code, func.count(Subscription.id))
        .join(Subscription, Subscription.server_id == Server.id)
        .group_by(Server.code)
    )
    by_server = "\n".join(f"  • {esc(c)} — {n}" for c, n in by_server_rows.all()) or "—"

    conversion = f"{trial_paid / trial_users * 100:.0f}%" if trial_users else "—"
    return (
        "📊 <b>Статистика</b>\n\n"
        f"👥 Пользователей: <b>{users_total}</b> (+{users_new} за сутки)\n"
        f"🔑 Активных подписок: <b>{active_subs}</b>\n"
        f"💰 MRR: ~<b>{format_price(int(mrr_kopeks))}</b>\n\n"
        f"Выручка:\n  • день: {format_price(revenue_day)}\n  • неделя: {format_price(revenue_week)}\n  • месяц: {format_price(revenue_month)}\n\n"
        f"🎁 Триалов: {trial_users}, конверсия в оплату: {conversion}\n\n"
        f"<b>По тарифам:</b>\n{by_plan}\n<b>По серверам:</b>\n{by_server}\n"
    )


# ---------- маршрутизация секций ----------

@router.callback_query(AdminCB.filter(F.action == "home"))
@admin_guard
async def cb_admin_home(cb: CallbackQuery) -> None:
    await _admin_edit(cb, "🛠 <b>Админ-панель</b>", admin_menu())


@router.callback_query(AdminCB.filter((F.section == "stats") & F.action.in_({"", "menu"})))
@admin_guard
async def cb_stats(cb: CallbackQuery, session: AsyncSession) -> None:
    text = await _stats_text(session)
    await _admin_edit(cb, text, admin_back("stats"))


@router.callback_query(
    AdminCB.filter(
        F.section.in_({"users", "subs", "servers", "plans", "promo", "broadcast", "payments", "logs"})
        & F.action.in_({"", "menu"})
    )
)
@admin_guard
async def cb_section_menu(cb: CallbackQuery, session: AsyncSession, callback_data: AdminCB) -> None:
    section = callback_data.section
    if cb.message is None:
        await cb.answer()
        return
    _track(cb)

    if section == "users":
        await _render_users_list(cb, session)
        return
    elif section == "subs":
        await _render_subs_list(cb, session)
        return
    elif section == "servers":
        await _render_servers_list(cb, session)
        return
    elif section == "plans":
        plans = list((await session.scalars(select(Plan).order_by(Plan.sort_order))).all())
        lines = [
            f"{'🟢' if p.is_active else '🔴'} {esc(p.title)}: {p.duration_days}д · {format_price(p.price_kopeks)} · {p.price_stars}⭐ · {p.device_limit} устр."
            for p in plans
        ]
        await safe_edit(cb.message,"\n".join(lines) or "Тарифов нет", reply_markup=admin_back(section))
    elif section == "promo":
        promos = await PromoRepo(session).list_all(20)
        lines = []
        for pr in promos:
            exp = fmt_dt(pr.expires_at, get_config().tz) if pr.expires_at else "∞"
            lines.append(f"{'🟢' if pr.is_active else '🔴'} <code>{esc(pr.code)}</code>: {pr.type.value}={pr.value}, использований {pr.used_count}/{pr.max_uses or '∞'}, до {exp}")
        await safe_edit(cb.message,
            "\n".join(lines) or "Промокодов нет.\nСоздать: /newpromo <code>code</code> <code>percent|fixed|days</code> <code>value</code> [max_uses]",
            reply_markup=admin_back(section),
        )
    elif section == "broadcast":
        await safe_edit(cb.message,
            "📣 Рассылка.\nСегменты: all / active / expired / no_purchase\nПришлите текст сообщения командой /broadcast <code>segment</code>",
            reply_markup=admin_back(section),
        )
    elif section == "payments":
        manual_pending = await PaymentRepo(session).recent_manual_pending()
        lines = [f"• №{p.id}: user={p.user.telegram_id}, {format_price(p.amount_kopeks)}, {fmt_dt(p.created_at, get_config().tz)}" for p in manual_pending]
        await safe_edit(cb.message,"🧾 Ручные платежи в ожидании:\n" + ("\n".join(lines) or "—"), reply_markup=admin_back(section))
    elif section == "logs":
        logs = (await session.execute(
            select(AuditLog).order_by(AuditLog.created_at.desc()).limit(20)
        )).scalars().all()
        lines = [f"• {fmt_dt(row.created_at, get_config().tz)} [{row.action}] {row.entity}:{row.entity_id} от {row.actor_telegram_id}" for row in logs]
        await safe_edit(cb.message,"📜 Последние действия:\n" + ("\n".join(lines) or "—"), reply_markup=admin_back(section))
    await cb.answer()


# ---------- навигация по пользователям и подпискам ----------

@router.callback_query(AdminCB.filter((F.section == "users") & (F.action == "list")))
@admin_guard
async def cb_users_list(cb: CallbackQuery, session: AsyncSession) -> None:
    await _render_users_list(cb, session)


@router.callback_query(AdminCB.filter((F.section == "subs") & (F.action == "list")))
@admin_guard
async def cb_subs_list(cb: CallbackQuery, session: AsyncSession) -> None:
    await _render_subs_list(cb, session)


@router.callback_query(AdminCB.filter((F.section == "subs") & (F.action == "sub")))
@admin_guard
async def cb_sub_card(cb: CallbackQuery, callback_data: AdminCB, session: AsyncSession) -> None:
    _track(cb)
    sub = await SubRepo(session).get(callback_data.target_id)
    if sub is None or cb.message is None:
        await cb.answer("Подписка не найдена", show_alert=True)
        return
    text = (
        f"🔑 <b>Подписка #{sub.id}</b>\n"
        f"Пользователь: {esc(sub.user.display_name())} (<code>{sub.user.telegram_id}</code>)\n"
        f"Тариф: {esc(sub.plan.title)} · {sub.plan.duration_days} дн\n"
        f"Сервер: {sub.server.country_flag} {esc(sub.server.code)}\n"
        f"Статус: {sub.status.value}\n"
        f"Действует до: {fmt_dt(sub.expires_at, get_config().tz)}\n"
        f"Трафик: {human_bytes(sub.traffic_used_bytes) if sub.last_synced_at else '—'}"
    )
    ikb = InlineKeyboardBuilder()
    ikb.button(text="👤 Открыть пользователя", callback_data=AdminCB(section="users", action="user", target_id=sub.user.telegram_id or 0))
    ikb.button(text="⬅️ К списку подписок", callback_data=AdminCB(section="subs", action="list"))
    ikb.button(text="🏠 В админку", callback_data=AdminCB(section="subs", action="home"))
    ikb.adjust(1)
    await safe_edit(cb.message, text, ikb.as_markup())
    await cb.answer()


@router.callback_query(AdminCB.filter((F.section == "users") & (F.action == "user")))
@admin_guard
async def cb_open_user(cb: CallbackQuery, callback_data: AdminCB, session: AsyncSession) -> None:
    await _open_user_card(cb.from_user.id, cb.bot, cb.message.chat.id, session, str(callback_data.target_id))


async def _render_users_list(cb: CallbackQuery, session: AsyncSession) -> None:
    users = await UserRepo(session).list_with_active_subs(30)
    subs_map: dict[int, list] = {}
    for u in users:
        subs_map[u.id] = await UserRepo(session).active_subs_of(u.id)
    if not users:
        text = "👤 Активных подписок пока нет. Пользователи с активными подписками появятся здесь."
    else:
        text = "👤 <b>Пользователи с активными подписками:</b>\nВыберите для открытия карточки:"
    await _admin_edit(cb, text, admin_users_list(users, subs_map))


async def _render_subs_list(cb: CallbackQuery, session: AsyncSession) -> None:
    now = utcnow()
    subs = list((await session.scalars(
        select(Subscription)
        .where(Subscription.status == SubStatus.ACTIVE, Subscription.expires_at > now)
        .order_by(Subscription.expires_at.asc())
        .limit(30)
    )).all())
    if not subs:
        await _admin_edit(cb, "🔑 Активных подписок нет.", admin_back("subs"))
        return
    kb = InlineKeyboardBuilder()
    for s in subs:
        name = s.user.display_name()
        if len(name) > 14:
            name = name[:14] + "…"
        label = f"🔑 #{s.id} · {name} · {esc(s.plan.title)} · до {fmt_dt(s.expires_at, get_config().tz)}"
        kb.button(text=label[:60], callback_data=AdminCB(section="subs", action="sub", target_id=s.id))
    kb.button(text="🏠 В админку", callback_data=AdminCB(section="subs", action="home"))
    kb.adjust(1)
    await _admin_edit(cb, f"🔑 <b>Активные подписки ({len(subs)}):</b>", kb.as_markup())


# ---------- серверы: карточка, вкл/выкл, смена inbound ----------

async def _render_servers_list(cb: CallbackQuery, session: AsyncSession) -> None:
    servers = await ServerRepo(session).list_all()
    text = "🖥 <b>Серверы:</b>" if servers else "Серверов нет"
    await _admin_edit(cb, text, admin_servers_list_kb(servers))


@router.callback_query(AdminCB.filter((F.section == "servers") & (F.action == "list")))
@admin_guard
async def cb_servers_list(cb: CallbackQuery, session: AsyncSession) -> None:
    await _render_servers_list(cb, session)


@router.callback_query(AdminCB.filter((F.section == "servers") & (F.action == "srv")))
@admin_guard
async def cb_server_card(cb: CallbackQuery, callback_data: AdminCB, session: AsyncSession) -> None:
    _track(cb)
    server = await ServerRepo(session).get(callback_data.target_id)
    if server is None or cb.message is None:
        await cb.answer("Сервер не найден", show_alert=True)
        return
    count = await ServerRepo(session).active_clients_count(server.id)
    status = "🟢 активен" if server.is_active else "🔴 отключён"
    text = (
        f"🖥 <b>{server.country_flag} {server.code}</b> ({esc(server.country_name)})\n"
        f"Статус: {status}\n"
        f"Inbound: <code>{server.inbound_id}</code>\n"
        f"Хост: <code>{esc(server.server_host)}</code>\n"
        f"Клиентов (активных): {count}/{server.max_clients}\n"
        f"Протокол: {esc(server.protocol)}"
    )
    await safe_edit(cb.message, text, admin_server_card_kb(server.id, is_active=server.is_active))
    await cb.answer()


@router.callback_query(AdminCB.filter((F.section == "servers") & (F.action == "toggle_active")))
@admin_guard
async def cb_server_toggle_active(cb: CallbackQuery, callback_data: AdminCB, session: AsyncSession) -> None:
    server = await ServerRepo(session).get(callback_data.target_id)
    if server is None:
        await cb.answer("Сервер не найден", show_alert=True)
        return
    server.is_active = not server.is_active
    session.add(server)
    await UserRepo(session).audit(cb.from_user.id, "admin.server_toggle", "server", server.id, {"active": server.is_active})
    _track(cb)
    status = "🟢 активен" if server.is_active else "🔴 отключён"
    count = await ServerRepo(session).active_clients_count(server.id)
    if cb.message is not None:
        await safe_edit(
            cb.message,
            f"🖥 <b>{server.country_flag} {server.code}</b> ({esc(server.country_name)})\n"
            f"Статус: {status}\nInbound: <code>{server.inbound_id}</code>\n"
            f"Хост: <code>{esc(server.server_host)}</code>\n"
            f"Клиентов (активных): {count}/{server.max_clients}\nПротокол: {esc(server.protocol)}",
            admin_server_card_kb(server.id, is_active=server.is_active),
        )
    await cb.answer("Сохранено")


@router.callback_query(AdminCB.filter((F.section == "servers") & (F.action == "inbound")))
@admin_guard
async def cb_server_inbound_start(cb: CallbackQuery, callback_data: AdminCB, state: FSMContext) -> None:
    await state.set_state(AdminFSM.server_inbound)
    await state.update_data(inbound_server_id=callback_data.target_id)
    _track(cb)
    if cb.message is not None:
        await safe_edit(
            cb.message,
            "🔢 Пришлите новый <b>inbound id</b> числом.\n\n⚠️ Все <b>активные</b> клиенты будут перенесены в новый "
            "inbound с новой конфигурацией, а каждому уйдёт сообщение с новой VPN-ссылкой.",
        )
    await cb.answer()


@router.message(AdminFSM.server_inbound, Command("cancel"))
async def on_server_inbound_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено. Вернитесь в раздел 🖥 Серверы.", reply_markup=admin_menu())


@router.message(AdminFSM.server_inbound)
async def on_server_inbound(message: Message, state: FSMContext, session: AsyncSession) -> None:
    data = await state.get_data()
    raw = (message.text or "").strip().lstrip("/")
    if not raw.isdigit():
        await message.answer("Нужен числовой inbound id. Попробуйте ещё раз или /cancel.")
        return
    new_inbound = int(raw)
    server = await ServerRepo(session).get(int(data.get("inbound_server_id", 0)))
    await state.clear()
    if server is None:
        await message.answer("Сервер не найден.")
        return

    msg = message.answer  # локально для краткости ниже
    # проверяем существование inbound в панели ДО изменения
    from app.services.xui.factory import get_client

    client = get_client(server)
    try:
        await client.get_inbound(new_inbound)
    except Exception as e:  # noqa: BLE001
        await msg(f"❌ Inbound {new_inbound} недоступен в панели: {esc(str(e)[:120])}")
        return

    old_inbound = server.inbound_id
    if new_inbound == old_inbound:
        await msg(f"Inbound уже {new_inbound} — менять нечего.")
        return

    subs = list((await session.scalars(
        select(Subscription)
        .where(Subscription.server_id == server.id, Subscription.status == SubStatus.ACTIVE)
        .order_by(Subscription.expires_at.asc())
    )).all())

    from app.services.provisioning import ProvisioningError, ProvisioningService

    service = ProvisioningService(session)
    ok: list[Subscription] = []
    failed: list[tuple[int, str]] = []
    for sub in subs:
        try:
            await service.move_to_inbound(sub, new_inbound)
            ok.append(sub)
        except ProvisioningError as e:
            failed.append((sub.id, str(e)[:80]))
        except Exception as e:  # noqa: BLE001
            failed.append((sub.id, str(e)[:80]))

    server.inbound_id = new_inbound
    session.add(server)
    await UserRepo(session).audit(
        message.from_user.id, "admin.change_inbound", "server", server.id,
        {"old": old_inbound, "new": new_inbound, "moved": len(ok), "failed": len(failed)},
    )
    await session.commit()

    summary = (
        f"✅ Inbound {old_inbound} → <b>{new_inbound}</b>\n"
        f"Перенесено подписок: <b>{len(ok)}/{len(subs)}</b>"
        + (f"\nОшибок: {len(failed)}:\n" + "\n".join(f"• #{sid}: {e}" for sid, e in failed[:5]) if failed else "")
    )
    admin_id = message.from_user.id
    tracked_msg = _admin_msg.get(admin_id)
    if tracked_msg:
        from app.handlers.common import safe_edit_id

        try:
            await safe_edit_id(message.bot, message.chat.id, tracked_msg, summary)
        except Exception:
            pass
    else:
        await msg(summary)

    # рассылка новых ссылок перенесённым клиентам
    sent = 0
    for sub in ok:
        try:
            url = await build_vless_url(sub)
            parts = [
                f"⚙️ Сервер <b>{sub.server.country_flag} {esc(sub.server.country_name)}</b> обновлён.\n",
                "<b>Новая ссылка для подключения</b> (старая перестала работать):\n",
                f"<code>{url}</code>",
            ]
            if sub.server.sub_url:
                from app.services.xui.links import build_subscription_link

                sub_url = build_subscription_link(sub.server.sub_url, sub.xui_sub_id)
                parts.append(f"\n\n🔗 Подписка (обновит конфиги автоматически):\n<code>{sub_url}</code>")
            if await notify_user(message.bot, sub.user.telegram_id, "".join(parts)):
                sent += 1
            await asyncio.sleep(0.05)
        except Exception:  # noqa: BLE001
            continue
    log.info("admin.inbound_migrated", server=server.code, moved=len(ok), notified=sent)


@router.message(StateFilter(None), F.text.regexp(r"^/?\d{5,}$"), F.from_user.filter(lambda u: str(u.id) in {str(i) for i in get_config().admin_ids}))
async def on_admin_search_by_id(message: Message, session: AsyncSession) -> None:
    await _open_user_card(message.from_user.id, message.bot, message.chat.id, session, message.text.strip())


@router.message(StateFilter(None), F.text.regexp(r"^@?\w{4,32}$"), F.from_user.filter(lambda u: str(u.id) in {str(i) for i in get_config().admin_ids}))
async def on_admin_search(message: Message, session: AsyncSession) -> None:
    text = message.text.strip()
    if text.startswith("/"):
        return
    await _open_user_card(message.from_user.id, message.bot, message.chat.id, session, text)


async def _open_user_card(admin_id: int, bot, chat_id: int, session: AsyncSession, query: str) -> None:
    users = await UserRepo(session).search(query)
    if not users:
        msg_id = _admin_msg.get(admin_id)
        if msg_id:
            try:
                await bot.edit_message_text(
                    "Пользователь не найден.", chat_id=chat_id, message_id=msg_id,
                    reply_markup=admin_users_list([], {}),
                )
            except Exception:
                pass
        return
    user = users[0]
    subs = await SubRepo(session).list_user(user.id)
    payments = (await session.execute(
        select(Payment).where(Payment.user_id == user.id).order_by(Payment.created_at.desc()).limit(5)
    )).scalars().all()
    ref_count = await UserRepo(session).referral_count(user.id)

    sub_lines = [
        f"  • #{s.id} {s.plan.title} до {fmt_dt(s.expires_at, get_config().tz)} [{s.status.value}]" for s in subs[:5]
    ] or ["  —"]
    pay_lines = [f"  • №{p.id} {format_price(p.amount_kopeks)} [{p.provider.value}] {p.status.value}" for p in payments] or ["  —"]

    text = (
        f"👤 <b>{esc(user.display_name())}</b>\n"
        f"ID: <code>{user.telegram_id}</code> · регистрация: {fmt_dt(user.created_at, get_config().tz)}\n"
        f"Триал: {'использован' if user.trial_used else 'доступен'} · бан: {'да' if user.is_banned else 'нет'}\n\n"
        f"<b>Подписки:</b>\n" + "\n".join(sub_lines) +
        "\n\n<b>Последние платежи:</b>\n" + "\n".join(pay_lines) +
        f"\n\n👥 Рефералов: {ref_count}"
    )
    msg_id = _admin_msg.get(admin_id)
    if msg_id:
        try:
            await bot.edit_message_text(
                text, chat_id=chat_id, message_id=msg_id,
                reply_markup=admin_user_card(user.telegram_id),
            )
            _admin_msg[admin_id] = msg_id
            return
        except Exception:
            pass
    await bot.send_message(chat_id, text, reply_markup=admin_user_card(user.telegram_id))


@router.callback_query(AdminCB.filter((F.action == "ban_toggle") & (F.section == "users")))
@admin_guard
async def cb_ban_toggle(cb: CallbackQuery, callback_data: AdminCB, session: AsyncSession) -> None:
    user = await UserRepo(session).get_by_telegram_id(callback_data.target_id)
    if user is None:
        await cb.answer("Не найден", show_alert=True)
        return
    user.is_banned = not user.is_banned
    session.add(user)
    await UserRepo(session).audit(cb.from_user.id, "admin.ban_toggle", "user", user.id, {"banned": user.is_banned})
    await _open_user_card(cb.from_user.id, cb.bot, cb.message.chat.id, session, str(callback_data.target_id))
    await cb.answer(f"Бан: {user.is_banned}", show_alert=True)


@router.callback_query(AdminCB.filter((F.action == "reset_trial") & (F.section == "users")))
@admin_guard
async def cb_reset_trial(cb: CallbackQuery, callback_data: AdminCB, session: AsyncSession) -> None:
    user = await UserRepo(session).get_by_telegram_id(callback_data.target_id)
    if user is None:
        await cb.answer("Не найден", show_alert=True)
        return
    user.trial_used = False
    session.add(user)
    await UserRepo(session).audit(cb.from_user.id, "admin.reset_trial", "user", user.id)
    await _open_user_card(cb.from_user.id, cb.bot, cb.message.chat.id, session, str(callback_data.target_id))
    await cb.answer("Триал сброшен", show_alert=True)


@router.callback_query(AdminCB.filter((F.action == "grant") & (F.section == "users")))
@admin_guard
async def cb_grant(cb: CallbackQuery, callback_data: AdminCB, state: FSMContext) -> None:
    await state.set_state(AdminFSM.grant_days)
    await state.update_data(grant_target=callback_data.target_id)
    _track(cb)
    if cb.message is not None:
        await safe_edit(cb.message, "Введите число дней для выдачи подписки:")
    await cb.answer()


@router.callback_query(AdminCB.filter((F.action == "extend") & (F.section == "users")))
@admin_guard
async def cb_extend(cb: CallbackQuery, callback_data: AdminCB, state: FSMContext) -> None:
    await state.set_state(AdminFSM.grant_days)
    await state.update_data(grant_target=callback_data.target_id, extend=True)
    _track(cb)
    if cb.message is not None:
        await safe_edit(cb.message, "Введите число дней продления:")
    await cb.answer()


# ---------- поиск пользователей и карточка ----------

@router.message(AdminFSM.grant_days)
async def on_grant_days(message: Message, state: FSMContext, session: AsyncSession) -> None:
    data = await state.get_data()
    target_tg = int(data["grant_target"])
    try:
        days = int(message.text.strip())
    except ValueError:
        await message.answer("Введите число дней:")
        return
    await state.clear()
    user = await UserRepo(session).get_by_telegram_id(target_tg)
    server = await ServerRepo(session).pick_best()
    sub_repo = SubRepo(session)
    subs = await sub_repo.list_user(user.id) if user else []
    if user is None or server is None:
        await message.answer("Пользователь или сервер не найден")
        return
    from app.db.repositories.plans import PlanRepo as PR
    from app.services.provisioning import ProvisioningService

    service = ProvisioningService(session)
    existing = next((s for s in subs if s.status != SubStatus.DELETED), None)
    if existing is not None:
        await service.renew(existing, days)
    else:
        plan = await PR(session).get_trial_plan() or await PR(session).get_by_code("1m")
        if plan is None:
            plan = await PR(session).upsert(code="custom", title=f"Выдача {days}д", duration_days=days, price_kopeks=0, price_stars=0, device_limit=3)
        await service.grant_access(user, plan, server, None)
    await UserRepo(session).audit(message.from_user.id, "admin.grant", "user", user.id, {"days": days})
    await _open_user_card(message.from_user.id, message.bot, message.chat.id, session, str(target_tg))


@router.message(F.text.regexp(r"^/newpromo\s+(\S+)\s+(percent|fixed|days)\s+(-?\d+)(?:\s+(\d+))?$"))
async def cmd_new_promo(message: Message, session: AsyncSession) -> None:
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    code, type_, value = parts[1], parts[2], int(parts[3])
    max_uses = int(parts[4]) if len(parts) > 4 else 0
    promo = await PromoRepo(session).create(code=code, type_=PromoType(type_), value=value, max_uses=max_uses)
    await message.answer(f"🎟 Промокод <code>{html.escape(promo.code)}</code> создан.")


@router.message(F.text.regexp(r"^/broadcast\s+(\w+)$"))
async def cmd_broadcast(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    segment = message.text.split()[1]
    if segment not in {"all", "active", "expired", "no_purchase"}:
        await message.answer("Сегмент: all / active / expired / no_purchase")
        return
    await state.set_state(AdminFSM.broadcast_text)
    await state.update_data(broadcast_segment=segment)
    await message.answer(f"📣 Пришлите текст рассылки для сегмента <b>{segment}</b>\n(можно приложить фото)")


@router.message(AdminFSM.broadcast_text)
async def do_broadcast(message: Message, state: FSMContext, session: AsyncSession) -> None:
    data = await state.get_data()
    segment = data.get("broadcast_segment", "all")
    await state.clear()
    targets = await UserRepo(session).broadcast_targets(segment)
    status = await message.answer(f"Рассылка запущена для {len(targets)} получателей…")

    sent = failed = 0

    async def send_one(bot, tg_id: int) -> bool:
        nonlocal sent, failed
        try:
            await bot.copy_message(chat_id=tg_id, from_chat_id=message.chat.id, message_id=message.message_id)
            sent += 1
            return True
        except Exception:
            failed += 1
            return False

    from app.services.notifications import broadcast_send

    ok, bad = await broadcast_send(message.bot, targets, send_one)
    await status.edit_text(f"📣 Рассылка завершена: ✅ {ok} / ❌ {bad}")


# ---------- экспорт CSV ----------

@router.message(F.text.regexp(r"^/export_payments$"))
async def cmd_export_payments(message: Message, session: AsyncSession) -> None:
    if not is_admin(message.from_user.id):
        return
    rows = (await session.execute(
        select(Payment).order_by(Payment.created_at.desc()).limit(5000)
    )).scalars().all()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["id", "telegram_id", "provider", "amount_rub", "status", "external_id", "created_at", "paid_at"])
    for p in rows:
        writer.writerow([
            p.id, p.user.telegram_id, p.provider.value, p.amount_kopeks / 100, p.status.value,
            p.external_id or "", p.created_at.isoformat(),
            p.paid_at.isoformat() if p.paid_at else "",
        ])
    buf.seek(0)
    await message.answer_document(BufferedInputFile(buf.getvalue().encode(), filename="payments.csv"))


# ---------- алерт при исключениях хендлеров ----------

async def alert_exception(bot, update, error: Exception) -> None:
    import traceback as tb_mod

    tb = tb_mod.format_exc()[-1200:]
    log.error("handler.error", update_id=getattr(update, "update_id", None), err=str(error))
    try:
        uid = getattr(getattr(update, "message", None), "from_user", None) or getattr(getattr(update, "callback_query", None), "from_user", None)
        who = f"от <code>{uid.id}</code>" if uid else ""
        await notify_admins(bot, f"🔥 Ошибка хендлера {who}:\n<pre>{html.escape(tb[-800:])}</pre>")
    except Exception:  # noqa: BLE001
        pass
