"""Генерация ссылки подключения VLESS + Reality из настроек inbound'а."""

from __future__ import annotations

from urllib.parse import quote

from app.services.xui.exceptions import XuiApiError
from app.services.xui.models import Inbound


def build_vless_link(
    *,
    uuid: str,
    inbound: Inbound,
    server_host: str,
    label: str,
    flow: str | None = None,
) -> str:
    """Сборка vless:// по настройкам inbound'а (reality / tls / none).

    host берётся из конфига сервера, не из inbound (там 0.0.0.0/пусто).
    Примеры:
      reality: vless://uuid@host:port?type=tcp&security=reality&pbk=..&fp=..&sni=..&sid=..&spx=%2F&flow=..#label
      tls:     vless://uuid@host:port?type=tcp&security=tls&sni=..&fp=chrome#label
    """
    stream = inbound.stream_settings
    network = stream.network or "tcp"
    security = (stream.security or "none").lower()
    use_flow = flow if flow is not None else inbound.client_flow

    params: list[tuple[str, str]] = [("type", network)]

    if security == "reality":
        reality = inbound.reality
        if reality is None:
            raise XuiApiError(f"inbound {inbound.id}: security == reality, но realitySettings отсутствуют")
        public_key = reality.public_key
        if not public_key:
            raise XuiApiError(f"inbound {inbound.id}: publicKey отсутствует в realitySettings.settings")

        sni = reality.server_names[0] if reality.server_names else ""
        sid = reality.short_ids[0] if reality.short_ids else ""
        fingerprint = reality.fingerprint or "chrome"
        params += [
            ("security", "reality"),
            ("pbk", public_key),
            ("fp", fingerprint),
            ("sni", sni),
            ("sid", sid),
            ("spx", "/"),
        ]
    elif security == "tls":
        tls_raw = getattr(stream, "tls_settings", None) or {}
        sni = ""
        if isinstance(tls_raw, dict):
            sni = str(tls_raw.get("serverName") or "")
        params.append(("security", "tls"))
        if sni:
            params.append(("sni", sni))
        params.append(("fp", "chrome"))

    if use_flow:
        params.append(("flow", use_flow))

    query = "&".join(
        f"{quote(str(k), safe='')}={quote(str(v), safe='')}" for k, v in params if v != ""
    )
    return f"vless://{uuid}@{server_host}:{inbound.port}?{query}#{quote(label, safe='')}"


def build_subscription_link(sub_url_base: str, sub_id: str) -> str:
    return f"{sub_url_base.rstrip('/')}/{sub_id}"


def make_label(brand: str, country_flag: str, country_name: str) -> str:
    parts = [brand.strip()]
    suffix = f"{country_flag} {country_name}".strip()
    if suffix:
        parts.append(suffix)
    return " | ".join(parts)
