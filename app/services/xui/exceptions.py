"""Исключения интеграции с 3x-ui."""

from __future__ import annotations


class XuiError(Exception):
    """Базовая ошибка панели."""


class XuiApiError(XuiError):
    def __init__(self, msg: str, status: int | None = None) -> None:
        super().__init__(msg)
        self.msg = msg
        self.status = status


class XuiAuthError(XuiError):
    """Не удалось залогиниться / неверные креды."""


class XuiClientNotFound(XuiError):
    """Клиент (email/uuid) не найден в inbound'е."""
