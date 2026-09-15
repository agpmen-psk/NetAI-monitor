"""
client_config.py — локальные настройки тонкого десктоп-клиента: адрес
API-сервера и токен сессии «запомнить меня».

Раньше (local_config.py) десктоп хранил параметры подключения к PostgreSQL —
теперь он вообще не подключается к БД напрямую, только к API-серверу, и
здесь хранится только его адрес плюс токен долгоживущей сессии.

Токен «запомнить меня» — в Диспетчере учётных данных Windows (через keyring),
а не в этом JSON-файле рядом с адресом сервера: файл настроек мог случайно
попасть в бэкап/скриншот/архив у инженера на рабочем столе, а Диспетчер
учётных данных — системное хранилище, защищённое учётной записью Windows.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import keyring

KEYRING_SERVICE = "NetAI Monitor"
KEYRING_USERNAME_KEY = "remember_me_username"
KEYRING_TOKEN_KEY = "remember_me_token"

DEFAULTS = {
    "api_base_url": "http://localhost:8000",
}


def _config_path() -> Path:
    base = Path(os.environ.get("APPDATA", ".")) / "NetAI Monitor"
    base.mkdir(parents=True, exist_ok=True)
    return base / "api_connection.json"


def load_client_config() -> dict:
    path = _config_path()
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return {**DEFAULTS, **data}
        except Exception:
            return dict(DEFAULTS)
    return dict(DEFAULTS)


def save_client_config(config: dict) -> None:
    _config_path().write_text(
        json.dumps({**DEFAULTS, **config}, ensure_ascii=False, indent=2), encoding="utf-8",
    )


def save_remembered_session(username: str, token: str) -> None:
    try:
        keyring.set_password(KEYRING_SERVICE, KEYRING_USERNAME_KEY, username)
        keyring.set_password(KEYRING_SERVICE, KEYRING_TOKEN_KEY, token)
    except Exception:
        pass  # Диспетчер учётных данных недоступен — просто не запомним сессию


def load_remembered_session() -> tuple[str, str] | None:
    try:
        username = keyring.get_password(KEYRING_SERVICE, KEYRING_USERNAME_KEY)
        token = keyring.get_password(KEYRING_SERVICE, KEYRING_TOKEN_KEY)
    except Exception:
        return None
    if username and token:
        return username, token
    return None


def clear_remembered_session() -> None:
    for key in (KEYRING_USERNAME_KEY, KEYRING_TOKEN_KEY):
        try:
            keyring.delete_password(KEYRING_SERVICE, key)
        except Exception:
            pass
