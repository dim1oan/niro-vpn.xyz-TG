"""Пробные 3 дня: проверки и выдача."""

from __future__ import annotations

from aiogram import Router
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_config
from app.db.repositories.plans import PlanRepo
from app.db.repositories.servers import ServerRepo
from app.db.repositories.subs import SubRepo
from app.db.repositories.users import account_age_days
from app.keyboards.callbacks import TrialCB
from app.locales.ru import t
from app.services.provisioning import ProvisioningError

router = Router(name="trial")


@router.callback_query(TrialCB.filter())
async def cb_grant_trial(cb: CallbackQuery, session: AsyncSession, user) -> None:
    cfg = get_config()
    if not cfg.trial_enabled:
        await cb.answer(t("trial_disabled"), show_alert=True)
        return
    if user.trial_used:
        await cb.answer(t("trial_already"), show_alert=True)
        return
    subs = SubRepo(session)
    if await subs.has_active(user.id):
        await cb.answer(t("has_active_sub"), show_alert=True)
        return
    # Telegram API не сообщает дату регистрации аккаунта; эвристика по ID ненадёжна.
    # Проверка активна только если порог явно задан (> 0).
    if cfg.trial_min_account_age_days > 0:
        age = account_age_days(user.telegram_id)
        if age < cfg.trial_min_account_age_days:
            await cb.answer(t("trial_too_new"), show_alert=True)
            return

    plan = await PlanRepo(session).get_trial_plan()
    server = await ServerRepo(session).pick_best()
    if plan is None or server is None:
        await cb.answer("Временно недоступно", show_alert=True)
        return

    from app.services.provisioning import ProvisioningService

    service = ProvisioningService(session)
    try:
        sub = await service.grant_access(user, plan, server, None)
    except ProvisioningError:
        await cb.answer("Ошибка выдачи, попробуйте позже", show_alert=True)
        return
    user.trial_used = True
    session.add(user)

    from app.handlers.buy import show_success

    assert cb.message is not None
    await show_success(cb.message, session, sub)
    await cb.answer("🎁 Готово!")
