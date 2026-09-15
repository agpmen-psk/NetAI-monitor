"""
realtime_service.py — фоновый сервис реального времени.

Опрашивает Zabbix, анализирует новые (и ранее не распарсившиеся) проблемы
через LLM и определяет закрытие — независимо от того, открыт ли у кого-то
десктоп-клиент. Все десктоп-клиенты в сети видят актуальную картину, читая
общую историю из PostgreSQL (см. AlertsTab в gui.py — там теперь только
чтение, без собственного опроса Zabbix).

Предполагается, что сервис работает постоянно на том же сервере, что
PostgreSQL и Ollama (там же, где уже настроен сетевой доступ к обеим). Для
запуска в фоне на Windows проще всего создать задачу в Планировщике заданий
с триггером «при запуске системы» и действием, запускающим эту команду —
подробности в README.md.

Запуск:
    python realtime_service.py
    (Ctrl+C — плавная остановка после текущего цикла)
"""
from __future__ import annotations

import logging
import signal
import time

from backend import create_backend
from settings import SettingsManager
from zabbix_client import ZabbixClient, MockZabbixClient
from llm_client import get_analyzer
from rag import EmbeddingClient, IncidentRAG
from realtime_engine import RealtimeEngine

POLL_INTERVAL_SECONDS = 60

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [realtime-service] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("realtime_service")

_running = True


def _handle_stop(signum, frame) -> None:
    global _running
    log.info("Получен сигнал остановки — завершаюсь после текущего цикла опроса...")
    _running = False


def _build_engine() -> tuple[RealtimeEngine, bool]:
    db, incident_repo, config_repo, settings_repo, is_postgres = create_backend()
    if not is_postgres:
        log.warning(
            "PostgreSQL недоступен — сервис работает на локальном SQLite. "
            "Десктоп-клиенты на ДРУГИХ машинах не увидят эти данные, пока "
            "PostgreSQL не станет доступен (см. Настройки → PostgreSQL в приложении)."
        )

    settings_manager = SettingsManager(settings_repo)
    settings = settings_manager.load()

    if settings.use_synthetic_data:
        zabbix_client = MockZabbixClient(seed=42)
        log.info("Режим синтетических данных — использую MockZabbixClient.")
    else:
        zabbix_client = ZabbixClient(
            url=settings.zabbix_url, user=settings.zabbix_user, password=settings.zabbix_password,
        )

    analyzer = get_analyzer(settings.ollama_host, settings.ollama_model)
    log.info("Анализатор: %s", type(analyzer).__name__)

    rag = None
    if is_postgres:
        embedder = EmbeddingClient(host=settings.ollama_host, model=settings.embedding_model)
        if embedder.is_available():
            rag = IncidentRAG(db, embedder)
            log.info("RAG подключён.")
        else:
            log.info("RAG недоступен (нет модели эмбеддингов) — работаю без него.")

    return RealtimeEngine(zabbix_client, analyzer, incident_repo, rag=rag), is_postgres


def main() -> None:
    signal.signal(signal.SIGINT, _handle_stop)
    signal.signal(signal.SIGTERM, _handle_stop)

    engine, is_postgres = _build_engine()
    engine.seed()
    log.info(
        "Сервис реального времени запущен (БД: %s). Опрос каждые %d сек.",
        "PostgreSQL" if is_postgres else "SQLite (офлайн)", POLL_INTERVAL_SECONDS,
    )

    while _running:
        cycle_start = time.monotonic()
        try:
            result = engine.tick()
            if result["new"] or result["closed"] or result["retried"]:
                log.info(
                    "новых: %d, закрыто: %d, повторно проанализировано: %d",
                    result["new"], result["closed"], result["retried"],
                )
            else:
                log.info("изменений нет")
        except Exception:
            log.exception("Ошибка в цикле опроса — продолжаю со следующей попытки.")

        elapsed = time.monotonic() - cycle_start
        remaining = max(0.0, POLL_INTERVAL_SECONDS - elapsed)
        slept = 0.0
        while _running and slept < remaining:
            time.sleep(min(1.0, remaining - slept))
            slept += 1.0

    log.info("Сервис остановлен.")


if __name__ == "__main__":
    main()
