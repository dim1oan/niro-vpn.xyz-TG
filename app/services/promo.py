"""Применение промокодов к цене из БД."""

from __future__ import annotations

from dataclasses import dataclass

from app.db.models import PromoCode, PromoType
from app.db.repositories.payments import PromoRepo


@dataclass(frozen=True)
class PromoResult:
    ok: bool
    discount_kopeks: int = 0
    bonus_days: int = 0
    reason: str = ""


def compute_discount(promo: PromoCode, price_kopeks: int) -> int:
    if promo.type == PromoType.PERCENT:
        return max(0, min(price_kopeks * int(promo.value) // 100, price_kopeks))
    if promo.type == PromoType.FIXED:
        return max(0, min(int(promo.value), price_kopeks))
    return 0


async def apply_promo(repo: PromoRepo, code: str | None, price_kopeks: int) -> tuple[PromoCode | None, PromoResult]:
    """Проверяет валидность промокода и считает скидку от прайса БД."""
    if not code or code.strip() in {"-", ""}:
        return None, PromoResult(ok=True)
    promo = await repo.get_by_code(code.strip())
    now_valid = (
        promo is not None
        and promo.is_active
        and (promo.max_uses == 0 or promo.used_count < promo.max_uses)
    )
    from app.utils.time import utcnow

    if promo is not None and promo.expires_at is not None and promo.expires_at <= utcnow():
        now_valid = False

    if not now_valid or promo is None:
        return None, PromoResult(ok=False, reason="invalid")

    if promo.type == PromoType.DAYS:
        return promo, PromoResult(ok=True, bonus_days=int(promo.value))
    discount = compute_discount(promo, price_kopeks)
    if discount <= 0:
        return None, PromoResult(ok=False, reason="no_discount")
    return promo, PromoResult(ok=True, discount_kopeks=discount)


def format_price(kopeks: int) -> str:
    rub = kopeks / 100
    return f"{rub:.0f} ₽" if rub == int(rub) else f"{rub:.2f} ₽"
