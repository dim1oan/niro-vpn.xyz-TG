"""CallbackData фабрики — никаких сырых строк в callback_data.

Важно: опциональные строковые поля должны быть `str | None`,
иначе aiogram при распаковке падает на пустых сегментах ("" -> None).
"""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData


class MenuCB(CallbackData, prefix="menu"):
    action: str  # keys|buy|trial|profile|guides|referral|support|back|check_sub|guide
    slug: str | None = None  # для action="guide": ios|android|windows|macos|linux


class BuyCB(CallbackData, prefix="buy"):
    step: str                     # country|plan|confirm|pay|promo
    country: str | None = None    # server code
    plan_id: int = 0
    provider: str | None = None
    promo: str | None = None


class KeysCB(CallbackData, prefix="keys"):
    action: str   # list|card|link|qr|renew|renew_do|renew_pay|reissue|reset_traffic|guides
    sub_id: int = 0
    plan_id: int = 0
    provider: str | None = None


class TrialCB(CallbackData, prefix="trial"):
    pass


class AdminCB(CallbackData, prefix="adm"):
    section: str      # stats|users|subs|servers|plans|promo|broadcast|payments|logs
    action: str = ""
    target_id: int = 0
    extra: str | None = None


class SupportReplyCB(CallbackData, prefix="srep"):
    user_id: int


class ManualPayCB(CallbackData, prefix="mpay"):
    action: str  # approve|reject
    payment_id: int


class BroadcastCB(CallbackData, prefix="bc"):
    action: str  # segment|preview|confirm
    segment: str | None = None


class SiteCB(CallbackData, prefix="site"):
    action: str  # info|reissue
