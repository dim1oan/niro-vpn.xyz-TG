"""Профиль пользователя."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_config
from app.db.repositories.subs import SubRepo, payments_of
from app.db.repositories.users import UserRepo
from app.handlers.common import safe_edit
from app.keyboards.callbacks import MenuCB
from app.keyboards.user import back_menu
from app.locales.ru import t
from app.services.promo import format_price
from app.utils.time import fmt_dt

router = Router(name="profile")


@router.callback_query(MenuCB.filter(F.action == "profile"))
async def cb_profile(cb: CallbackQuery, session: AsyncSession, user) -> None:
    cfg = get_config()
    subs = await SubRepo(session).list_user(user.id)
    active_subs = sum(1 for s in subs if s.status.value == "active" and s.expires_at is not None)
    referrals = await UserRepo(session).referral_count(user.id)
    payments = await payments_of(session, user.id, limit=10)

    if payments:
        payment_lines = "\n".join(
            f"• {fmt_dt(p.created_at, cfg.tz)} — {format_price(p.amount_kopeks)} [{p.provider.value}] {p.status.value}"
            for p in payments
        )
    else:
        payment_lines = t("no_payments")

    text = t(
        "profile",
        telegram_id=user.telegram_id,
        balance=format_price(user.balance_kopeks),
        registered=fmt_dt(user.created_at, cfg.tz),
        subs=active_subs,
        referrals=referrals,
        payments=payment_lines,
    )
    if cb.message is not None:
        await safe_edit(cb.message, text, back_menu())
    await cb.answer()


@router.callback_query(MenuCB.filter(F.action == "referral"))
async def cb_referral_info(cb: CallbackQuery, session: AsyncSession, user) -> None:
    from app.handlers.referral import referral_text

    if cb.message is not None:
        await safe_edit(cb.message, await referral_text(cb.bot, session, user), back_menu(), disable_web_page_preview=True)
    await cb.answer()
