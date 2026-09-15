"""
realtime_service.py — служба реального времени: опрос Zabbix.

Опрашивает Zabbix, анализирует новые (и ранее не распарсившиеся) проблемы
через LLM и определяет закрытие — независимо от того, открыт ли у кого-то
десктоп-клиент. Все десктоп-клиенты в сети видят актуальную картину через
API-службу (api_server.py), которая читает ту же PostgreSQL.

Это ОДНА из двух независимых служб на сервере — вторая, job_worker.py,
обрабатывает очередь LLM-заданий (аудит конфигов, сравнение с RAG/без,
переиндексация, приём из Генератора). Разделены специально: долгий LLM-
запрос в очереди не должен задерживать опрос Zabbix, и падение одной не
тянет за собой другую.

Рассчитан на постоянную работу службой Windows на серверной машине — там
же, где PostgreSQL, Ollama и (в типовой схеме) поднятый L2TP-туннель, через
который вообще доступен Zabbix. Поэтому:

  * настройки берутся из %PROGRAMDATA%\\NetAI Monitor\\service_config.json
    (см. service_config.py — служба под системной учётной записью не видит
    профиль пользователя, а Zabbix-доступ есть только у этой машины);
  * логи, ожидание PostgreSQL, блокировка второй копии — см. service_common.py;
  * состояние пишется в таблицу service_heartbeat, чтобы десктоп-клиенты (и
    API-служба) видели «Zabbix доступен, последний опрос N минут назад».

Запуск вручную:
    python realtime_service.py
    (Ctrl+C — остановка после текущего цикла опроса)

Установка службой — см. tools/install_service.ps1 и DEPLOYMENT.md.
"""
from __future__ import annotations

import logging
import os
import signal
import socket
import sys
import time
from datetime import datetime

from zabbix_client import ZabbixClient, MockZabbixClient
from llm_client import get_analyzer
from rag import EmbeddingClient, IncidentRAG
from realtime_engine import RealtimeEngine
from service_config import ensure_config_file, load_service_config, log_dir, config_path
from service_common import (
    setup_logging, acquire_single_instance_lock, connect_db_with_retry, sleep_with_interrupt,
)

log = logging.getLogger("realtime-service")
_running = True


def _handle_stop(signum, frame) -> None:
    global _running
    log.info("Получен сигнал остановки — завершаюсь после текущего цикла опроса...")
    _running = False


def _build_clients(config: dict):
    if config["use_synthetic_data"]:
        log.info("Режим синтетических данных — использую MockZabbixClient.")
        zabbix_client = MockZabbixClient(seed=42)
    else:
        if not config["zabbix_url"]:
            log.warning(
                "В %s не заданы параметры Zabbix (zabbix_url пуст) — "
                "опрос работать не будет. Заполните файл и перезапустите службу.",
                config_path(),
            )
        zabbix_client = ZabbixClient(
            url=config["zabbix_url"], user=config["zabbix_user"], password=config["zabbix_password"],
        )

    analyzer = get_analyzer(config["ollama_host"], config["ollama_model"])
    log.info("Анализатор: %s", type(analyzer).__name__)
    return zabbix_client, analyzer


