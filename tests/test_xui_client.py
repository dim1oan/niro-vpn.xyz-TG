"""Тесты XuiClient на respx-моках (протокол форка с CSRF-защитой)."""

from __future__ import annotations

import httpx
import pytest
import respx

from app.services.xui.client import SESSION_COOKIE, XuiClient
from app.services.xui.exceptions import XuiApiError, XuiAuthError
from app.services.xui.models import XuiClientConfig
from tests.conftest import INBOUND_FIXTURE

BASE = "https://panel.example.com:2053/secret"
TOKEN = "csrf-token-1"


def ok(obj=None, msg=""):
    return {"success": True, "msg": msg, "obj": obj}


def mock_auth(respx_mock: respx.router.MockRouter) -> None:
    respx_mock.get(f"{BASE}/csrf-token").respond(json=ok(TOKEN))
    respx_mock.post(f"{BASE}/login").respond(json=ok(), cookies={SESSION_COOKIE: "abc123"})


@respx.mock
async def test_login_sets_cookie_and_csrf():
    csrf = respx.get(f"{BASE}/csrf-token").respond(json=ok(TOKEN))
    login = respx.post(f"{BASE}/login").respond(json=ok(), cookies={SESSION_COOKIE: "abc123"})
    client = XuiClient(BASE, "admin", "pass")
    await client.login()
    assert csrf.called
    assert login.called
    assert client._logged_in
    assert client._has_session
    assert client._csrf == TOKEN
    req = login.calls.last.request
    assert req.headers["X-CSRF-Token"] == TOKEN
    assert req.headers["Content-Type"] == "application/json"
    await client.close()


@respx.mock
async def test_login_bad_credentials():
    mock_auth(respx)
    respx.post(f"{BASE}/login").respond(json={"success": False, "msg": "Login failed", "obj": None})
    client = XuiClient(BASE, "admin", "wrong")
    with pytest.raises(XuiAuthError):
        await client.login()
    await client.close()


@respx.mock
async def test_list_inbounds_requires_login_then_parses():
    mock_auth(respx)
    respx.get(f"{BASE}/panel/api/inbounds/list").respond(json=ok([INBOUND_FIXTURE]))
    client = XuiClient(BASE, "admin", "pass")
    inbounds = await client.list_inbounds()
    assert len(inbounds) == 1
    inbound = inbounds[0]
    assert inbound.port == 443
    assert inbound.reality is not None
    assert inbound.reality.public_key == "PUBKEY_BASE64xyz"
    assert inbound.stream_settings.security == "reality"
    await client.close()


@respx.mock
async def test_add_client_fork_format():
    """add: POST /panel/api/clients/add с телом {"client": {...}, "inboundIds": [7]}."""
    mock_auth(respx)
    route = respx.post(f"{BASE}/panel/api/clients/add").respond(json=ok())
    client = XuiClient(BASE, "admin", "pass")

    payload = XuiClientConfig(id="uuid-1", email="tg1-ab", limitIp=3, expiryTime=1234567890000)
    await client.add_client(7, payload)

    body = route.calls.last.request.read().decode()
    import json as _json

    data = _json.loads(body)
    assert data["inboundIds"] == [7]
    cl = data["client"]
    assert cl["id"] == "uuid-1"
    assert cl["email"] == "tg1-ab"
    assert cl["limitIp"] == 3
    assert cl["expiryTime"] == 1234567890000
    assert cl["totalGB"] == 0
    assert isinstance(cl["tgId"], int)
    await client.close()


@respx.mock
async def test_update_and_delete_by_email():
    mock_auth(respx)
    upd = respx.post(f"{BASE}/panel/api/clients/update/e@x").respond(json=ok())
    dele = respx.post(f"{BASE}/panel/api/clients/del/e@x").respond(json=ok())
    client = XuiClient(BASE, "a", "b")
    await client.update_client("e@x", XuiClientConfig(id="uuid-x", email="e@x", expiryTime=1))
    await client.delete_client("e@x")
    assert upd.called and dele.called
    body = upd.calls.last.request.read().decode()
    assert '"id":"uuid-x"' in body.replace(" ", "").replace('": "', '":"')
    await client.close()


@respx.mock
async def test_get_client_traffic_found_and_missing():
    mock_auth(respx)
    respx.get(f"{BASE}/panel/api/clients/traffic/e@x").respond(
        json=ok({"id": 6, "inboundId": 1, "enable": True, "email": "e@x",
                 "uuid": "uuid-x", "subId": "abc", "up": 10, "down": 20,
                 "expiryTime": 1790000000000, "total": 0, "reset": 0})
    )
    respx.get(f"{BASE}/panel/api/clients/traffic/nope").respond(json=ok(None))
    client = XuiClient(BASE, "a", "b")
    traffic = await client.get_client_traffic("e@x")
    assert traffic is not None
    assert traffic.used_bytes == 30
    assert await client.get_client_traffic("nope") is None
    await client.close()


@respx.mock
async def test_reset_traffic_path():
    mock_auth(respx)
    route = respx.post(f"{BASE}/panel/api/clients/resetTraffic/e@x").respond(json=ok())
    client = XuiClient(BASE, "a", "b")
    await client.reset_client_traffic("e@x")
    assert route.called
    await client.close()


@respx.mock
async def test_csrf_refresh_on_403():
    """403 при живой сессии -> обновить CSRF-токен один раз -> повторить."""
    mock_auth(respx)
    add_route = respx.post(f"{BASE}/panel/api/clients/add")
    add_route.side_effect = [
        httpx.Response(403),
        httpx.Response(200, json=ok()),
    ]
    client = XuiClient(BASE, "a", "b")
    await client.add_client(1, XuiClientConfig(id="u", email="e"))
    assert add_route.call_count == 2
    assert client._logged_in  # релогин не понадобился
    await client.close()


@respx.mock
async def test_api_error_raises():
    mock_auth(respx)
    respx.get(f"{BASE}/panel/api/inbounds/get/9").respond(json={"success": False, "msg": "fail", "obj": None})
    client = XuiClient(BASE, "a", "b")
    with pytest.raises(XuiApiError):
        await client.get_inbound(9)
    await client.close()


@respx.mock
async def test_relogin_on_401():
    """401 → релогин один раз → повтор запроса."""
    respx.get(f"{BASE}/csrf-token").mock(
        return_value=httpx.Response(200, json=ok(TOKEN))
    )
    login_route = respx.post(f"{BASE}/login").respond(json=ok(), cookies={SESSION_COOKIE: "abc123"})
    list_route = respx.get(f"{BASE}/panel/api/inbounds/list")
    list_route.side_effect = [
        httpx.Response(401),
        httpx.Response(200, json=ok([INBOUND_FIXTURE])),
    ]
    client = XuiClient(BASE, "a", "b")
    inbounds = await client.list_inbounds()
    assert len(inbounds) == 1
    assert login_route.call_count == 2  # initial + relogin
    assert client._logged_in
    await client.close()


@respx.mock
async def test_relogin_once_only():
    """Повторный 401 после релогина → XuiAuthError (без бесконечного цикла)."""
    respx.get(f"{BASE}/csrf-token").mock(
        return_value=httpx.Response(200, json=ok(TOKEN))
    )
    respx.post(f"{BASE}/login").respond(json=ok(), cookies={SESSION_COOKIE: "abc123"})
    respx.get(f"{BASE}/panel/api/inbounds/list").mock(return_value=httpx.Response(401))
    client = XuiClient(BASE, "a", "b")
    with pytest.raises(XuiAuthError):
        await client.list_inbounds()
    await client.close()
