"""Тесты генерации VLESS+Reality ссылки."""

from __future__ import annotations

from urllib.parse import parse_qs, unquote, urlparse

from app.services.xui.links import build_subscription_link, build_vless_link, make_label
from app.services.xui.models import Inbound
from tests.conftest import INBOUND_FIXTURE


def test_vless_link_full() -> None:
    inbound = Inbound.from_raw(INBOUND_FIXTURE)
    url = build_vless_link(
        uuid="uuid-1234",
        inbound=inbound,
        server_host="185.0.0.1",
        label=make_label("MyVPN", "🇫🇷", "Франция"),
    )
    parsed = urlparse(url)
    assert parsed.scheme == "vless"
    assert parsed.hostname == "185.0.0.1"
    assert parsed.port == 443
    assert parsed.username == "uuid-1234"
    qs = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    assert qs["type"] == "tcp"
    assert qs["security"] == "reality"
    assert qs["pbk"] == "PUBKEY_BASE64xyz"
    assert qs["fp"] == "chrome"
    assert qs["sni"] == "www.microsoft.com"  # первый serverName
    assert qs["sid"] == "ab12cd34"           # первый shortId
    assert qs["spx"] == "/"
    assert qs["flow"] == "xtls-rprx-vision"
    assert unquote(parsed.fragment) == "MyVPN | 🇫🇷 Франция"


def test_vless_link_urlencoded() -> None:
    inbound = Inbound.from_raw(INBOUND_FIXTURE)
    url = build_vless_link(uuid="u", inbound=inbound, server_host="h", label="A | B C")
    assert "#" in url and "%" not in unquote(urlparse(url).query)


def test_vless_link_tls() -> None:
    raw = dict(INBOUND_FIXTURE)
    import json as j

    raw["streamSettings"] = j.dumps({"network": "tcp", "security": "tls", "tlsSettings": {"serverName": "ex.com"}})
    inbound = Inbound.from_raw(raw)
    url = build_vless_link(uuid="u", inbound=inbound, server_host="h", label="x")
    assert url.startswith("vless://u@h:")
    assert "security=tls" in url
    assert "sni=ex.com" in url
    assert "pbk=" not in url  # reality-параметров быть не должно


def test_vless_link_no_flow_when_inbound_has_none() -> None:
    raw = dict(INBOUND_FIXTURE)
    import json as j

    stream = j.loads(raw["streamSettings"]) if isinstance(raw["streamSettings"], str) else raw["streamSettings"]
    stream["security"] = "reality"
    raw["streamSettings"] = j.dumps(stream)
    raw["settings"] = j.dumps({"clients": [{"id": "u", "email": "e", "flow": ""}]})
    inbound = Inbound.from_raw(raw)
    url = build_vless_link(uuid="u", inbound=inbound, server_host="h", label="x")
    assert "flow" not in url


def test_subscription_link() -> None:
    assert build_subscription_link("https://p.example/sub/", "abc123") == "https://p.example/sub/abc123"


def test_inbound_client_flow_fallback() -> None:
    raw = dict(INBOUND_FIXTURE)
    import json as j

    raw["settings"] = j.dumps({"clients": [], "decryption": "none"})
    inbound = Inbound.from_raw(raw)
    # flow берётся из streamSettings.realitySettings.settings или пусто → ""
    assert inbound.client_flow in ("xtls-rprx-vision", "")
