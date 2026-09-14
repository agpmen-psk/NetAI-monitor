"""
local_config.py — параметры подключения к PostgreSQL, которые нужно знать
ДО выбора бэкенда (см. backend.py). Все остальные настройки приложения
(Zabbix, Oxidized, Ollama) хранятся в таблице app_settings — но она живёт
уже ВНУТРИ выбранной БД, так что для самого подключения к Postgres нужен
независимый источник. Храним в JSON-файле в %APPDATA%, а не в коде —
это то место, которое правит вкладка «Настройки», а backend.py читает
при каждом запуске приложения.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULTS = {
    "host": "localhost",
    "port": "5432",
    "dbname": "netai_monitor",
    "user": "postgres",
    "password": "postgres",
}


def _config_path() -> Path:
    base = Path(os.environ.get("APPDATA", ".")) / "NetAI Monitor"
    base.mkdir(parents=True, exist_ok=True)
    return base / "postgres_connection.json"


def load_postgres_config() -> dict:
    path = _config_path()
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return {**DEFAULTS, **data}
        except Exception:
            return dict(DEFAULTS)
    return dict(DEFAULTS)


def save_postgres_config(config: dict) -> None:
    path = _config_path()
    path.write_text(
        json.dumps({**DEFAULTS, **config}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def build_dsn(config: dict) -> str:
    return (
        f"dbname={config['dbname']} user={config['user']} "
        f"password={config['password']} host={config['host']} port={config['port']}"
    )


def _theme_path() -> Path:
    base = Path(os.environ.get("APPDATA", ".")) / "NetAI Monitor"
    base.mkdir(parents=True, exist_ok=True)
    return base / "ui_theme.json"


def load_theme() -> str:
    """"light" или "dark". Светлая — тема по умолчанию (см. gui.py):
    приложение задумано как рабочий инструмент на целый день, тёмная тема —
    осознанный выбор оператора, а не стартовое состояние."""
    path = _theme_path()
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            theme = data.get("theme", "light")
            return theme if theme in ("light", "dark") else "light"
        except Exception:
            return "light"
    return "light"


def save_theme(theme: str) -> None:
    path = _theme_path()
    path.write_text(json.dumps({"theme": theme}, ensure_ascii=False, indent=2), encoding="utf-8")
