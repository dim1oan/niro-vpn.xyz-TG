"""HTTP-клиент панели 3x-ui (форк с CSRF-защитой): сессия, релогин, retry, таймауты.

Особенности протокола этого форка (отличия от стокового 3x-ui):
  - логин: POST /login с JSON-телом и заголовком X-CSRF-Token;
  - все изменяющие запросы требуют заголовок X-CSRF-Token;
  - токен: GET /csrf-token (заголовок X-Requested-With) -> {"success":true,"obj":"<token>"};
  - клиенты: /panel/api/clients/*, ключ клиента — email;
  - settings/streamSettings в inbound'ах приходят объектами (не JSON-строками).
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from app.services.xui.exceptions import XuiApiError, XuiAuthError
from app.services.xui.models import ApiResponse, ClientTraffic, Inbound, XuiClientConfig
from app.utils.logging import get_logger

log = get_logger("xui")

SESSION_COOKIE = "3x-ui"
DEFAULT_TIMEOUT = httpx.Timeout(15.0, connect=10.0)
MAX_RETRIES = 3
RETRY_BACKOFF = 1.5  # сек, экспоненциально


def with_relogin(func):
    """При 401/редиректе на логин — один раз перелогиниться и повторить запрос."""

    async def wrapper(self: XuiClient, *args: Any, **kwargs: Any) -> Any:
        try:
            return await func(self, *args, **kwargs)
        except XuiAuthError:
            log.info("xui.session_expired", base=self.base_url)
            async with self._login_lock:
                if not self._logged_in:
                    raise
                self._clear_session()
            await self.login()
            return await func(self, *args, **kwargs)

    return wrapper


class XuiClient:
    """Клиент REST API панели. Один экземпляр на сервер (на базовый URL)."""

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        timeout: httpx.Timeout | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self._http = httpx.AsyncClient(
            base_url=f"{self.base_url}/",
            timeout=timeout or DEFAULT_TIMEOUT,
            follow_redirects=False,
            headers={"User-Agent": "vpn-bot/1.0"},
        )
        self._login_lock = asyncio.Lock()
        self._logged_in = False
        self._csrf: str | None = None

    async def close(self) -> None:
        await self._http.aclose()

    @property
    def _has_session(self) -> bool:
        return SESSION_COOKIE in self._http.cookies or "session" in self._http.cookies

    def _clear_session(self) -> None:
        self._http.cookies.clear()
        self._logged_in = False
        self._csrf = None

    # ---------- низкий уровень ----------

    async def _fetch_csrf(self) -> str:
        resp = await self._http.get(
            "csrf-token", headers={"X-Requested-With": "XMLHttpRequest"}
        )
        if resp.status_code != 200:
            raise XuiAuthError(f"csrf fetch failed: HTTP {resp.status_code}")
        try:
            body = resp.json()
        except ValueError as e:
            raise XuiAuthError("csrf fetch: invalid JSON") from e
        token = body.get("obj") if isinstance(body, dict) else None
        if not body.get("success") or not isinstance(token, str) or not token:
            raise XuiAuthError("csrf fetch: token missing")
        return token

    async def _ensure_csrf(self) -> str:
        if not self._csrf:
            self._csrf = await self._fetch_csrf()
        return self._csrf

    def _headers(self, method: str, csrf: str | None = None) -> dict[str, str]:
        headers = {"X-Requested-With": "XMLHttpRequest"}
        if method.upper() != "GET":
            if csrf:
                headers["X-CSRF-Token"] = csrf
            headers["Content-Type"] = "application/json"
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | list[Any] | None = None,
        auth_retry_done: bool = False,
        csrf_retry_done: bool = False,
    ) -> ApiResponse:
        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                csrf = await self._ensure_csrf() if method.upper() != "GET" else None
                resp = await self._http.request(
                    method,
                    path.lstrip("/"),
                    json=json_body,
                    headers=self._headers(method, csrf),
                )
            except httpx.HTTPError as e:  # сеть/таймаут — ретраим
                last_exc = e
                if attempt == MAX_RETRIES:
                    break
                await asyncio.sleep(RETRY_BACKOFF**attempt)
                continue

            if resp.status_code in (401, 403) or (
                resp.status_code in (301, 302)
                and "/login" in str(resp.headers.get("location", ""))
            ):
                if resp.status_code == 403 and not csrf_retry_done:
                    # токен мог протухнуть при живой сессии — обновляем один раз
                    log.info("xui.csrf_refresh", path=path)
                    self._csrf = None
                    try:
                        await self._ensure_csrf()
                    except XuiAuthError:
                        pass
                    return await self._request(
                        method,
                        path,
                        json_body=json_body,
                        auth_retry_done=auth_retry_done,
                        csrf_retry_done=True,
                    )
                raise XuiAuthError(f"session expired ({resp.status_code})")

            if resp.status_code >= 500:
                last_exc = XuiApiError(f"panel 5xx: {resp.status_code}", status=resp.status_code)
                if attempt == MAX_RETRIES:
                    raise last_exc
                await asyncio.sleep(RETRY_BACKOFF**attempt)
                continue

            if resp.status_code >= 400:
                raise XuiApiError(f"panel {resp.status_code}: {path}", status=resp.status_code)

            return self._parse_json(path, resp)
        raise XuiApiError(f"panel unreachable after retries: {path} ({last_exc})")

    @staticmethod
    def _parse_json(path: str, resp: httpx.Response) -> ApiResponse:
        try:
            body = resp.json()
        except ValueError as e:
            raise XuiApiError(f"invalid JSON from {path}") from e
        parsed = ApiResponse.model_validate(body) if isinstance(body, dict) else ApiResponse(success=False)
        if not parsed.ok:
            raise XuiApiError(parsed.msg or f"panel error at {path}")
        return parsed

    async def login(self) -> None:
        async with self._login_lock:
            self._csrf = await self._fetch_csrf()
            try:
                resp = await self._http.post(
                    "login",
                    json={"username": self.username, "password": self.password},
                    headers=self._headers("POST", self._csrf),
                )
            except httpx.HTTPError as e:
                raise XuiApiError(f"login request failed: {e!r}") from e
            if resp.status_code == 403:
                raise XuiAuthError("login forbidden (bad credentials?)")
            if resp.status_code != 200:
                raise XuiAuthError(f"login failed: HTTP {resp.status_code}")
            try:
                api = self._parse_json("login", resp)
            except XuiApiError as e:
                raise XuiAuthError(e.msg or "invalid credentials") from e
            if not api.success:
                raise XuiAuthError(api.msg or "invalid credentials")
            self._logged_in = True

    async def ensure_logged_in(self) -> None:
        if not self._has_session or not self._csrf:
            if not self._has_session:
                await self.login()
            else:
                await self._ensure_csrf()

    async def _authed_request(
        self,
        method: str,
        path: str,
        json_body: dict[str, Any] | list[Any] | None = None,
    ) -> ApiResponse:
        await self.ensure_logged_in()
        return await self._request(method, path, json_body=json_body)

    # ---------- API ----------

    @with_relogin
    async def list_inbounds(self) -> list[Inbound]:
        api = await self._authed_request("GET", "/panel/api/inbounds/list")
        raw_list = api.obj or []
        return [Inbound.from_raw(item) for item in raw_list]

    @with_relogin
    async def get_inbound(self, inbound_id: int) -> Inbound:
        api = await self._authed_request("GET", f"/panel/api/inbounds/get/{inbound_id}")
        if api.obj is None:
            raise XuiApiError(f"inbound {inbound_id} not found")
        return Inbound.from_raw(api.obj)

    @staticmethod
    def _add_payload(inbound_id: int, client: XuiClientConfig) -> dict[str, Any]:
        """POST /panel/api/clients/add принимает {"client": {...}, "inboundIds": [..]}."""
        return {"client": client.to_panel_dict(), "inboundIds": [int(inbound_id)]}

    @with_relogin
    async def add_client(self, inbound_id: int, client: XuiClientConfig) -> None:
        await self._authed_request(
            "POST", "/panel/api/clients/add", json_body=self._add_payload(inbound_id, client)
        )

    @with_relogin
    async def update_client(self, email: str, client: XuiClientConfig) -> None:
        """Обновление по email клиента (uuid передаётся внутри тела в поле id)."""
        await self._authed_request(
            "POST",
            f"/panel/api/clients/update/{email}",
            json_body=client.to_panel_dict(),
        )

    @with_relogin
    async def delete_client(self, email: str) -> None:
        await self._authed_request("POST", f"/panel/api/clients/del/{email}")

    @with_relogin
    async def get_client_traffic(self, email: str) -> ClientTraffic | None:
        api = await self._authed_request("GET", f"/panel/api/clients/traffic/{email}")
        obj = api.obj
        if not obj:
            return None
        traffic = ClientTraffic.model_validate(obj)
        if not traffic.email:
            return None
        return traffic

    @with_relogin
    async def reset_client_traffic(self, email: str) -> None:
        await self._authed_request("POST", f"/panel/api/clients/resetTraffic/{email}")

    async def client_ips(self, email: str) -> list[str]:
        """В этом форке отдельного эндпоинта IP нет — возвращаем пусто."""
        log.info("xui.client_ips.unsupported", email=email)
        return []

    async def clear_client_ips(self, email: str) -> None:
        log.info("xui.clear_client_ips.unsupported", email=email)

    # ---------- удобное ----------

    async def ping(self) -> bool:
        try:
            await self.ensure_logged_in()
            return True
        except Exception:
            return False


__all__ = ["XuiClient", "with_relogin", "SESSION_COOKIE"]
