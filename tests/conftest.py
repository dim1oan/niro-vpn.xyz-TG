"""Общие фикстуры тестов."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from app.config import Config, get_config
from app.db.base import Base
from app.db.session import make_engine, make_session_factory


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(autouse=True)
def _config(monkeypatch, tmp_path) -> Config:
    """Изолированный конфиг без внешних зависимостей."""
    get_config.cache_clear()
    monkeypatch.setenv("BOT_TOKEN", "123:TEST")
    monkeypatch.setenv("ADMIN_IDS", "111")
    monkeypatch.setenv("SECRET_KEY", "k")
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/test.db")
    cfg = get_config()
    yield cfg
    get_config.cache_clear()


@pytest.fixture
async def db_session(_config: Config):
    engine = make_engine(_config)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = make_session_factory(engine)
    async with factory() as session:
        yield session, factory
    await engine.dispose()


INBOUND_FIXTURE: dict[str, Any] = {
    "id": 1,
    "up": 1000,
    "down": 2000,
    "total": 0,
    "remark": "fr-vless",
    "enable": True,
    "expiryTime": 0,
    "listen": "",
    "port": 443,
    "protocol": "vless",
    "settings": json.dumps(
        {
            "clients": [{"id": "existing-uuid", "flow": "xtls-rprx-vision", "email": "seed@x", "limitIp": 2}],
            "decryption": "none",
            "fallbacks": [],
        }
    ),
    "streamSettings": json.dumps(
        {
            "network": "tcp",
            "security": "reality",
            "externalProxy": [],
            "realitySettings": {
                "show": False,
                "xver": "",
                "dest": "www.microsoft.com:443",
                "serverNames": ["www.microsoft.com", "microsoft.com"],
                "privateKey": "SECRET_PRIVATE",
                "minClientVer": "",
                "maxClientVer": "",
                "maxTimeDiff": 0,
                "shortIds": ["ab12cd34", ""],
                "settings": {"publicKey": "PUBKEY_BASE64xyz", "fingerprint": "chrome"},
                "spiderX": "/",
            },
            "tcpSettings": {"acceptProxyProtocol": False, "header": {"type": "none"}},
        }
    ),
    "tag": "inbound-443",
}
