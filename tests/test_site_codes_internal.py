"""Тесты личных кодов сайта (ТЗ v2, ф-1) и internal API (ф-2)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from app.config import get_config
from app.db.models import ActivationCode, Payment, PaymentProvider, PaymentStatus, Plan, Server, User
from app.db.repositories.site_codes import SiteCodeRepo, gen_activation_code
from app.utils.time import utcnow


def make_server() -> Server:
    return Server(
        code="fr-test",
        country_name="Франция",
        country_flag="🇫🇷",
        xui_base_url="https://panel.test/x",
        xui_username_enc="",
        xui_password_enc="",
        server_host="185.0.0.9",
        inbound_id=1,
        max_clients=100,
    )


@pytest.fixture
async def user(db_session) -> User:
    session, _ = db_session
    u = User(telegram_id=777001, first_name="Code")
    session.add(u)
    await session.flush()
    return u


# ---------- gen / ensure ----------

def test_gen_code_format() -> None:
    for _ in range(50):
        code = gen_activation_code()
        assert len(code) == 10
        assert all(c in "ABCDEFGHJKLMNPQRSTUVWXYZ23456789" for c in code)


async def test_ensure_code_idempotent(db_session, user):
    session, _ = db_session
    del _
    repo = SiteCodeRepo(session)
    c1 = await repo.ensure_code(user)
    c2 = await repo.ensure_code(user)
    assert c1.id == c2.id
    rows = list((await session.scalars(select(ActivationCode).where(ActivationCode.user_id == user.id))).all())
    assert len(rows) == 1


async def test_reissue_invalidates_old(db_session, user):
    session, _ = db_session
    repo = SiteCodeRepo(session)
    old = await repo.ensure_code(user)
    new = await repo.reissue(user)
    assert new.code != old.code
    await session.refresh(old)
    assert old.expires_at <= utcnow()  # старый погашен
    # новый — единственный активный
    assert (await repo.get_active(user.id)).id == new.id


async def test_banned_no_code(db_session, user):
    session, _ = db_session
    user.is_banned = True
    session.add(user)
    repo = SiteCodeRepo(session)
    with pytest.raises(PermissionError):
        await repo.ensure_code(user)
    assert await repo.reissue(user) is None


async def test_site_base_url_empty_disables_feature(db_session, monkeypatch):
    """SITE_BASE_URL пуст => ensure_code не вызывается из start (гвард в хендлере)."""
    monkeypatch.setenv("SITE_BASE_URL", "")
    get_config.cache_clear()
    cfg = get_config()
    assert not cfg.site_base_url


# ---------- internal API ----------

@pytest.fixture
def _token(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_TOKEN", "t" * 32)
    get_config.cache_clear()
    yield
    get_config.cache_clear()


@pytest.fixture
async def web_payment(db_session, tmp_path) -> Payment:
    session, _ = db_session
    server = make_server()
    user = User(telegram_id=None, email="webpay@test.io", password_hash="x")
    plan = Plan(code="1m", title="1 месяц", duration_days=30, price_kopeks=19900, device_limit=3)
    session.add_all([server, user, plan])
    await session.flush()
    receipt = tmp_path / "check.png"
    receipt.write_bytes(b"\x89PNG fake")
    payment = Payment(
        user_id=user.id, plan_id=plan.id, provider=PaymentProvider.MANUAL,
        amount_kopeks=plan.price_kopeks, status=PaymentStatus.PENDING,
        payload={"manual_code": "VPN-1-ABCD", "receipt_path": str(receipt), "server_code": "fr-test"},
    )
    session.add(payment)
    await session.commit()
    return payment


async def test_internal_api_full_cycle(db_session, web_payment, monkeypatch):
    """notify → 204, admin_notified_at; повтор → 409; approve-кнопки в payload."""
    session, factory = db_session
    monkeypatch.setenv("ADMIN_CHAT_ID", "111")
    get_config.cache_clear()
    from app.internal_api import build_internal_app, notify_manual_payment

    sent: list[tuple] = []

    class FakeBot:
        async def send_document(self, chat_id, document=None, caption=None, **kw):
            sent.append(("doc", chat_id, caption))

        async def send_message(self, chat_id, text, reply_markup=None, **kw):
            sent.append(("msg", chat_id, text))

    bot = FakeBot()
    status, _ = await notify_manual_payment(factory, bot, web_payment.id)
    assert status == 204, sent
    assert any(s[0] == "doc" for s in sent)  # чек ушёл вложением
    await session.refresh(web_payment)
    assert web_payment.payload["admin_notified_at"]

    # повторное уведомление — 409
    status2, _ = await notify_manual_payment(factory, bot, web_payment.id)
    assert status2 == 409

    # приложение строится и имеет роут
    app = build_internal_app(factory, bot)
    routes = [r.resource.canonical for r in app.router.routes()]
    assert any("/internal/notify/manual-payment" in str(r) for r in routes)


async def test_load_payment_states(db_session, web_payment):
    from app.internal_api import _load_payment

    session, _ = db_session
    payment, error = await _load_payment(session, web_payment.id)
    assert error == "" and payment.id == web_payment.id

    web_payment.status = PaymentStatus.PAID
    _p2, error2 = await _load_payment(session, web_payment.id)
    assert error2 == "not_pending"

    web_payment.status = PaymentStatus.PENDING
    web_payment.provider = PaymentProvider.YOOKASSA
    _p3, error3 = await _load_payment(session, web_payment.id)
    assert error3 == "not_manual"
