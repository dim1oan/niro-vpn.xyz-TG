"""Фабрика клиентов панели по модели Server (пароли расшифровываются Fernet)."""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING

from app.config import get_config
from app.db.models import Server
from app.services.xui.client import XuiClient
from app.utils.crypto import decrypt

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@lru_cache(maxsize=64)
def _client_for(base_url: str, username: str, password_plain: str) -> XuiClient:
    return XuiClient(base_url=base_url, username=username, password=password_plain)


def get_client(server: Server) -> XuiClient:
    cfg = get_config()
    username = decrypt(server.xui_username_enc, cfg.secret_key) or cfg.xui_username
    password = decrypt(server.xui_password_enc, cfg.secret_key) or cfg.xui_password
    return _client_for(server.xui_base_url, username, password)


async def close_all() -> None:
    _client_for.cache_clear()


async def iter_clients(servers: AsyncIterator[Server]) -> AsyncIterator[tuple[Server, XuiClient]]:
    async for s in servers:
        yield s, get_client(s)
