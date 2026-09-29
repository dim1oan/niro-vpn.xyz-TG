"""ЮKassa REST API: создание платежа + подтверждение по webhook/реконсиляции."""

from __future__ import annotations

import base64
import uuid as uuid_lib
from typing import Any

import httpx

from app.config import get_config
from app.services.payments.base import ProviderError
from app.utils.logging import get_logger

log = get_logger("yookassa")

API_BASE = "https://api.yookassa.ru/v3"
# Официальные сети уведомлений ЮKassa (https://yookassa.ru/developers/using-api/webhooks)
YOOKASSA_WEBHOOK_NETWORKS = [
    "185.71.76.0/27",
    "185.71.77.0/27",
    "77.75.153.0/25",
    "77.75.156.11",
    "77.75.156.35",
    "77.75.154.128/25",
    "2a02:5180::/32",
]


class YooKassaClient:
    def __init__(self, shop_id: str | None = None, secret_key: str | None = None) -> None:
        cfg = get_config()
        self.shop_id = shop_id or cfg.yookassa_shop_id
        self.secret_key = secret_key or cfg.yookassa_secret_key
        self._http = httpx.AsyncClient(
            base_url=API_BASE,
            auth=(self.shop_id, self.secret_key),
            timeout=httpx.Timeout(15.0),
            headers={"Content-Type": "application/json"},
        )

    async def close(self) -> None:
        await self._http.aclose()

    @property
    def enabled(self) -> bool:
        return bool(self.shop_id and self.secret_key)

    def _auth_headers(self) -> dict[str, str]:
        token = base64.b64encode(f"{self.shop_id}:{self.secret_key}".encode()).decode()
        return {"Authorization": f"Basic {token}", "Content-Type": "application/json"}

    async def create_payment(
        self,
        *,
        amount_kopeks: int,
        description: str,
        payment_db_id: int,
        return_url: str,
        metadata: dict[str, str] | None = None,
        sbp: bool = False,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "amount": {"value": f"{amount_kopeks / 100:.2f}", "currency": "RUB"},
            "capture": True,
            "confirmation": {"type": "redirect", "return_url": return_url},
            "description": description[:128],
            "metadata": metadata or {"payment_id": str(payment_db_id)},
        }
        if sbp:
            # СБП: платёж сразу привязывается к системе быстрых платежей
            body["payment_method_data"] = {"type": "sbp"}
            body["confirmation"]["return_url"] = return_url
        resp = await self._http.post(
            "/payments",
            json=body,
            headers={**self._auth_headers(), "Idempotence-Key": str(uuid_lib.uuid4())},
        )
        data = self._unwrap(resp, "create_payment")
        confirmation_url: str | None = None
        conf = data.get("confirmation") or {}
        if isinstance(conf, dict):
            confirmation_url = conf.get("confirmation_url")
        return {"id": data.get("id"), "status": data.get("status"), "confirmation_url": confirmation_url}

    async def get_payment(self, payment_api_id: str) -> dict[str, Any]:
        resp = await self._http.get(f"/payments/{payment_api_id}", headers=self._auth_headers())
        return self._unwrap(resp, "get_payment")

    async def is_succeeded(self, payment_api_id: str) -> tuple[bool, dict[str, Any]]:
        """Всегда перезапрашиваем платёж — телу вебхука не доверяем."""
        try:
            data = await self.get_payment(payment_api_id)
        except ProviderError:
            return False, {}
        return data.get("status") == "succeeded", data

    @staticmethod
    def _unwrap(resp: httpx.Response, action: str) -> dict[str, Any]:
        if resp.status_code >= 400:
            log.warning("api_error", action=action, status=resp.status_code, body=resp.text[:200])
            raise ProviderError(f"yookassa {action} failed: HTTP {resp.status_code}")
        data = resp.json()
        if not isinstance(data, dict):
            raise ProviderError(f"yookassa {action}: unexpected payload")
        return data
