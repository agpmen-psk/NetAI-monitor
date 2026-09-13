"""
main.py — точка входа NetAI Monitor v2 с RAG.

Хранилище выбирается автоматически (см. backend.py): PostgreSQL, если он
доступен, иначе — локальный SQLite в офлайн-режиме (без RAG, но со всей
остальной функциональностью). Это позволяет собранному .exe запускаться на
любом компьютере, даже без развёрнутой инфраструктуры предприятия.
"""
from backend import create_backend
from settings import SettingsManager
from zabbix_client import ZabbixClient, MockZabbixClient
from oxidized_client import OxidizedClient
from mock_oxidized_client import MockOxidizedClient
from llm_client import get_analyzer
from rag import EmbeddingClient, IncidentRAG, ConfigRAG
from gui import run_app


def main():
    # Реальный пароль PostgreSQL задаётся переменной окружения NETAI_DB_DSN:
    #   setx NETAI_DB_DSN "dbname=netai_monitor user=postgres password=... host=localhost"
    # Если PostgreSQL недоступен вообще — приложение само переключится на
    # локальный SQLite-файл (%APPDATA%/NetAI Monitor/netai_local.db).
    db, incident_repo, config_repo, settings_repo, is_postgres = create_backend()
    settings_manager = SettingsManager(settings_repo)
    settings = settings_manager.load()

    if settings.use_synthetic_data:
        zabbix_client = MockZabbixClient(seed=42)
        oxidized_client = MockOxidizedClient(seed=42)
    else:
        zabbix_client = ZabbixClient(
            url=settings.zabbix_url,
            user=settings.zabbix_user,
            password=settings.zabbix_password,
        )
        oxidized_client = OxidizedClient(base_url=settings.oxidized_url)

    analyzer = get_analyzer(settings.ollama_host, settings.ollama_model)

    incident_rag = None
    config_rag = None
    if is_postgres:
        # RAG требует pgvector — доступен только вместе с настоящим PostgreSQL.
        embedder = EmbeddingClient(host=settings.ollama_host, model=settings.embedding_model)
        if embedder.is_available():
            incident_rag = IncidentRAG(db, embedder)
            config_rag = ConfigRAG(db, embedder)

    run_app(
        incident_repo=incident_repo,
        config_repo=config_repo,
        settings_manager=settings_manager,
        zabbix_client=zabbix_client,
        oxidized_client=oxidized_client,
        analyzer=analyzer,
        incident_rag=incident_rag,
        config_rag=config_rag,
        offline_mode=not is_postgres,
    )


if __name__ == "__main__":
    main()
