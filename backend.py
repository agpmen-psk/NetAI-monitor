"""
backend.py — подключение к PostgreSQL для серверной стороны (API-служба и
worker-служба). Десктоп-клиент сюда больше не заглядывает вообще: он не
знает ни пароля PostgreSQL, ни адреса Ollama/Zabbix, только URL API — это и
есть суть перехода на тонкий клиент (см. api_client.py, main.py).

create_postgres_backend() специально НЕ откатывается на локальный SQLite:
раньше (в старой архитектуре) откат был уместен для десктоп-клиента —
лучше работать локально, чем не работать вовсе. Для сервера это было бы
вредно — служба молча писала бы результаты анализа в файл, которого никто
не видит. Поэтому при сбое подключения бросается исключение, а вызывающий
код (realtime_service.py, api_server.py) сам решает, ждать и повторить или
завершиться.
"""
from __future__ import annotations

import os
from typing import NamedTuple

from local_config import load_postgres_config, build_dsn


class Backend(NamedTuple):
    db: object
    incidents: object
    configs: object
    settings: object
    service_status: object
    users: object
    sessions: object
    jobs: object


def _resolve_dsn(explicit_dsn: str | None = None) -> str:
    # Приоритет: явно переданный DSN (из service_config.json у службы) >
    # NETAI_DB_DSN (оверрайд для CI/скриптов и для службы Windows, у которой
    # своя учётная запись и свой профиль) > то, что сохранено через GUI
    # (актуально только если backend.py всё ещё запускают на машине, где
    # когда-то стоял старый десктоп-клиент с локальным подключением).
    return explicit_dsn or os.environ.get("NETAI_DB_DSN") or build_dsn(load_postgres_config())


def create_postgres_backend(dsn: str | None = None) -> Backend:
    """Строго PostgreSQL, без отката. Бросает исключение, если подключиться
    не удалось."""
    from db import (
        Database, IncidentRepository, ConfigDiffRepository, SettingsRepository,
        ServiceStatusRepository, UserRepository, SessionRepository, JobQueueRepository,
    )

    db = Database(dsn=_resolve_dsn(dsn))
    settings_repo = SettingsRepository(db)
    # Простой запрос, чтобы убедиться, что подключение реально рабочее.
    settings_repo.get_all()
    return Backend(
        db=db,
        incidents=IncidentRepository(db),
        configs=ConfigDiffRepository(db),
        settings=settings_repo,
        service_status=ServiceStatusRepository(db),
        users=UserRepository(db),
        sessions=SessionRepository(db),
        jobs=JobQueueRepository(db),
    )
