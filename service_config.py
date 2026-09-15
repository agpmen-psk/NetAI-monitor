"""
service_config.py — настройки фонового сервиса реального времени
(realtime_service.py), живущие НА СЕРВЕРЕ, отдельно от общих настроек
приложения в таблице app_settings.

Почему отдельно, а не в общей БД, как всё остальное:

1. Доступ к Zabbix есть только у серверной машины (например, через
   поднятый на ней L2TP-туннель). Десктоп-клиентам эти параметры
   бесполезны: они физически не могут подключиться к Zabbix, поэтому и
   задавать их из GUI на любом ПК — вводить в заблуждение. В интерфейсе
   они показываются только для чтения (служба дублирует их в app_settings
   именно для показа).
2. ollama_host у сервера и у десктопов РАЗНЫЙ: для службы Ollama — это
   localhost, для клиентов — IP сервера. Один общий параметр в БД не может
   быть верным сразу для обоих.
3. Служба Windows работает под системной учётной записью, у которой своя
   %APPDATA% — она НЕ увидит postgres_connection.json из профиля
   пользователя (см. local_config.py). Поэтому конфиг службы лежит в
   %PROGRAMDATA% (машинный, общий для всех учётных записей) и может
   содержать свой DSN к PostgreSQL.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULTS = {
    # Пустой postgres_dsn => берётся переменная окружения NETAI_DB_DSN,
    # а если и её нет — postgres_connection.json текущего пользователя
    # (см. backend.py). Для службы под системной учётной записью надёжнее
    # указать DSN явно здесь.
    "postgres_dsn": "",
    "zabbix_url": "",
    "zabbix_user": "",
    "zabbix_password": "",
    # Используется job_worker.py для аудита/diff-проверки конфигураций —
    # тот же принцип, что и с Zabbix: доступ к Oxidized есть только у
    # серверной машины (через туннель), десктоп-клиент туда не ходит.
    "oxidized_url": "",
    # Для службы Ollama обычно локальная — в отличие от десктоп-клиентов,
    # которые обращаются к ней по IP сервера.
    "ollama_host": "http://localhost:11434",
    "ollama_model": "gemma3:4b",
    "embedding_model": "nomic-embed-text",
    # Демо-режим сервиса: синтетические данные вместо реального Zabbix.
    # Специально НЕ берётся из app_settings — иначе оператор с любого
    # десктопа мог бы случайно перевести серверную службу в демо-режим.
    "use_synthetic_data": False,
    "poll_interval_seconds": 60,
    # На каком адресе/порту слушает api_server.py. 0.0.0.0 — на всех
    # интерфейсах сервера (десктоп-клиенты подключаются по IP сервера в
    # локальной сети); порт наружу в интернет (на время защиты) публикуется
    # отдельно способом, который выберете сами — сам API от этого не зависит.
    "api_host": "0.0.0.0",
    "api_port": 8000,
}


def config_dir() -> Path:
    """%PROGRAMDATA%\\NetAI Monitor — машинный каталог, доступный и службе
    под системной учётной записью, и обычному пользователю."""
    base = Path(os.environ.get("PROGRAMDATA", os.environ.get("APPDATA", "."))) / "NetAI Monitor"
    base.mkdir(parents=True, exist_ok=True)
    return base


def config_path() -> Path:
    return config_dir() / "service_config.json"


def log_dir() -> Path:
    path = config_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_service_config() -> dict:
    path = config_path()
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return {**DEFAULTS, **data}
        except Exception:
            return dict(DEFAULTS)
    return dict(DEFAULTS)


def save_service_config(config: dict) -> None:
    config_path().write_text(
        json.dumps({**DEFAULTS, **config}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def ensure_config_file() -> tuple[Path, bool]:
    """Создаёт файл-шаблон, если его ещё нет — чтобы администратору было
    что править сразу после установки службы, а не угадывать формат.
    Возвращает (путь, был_ли_создан_сейчас)."""
    path = config_path()
    if path.exists():
        return path, False
    save_service_config(dict(DEFAULTS))
    return path, True
