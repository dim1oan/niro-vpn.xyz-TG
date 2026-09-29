"""Тесты: расчёт продления, идемпотентность выдачи, применение промокода."""

from __future__ import annotations

import uuid as uuid_lib
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    PaymentProvider,
    PaymentStatus,
    Plan,
    PromoType,
    Server,
    User,
)
from app.db.repositories.payments import PromoRepo
from app.db.repositories.subs import SubRepo, compute_renewal
from app.services.promo import apply_promo
from app.utils.time import to_ms


def make_server() -> Server:
    return Server(
        code="fr-test",
        country_name="Франция",
        country_flag="🇫🇷",
        xui_base_url="https://panel.test/x",
        xui_username_enc="enc",
        xui_password_enc="enc",
        server_host="185.0.0.9",
        inbound_id=1,
        max_clients=100,
    )


def make_user(tg: int = 42) -> User:
    return User(telegram_id=tg, first_name="Test")


@pytest.fixture
async def seeded(db_session) -> dict[str, Any]:
    session, factory = db_session
    server = make_server()
    user = make_user()
    plan = Plan(code="1m", title="1 месяц", duration_days=30, price_kopeks=19900, price_stars=150, device_limit=3)
    session.add_all([server, user, plan])
    await session.flush()
    await session.refresh(server)
    return {"session": session, "factory": factory, "server": server, "user": user, "plan": plan}


# ---------- compute_renewal ----------

def test_renewal_active_extends_from_expiry() -> None:
    now = datetime.now(UTC)
    expires = now + timedelta(days=10)
    new_exp, was_active = compute_renewal(expires, 30, now=now)
    assert was_active is True
    assert new_exp == expires + timedelta(days=30)


def test_renewal_expired_starts_from_now() -> None:
    now = datetime.now(UTC)
    expired_at = now - timedelta(days=5)
    new_exp, was_active = compute_renewal(expired_at, 90, now=now)
    assert was_active is False
    assert new_exp == now + timedelta(days=90)


# ---------- идемпотентность выдачи ----------

async def test_grant_access_idempotent(seeded, monkeypatch):
    """Повторный вызов grant_access по тому же payment не создаёт вторую подписку."""
    data = seeded
    session: AsyncSession = data["session"]
    from app.services.provisioning import ProvisioningService

    added_clients: list[str] = []

    async def fake_add(self, inbound_id, client):
        added_clients.append(client.email)

    monkeypatch.setattr("app.services.xui.client.XuiClient.add_client", fake_add)

    service = ProvisioningService(session)
    from app.db.models import Payment

    payment = Payment(
        user_id=data["user"].id,
        plan_id=data["plan"].id,
        provider=PaymentProvider.STARS,
        amount_kopeks=data["plan"].price_kopeks,
        status=PaymentStatus.PENDING,
        external_id="ext-1",
    )
    session.add(payment)
    await session.flush()

    sub1 = await service.grant_access(data["user"], data["plan"], data["server"], payment)
    sub2 = await service.grant_access(data["user"], data["plan"], data["server"], payment)

    assert sub1.id == sub2.id
    subs = await SubRepo(session).list_user(data["user"].id)
    assert len(subs) == 1
    assert len(added_clients) <= 2  # идемпотентный вызов мог пересоздать клиента в панели, но не запись


async def test_grant_access_panel_error_marks_provision_failed(seeded, monkeypatch):
    """Ошибка панели → payment.provision_failed=True, retry_failed восстанавливает."""
    data = seeded
    session: AsyncSession = data["session"]
    from app.db.models import Payment
    from app.services.provisioning import ProvisioningError, ProvisioningService

    call_count = {"n": 0}

    async def flaky_add(self, inbound_id, client):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("panel down")
        # вторая попытка успешна (retry job)

    async def ok_add(self, inbound_id, client):
        return None

    monkeypatch.setattr("app.services.xui.client.XuiClient.add_client", flaky_add)

    service = ProvisioningService(session)
    payment = Payment(
        user_id=data["user"].id,
        plan_id=data["plan"].id,
        provider=PaymentProvider.MANUAL,
        amount_kopeks=data["plan"].price_kopeks,
        status=PaymentStatus.PENDING,
        external_id="ext-2",
    )
    session.add(payment)
    await session.flush()

    with pytest.raises(ProvisioningError):
        await service.grant_access(data["user"], data["plan"], data["server"], payment)

    assert payment.provision_failed is True
    assert payment.status == PaymentStatus.PAID

    # retry job
    monkeypatch.setattr("app.services.xui.client.XuiClient.add_client", ok_add)
    sub = await service.retry_failed(payment)
    assert sub is not None
    assert payment.provision_failed is False


# ---------- промокоды ----------

async def test_promo_percent(db_session):
    session, _ = db_session
    repo = PromoRepo(session)
    promo = await repo.create(code="SAVE10", type_=PromoType.PERCENT, value=10)
    _, result = await apply_promo(repo, "save10", 19900)
    assert result.ok
    assert result.discount_kopeks == 1990
    assert promo.code == "SAVE10"


async def test_promo_fixed_capped_at_price(db_session):
    session, _ = db_session
    repo = PromoRepo(session)
    await repo.create(code="BIG", type_=PromoType.FIXED, value=999_00 * 100)  # больше цены
    _, result = await apply_promo(repo, "BIG", 19900)
    assert result.ok and result.discount_kopeks == 19900


async def test_promo_invalid_and_exhausted(db_session):
    session, _ = db_session
    repo = PromoRepo(session)
    _, result = await apply_promo(repo, "NOPE", 19900)
    assert result.ok is False

    promo = await repo.create(code="ONCE", type_=PromoType.PERCENT, value=5, max_uses=1)
    promo.used_count = 1
    session.add(promo)
    _, result2 = await apply_promo(repo, "ONCE", 19900)
    assert result2.ok is False

    from datetime import timedelta as _td

    from app.utils.time import utcnow as _u
    old = await repo.create(code="OLD", type_=PromoType.PERCENT, value=5)
    old.expires_at = _u() - _td(days=1)
    _, result3 = await apply_promo(repo, "OLD", 19900)
    assert result3.ok is False


# ---------- утилиты времени ----------

def test_to_ms_roundtrip():
    dt = datetime(2030, 1, 1, tzinfo=UTC)
    ms = to_ms(dt)
    assert ms == int(dt.timestamp() * 1000)


def test_uuid_email_subid_generation():
    from app.db.repositories.subs import gen_xui_email
    from app.services.provisioning import gen_sub_id, gen_uuid

    email = gen_xui_email(123456)
    assert email.startswith("tg123456-")
    uuid_lib.UUID(gen_uuid())  # валидный UUID4
    assert len(gen_sub_id()) == 16  # secrets.token_hex(8)
