"""Тесты загрузки конфига из .env-файла.

Регрессия на два бага, из-за которых бот не стартовал по инструкции из README:
1) ADMIN_IDS=1,2 падал с SettingsError — pydantic-settings пытался
   декодировать list[int] как JSON до вызова валидатора;
2) пустое значение необязательного поля (REQUIRED_CHANNEL_ID=) падало
   с ValidationError вместо того, чтобы стать None.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from app.config import Config


def _write_env(tmp_path: Path, body: str) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    env = tmp_path / ".env"
    env.write_text(body, encoding="utf-8")
    return env


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Переменные процесса перебивают .env — убираем их для чистоты теста."""
    for key in (
        "BOT_TOKEN",
        "ADMIN_IDS",
        "ADMIN_CHAT_ID",
        "REQUIRED_CHANNEL_ID",
        "REDIS_URL",
        "SECRET_KEY",
        "DATABASE_URL",
        "XUI_SUB_URL",
        "WEBHOOK_BASE_URL",
    ):
        monkeypatch.delenv(key, raising=False)


def test_admin_ids_from_comma_separated_string(tmp_path):
    """Формат из .env.example: ADMIN_IDS=123456789,987654321."""
    env = _write_env(tmp_path, "BOT_TOKEN=1:x\nADMIN_IDS=123456789,987654321\n")
    cfg = Config(_env_file=env)
    assert cfg.admin_ids == [123456789, 987654321]


def test_admin_ids_accepts_spaces_and_json_like_brackets(tmp_path):
    """Терпим и «1, 2», и «[1, 2]» — люди пишут по-разному."""
    env = _write_env(tmp_path, "ADMIN_IDS=[111, 222]\n")
    assert Config(_env_file=env).admin_ids == [111, 222]

    env2 = _write_env(tmp_path / "b", "ADMIN_IDS=111, 222\n")
    assert Config(_env_file=env2).admin_ids == [111, 222]


def test_admin_ids_single_value_and_empty(tmp_path):
    assert Config(_env_file=_write_env(tmp_path, "ADMIN_IDS=555\n")).admin_ids == [555]
    assert Config(_env_file=_write_env(tmp_path / "b", "ADMIN_IDS=\n")).admin_ids == []


def test_empty_optional_fields_become_none(tmp_path):
    """Пустые необязательные поля не должны ломать валидацию."""
    env = _write_env(
        tmp_path,
        "BOT_TOKEN=1:x\n"
        "ADMIN_IDS=111\n"
        "ADMIN_CHAT_ID=\n"
        "REQUIRED_CHANNEL_ID=\n"
        "REDIS_URL=\n"
        "XUI_SUB_URL=\n"
        "WEBHOOK_BASE_URL=\n",
    )
    cfg = Config(_env_file=env)
    assert cfg.admin_chat_id is None
    assert cfg.required_channel_id is None
    assert cfg.redis_url is None
    assert cfg.xui_sub_url is None
    assert cfg.webhook_base_url is None


def test_negative_chat_id_is_parsed(tmp_path):
    """ID супергруппы приходит отрицательным."""
    env = _write_env(tmp_path, "ADMIN_CHAT_ID=-1001234567890\n")
    assert Config(_env_file=env).admin_chat_id == -1001234567890


def test_example_env_file_loads_as_is():
    """.env.example должен грузиться без правок — иначе инструкция врёт."""
    example = Path(__file__).resolve().parent.parent / ".env.example"
    cfg = Config(_env_file=example)
    assert cfg.admin_ids == [123456789, 987654321]
    assert cfg.required_channel_id is None
    assert cfg.brand_name == "MyVPN"


def test_admin_chat_falls_back_to_first_admin(tmp_path):
    env = _write_env(tmp_path, "ADMIN_IDS=777,888\nADMIN_CHAT_ID=\n")
    assert Config(_env_file=env).admin_chat() == 777


def test_generate_missing_secret_derives_valid_fernet_key(tmp_path):
    """Короткий SECRET_KEY деривируется в валидный ключ, а не роняет бота."""
    cfg = Config(_env_file=_write_env(tmp_path, "SECRET_KEY=k\n"))
    cfg.generate_missing_secret()
    Fernet(cfg.secret_key.encode())  # не должно бросить

    empty = Config(_env_file=_write_env(tmp_path / "b", "SECRET_KEY=\n"))
    empty.generate_missing_secret()
    Fernet(empty.secret_key.encode())


def test_ensure_runtime_requires_bot_token(tmp_path):
    cfg = Config(_env_file=_write_env(tmp_path, "ADMIN_IDS=1\n"))
    cfg.generate_missing_secret()
    with pytest.raises(RuntimeError, match="BOT_TOKEN"):
        cfg.ensure_runtime()
