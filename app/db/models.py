"""Модели данных. Все datetime — timezone-aware UTC."""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, TZDateTime, utcnow


class SubStatus(enum.StrEnum):
    ACTIVE = "active"
    EXPIRED = "expired"
    DISABLED = "disabled"
    DELETED = "deleted"


class PaymentProvider(enum.StrEnum):
    YOOKASSA = "yookassa"
    YOOKASSA_SBP = "yookassa_sbp"
    STARS = "stars"
    MANUAL = "manual"
    BALANCE = "balance"


class PaymentStatus(enum.StrEnum):
    PENDING = "pending"
    PAID = "paid"
    FAILED = "failed"
    REFUNDED = "refunded"
    CANCELED = "canceled"


class PromoType(enum.StrEnum):
    PERCENT = "percent"
    FIXED = "fixed"
    DAYS = "days"


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    # NULL => чисто веб-аккаунт (создан на сайте, миграция web_accounts)
    telegram_id: Mapped[int | None] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str | None] = mapped_column(String(128))
    email: Mapped[str | None] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str | None] = mapped_column(String(255))
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_site_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    language_code: Mapped[str | None] = mapped_column(String(8), default="ru")
    referrer_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    balance_kopeks: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    trial_used: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(TZDateTime())

    referrer: Mapped[User | None] = relationship(remote_side="User.id", lazy="selectin")
    subscriptions: Mapped[list[Subscription]] = relationship(back_populates="user")
    payments: Mapped[list[Payment]] = relationship(back_populates="user")

    def display_name(self) -> str:
        if self.first_name:
            return self.first_name
        if self.username:
            return self.username
        if self.email:
            return self.email
        return str(self.telegram_id) if self.telegram_id else f"web-{self.id}"


class Server(Base, TimestampMixin):
    __tablename__ = "servers"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    country_name: Mapped[str] = mapped_column(String(64))
    country_flag: Mapped[str] = mapped_column(String(16), default="")
    xui_base_url: Mapped[str] = mapped_column(Text)
    xui_username_enc: Mapped[str] = mapped_column(Text)
    xui_password_enc: Mapped[str] = mapped_column(Text)
    server_host: Mapped[str] = mapped_column(String(128))
    inbound_id: Mapped[int] = mapped_column(Integer)
    sub_url: Mapped[str | None] = mapped_column(Text)
    protocol: Mapped[str] = mapped_column(String(32), default="vless-reality")
    max_clients: Mapped[int] = mapped_column(Integer, default=300)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=100)


class Plan(Base, TimestampMixin):
    __tablename__ = "plans"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    title: Mapped[str] = mapped_column(String(128))
    duration_days: Mapped[int] = mapped_column(Integer)
    price_kopeks: Mapped[int] = mapped_column(Integer)
    price_stars: Mapped[int] = mapped_column(Integer, default=0)
    device_limit: Mapped[int] = mapped_column(Integer, default=3)
    is_trial: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=100)

    @property
    def price_rub(self) -> float:
        return self.price_kopeks / 100


class Subscription(Base, TimestampMixin):
    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    server_id: Mapped[int] = mapped_column(ForeignKey("servers.id"))
    plan_id: Mapped[int] = mapped_column(ForeignKey("plans.id"))

    xui_client_uuid: Mapped[str] = mapped_column(String(64))
    xui_email: Mapped[str] = mapped_column(String(255), unique=True)
    xui_sub_id: Mapped[str] = mapped_column(String(64))
    xui_inbound_id: Mapped[int] = mapped_column(Integer)

    status: Mapped[SubStatus] = mapped_column(
        SAEnum(SubStatus, native_enum=False, length=16),
        default=SubStatus.ACTIVE,
        index=True,
    )
    started_at: Mapped[datetime] = mapped_column(TZDateTime(), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(TZDateTime())
    traffic_used_bytes: Mapped[int] = mapped_column(Integer, default=0)
    last_synced_at: Mapped[datetime | None] = mapped_column(TZDateTime())

    notified_3d: Mapped[bool] = mapped_column(Boolean, default=False)
    notified_1d: Mapped[bool] = mapped_column(Boolean, default=False)
    notified_expired: Mapped[bool] = mapped_column(Boolean, default=False)
    last_reissue_at: Mapped[datetime | None] = mapped_column(TZDateTime())

    user: Mapped[User] = relationship(back_populates="subscriptions", lazy="selectin")
    server: Mapped[Server] = relationship(lazy="selectin")
    plan: Mapped[Plan] = relationship(lazy="selectin")


class Payment(Base, TimestampMixin):
    __tablename__ = "payments"
    __table_args__ = (UniqueConstraint("external_id", name="uq_payments_external_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("plans.id"))
    subscription_id: Mapped[int | None] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="SET NULL")
    )

    provider: Mapped[PaymentProvider] = mapped_column(SAEnum(PaymentProvider, native_enum=False, length=16))
    amount_kopeks: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(8), default="RUB")
    external_id: Mapped[str | None] = mapped_column(String(255), index=True)
    status: Mapped[PaymentStatus] = mapped_column(
        SAEnum(PaymentStatus, native_enum=False, length=16),
        default=PaymentStatus.PENDING,
        index=True,
    )
    receipt_file_id: Mapped[str | None] = mapped_column(String(255))
    admin_id: Mapped[int | None] = mapped_column(BigInteger)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    promo_code_id: Mapped[int | None] = mapped_column(ForeignKey("promo_codes.id", ondelete="SET NULL"))
    discount_kopeks: Mapped[int] = mapped_column(Integer, default=0)
    paid_at: Mapped[datetime | None] = mapped_column(TZDateTime())
    provision_failed: Mapped[bool] = mapped_column(Boolean, default=False)
    provision_attempts: Mapped[int] = mapped_column(Integer, default=0)

    user: Mapped[User] = relationship(back_populates="payments", lazy="selectin")
    plan: Mapped[Plan] = relationship(lazy="selectin")


