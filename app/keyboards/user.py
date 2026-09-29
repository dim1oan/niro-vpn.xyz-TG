"""Инлайн-клавиатуры пользовательской части."""

from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.db.models import PaymentProvider, Plan
from app.keyboards.callbacks import BuyCB, KeysCB, MenuCB, SiteCB, TrialCB
from app.services.promo import format_price


def main_menu(*, trial_used: bool, has_trial_enabled: bool = True, site_enabled: bool = False) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔑 Мои ключи", callback_data=MenuCB(action="keys"))
    kb.button(text="💳 Купить VPN", callback_data=MenuCB(action="buy"))
    if has_trial_enabled and not trial_used:
        kb.button(text="🎁 Пробные 3 дня", callback_data=TrialCB())
    kb.button(text="👤 Профиль", callback_data=MenuCB(action="profile"))
    kb.button(text="📖 Инструкция", callback_data=MenuCB(action="guides"))
    kb.button(text="👥 Партнёрам", callback_data=MenuCB(action="referral"))
    if site_enabled:
        kb.button(text="🌐 Кабинет на сайте", callback_data=SiteCB(action="info"))
    kb.button(text="🆘 Поддержка", callback_data=MenuCB(action="support"))
    rows = [2, 2, 2] + ([1] if site_enabled else []) + [1]
    kb.adjust(*rows)
    return kb.as_markup()


def back_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ В меню", callback_data=MenuCB(action="back"))
    return kb.as_markup()


def to_menu_kb() -> InlineKeyboardMarkup:
    """Одна кнопка возврата в главное меню — для событий без панели (QR, чек, поддержка)."""
    kb = InlineKeyboardBuilder()
    kb.button(text="🏠 Главное меню", callback_data=MenuCB(action="back"))
    return kb.as_markup()


