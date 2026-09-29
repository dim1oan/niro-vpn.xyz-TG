"""Время: всё в UTC (timezone-aware), показ пользователю — в таймзоне из конфига."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from zoneinfo import ZoneInfo


def utcnow() -> datetime:
    return datetime.now(UTC)


def to_ms(dt: datetime) -> int:
    """Unix epoch в миллисекундах для expiryTime панели 3x-ui."""
    return int(dt.timestamp() * 1000)


def from_ms(ms: int | float | None) -> datetime | None:
    if ms in (None, 0):
        return None
    return datetime.fromtimestamp(int(ms) / 1000, tz=UTC)


def fmt_dt(dt: datetime | None, tz: ZoneInfo) -> str:
    if dt is None:
        return "—"
    local = dt.astimezone(tz)
    return local.strftime("%d.%m.%Y %H:%M")


def days_left(expires_at: datetime, now: datetime | None = None) -> int:
    now = now or utcnow()
    delta: timedelta = expires_at - now
    seconds = delta.total_seconds()
    if seconds <= 0:
        return 0
    return max(1, int(seconds // 86400) + (1 if seconds % 86400 else 0))


def human_days_left(expires_at: datetime, now: datetime | None = None) -> str:
    n = days_left(expires_at, now)
    if n == 1:
        return "остался 1 день"
    if 2 <= n <= 4:
        return f"осталось {n} дня"
    return f"осталось {n} дней"


def plural_ru(n: int, one: str, few: str, many: str) -> str:
    n = abs(n) % 100
    if 11 <= n <= 14:
        return many
    n %= 10
    if n == 1:
        return one
    if 2 <= n <= 4:
        return few
    return many


def human_bytes(num_bytes: int) -> str:
    value = float(num_bytes)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if value < 1024:
            return f"{value:.1f} {unit}" if unit != "Б" else f"{int(value)} {unit}"
        value /= 1024
    return f"{value:.1f} ПБ"
