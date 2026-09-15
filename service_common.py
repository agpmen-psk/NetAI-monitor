"""
service_common.py — общая инфраструктура для двух независимых служб
Windows: realtime_service.py (опрос Zabbix) и job_worker.py (очередь
LLM-заданий: аудит конфигов, сравнение с RAG/без, переиндексация,
приём из Генератора).

Это ДВЕ отдельные службы NSSM, а не одна с двумя потоками: если долгий
LLM-запрос в очереди зависнет, опрос Zabbix не должен от этого страдать,
и наоборот — падение одной не тянет за собой другую. За это приходится
platit небольшим дублированием инфраструктуры (логирование, блокировка от
второй копии, ожидание PostgreSQL) — вынесено сюда один раз, чтобы не
повторять в обоих файлах.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import socket
import sys
import time
from datetime import datetime
from typing import Callable

from backend import create_postgres_backend, Backend
from service_config import log_dir, config_dir

DB_RETRY_SECONDS = 15
LOG_FILE_BYTES = 5 * 1024 * 1024
LOG_FILE_COUNT = 5


def setup_logging(tag: str, log_filename: str) -> logging.Logger:
    """Файл с ротацией + консоль. Файл — у службы Windows нет консоли, и
    весь stdout иначе пропал бы бесследно; консоль — чтобы при ручном
    запуске (отладка) всё было видно, как раньше."""
    formatter = logging.Formatter(
        f"%(asctime)s [{tag}] %(message)s", datefmt="%Y-%m-%d %H:%M:%S",
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    # Консоль Windows по умолчанию в cp1251: символ вне этой кодировки в
    # тексте чужого исключения обрушил бы logging целиком. Для службы,
    # рассчитанной на месяцы работы, падение из-за ЛОГА недопустимо.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass

    file_handler = logging.handlers.RotatingFileHandler(
        log_dir() / log_filename, maxBytes=LOG_FILE_BYTES, backupCount=LOG_FILE_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    return logging.getLogger(tag)


def acquire_single_instance_lock(lock_filename: str):
    """Файловая блокировка через msvcrt — снимается ОС автоматически при
    завершении процесса, в том числе аварийном (в отличие от «файла-флага»,
    который остался бы висеть после падения и блокировал бы запуск
    навсегда). Возвращает открытый файловый объект (нельзя закрывать, пока
    служба работает) или None, если блокировку уже держит другая копия."""
    lock_path = config_dir() / lock_filename
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


def connect_db_with_retry(dsn: str, should_continue: Callable[[], bool], log: logging.Logger) -> Backend | None:
    """Ждём PostgreSQL столько, сколько нужно: при перезагрузке сервера
    службы стартуют в произвольном порядке, и упасть только потому, что
    СУБД ещё поднимается, — неприемлемо для постоянно работающей службы."""
    attempt = 0
    while should_continue():
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
            while should_continue() and slept < DB_RETRY_SECONDS:
                time.sleep(1)
                slept += 1
    return None


def sleep_with_interrupt(seconds: float, should_continue: Callable[[], bool]) -> None:
    """time.sleep(seconds), но проверяет should_continue() каждую секунду —
    чтобы Ctrl+C/остановка службы не заставляли ждать полный интервал
    (может быть минуты) перед реакцией на сигнал остановки."""
    slept = 0.0
    while should_continue() and slept < seconds:
        time.sleep(min(1.0, seconds - slept))
        slept += 1.0
