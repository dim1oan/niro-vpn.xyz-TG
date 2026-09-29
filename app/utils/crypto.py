"""Шифрование чувствительных данных (пароли панелей) через Fernet."""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken


def _fernet(key: str) -> Fernet:
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt(plain: str, key: str) -> str:
    if not plain:
        return ""
    return _fernet(key).encrypt(plain.encode()).decode()


def decrypt(token: str, key: str) -> str:
    if not token:
        return ""
    try:
        return _fernet(key).decrypt(token.encode()).decode()
    except (InvalidToken, ValueError):
        return ""