def main() -> None:
    setup_logging("realtime-service", "realtime.log")

    config_file, created = ensure_config_file()
    if created:
        log.warning(
            "Создан файл настроек службы: %s — заполните параметры Zabbix и "
            "перезапустите службу.", config_file,
        )

    lock = acquire_single_instance_lock("realtime_service.lock")
    if lock is None:
        log.error(
            "Служба уже запущена (блокировка занята другим процессом). "
            "Вторая копия дублировала бы опрос и вызовы LLM — завершаюсь.",
        )
        sys.exit(1)

    signal.signal(signal.SIGINT, _handle_stop)
    signal.signal(signal.SIGTERM, _handle_stop)

    config = load_service_config()
    poll_interval = max(10, int(config["poll_interval_seconds"]))

    backend = connect_db_with_retry(config["postgres_dsn"], lambda: _running, log)
    if backend is None:
        log.info("Остановлено до подключения к БД.")
        return

    # Серверные параметры (Zabbix, интервал опроса и т.п.) НЕ дублируются в
    # БД: API-служба работает на той же машине и читает service_config.json
    # напрямую (см. api_server.py: GET/PUT /admin/service-config) — один
    # источник истины вместо синхронизации между файлом и таблицей.
    zabbix_client, analyzer = _build_clients(config)

    rag = None
    embedder = EmbeddingClient(host=config["ollama_host"], model=config["embedding_model"])
    if embedder.is_available():
        rag = IncidentRAG(backend.db, embedder)
        log.info("RAG подключён.")
    else:
        log.info("RAG недоступен (нет модели эмбеддингов) — работаю без него.")

    host_name = socket.gethostname()
    pid = os.getpid()
    log.info(
        "Сервис реального времени запущен на %s (pid %d). Опрос каждые %d сек. Логи: %s",
        host_name, pid, poll_interval, log_dir() / "realtime.log",
    )

    def heartbeat(status: str, message: str = "", **kwargs) -> None:
        try:
            backend.service_status.write(
                service_id=backend.service_status.REALTIME,
                status=status, message=message, service_host=host_name, pid=pid, **kwargs,
            )
        except Exception as e:
            log.warning("Не удалось записать heartbeat: %s", e)

    # Движок сообщает о переходах внутри цикла (опрос → анализ) — так
    # heartbeat обновляется и посреди долгого LLM-прогона, а не только
    # между циклами.
    engine = RealtimeEngine(
        zabbix_client, analyzer, backend.incidents, rag=rag,
        on_status=lambda status, message: heartbeat(status, message),
    )
    engine.seed()

    heartbeat("starting", "служба запущена, первый опрос сейчас")

    zabbix_ok: bool | None = None
    zabbix_message = ""
    last_result = {"new": 0, "closed": 0, "retried": 0}

    while _running:
        cycle_start = time.monotonic()
        try:
            # status='analyzing' ставится ДО долгого LLM-прогона: первый
            # опрос на непустом Zabbix может анализировать сотню инцидентов
            # много минут, и без этого наблюдатели решили бы, что служба
            # умерла (heartbeat не обновлялся бы всё это время).
            heartbeat("polling", "опрашиваю Zabbix", zabbix_ok=zabbix_ok, zabbix_message=zabbix_message,
                      last_new=last_result["new"], last_closed=last_result["closed"],
                      last_retried=last_result["retried"])
            result = engine.tick()
            last_result = result
            zabbix_ok, zabbix_message = True, "опрос выполняется штатно"
            if result["new"] or result["closed"] or result["retried"]:
                log.info(
                    "новых: %d, закрыто: %d, повторно проанализировано: %d",
                    result["new"], result["closed"], result["retried"],
                )
            else:
                log.info("изменений нет")
            heartbeat(
                "idle", "ожидаю следующий опрос", zabbix_ok=True, zabbix_message=zabbix_message,
                last_poll_at=datetime.now(), last_new=result["new"],
                last_closed=result["closed"], last_retried=result["retried"],
            )
        except Exception as e:
            log.exception("Ошибка в цикле опроса — продолжаю со следующей попытки.")
            zabbix_ok, zabbix_message = False, str(e)[:500]
            heartbeat("error", str(e)[:500], zabbix_ok=False, zabbix_message=zabbix_message,
                      last_new=last_result["new"], last_closed=last_result["closed"],
                      last_retried=last_result["retried"])

        elapsed = time.monotonic() - cycle_start
        sleep_with_interrupt(max(0.0, poll_interval - elapsed), lambda: _running)

    heartbeat("stopped", "служба остановлена", zabbix_ok=zabbix_ok, zabbix_message=zabbix_message)
    log.info("Сервис остановлен.")


if __name__ == "__main__":
    main()
