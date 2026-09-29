"""Партнёрская программа."""

from __future__ import annotations

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_config
from app.db.models import User
from app.db.repositories.users import UserRepo
from app.locales.ru import t
from app.services.promo import format_price


async def referral_text(bot: Bot, session: AsyncSession, user: User) -> str:
    cfg = get_config()
    me = await bot.me()
    link = f"https://t.me/{me.username}?start=ref{user.telegram_id}"
    repo = UserRepo(session)
    count = await repo.referral_count(user.id)
    earned = sum(r.reward_kopeks for r in await repo.referrals_of(user.id))
    from app.services.promo import format_price as _fp

    reward_text = (
        _fp(cfg.referral_fixed_kopeks) if cfg.referral_fixed_kopeks > 0 else f"{cfg.referral_percent}%"
    )
    return t("referral_info", percent=cfg.referral_percent, reward=reward_text, link=link, count=count, earned=format_price(earned))
