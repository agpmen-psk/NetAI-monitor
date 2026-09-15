"""
backend.py — выбор хранилища при запуске приложения.

Пробует подключиться к PostgreSQL (боевой режим, с pgvector/RAG), используя
параметры из local_config.py (их правит вкладка «Настройки» → «PostgreSQL»).
Если подключиться не удаётся — сеть недоступна, сервер не запущен, компьютер
комиссии на защите без развёрнутой инфраструктуры — автоматически
откатывается на локальный SQLite-файл в %APPDATA%. В офлайн-режиме RAG
отключается (нет pgvector), но сама программа, синтетические данные,
Zabbix/Oxidized-моки и Ollama-анализ (если Ollama установлена) работают
как обычно.

ВАЖНО про фоновый сервис: откат на SQLite уместен только для десктоп-
клиента (лучше работать локально, чем не работать вовсе). Для
realtime_service.py он был бы вреден — служба молча писала бы результаты
анализа в локальный файл, которого не видит ни один клиент. Поэтому у
службы своя точка входа: create_postgres_backend(), которая НЕ откатывается,
а бросает исключение, чтобы служба могла подождать и повторить попытку.
"""
from __future__ import annotations

import os
from typing import Tuple

from local_config import load_postgres_config, build_dsn


def _resolve_dsn(explicit_dsn: str | None = None) -> str:
    # Приоритет: явно переданный DSN (из service_config.json у службы) >
    # NETAI_DB_DSN (оверрайд для CI/скриптов и для службы Windows, у которой
    # своя учётная запись и свой профиль) > то, что сохранено через GUI.
    return explicit_dsn or os.environ.get("NETAI_DB_DSN") or build_dsn(load_postgres_config())


def create_postgres_backend(dsn: str | None = None) -> Tuple[object, object, object, object, object]:
    """Строго PostgreSQL, без отката на SQLite. Бросает исключение, если
    подключиться не удалось — вызывающий код (служба) решает, подождать и
    повторить или завершиться.

    Возвращает (db, incident_repo, config_repo, settings_repo, status_repo)."""
    from db import (
        Database, IncidentRepository, ConfigDiffRepository, SettingsRepository,
        ServiceStatusRepository,
    )

    db = Database(dsn=_resolve_dsn(dsn))
    settings_repo = SettingsRepository(db)
    # Простой запрос, чтобы убедиться, что подключение реально рабочее.
    settings_repo.get_all()
    return (
        db,
        IncidentRepository(db),
        ConfigDiffRepository(db),
        settings_repo,
        ServiceStatusRepository(db),
    )


def create_backend() -> Tuple[object, object, object, object, object, bool]:
    """Возвращает (db, incident_repo, config_repo, settings_repo, status_repo, is_postgres)."""
    try:
        db, incident_repo, config_repo, settings_repo, status_repo = create_postgres_backend()
        return db, incident_repo, config_repo, settings_repo, status_repo, True
    except Exception as e:
        print(f"[backend] PostgreSQL недоступен ({e}) — переключаюсь на локальный SQLite-режим.")
        from db_sqlite import (
            SQLiteDatabase, SQLiteIncidentRepository, SQLiteConfigDiffRepository,
            SQLiteSettingsRepository, SQLiteServiceStatusRepository,
        )

        db = SQLiteDatabase()
        return (
            db,
            SQLiteIncidentRepository(db),
            SQLiteConfigDiffRepository(db),
            SQLiteSettingsRepository(db),
            SQLiteServiceStatusRepository(db),
            False,
        )