def check_channel(channel_link: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if channel_link.startswith("http"):
        kb.button(text="📢 Подписаться", url=channel_link)
    kb.button(text="✅ Я подписался", callback_data=MenuCB(action="check_sub"))
    return kb.as_markup()


def countries_keyboard(countries: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    """countries: (label, server_code) — в callback идёт именно КОД сервера."""
    kb = InlineKeyboardBuilder()
    for label, code in countries:
        kb.button(
            text=label,
            callback_data=BuyCB(step="country", country=code),
        )
    kb.button(text="⬅️ В меню", callback_data=MenuCB(action="back"))
    kb.adjust(1)
    return kb.as_markup()


def plans_keyboard(plans: list[Plan], country_code: str, *, back_to_menu: bool = False) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for p in plans:
        stars = f" / {p.price_stars}⭐" if p.price_stars else ""
        kb.button(
            text=f"{p.title} — {p.price_rub:.0f} ₽{stars}",
            callback_data=BuyCB(step="plan", country=country_code, plan_id=p.id),
        )
    if back_to_menu:
        kb.button(text="⬅️ В меню", callback_data=MenuCB(action="back"))
    else:
        kb.button(text="⬅️ Назад к странам", callback_data=BuyCB(step="start"))
    kb.adjust(1)
    return kb.as_markup()


def confirm_purchase_kb(plan_id: int, country: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🎟 Промокод", callback_data=BuyCB(step="promo", plan_id=plan_id, country=country))
    kb.button(text="➡️ Далее", callback_data=BuyCB(step="confirm", plan_id=plan_id, country=country))
    kb.button(text="⬅️ Назад к тарифам", callback_data=BuyCB(step="plans", country=country))
    kb.adjust(1)
    return kb.as_markup()


def payment_methods_kb(
    plan_id: int,
    country: str,
    *,
    stars_enabled: bool = True,
    manual_enabled: bool = True,
    yookassa_enabled: bool = True,
    sbp_enabled: bool = False,
    balance_enabled: bool = True,
    balance_kopeks: int = 0,
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if yookassa_enabled:
        kb.button(text="💳 Картой (ЮKassa)", callback_data=BuyCB(step="pay", plan_id=plan_id, country=country, provider=PaymentProvider.YOOKASSA))
    if yookassa_enabled and sbp_enabled:
        kb.button(text="⚡ СБП — из приложения банка", callback_data=BuyCB(step="pay", plan_id=plan_id, country=country, provider=PaymentProvider.YOOKASSA_SBP))
    if stars_enabled:
        kb.button(text="⭐ Telegram Stars", callback_data=BuyCB(step="pay", plan_id=plan_id, country=country, provider=PaymentProvider.STARS))
    if manual_enabled:
        kb.button(text="🏦 Оплата вручную", callback_data=BuyCB(step="pay", plan_id=plan_id, country=country, provider=PaymentProvider.MANUAL))
    if balance_enabled:
        kb.button(text=f"💰 С баланса — {format_price(balance_kopeks)}", callback_data=BuyCB(step="pay", plan_id=plan_id, country=country, provider=PaymentProvider.BALANCE))
    kb.button(text="⬅️ Назад к тарифам", callback_data=BuyCB(step="plans", country=country))
    kb.adjust(1)
    return kb.as_markup()


def key_card_kb(sub_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔗 Показать ссылку", callback_data=KeysCB(action="link", sub_id=sub_id))
    kb.button(text="📷 QR-код", callback_data=KeysCB(action="qr", sub_id=sub_id))
    kb.button(text="➕ Продлить", callback_data=KeysCB(action="renew", sub_id=sub_id))
    kb.button(text="🔄 Перевыпустить UUID", callback_data=KeysCB(action="reissue", sub_id=sub_id))
    kb.button(text="♻️ Сбросить трафик", callback_data=KeysCB(action="reset_traffic", sub_id=sub_id))
    kb.button(text="📖 Инструкция", callback_data=KeysCB(action="guides", sub_id=sub_id))
    kb.button(text="⬅️ К ключам", callback_data=KeysCB(action="list"))
    kb.adjust(2, 2, 2, 1)
    return kb.as_markup()


def renew_plans_kb(plans: list[Plan], sub_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for p in plans:
        kb.button(
            text=f"{p.title} (+{p.duration_days} дн)",
            callback_data=KeysCB(action="renew_do", sub_id=sub_id, plan_id=p.id),
        )
    kb.button(text="⬅️ Назад", callback_data=KeysCB(action="card", sub_id=sub_id))
    kb.adjust(1)
    return kb.as_markup()


def renew_pay_kb(
    sub_id: int,
    plan_id: int,
    *,
    stars_enabled: bool = True,
    manual_enabled: bool = True,
    yookassa_enabled: bool = True,
    sbp_enabled: bool = False,
    balance_enabled: bool = True,
    balance_kopeks: int = 0,
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if yookassa_enabled:
        kb.button(text="💳 Картой (ЮKassa)", callback_data=KeysCB(action="renew_pay", sub_id=sub_id, plan_id=plan_id, provider=PaymentProvider.YOOKASSA))
    if yookassa_enabled and sbp_enabled:
        kb.button(text="⚡ СБП — из приложения банка", callback_data=KeysCB(action="renew_pay", sub_id=sub_id, plan_id=plan_id, provider=PaymentProvider.YOOKASSA_SBP))
    if stars_enabled:
        kb.button(text="⭐ Telegram Stars", callback_data=KeysCB(action="renew_pay", sub_id=sub_id, plan_id=plan_id, provider=PaymentProvider.STARS))
    if manual_enabled:
        kb.button(text="🏦 Оплата вручную", callback_data=KeysCB(action="renew_pay", sub_id=sub_id, plan_id=plan_id, provider=PaymentProvider.MANUAL))
    if balance_enabled:
        kb.button(text=f"💰 С баланса — {format_price(balance_kopeks)}", callback_data=KeysCB(action="renew_pay", sub_id=sub_id, plan_id=plan_id, provider=PaymentProvider.BALANCE))
    kb.button(text="⬅️ Назад", callback_data=KeysCB(action="renew", sub_id=sub_id))
    kb.adjust(1)
    return kb.as_markup()


def success_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📖 Инструкция", callback_data=MenuCB(action="guides"))
    kb.button(text="🔑 Мои ключи", callback_data=MenuCB(action="keys"))
    kb.button(text="🏠 Меню", callback_data=MenuCB(action="back"))
    kb.adjust(2, 1)
    return kb.as_markup()


def server_status_kb(server_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="ℹ️ Обновить", callback_data=MenuCB(action="keys"))
    return kb.as_markup()
