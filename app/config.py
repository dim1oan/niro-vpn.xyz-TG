"""Конфигурация приложения на pydantic-settings. Все секреты — только через env."""

from __future__ import annotations

import functools
import secrets
from enum import StrEnum
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class PaymentProvider(StrEnum):
    YOOKASSA = "yookassa"
    YOOKASSA_SBP = "yookassa_sbp"
    STARS = "stars"
    MANUAL = "manual"
    BALANCE = "balance"


class Config(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Telegram
    bot_token: str = ""
    # Храним сырой строкой: для list[int] pydantic-settings применяет
    # JSON-декодер ещё до валидаторов и падает на "123,456" из .env.
    # Разбор — в property admin_ids ниже.
    admin_ids_raw: str = Field(default="", validation_alias="admin_ids")
    admin_chat_id: int | None = None
    required_channel_id: int | None = None
    brand_name: str = "Niro VPN"
    timezone: str = "Europe/Moscow"

    # Хранилище
    database_url: str = "sqlite+aiosqlite:///./data/bot.db"
    redis_url: str | None = None
    secret_key: str = ""

    # 3x-ui (seed первого сервера)
    xui_base_url: str = ""
    xui_username: str = ""
    xui_password: str = ""
    xui_inbound_id: int = 1
    xui_sub_url: str | None = None
    server_host: str = ""
    server_code: str = "fr-1"
    server_country: str = "Франция"
    server_flag: str = "🇫🇷"
    server_max_clients: int = 300

    # Платежи
    yookassa_shop_id: str = ""
    yookassa_secret_key: str = ""
    stars_enabled: bool = True
    manual_pay_enabled: bool = True
    manual_pay_details: str = ""

    # Логика
    trial_enabled: bool = True
    trial_min_account_age_days: int = 30
    referral_percent: int = 20
    referral_fixed_kopeks: int = 0  # фикс-вознаграждение за оплату приглашённого (0 = процентный режим)
    delete_expired_after_days: int = 14
    webhook_enabled: bool = False
    webhook_base_url: str | None = None
    web_port: int = 8080
    log_level: str = "INFO"

    # Веб-кабинет (ТЗ v2): личные коды + внутренний API для уведомлений с сайта
    site_base_url: str | None = ""   # https://домен сайта; пусто => фича кодов выключена
    site_receipts_dir: str = "/opt/vpn_site/data/receipts"  # чеки ручных оплат сайта
    internal_api_port: int = 8081    # aiohttp на 127.0.0.1: /internal/notify/*
    internal_api_token: str = ""     # hex 32+; пусто => внутренний API не стартует
    activation_code_days: int = 7    # TTL кода активации сайта

    @property
    def admin_ids(self) -> list[int]:
        """Telegram ID админов. Принимает «1,2», «1, 2» и «[1, 2]»."""
        raw = (self.admin_ids_raw or "").strip().strip("[]")
        return [int(x) for x in raw.replace(" ", "").split(",") if x]

    @field_validator(
        "admin_chat_id",
        "required_channel_id",
        "redis_url",
        "xui_sub_url",
        "webhook_base_url",
        "site_base_url",
        mode="before",
    )
    @classmethod
    def _empty_to_none(cls, v: object) -> object:
        """Пустая строка в .env для необязательного поля означает «не задано»."""
        if isinstance(v, str) and not v.strip():
            return None
        return v

    def ensure_runtime(self) -> None:
        if not self.bot_token:
            raise RuntimeError("BOT_TOKEN не задан")
        if not self.secret_key:
            raise RuntimeError("SECRET_KEY не задан (base64 32 байта для Fernet)")

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def bot_username_hint(self) -> str:
        return "bot"

    def generate_missing_secret(self) -> None:
        """Гарантирует валидный Fernet-ключ: пустой генерирует, произвольный деривирует."""
        import base64
        import hashlib

        from cryptography.fernet import Fernet

        if not self.secret_key:
            self.secret_key = Fernet.generate_key().decode()
        else:
            try:
                Fernet(self.secret_key.encode())
            except ValueError:
                digest = hashlib.sha256(self.secret_key.encode()).digest()
                self.secret_key = base64.urlsafe_b64encode(digest).decode()

    def admin_chat(self) -> int:
        """Куда слать алерты: ADMIN_CHAT_ID или первый админ."""
        return self.admin_chat_id or (self.admin_ids[0] if self.admin_ids else 0)


@functools.lru_cache
def get_config() -> Config:
    cfg = Config()
    cfg.generate_missing_secret()
    return cfg


def random_token(n: int = 16) -> str:
    return secrets.token_hex(n // 2)
