"""Базовые контракты платёжных провайдеров."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.db.models import Payment, Plan


@dataclass(frozen=True)
class CreatePaymentResult:
    payment: Payment
    confirmation_url: str | None = None
    extra: dict = field(default_factory=dict)


class ProviderError(Exception):
    pass


def payment_payload(payment_id: int) -> str:
    """payload для invoice/метаданных — всегда содержит payment_id из БД."""
    return f"pay:{payment_id}"


def parse_payload(payload: str) -> int | None:
    if not payload.startswith("pay:"):
        return None
    try:
        return int(payload.split(":", 1)[1])
    except ValueError:
        return None


def assert_plan_price(plan: Plan, amount_kopeks: int) -> bool:
    """Сумма и тариф сверяются с БД — клиентский ввод не доверенный."""
    return amount_kopeks == plan.price_kopeks
