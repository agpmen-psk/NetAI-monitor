"""
main.py — точка входа NetAI Monitor v2 с RAG.
"""
from db import Database
from settings import SettingsManager
from zabbix_client import ZabbixClient, MockZabbixClient
from oxidized_client import OxidizedClient
from mock_oxidized_client import MockOxidizedClient
from llm_client import get_analyzer
from rag import EmbeddingClient, IncidentRAG, ConfigRAG
from gui import run_app


def main():
    # Реальный пароль задаётся переменной окружения NETAI_DB_DSN, например:
    #   setx NETAI_DB_DSN "dbname=netai_monitor user=postgres password=... host=localhost"
    # Без неё используется безопасный дефолт (пароль "postgres" для локальной БД).
    db = Database()
    settings_manager = SettingsManager(db)
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

    embedder = EmbeddingClient(host=settings.ollama_host, model=settings.embedding_model)
    incident_rag = IncidentRAG(db, embedder) if embedder.is_available() else None
    config_rag = ConfigRAG(db, embedder) if embedder.is_available() else None

    run_app(
        db=db,
        settings_manager=settings_manager,
        zabbix_client=zabbix_client,
        oxidized_client=oxidized_client,
        analyzer=analyzer,
        incident_rag=incident_rag,
        config_rag=config_rag,
    )


if __name__ == "__main__":
    main()