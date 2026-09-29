"""Интеграционный тест критического пути с мок-панелью 3x-ui (форк с CSRF)."""

from __future__ import annotations

import json as j
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx

from app.db.models import PaymentProvider, PaymentStatus, Plan, Server, User
from app.services.provisioning import ProvisioningService
from tests.conftest import INBOUND_FIXTURE

BASE = "https://p.example:2053/s"
TOKEN = "tok-1"


@pytest.fixture
def mock_panel():
    """Минимальная имитация панели форка: клиенты inbound'а в памяти."""
    state: dict[str, dict[str, Any]] = {"clients": {}}  # email -> payload

    with respx.mock(assert_all_mocked=False) as m:
        m.get(f"{BASE}/csrf-token").respond(json={"success": True, "msg": "", "obj": TOKEN})
        m.post(f"{BASE}/login").respond(
            json={"success": True, "msg": "", "obj": None}, cookies={"3x-ui": "t"}
        )
        m.get(url__regex=rf"^{BASE}/panel/api/inbounds/get/1$").mock(side_effect=_inbound_handler(state))
        m.post(f"{BASE}/panel/api/clients/add").mock(side_effect=_add_handler(state))
        m.post(url__regex=rf"^{BASE}/panel/api/clients/update/[^/]+$").mock(
            side_effect=_update_handler(state)
        )
        m.get(url__regex=rf"^{BASE}/panel/api/clients/traffic/[^/]+$").mock(
            side_effect=_traffic_handler(state)
        )
        yield state


def _inbound_handler(state):
    async def handler(request):
        return httpx.Response(
            200,
            json={
                "success": True,
                "msg": "",
                "obj": INBOUND_FIXTURE
                | {"settings": j.dumps({"clients": list(state["clients"].values()), "decryption": "none"})},
            },
        )

    return handler


def _add_handler(state):
    async def handler(request):
        body = j.loads(request.content.decode())
        for c in [body["client"]]:
            state["clients"][c["email"]] = c
        return httpx.Response(200, json={"success": True, "msg": "", "obj": None})

    return handler


def _update_handler(state):
    async def handler(request):
        email = request.url.path.rsplit("/", 1)[-1]
        client = j.loads(request.content.decode())
        # update по старому ключу: убираем прежнюю запись, кладём по новому email
        state["clients"].pop(email, None)
        state["clients"][client["email"]] = client
        return httpx.Response(200, json={"success": True, "msg": "", "obj": None})

    return handler


def _traffic_handler(state):
    async def handler(request):
        email = request.url.path.rsplit("/", 1)[-1]
        client = state["clients"].get(email)
        if client is None:
            return httpx.Response(200, json={"success": True, "msg": "", "obj": None})
        return httpx.Response(
            200,
            json={
                "success": True,
                "msg": "",
                "obj": {
                    "id": 6,
                    "inboundId": 1,
                    "email": email,
                    "uuid": client.get("id", ""),
                    "subId": client.get("subId", ""),
                    "up": 100,
                    "down": 200,
                    "total": 0,
                    "enable": client.get("enable", True),
                    "expiryTime": client.get("expiryTime", 0),
                },
            },
        )

    return handler


async def test_full_provision_renew_reissue_flow(db_session, mock_panel):
    session, factory = db_session
    del factory

    server = Server(
        code="fr-t",
        country_name="Франция",
        country_flag="🇫🇷",
        xui_base_url=BASE,
        xui_username_enc="",
        xui_password_enc="",
        server_host="185.5.5.5",
        inbound_id=1,
        sub_url="https://p.example:2096/sub",
    )
    user = User(telegram_id=999, first_name="U")
    plan = Plan(code="1m", title="1 месяц", duration_days=30, price_kopeks=19900, price_stars=150, device_limit=3)
    session.add_all([server, user, plan])
    await session.flush()

    from app.db.models import Payment

    payment = Payment(
        user_id=user.id,
        plan_id=plan.id,
        provider=PaymentProvider.STARS,
        amount_kopeks=plan.price_kopeks,
        status=PaymentStatus.PENDING,
        external_id="charge_1",
    )
    session.add(payment)
    await session.flush()

    service = ProvisioningService(session)

    # 1) выдача
    sub = await service.grant_access(user, plan, server, payment)
    assert sub.id is not None
    assert payment.status == PaymentStatus.PAID
    assert sub.xui_email in mock_panel["clients"]
    assert mock_panel["clients"][sub.xui_email]["limitIp"] == 3
    assert abs(mock_panel["clients"][sub.xui_email]["expiryTime"] - int((datetime.now(UTC) + timedelta(days=30)).timestamp() * 1000)) < 60_000
    assert plan.is_trial or not user.trial_used

    # 2) sync traffic
    used = await service.sync_traffic_one(sub)
    assert used == 300
    assert sub.traffic_used_bytes == 300

    # 3) продление активной: +30 дней от expires_at
    old_expiry = sub.expires_at
    await service.renew(sub, 30)
    assert sub.expires_at - old_expiry == timedelta(days=30)
    assert mock_panel["clients"][sub.xui_email]["expiryTime"] > int(old_expiry.timestamp() * 1000)

    # 4) перевыпуск UUID: клиент в панели отсутствует -> создаётся с новым uuid/email-ключом
    old_uuid = sub.xui_client_uuid
    mock_panel["clients"].pop(sub.xui_email)
    await service.reissue(sub)
    assert sub.xui_client_uuid != old_uuid
    assert sub.xui_email in mock_panel["clients"]

    # 5) отключение при истечении
    await service.disable_in_panel(sub, enable=False)
    assert mock_panel["clients"][sub.xui_email]["enable"] is False

    # 6) vless-ссылка собирается из реального ответа панели
    from app.handlers.buy import build_vless_url

    url = await build_vless_url(sub)
    assert url.startswith("vless://")
    assert "security=reality" in url