class PromoCode(Base, TimestampMixin):
    __tablename__ = "promo_codes"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True)
    type: Mapped[PromoType] = mapped_column(SAEnum(PromoType, native_enum=False, length=16))
    value: Mapped[int] = mapped_column(Integer)  # percent / kopeks / days
    max_uses: Mapped[int] = mapped_column(Integer, default=0)  # 0 = безлимит
    used_count: Mapped[int] = mapped_column(Integer, default=0)
    expires_at: Mapped[datetime | None] = mapped_column(TZDateTime())
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class PromoUse(Base, TimestampMixin):
    __tablename__ = "promo_uses"

    id: Mapped[int] = mapped_column(primary_key=True)
    promo_id: Mapped[int] = mapped_column(ForeignKey("promo_codes.id", ondelete="CASCADE"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    payment_id: Mapped[int] = mapped_column(ForeignKey("payments.id", ondelete="CASCADE"))


class Referral(Base, TimestampMixin):
    __tablename__ = "referrals"
    __table_args__ = (UniqueConstraint("referee_id", name="uq_referrals_referee"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    referrer_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    referee_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    reward_kopeks: Mapped[int] = mapped_column(Integer, default=0)
    paid: Mapped[bool] = mapped_column(Boolean, default=False)


class AuditLog(Base, TimestampMixin):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    actor_telegram_id: Mapped[int | None]
    action: Mapped[str] = mapped_column(String(64))
    entity: Mapped[str | None] = mapped_column(String(64))
    entity_id: Mapped[int | None]
    payload: Mapped[dict] = mapped_column(JSON, default=dict)


class ActivationCode(Base, TimestampMixin):
    """Одноразовый код: существующий пользователь бота задаёт пароль на сайте."""

    __tablename__ = "activation_codes"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    expires_at: Mapped[datetime] = mapped_column(TZDateTime())
    used_at: Mapped[datetime | None] = mapped_column(TZDateTime())


class SupportTicket(Base, TimestampMixin):
    """Тикет поддержки с сайта: ответ админа юзер видит в ЛК."""

    __tablename__ = "support_tickets"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    subject: Mapped[str] = mapped_column(String(255))
    body: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(16), default="support", index=True)  # support/payout
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)  # open/closed
    admin_reply: Mapped[str | None] = mapped_column(Text)
    answered_at: Mapped[datetime | None] = mapped_column(TZDateTime())


class PayoutRequest(Base, TimestampMixin):
    """Заявка на выплату реферальных начислений (кабинет сайта)."""

    __tablename__ = "payout_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    amount_kopeks: Mapped[int] = mapped_column(Integer)
    contact: Mapped[str] = mapped_column(String(255))  # карта / кошелёк для выплаты
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)  # pending/approved/rejected
    admin_comment: Mapped[str | None] = mapped_column(Text)
    ticket_id: Mapped[int | None] = mapped_column(
        ForeignKey("support_tickets.id", ondelete="SET NULL")
    )
    processed_at: Mapped[datetime | None] = mapped_column(TZDateTime())


class EmailToken(Base, TimestampMixin):
    """Токен подтверждения email (регистрация/смена адреса на сайте)."""

    __tablename__ = "email_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    expires_at: Mapped[datetime] = mapped_column(TZDateTime())
    used_at: Mapped[datetime | None] = mapped_column(TZDateTime())
