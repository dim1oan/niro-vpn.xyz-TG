"""Pydantic-модели ответов 3x-ui."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field


class ApiResponse(BaseModel):
    success: bool
    msg: str = ""
    obj: Any = None

    @property
    def ok(self) -> bool:
        return self.success and self.msg.lower() != "fail"


class ClientTraffic(BaseModel):
    id: str | int | None = None
    email: str = ""
    up: int = 0
    down: int = 0
    total: int = 0
    enable: bool = True
    expiry_time: int = 0
    sub_id: str | None = None
    total_gb: int = 0

    @property
    def used_bytes(self) -> int:
        return self.up + self.down


class XuiClientConfig(BaseModel):
    """Тело клиента для /panel/api/clients/add|update (формат форка с CSRF)."""

    id: str
    flow: str = "xtls-rprx-vision"
    email: str
    limit_ip: int = Field(default=3, alias="limitIp")
    total_gb: int = Field(default=0, alias="totalGB")
    expiry_time: int = Field(default=0, alias="expiryTime")
    enable: bool = True
    tg_id: int = Field(default=0, alias="tgId")
    sub_id: str = Field(default="", alias="subId")
    comment: str = ""
    reset: int = 0
    security: str = "auto"
    group: str = ""

    model_config = {"populate_by_name": True}

    def to_panel_dict(self) -> dict[str, Any]:
        data = self.model_dump(by_alias=True)
        data["totalGB"] = int(data["totalGB"])
        data["expiryTime"] = int(data["expiryTime"])
        data["limitIp"] = int(data["limitIp"])
        data["tgId"] = int(data["tgId"] or 0)
        return data


class RealitySettings(BaseModel):
    show: bool = False
    xver: str | int = ""
    dest: str | None = None
    server_names: list[str] = Field(default_factory=list, alias="serverNames")
    private_key: str = Field(default="", alias="privateKey")
    min_client_ver: str = Field(default="", alias="minClientVer")
    max_client_ver: str = Field(default="", alias="maxClientVer")
    max_time_diff: str | int = Field(default="", alias="maxTimeDiff")
    short_ids: list[str] = Field(default_factory=list, alias="shortIds")
    settings: dict[str, Any] = Field(default_factory=dict)
    spider_x: str = Field(default="", alias="spiderX")

    model_config = {"populate_by_name": True}

    @property
    def public_key(self) -> str:
        return str(self.settings.get("publicKey", ""))

    @property
    def fingerprint(self) -> str:
        return str(self.settings.get("fingerprint", "chrome"))


class StreamSettings(BaseModel):
    network: str = "tcp"
    security: str = "none"
    external_proxy: list[Any] | None = Field(default=None, alias="externalProxy")

    reality_settings: RealitySettings | None = Field(default=None, alias="realitySettings")
    tls_settings: dict[str, Any] = Field(default_factory=dict, alias="tlsSettings")
    tcp_settings: dict[str, Any] = Field(default_factory=dict, alias="tcpSettings")
    ws_settings: dict[str, Any] = Field(default_factory=dict, alias="wsSettings")
    grpc_settings: dict[str, Any] = Field(default_factory=dict, alias="grpcSettings")

    model_config = {"populate_by_name": True}


class Inbound(BaseModel):
    id: int
    user_id: int | None = Field(default=None, alias="userId")
    up: int = 0
    down: int = 0
    total: int = 0
    remark: str = ""
    enable: bool = True
    expiry_time: int = Field(default=0, alias="expiryTime")
    listen: str = ""
    port: int
    protocol: str = "vless"
    settings: dict[str, Any] = Field(default_factory=dict)
    stream_settings: StreamSettings = Field(default_factory=StreamSettings, alias="streamSettings")
    tag: str = ""

    model_config = {"populate_by_name": True}

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> Inbound:
        """Панель отдаёт settings/streamSettings как JSON-строки."""
        data = dict(raw)
        for key in ("settings", "streamSettings"):
            if isinstance(data.get(key), str):
                try:
                    data[key] = json.loads(data[key])
                except json.JSONDecodeError:
                    data[key] = {}
        return cls.model_validate(data)

    @property
    def client_flow(self) -> str:
        """flow из streamSettings.realitySettings.settings (обычно xtls-rprx-vision)."""
        ss = self.stream_settings.reality_settings
        if ss is not None:
            flow = ss.settings.get("flow") or self._settings_flow()
            if flow:
                return str(flow)
        return self._settings_flow()

    def _settings_flow(self) -> str:
        clients = self.settings.get("clients", [])
        for c in clients:
            if c.get("flow"):
                return str(c["flow"])
        return ""

    @property
    def reality(self) -> RealitySettings | None:
        ss = self.stream_settings
        if ss.security == "reality" and ss.reality_settings is not None:
            return ss.reality_settings
        return None
