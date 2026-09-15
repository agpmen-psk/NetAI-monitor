"""
realtime_service.py — фоновый сервис реального времени.

Опрашивает Zabbix, анализирует новые (и ранее не распарсившиеся) проблемы
через LLM и определяет закрытие — независимо от того, открыт ли у кого-то
десктоп-клиент. Все десктоп-клиенты в сети видят актуальную картину, читая
общую историю из PostgreSQL (см. AlertsTab в gui.py — там теперь только
чтение, без собственного опроса Zabbix).

Рассчитан на постоянную работу службой Windows на серверной машине — там
же, где PostgreSQL, Ollama и (в типовой схеме) поднятый L2TP-туннель, через
который вообще доступен Zabbix. Поэтому:

  * настройки берутся из %PROGRAMDATA%\\NetAI Monitor\\service_config.json
    (см. service_config.py — служба под системной учётной записью не видит
    профиль пользователя, а Zabbix-доступ есть только у этой машины);
  * логи пишутся в файл с ротацией (у службы нет консоли, stdout уходит
    в никуда);
  * PostgreSQL ожидается с повторами, а НЕ подменяется локальным SQLite —
    иначе служба молча писала бы в файл, которого никто не видит;
  * состояние пишется в таблицу service_heartbeat, чтобы десктоп-клиенты
    видели «служба жива, Zabbix доступен, последний опрос N минут назад»
    (сами они Zabbix проверить не могут — туннеля у них нет);
  * вторая копия не запускается (файловая блокировка), чтобы служба и
    случайно запущенный вручную скрипт не дублировали анализ.

Запуск вручную:
    python realtime_service.py
    (Ctrl+C — остановка после текущего цикла опроса)

Установка службой — см. tools/install_service.ps1 и DEPLOYMENT.md.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import signal
import socket
import sys
import time
from datetime import datetime

from backend import create_postgres_backend
from zabbix_client import ZabbixClient, MockZabbixClient
from llm_client import get_analyzer
from rag import EmbeddingClient, IncidentRAG
from realtime_engine import RealtimeEngine
from service_config import ensure_config_file, load_service_config, log_dir, config_dir

DB_RETRY_SECONDS = 15
LOG_FILE_BYTES = 5 * 1024 * 1024
LOG_FILE_COUNT = 5

log = logging.getLogger("realtime_service")

_running = True


def _setup_logging() -> None:
    """Файл с ротацией + консоль. Файл — потому что у службы Windows нет
    консоли и весь stdout пропал бы бесследно; консоль — чтобы при ручном
    запуске всё было видно как раньше."""
    formatter = logging.Formatter(
        "%(asctime)s [realtime-service] %(message)s", datefmt="%Y-%m-%d %H:%M:%S",
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    # Консоль Windows по умолчанию в cp1251: любой символ вне этой кодировки
    # в тексте лога (например, в сообщении исключения от сторонней
    # библиотеки) обрушил бы logging с UnicodeEncodeError. Для службы,
    # которая должна работать месяцами, падение из-за ЛОГА недопустимо.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass

    file_handler = logging.handlers.RotatingFileHandler(
        log_dir() / "realtime.log", maxBytes=LOG_FILE_BYTES, backupCount=LOG_FILE_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)


def _acquire_single_instance_lock():
    """Файловая блокировка через msvcrt — снимается ОС автоматически при
    завершении процесса, в том числе аварийном (в отличие от «файл-флага»,
    который остался бы висеть после падения и блокировал бы запуск навсегда).
    Возвращает открытый файловый объект (его нельзя закрывать, пока служба
    работает) или None, если блокировку уже держит другая копия."""
    lock_path = config_dir() / "realtime_service.lock"
    try:
        handle = open(lock_path, "a+")
    except OSError:
        return None  # не смогли даже открыть — пусть служба просто работает
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    handle.seek(0)
    handle.truncate()
    handle.write(f"pid={os.getpid()} host={socket.gethostname()} started={datetime.now().isoformat()}\n")
    handle.flush()
    return handle


def _handle_stop(signum, frame) -> None:
    global _running
    log.info("Получен сигнал остановки — завершаюсь после текущего цикла опроса...")
    _running = False


def _connect_db_with_retry(dsn: str):
    """Ждём PostgreSQL столько, сколько нужно: при перезагрузке сервера
    службы стартуют в произвольном порядке, и упасть только потому, что
    СУБД ещё поднимается, — неприемлемо для постоянно работающей службы."""
    attempt = 0
    while _running:
        attempt += 1
        try:
            backend = create_postgres_backend(dsn or None)
            if attempt > 1:
                log.info("PostgreSQL доступен — продолжаю.")
            return backend
        except Exception as e:
            log.warning(
                "PostgreSQL недоступен (попытка %d): %s. Повтор через %d сек.",
                attempt, e, DB_RETRY_SECONDS,
            )
            slept = 0
            while _running and slept < DB_RETRY_SECONDS:
                time.sleep(1)
                slept += 1
    return None


def _build_clients(config: dict):
    if config["use_synthetic_data"]:
        log.info("Режим синтетических данных — использую MockZabbixClient.")
        zabbix_client = MockZabbixClient(seed=42)
    else:
        if not config["zabbix_url"]:
            log.warning(
                "В %s не заданы параметры Zabbix (zabbix_url пуст) — "
                "опрос работать не будет. Заполните файл и перезапустите службу.",
                config_path_hint(),
            )
        zabbix_client = ZabbixClient(
            url=config["zabbix_url"], user=config["zabbix_user"], password=config["zabbix_password"],
        )

    analyzer = get_analyzer(config["ollama_host"], config["ollama_model"])
    log.info("Анализатор: %s", type(analyzer).__name__)
    return zabbix_client, analyzer


def config_path_hint() -> str:
    from service_config import config_path

    return str(config_path())


def _publish_settings_for_gui(settings_repo, config: dict) -> None:
    """Дублируем серверные параметры в app_settings — десктоп-клиенты
    показывают их в «Настройках» только для чтения (задавать их с любого
    ПК смысла нет: подключиться к Zabbix может только серверная машина).
    ollama_host намеренно НЕ перезаписываем: у службы это localhost, а
    клиентам нужен IP сервера — это разные значения по своей природе."""
    try:
        settings_repo.set_many({
            "zabbix_url": config["zabbix_url"],
            "zabbix_user": config["zabbix_user"],
            "zabbix_password": config["zabbix_password"],
        })
    except Exception as e:
        log.warning("Не удалось опубликовать настройки для GUI: %s", e)


def main() -> None:
    _setup_logging()

    config_file, created = ensure_config_file()
    if created:
        log.warning(
            "Создан файл настроек службы: %s — заполните параметры Zabbix и "
            "перезапустите службу.", config_file,
        )

    lock = _acquire_single_instance_lock()
    if lock is None:
        log.error(
            "Служба уже запущена (блокировка %s занята другим процессом). "
            "Вторая копия дублировала бы опрос и вызовы LLM — завершаюсь.",
            config_dir() / "realtime_service.lock",
        )
        sys.exit(1)

    signal.signal(signal.SIGINT, _handle_stop)
    signal.signal(signal.SIGTERM, _handle_stop)

    config = load_service_config()
    poll_interval = max(10, int(config["poll_interval_seconds"]))

    backend = _connect_db_with_retry(config["postgres_dsn"])
    if backend is None:
        log.info("Остановлено до подключения к БД.")
        return
    db, incident_repo, config_repo, settings_repo, status_repo = backend

    _publish_settings_for_gui(settings_repo, config)
    zabbix_client, analyzer = _build_clients(config)

    rag = None
    embedder = EmbeddingClient(host=config["ollama_host"], model=config["embedding_model"])
    if embedder.is_available():
        rag = IncidentRAG(db, embedder)
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
            status_repo.write(
                status=status, message=message, service_host=host_name, pid=pid, **kwargs,
            )
        except Exception as e:
            log.warning("Не удалось записать heartbeat: %s", e)

    # Движок сообщает о переходах внутри цикла (опрос → анализ) — так
    # heartbeat обновляется и посреди долгого LLM-прогона, а не только
    # между циклами.
    engine = RealtimeEngine(
        zabbix_client, analyzer, incident_repo, rag=rag,
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
            # много минут, и без этого GUI решил бы, что служба умерла
            # (heartbeat не обновлялся бы всё это время).
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
        remaining = max(0.0, poll_interval - elapsed)
        slept = 0.0
        while _running and slept < remaining:
            time.sleep(min(1.0, remaining - slept))
            slept += 1.0

    heartbeat("stopped", "служба остановлена", zabbix_ok=zabbix_ok, zabbix_message=zabbix_message)
    log.info("Сервис остановлен.")


if __name__ == "__main__":
    main()
