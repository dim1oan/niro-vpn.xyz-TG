"""Инлайн-клавиатуры админ-панели."""

from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.keyboards.callbacks import AdminCB, ManualPayCB, SupportReplyCB


def admin_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for text, section in [
        ("📊 Статистика", "stats"),
        ("👤 Пользователи", "users"),
        ("🔑 Подписки", "subs"),
        ("🖥 Серверы", "servers"),
        ("💰 Тарифы", "plans"),
        ("🎟 Промокоды", "promo"),
        ("📣 Рассылка", "broadcast"),
        ("🧾 Платежи", "payments"),
        ("📜 Логи", "logs"),
    ]:
        kb.button(text=text, callback_data=AdminCB(section=section, action="menu"))
    kb.adjust(2)
    return kb.as_markup()


def admin_back(section: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ В админку", callback_data=AdminCB(section=section, action="home"))
    return kb.as_markup()


def admin_user_card(user_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🎁 Выдать подписку", callback_data=AdminCB(section="users", action="grant", target_id=user_id))
    kb.button(text="➕ Продлить +30д", callback_data=AdminCB(section="users", action="extend", target_id=user_id))
    kb.button(text="🚫 Забанить/разбанить", callback_data=AdminCB(section="users", action="ban_toggle", target_id=user_id))
    kb.button(text="♻️ Сбросить триал", callback_data=AdminCB(section="users", action="reset_trial", target_id=user_id))
    kb.button(text="⬅️ К списку", callback_data=AdminCB(section="users", action="list"))
    kb.button(text="🏠 В админку", callback_data=AdminCB(section="users", action="home"))
    kb.adjust(1)
    return kb.as_markup()


def admin_users_list(users: list, subs_by_user: dict[int, list]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for u in users:
        active = subs_by_user.get(u.id, [])
        name = u.display_name()
        if len(name) > 24:
            name = name[:24] + "…"
        label = f"{name} · {u.telegram_id if u.telegram_id else 'web'} · ✅{len(active)}"
        if len(label) > 60:
            label = label[:60]
        kb.button(text=label, callback_data=AdminCB(section="users", action="user", target_id=u.id))
    kb.button(text="🏠 В админку", callback_data=AdminCB(section="users", action="home"))
    kb.adjust(1)
    return kb.as_markup()


def admin_servers_list_kb(servers) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for s in servers:
        label = f"🖥 {s.country_flag} {s.code} · inbound {s.inbound_id}" + ("" if s.is_active else " · 🔴")
        kb.button(text=label[:60], callback_data=AdminCB(section="servers", action="srv", target_id=s.id))
    kb.button(text="🏠 В админку", callback_data=AdminCB(section="servers", action="home"))
    kb.adjust(1)
    return kb.as_markup()


def admin_server_card_kb(server_id: int, *, is_active: bool = True) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔢 Сменить inbound", callback_data=AdminCB(section="servers", action="inbound", target_id=server_id))
    kb.button(text="🔴 Отключить" if is_active else "🟢 Включить",
              callback_data=AdminCB(section="servers", action="toggle_active", target_id=server_id))
    kb.button(text="⬅️ К списку серверов", callback_data=AdminCB(section="servers", action="list"))
    kb.button(text="🏠 В админку", callback_data=AdminCB(section="servers", action="home"))
    kb.adjust(1)
    return kb.as_markup()


def manual_pay_review(payment_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Подтвердить", callback_data=ManualPayCB(action="approve", payment_id=payment_id))
    kb.button(text="❌ Отклонить", callback_data=ManualPayCB(action="reject", payment_id=payment_id))
    kb.adjust(2)
    return kb.as_markup()


def support_reply_kb(user_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="💬 Ответить", callback_data=SupportReplyCB(user_id=user_id))
    return kb.as_markup()
