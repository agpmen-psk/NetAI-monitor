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
"""
from __future__ import annotations

import os
from typing import Tuple

from local_config import load_postgres_config, build_dsn


def _resolve_dsn() -> str:
    # NETAI_DB_DSN — явный оверрайд для CI/скриптов/тех, кто предпочитает
    # переменные окружения; без неё используется то, что сохранено через GUI.
    return os.environ.get("NETAI_DB_DSN") or build_dsn(load_postgres_config())


def create_backend() -> Tuple[object, object, object, object, bool]:
    """Возвращает (db, incident_repo, config_repo, settings_repo, is_postgres)."""
    try:
        from db import Database, IncidentRepository, ConfigDiffRepository, SettingsRepository

        db = Database(dsn=_resolve_dsn())
        incident_repo = IncidentRepository(db)
        config_repo = ConfigDiffRepository(db)
        settings_repo = SettingsRepository(db)
        # Простой запрос, чтобы проверить, что подключение реально рабочее —
        # Database._init_schema() уже должен был на этом упасть, если БД
        # недоступна, но перестраховка не помешает.
        settings_repo.get_all()
        return db, incident_repo, config_repo, settings_repo, True
    except Exception as e:
        print(f"[backend] PostgreSQL недоступен ({e}) — переключаюсь на локальный SQLite-режим.")
        from db_sqlite import (
            SQLiteDatabase, SQLiteIncidentRepository, SQLiteConfigDiffRepository,
            SQLiteSettingsRepository,
        )

        db = SQLiteDatabase()
        incident_repo = SQLiteIncidentRepository(db)
        config_repo = SQLiteConfigDiffRepository(db)
        settings_repo = SQLiteSettingsRepository(db)
        return db, incident_repo, config_repo, settings_repo, False
