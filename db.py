"""
db.py — слой доступа к PostgreSQL.
Добавлена поддержка pgvector (embedding) для RAG — поиска похожих
прошлых инцидентов и конфигураций при анализе новых.
"""
from __future__ import annotations

import os

# На Windows с русской локалью PostgreSQL по умолчанию присылает системные
# сообщения (lc_messages) в кодировке Windows-1251, а psycopg2/libpq пытаются
# декодировать их как UTF-8 — в итоге вместо реальной ошибки (неверный пароль,
# порт и т.п.) вылетает UnicodeDecodeError "invalid continuation byte", которая
# маскирует настоящую причину. lc_messages=C заставляет сервер отвечать
# по-английски (ASCII), что декодируется как UTF-8 без проблем. Ставится ДО
# первого psycopg2.connect() в процессе — переменные окружения читает libpq.
os.environ.setdefault("PGCLIENTENCODING", "UTF8")
os.environ.setdefault("PGOPTIONS", "-c lc_messages=C")

import psycopg2
import psycopg2.extras
from datetime import datetime
from typing import List

from models import Incident, Severity

# Реальный пароль/DSN задаётся переменной окружения NETAI_DB_DSN (например,
# в .env локально или в переменных окружения CI/сервера) — в коде и в
# истории git не должно быть настоящих учётных данных от боевой БД.
DEFAULT_DSN = "dbname=netai_monitor user=postgres password=postgres host=localhost"

SCHEMA = """
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS incidents (
    id              TEXT PRIMARY KEY,
    host            TEXT NOT NULL,
    problem_name    TEXT NOT NULL,
    severity        INTEGER NOT NULL,
    timestamp       TIMESTAMP NOT NULL,
    item_key        TEXT,
    last_value      TEXT,
    ai_summary      TEXT,
    ai_recommendation TEXT,
    saved_at        TIMESTAMP NOT NULL DEFAULT now()
);

ALTER TABLE incidents ADD COLUMN IF NOT EXISTS embedding vector(768);
ALTER TABLE incidents ADD COLUMN IF NOT EXISTS ai_verified BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE incidents ADD COLUMN IF NOT EXISTS resolution TEXT;
-- resolved_at: когда проблема пропала из активных в Zabbix (реальное время,
-- см. AlertsTab). opened_at: когда инженер реально открыл карточку алерта —
-- независимо друг от друга, оба NULL у только что пришедшего активного алерта.
ALTER TABLE incidents ADD COLUMN IF NOT EXISTS resolved_at TIMESTAMP;
ALTER TABLE incidents ADD COLUMN IF NOT EXISTS opened_at TIMESTAMP;

CREATE TABLE IF NOT EXISTS config_diffs (
    id              SERIAL PRIMARY KEY,
    node            TEXT NOT NULL,
    review_type     TEXT NOT NULL DEFAULT 'diff',
    version_from    TEXT,
    version_to      TEXT,
    diff_text       TEXT NOT NULL,
    ai_summary      TEXT,
    ai_risk_level   TEXT,
    ai_recommendation TEXT,
    saved_at        TIMESTAMP NOT NULL DEFAULT now()
);

ALTER TABLE config_diffs ADD COLUMN IF NOT EXISTS review_type TEXT NOT NULL DEFAULT 'diff';
ALTER TABLE config_diffs ADD COLUMN IF NOT EXISTS embedding vector(768);
ALTER TABLE config_diffs ADD COLUMN IF NOT EXISTS ai_verified BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE config_diffs ADD COLUMN IF NOT EXISTS resolution TEXT;

CREATE TABLE IF NOT EXISTS app_settings (
    key             TEXT PRIMARY KEY,
    value           TEXT
);

-- Одна строка (id=1) со статусом фонового сервиса реального времени.
-- Через неё десктоп-клиенты видят, живёт ли служба на сервере и доступен
-- ли ей Zabbix — сами они к Zabbix подключиться не могут (туннель есть
-- только у серверной машины), поэтому локальная проверка подключения с
-- десктопа бессмысленна, а эти данные приходят от того, кто реально
-- опрашивает Zabbix.
CREATE TABLE IF NOT EXISTS service_heartbeat (
    id              INTEGER PRIMARY KEY,
    updated_at      TIMESTAMP NOT NULL,
    status          TEXT NOT NULL,
    message         TEXT,
    zabbix_ok       BOOLEAN,
    zabbix_message  TEXT,
    last_poll_at    TIMESTAMP,
    last_new        INTEGER,
    last_closed     INTEGER,
    last_retried    INTEGER,
    service_host    TEXT,
    pid             INTEGER
);

-- Учётные записи и сессии API. Десктоп-клиент теперь тонкий: не знает ни
-- пароля PostgreSQL, ни адреса Ollama/Zabbix — только URL API и (после
-- входа) токен сессии. role: 'admin' — управляет пользователями и
-- серверными настройками (Zabbix, интервал опроса, модели), 'engineer' —
-- работает с алертами/конфигами/чатом, серверные настройки видит, но не
-- редактирует.
CREATE TABLE IF NOT EXISTS users (
    id              SERIAL PRIMARY KEY,
    username        TEXT UNIQUE NOT NULL,
    password_hash   TEXT NOT NULL,
    role            TEXT NOT NULL DEFAULT 'engineer',
    is_active       BOOLEAN NOT NULL DEFAULT true,
    created_at      TIMESTAMP NOT NULL DEFAULT now(),
    last_login_at   TIMESTAMP
);

-- Токен хранится ХЕШЕМ (sha256), не в открытом виде — так же, как пароль:
-- утечка БД не должна означать возможность входа под чужой сессией.
-- expires_at различается для обычного входа и "запомнить меня" (см.
-- auth.py: TOKEN_TTL_HOURS / REMEMBER_TTL_DAYS).
CREATE TABLE IF NOT EXISTS sessions (
    token_hash      TEXT PRIMARY KEY,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at      TIMESTAMP NOT NULL DEFAULT now(),
    expires_at      TIMESTAMP NOT NULL,
    remember_me     BOOLEAN NOT NULL DEFAULT false
);

-- Очередь длинных LLM-операций (аудит конфига, сравнение с RAG/без,
-- переиндексация, анализ из Генератора) — выполняет worker-служба
-- (realtime_service.py), а НЕ сама API-служба: иначе долгий вызов Ollama
-- (30-120с) занимал бы один из немногих обработчиков API и замедлял бы
-- ответы всем остальным клиентам одновременно.
CREATE TABLE IF NOT EXISTS job_queue (
    id              SERIAL PRIMARY KEY,
    job_type        TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    payload         JSONB NOT NULL,
    result          JSONB,
    error           TEXT,
    created_by      INTEGER REFERENCES users(id),
    created_at      TIMESTAMP NOT NULL DEFAULT now(),
    started_at      TIMESTAMP,
    finished_at     TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_job_queue_pending ON job_queue (status, created_at)
    WHERE status = 'pending';
"""

DEFAULT_SETTINGS = {
    "zabbix_url": "",
    "zabbix_user": "",
    "zabbix_password": "",
    "oxidized_url": "",
    "ollama_host": "http://localhost:11434",
    "ollama_model": "gemma4:e4b",
    "embedding_model": "nomic-embed-text",
    "use_synthetic_data": "true",
    "use_rag": "true",
}


class Database:
    def __init__(self, dsn: str | None = None):
        self.dsn = dsn or os.environ.get("NETAI_DB_DSN", DEFAULT_DSN)
        self._init_schema()

    def _connect(self):
        return psycopg2.connect(self.dsn)

    def _init_schema(self) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(SCHEMA)
                for key, value in DEFAULT_SETTINGS.items():
                    cur.execute(
                        "INSERT INTO app_settings (key, value) VALUES (%s, %s) "
                        "ON CONFLICT (key) DO NOTHING",
                        (key, value),
                    )
            conn.commit()


class SettingsRepository:
    def __init__(self, db: Database):
        self.db = db

    def get_all(self) -> dict:
        with self.db._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT key, value FROM app_settings")
                return dict(cur.fetchall())

    def set(self, key: str, value: str) -> None:
        with self.db._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO app_settings (key, value) VALUES (%s, %s) "
                    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                    (key, value),
                )
            conn.commit()

    def set_many(self, values: dict) -> None:
        for key, value in values.items():
            self.set(key, value)


class ServiceStatusRepository:
    """Статус фоновых служб на сервере — по одной строке на службу
    (service_id=1 — realtime_service.py/опрос Zabbix, service_id=2 —
    job_worker.py/очередь LLM-заданий). API-служба отдаёт обе клиенту (см.
    SettingsTab: карточка «Службы на сервере»)."""

    REALTIME = 1
    JOB_WORKER = 2

    def __init__(self, db: Database):
        self.db = db

    def write(self, service_id: int, status: str, message: str = "", zabbix_ok: bool | None = None,
              zabbix_message: str = "", last_poll_at=None, last_new: int = 0,
              last_closed: int = 0, last_retried: int = 0,
              service_host: str = "", pid: int = 0) -> None:
        with self.db._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO service_heartbeat
                    (id, updated_at, status, message, zabbix_ok, zabbix_message,
                     last_poll_at, last_new, last_closed, last_retried, service_host, pid)
                    VALUES (%s, now(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET
                        updated_at = now(),
                        status = EXCLUDED.status,
                        message = EXCLUDED.message,
                        zabbix_ok = EXCLUDED.zabbix_ok,
                        zabbix_message = EXCLUDED.zabbix_message,
                        last_poll_at = COALESCE(EXCLUDED.last_poll_at, service_heartbeat.last_poll_at),
                        last_new = EXCLUDED.last_new,
                        last_closed = EXCLUDED.last_closed,
                        last_retried = EXCLUDED.last_retried,
                        service_host = EXCLUDED.service_host,
                        pid = EXCLUDED.pid
                    """,
                    (service_id, status, message, zabbix_ok, zabbix_message, last_poll_at,
                     last_new, last_closed, last_retried, service_host, pid),
                )
            conn.commit()

    def read(self, service_id: int = 1) -> dict | None:
        with self.db._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT * FROM service_heartbeat WHERE id = %s", (service_id,))
                row = cur.fetchone()
        return dict(row) if row else None

    def read_all(self) -> list[dict]:
        with self.db._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT * FROM service_heartbeat ORDER BY id")
                return [dict(r) for r in cur.fetchall()]


class UserRepository:
    """Учётные записи API. Пароли хранятся только хешем (см. auth.py) —
    сам репозиторий о хешировании не знает, принимает уже готовый хеш."""

    def __init__(self, db: Database):
        self.db = db

    def create(self, username: str, password_hash: str, role: str = "engineer") -> int:
        with self.db._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO users (username, password_hash, role) VALUES (%s, %s, %s) RETURNING id",
                    (username, password_hash, role),
                )
                new_id = cur.fetchone()[0]
            conn.commit()
        return new_id

    def get_by_username(self, username: str) -> dict | None:
        with self.db._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT * FROM users WHERE username = %s", (username,))
                row = cur.fetchone()
        return dict(row) if row else None

    def get_by_id(self, user_id: int) -> dict | None:
        with self.db._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))
                row = cur.fetchone()
        return dict(row) if row else None

    def list_all(self) -> list[dict]:
        with self.db._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT id, username, role, is_active, created_at, last_login_at "
                            "FROM users ORDER BY username")
                return [dict(r) for r in cur.fetchall()]

    def set_active(self, user_id: int, is_active: bool) -> None:
        with self.db._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE users SET is_active = %s WHERE id = %s", (is_active, user_id))
            conn.commit()

    def set_password(self, user_id: int, password_hash: str) -> None:
        with self.db._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE users SET password_hash = %s WHERE id = %s", (password_hash, user_id))
            conn.commit()

    def mark_login(self, user_id: int) -> None:
        with self.db._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE users SET last_login_at = now() WHERE id = %s", (user_id,))
            conn.commit()

    def count(self) -> int:
        with self.db._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM users")
                return cur.fetchone()[0]


class SessionRepository:
    """Токен хранится и ищется по sha256-хешу (см. auth.py.hash_token) —
    сам исходный токен в БД никогда не попадает, аналогично паролю."""

    def __init__(self, db: Database):
        self.db = db

    def create(self, token_hash: str, user_id: int, expires_at, remember_me: bool) -> None:
        with self.db._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO sessions (token_hash, user_id, expires_at, remember_me) "
                    "VALUES (%s, %s, %s, %s)",
                    (token_hash, user_id, expires_at, remember_me),
                )
            conn.commit()

    def get_valid(self, token_hash: str) -> dict | None:
        with self.db._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT s.*, u.username, u.role, u.is_active FROM sessions s "
                    "JOIN users u ON u.id = s.user_id "
                    "WHERE s.token_hash = %s AND s.expires_at > now()",
                    (token_hash,),
                )
                row = cur.fetchone()
        return dict(row) if row else None

    def delete(self, token_hash: str) -> None:
        with self.db._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM sessions WHERE token_hash = %s", (token_hash,))
            conn.commit()

    def delete_expired(self) -> int:
        with self.db._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM sessions WHERE expires_at <= now()")
                deleted = cur.rowcount
            conn.commit()
        return deleted


class JobQueueRepository:
    """Очередь заданий для worker-службы — см. job_worker.py. API-служба
    только создаёт записи и опрашивает их статус, сама LLM не вызывает."""

    def __init__(self, db: Database):
        self.db = db

    def enqueue(self, job_type: str, payload: dict, created_by: int | None = None) -> int:
        with self.db._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO job_queue (job_type, payload, created_by) VALUES (%s, %s, %s) RETURNING id",
                    (job_type, psycopg2.extras.Json(payload), created_by),
                )
                new_id = cur.fetchone()[0]
            conn.commit()
        return new_id

    def get(self, job_id: int) -> dict | None:
        with self.db._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT * FROM job_queue WHERE id = %s", (job_id,))
                row = cur.fetchone()
        return dict(row) if row else None

    def claim_next(self) -> dict | None:
        """Атомарно берёт самое старое 'pending' задание и сразу помечает
        его 'running' — FOR UPDATE SKIP LOCKED делает это безопасным, даже
        если бы воркеров было несколько (сейчас один, но не полагаемся на
        это неявно)."""
        with self.db._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT * FROM job_queue WHERE status = 'pending' "
                    "ORDER BY created_at ASC LIMIT 1 FOR UPDATE SKIP LOCKED"
                )
                row = cur.fetchone()
                if row is None:
                    conn.commit()
                    return None
                cur.execute(
                    "UPDATE job_queue SET status = 'running', started_at = now() WHERE id = %s",
                    (row["id"],),
                )
            conn.commit()
        return dict(row)

    def complete(self, job_id: int, result: dict) -> None:
        with self.db._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE job_queue SET status = 'done', result = %s, finished_at = now() WHERE id = %s",
                    (psycopg2.extras.Json(result), job_id),
                )
            conn.commit()

    def fail(self, job_id: int, error: str) -> None:
        with self.db._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE job_queue SET status = 'error', error = %s, finished_at = now() WHERE id = %s",
                    (error[:2000], job_id),
                )
            conn.commit()

    def purge_old(self, older_than_hours: int = 72) -> int:
        """Чистит старые завершённые задания — очередь не должна расти
        бесконечно (та же логика, что учли для incidents в realtime_engine)."""
        with self.db._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM job_queue WHERE status IN ('done', 'error') "
                    "AND finished_at < now() - (%s || ' hours')::interval",
                    (older_than_hours,),
                )
                deleted = cur.rowcount
            conn.commit()
        return deleted


class IncidentRepository:
    def __init__(self, db: Database):
        self.db = db

    def save_incidents(self, incidents: List[Incident]) -> None:
        """ai_verified и resolution намеренно отсутствуют в списке колонок INSERT —
        Postgres обновляет по ON CONFLICT только перечисленные поля, так что
        повторный анализ того же id никогда не заденет решение, вписанное
        инженером вручную (см. update_correction). saved_at для уже
        проверенных записей тоже не трогаем (CASE ниже) — иначе повторное
        сохранение того же id (повторный опрос, коллизия синтетических id)
        сдвигало бы saved_at на текущий момент и портило расчёт среднего
        времени до проверки в get_verification_stats."""
        with self.db._connect() as conn:
            with conn.cursor() as cur:
                for inc in incidents:
                    cur.execute(
                        """
                        INSERT INTO incidents
                        (id, host, problem_name, severity, timestamp, item_key,
                         last_value, ai_summary, ai_recommendation, saved_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (id) DO UPDATE SET
                            ai_summary = EXCLUDED.ai_summary,
                            ai_recommendation = EXCLUDED.ai_recommendation,
                            saved_at = CASE WHEN incidents.ai_verified
                                THEN incidents.saved_at ELSE EXCLUDED.saved_at END
                        """,
                        (
                            inc.id, inc.host, inc.problem_name, inc.severity.value,
                            inc.timestamp, inc.item_key, inc.last_value,
                            inc.ai_summary, inc.ai_recommendation, datetime.now(),
                        ),
                    )
            conn.commit()

    _ROW_COLUMNS = (
        "id, host, problem_name, severity, timestamp, item_key, "
        "last_value, ai_summary, ai_recommendation, ai_verified, resolution, "
        "resolved_at, opened_at"
    )

    @staticmethod
    def _row_to_incident(r) -> Incident:
        return Incident(
            id=r[0], host=r[1], problem_name=r[2], severity=Severity(r[3]),
            timestamp=r[4], item_key=r[5] or "", last_value=r[6] or "",
            ai_summary=r[7] or "", ai_recommendation=r[8] or "",
            ai_analyzed=bool(r[7]), ai_verified=bool(r[9]), resolution=r[10] or "",
            resolved_at=r[11], opened_at=r[12],
        )

    def get_history(self, limit: int = 200) -> List[Incident]:
        with self.db._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {self._ROW_COLUMNS} FROM incidents ORDER BY timestamp DESC LIMIT %s",
                    (limit,),
                )
                rows = cur.fetchall()
        return [self._row_to_incident(r) for r in rows]

    def get_by_id(self, incident_id: str) -> Incident | None:
        """Для job_worker.py: сравнение «с RAG / без RAG» получает не сам
        объект инцидента (клиент их больше не хранит), а только id — и
        запрашивает актуальную запись сам, на сервере."""
        with self.db._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT {self._ROW_COLUMNS} FROM incidents WHERE id = %s", (incident_id,))
                row = cur.fetchone()
        return self._row_to_incident(row) if row else None

    def get_existing_ids(self, ids: List[str]) -> set:
        """Какие из переданных id уже есть в БД — используется реальным
        временем (realtime_engine.py) для отличения новых инцидентов от уже
        известных без загрузки всей истории в память (см. get_unanalyzed/
        get_open_ids — раньше всё это делалось Python-фильтром по
        get_history(limit=5000), что переставало видеть записи старше этого
        окна, стоило БД расти)."""
        if not ids:
            return set()
        with self.db._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM incidents WHERE id = ANY(%s)", (ids,))
                return {r[0] for r in cur.fetchall()}

    def get_open_ids(self) -> set:
        """Все сейчас открытые (resolved_at IS NULL) id — без ограничения
        на количество, иначе старый ещё активный инцидент вне окна
        get_history(limit=N) никогда не смог бы быть закрыт."""
        with self.db._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM incidents WHERE resolved_at IS NULL")
                return {r[0] for r in cur.fetchall()}

    def get_unanalyzed(self, limit: int = 20) -> List[Incident]:
        """Инциденты без анализа (ai_summary пуст) — САМЫЕ СТАРЫЕ сначала.
        Раньше повтор анализа брал 20 самых свежих неанализированных из
        get_history(limit=5000) — при устойчивом сбое LLM новые проблемные
        пачки постоянно вытесняли старые из повторной попытки, и те
        застревали без анализа навсегда."""
        with self.db._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {self._ROW_COLUMNS} FROM incidents "
                    "WHERE ai_summary IS NULL OR ai_summary = '' "
                    "ORDER BY timestamp ASC LIMIT %s",
                    (limit,),
                )
                rows = cur.fetchall()
        return [self._row_to_incident(r) for r in rows]

    def mark_resolved(self, ids: List[str]) -> None:
        """Проставляет resolved_at тем алертам из списка, которые ещё не были
        закрыты — вызывается, когда опрос Zabbix перестал возвращать их среди
        активных (см. AlertsTab._on_poll_fetched)."""
        if not ids:
            return
        with self.db._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE incidents SET resolved_at = now() "
                    "WHERE id = ANY(%s) AND resolved_at IS NULL",
                    (ids,),
                )
            conn.commit()

    def mark_opened(self, incident_id: str) -> None:
        """Инженер открыл карточку алерта — фиксируем первый факт просмотра,
        независимо от того, закрыт ли уже алерт в Zabbix."""
        with self.db._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE incidents SET opened_at = now() "
                    "WHERE id = %s AND opened_at IS NULL",
                    (incident_id,),
                )
            conn.commit()

    def update_correction(self, incident_id: str, resolution: str) -> None:
        """Инженер вписал, как проблема была решена на самом деле. Хранится
        отдельно от ai_recommendation (вывод модели не трогаем и не теряем) —
        именно resolution приоритетно попадёт в RAG-контекст похожих будущих
        инцидентов, см. IncidentRAG.build_context_block."""
        with self.db._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE incidents SET resolution = %s, ai_verified = TRUE, "
                    "saved_at = now() WHERE id = %s",
                    (resolution, incident_id),
                )
            conn.commit()

    def get_without_embedding(self, limit: int = 500) -> List[dict]:
        """Инциденты без вектора — для кнопки «Переиндексировать историю»."""
        with self.db._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT id, problem_name FROM incidents "
                    "WHERE embedding IS NULL AND ai_summary IS NOT NULL LIMIT %s",
                    (limit,),
                )
                return list(cur.fetchall())

    def get_verification_stats(self) -> dict:
        """Для вкладки «Аналитика»: сколько инцидентов проверено инженером и
        сколько в среднем времени проходит между появлением инцидента и тем,
        как инженер вписал фактическое решение (saved_at обновляется именно
        в update_correction, так что разница — грубый аналог MTTR)."""
        with self.db._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM incidents")
                total = cur.fetchone()[0]
                cur.execute(
                    "SELECT COUNT(*), AVG(EXTRACT(EPOCH FROM (saved_at - timestamp))) "
                    "FROM incidents WHERE ai_verified = TRUE"
                )
                verified_count, avg_seconds = cur.fetchone()
                # Настоящий счётчик проанализированных по всей таблице — раньше
                # DashboardTab считал это как sum(ai_analyzed) по get_history(limit=200),
                # что молча занижало и total, и процент, как только история
                # переросла 200 записей.
                cur.execute("SELECT COUNT(*) FROM incidents WHERE ai_summary IS NOT NULL AND ai_summary != ''")
                analyzed_count = cur.fetchone()[0]
        return {
            "total": total,
            "verified_count": verified_count or 0,
            "avg_seconds": float(avg_seconds) if avg_seconds is not None else 0.0,
            "analyzed_count": analyzed_count,
        }

    def get_stats_by_host(self) -> dict:
        with self.db._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT host, COUNT(*) FROM incidents GROUP BY host ORDER BY COUNT(*) DESC"
                )
                return dict(cur.fetchall())

    def get_critical_count(self) -> int:
        """Для chat_query.py — честное 'по всей истории' вместо подсчёта по
        обрезанному списку. Severity.HIGH=4, Severity.DISASTER=5."""
        with self.db._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM incidents WHERE severity IN (4, 5)")
                return cur.fetchone()[0]

    def get_distinct_hosts(self) -> List[str]:
        """Все имена хостов, встречавшиеся в истории — для chat_query.py
        (распознавание хоста в вопросе): раньше список хостов строился из
        get_history(limit=5000), так что хост, все инциденты которого старше
        первых 5000 строк, вообще не распознавался бы в тексте вопроса."""
        with self.db._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT DISTINCT host FROM incidents")
                return [r[0] for r in cur.fetchall()]

    def search(self, since, host: str | None = None, limit: int = 5000) -> List[Incident]:
        """Фильтрация по дате/хосту в самой БД (а не Python-фильтром по уже
        обрезанному get_history(limit=N)) — для chat_query.py, где иначе
        совпадения старше первых N строк истории просто не находились бы,
        даже если реально существуют."""
        query = f"SELECT {self._ROW_COLUMNS} FROM incidents WHERE timestamp >= %s"
        params: list = [since]
        if host:
            query += " AND host = %s"
            params.append(host)
        query += " ORDER BY timestamp DESC LIMIT %s"
        params.append(limit)
        with self.db._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(query, params)
                rows = cur.fetchall()
        return [self._row_to_incident(r) for r in rows]

    def clear_all(self) -> int:
        with self.db._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM incidents")
                deleted = cur.rowcount
            conn.commit()
        return deleted


class ConfigDiffRepository:
    def __init__(self, db: Database):
        self.db = db

    def save_diff(self, node: str, version_from: str, version_to: str, diff_text: str,
                  ai_summary: str = "", ai_risk_level: str = "", ai_recommendation: str = "",
                  review_type: str = "diff") -> int:
        """Возвращает id новой записи — нужен для последующего сохранения embedding."""
        with self.db._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO config_diffs
                    (node, review_type, version_from, version_to, diff_text,
                     ai_summary, ai_risk_level, ai_recommendation)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (node, review_type, version_from, version_to, diff_text,
                     ai_summary, ai_risk_level, ai_recommendation),
                )
                new_id = cur.fetchone()[0]
            conn.commit()
        return new_id

    def get_history(self, limit: int = 100, review_type: str | None = None) -> List[dict]:
        query = "SELECT * FROM config_diffs"
        params: tuple = ()
        if review_type:
            query += " WHERE review_type = %s"
            params = (review_type,)
        query += " ORDER BY saved_at DESC LIMIT %s"
        params = params + (limit,)

        with self.db._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(query, params)
                return list(cur.fetchall())

    def get_by_id(self, row_id: int) -> dict | None:
        with self.db._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT * FROM config_diffs WHERE id = %s", (row_id,))
                row = cur.fetchone()
        return dict(row) if row else None

    def get_without_embedding(self, limit: int = 500) -> List[dict]:
        with self.db._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT id, diff_text FROM config_diffs "
                    "WHERE embedding IS NULL AND ai_summary IS NOT NULL LIMIT %s",
                    (limit,),
                )
                return list(cur.fetchall())

    def get_risk_counts(self) -> dict:
        """Для chat_query.py — честное 'по всей истории' вместо подсчёта по
        обрезанному списку."""
        with self.db._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COALESCE(UPPER(ai_risk_level), 'UNKNOWN'), COUNT(*) "
                    "FROM config_diffs GROUP BY 1"
                )
                return dict(cur.fetchall())

    def get_distinct_nodes(self) -> List[str]:
        """Для chat_query.py — см. IncidentRepository.get_distinct_hosts,
        тот же смысл: не терять распознавание узла в тексте вопроса из-за
        обрезки get_history(limit=N)."""
        with self.db._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT DISTINCT node FROM config_diffs")
                return [r[0] for r in cur.fetchall()]

    def search(self, since, node: str | None = None, limit: int = 5000) -> List[dict]:
        """Фильтрация по дате/узлу в самой БД — см. IncidentRepository.search."""
        query = "SELECT * FROM config_diffs WHERE saved_at >= %s"
        params: list = [since]
        if node:
            query += " AND node = %s"
            params.append(node)
        query += " ORDER BY saved_at DESC LIMIT %s"
        params.append(limit)
        with self.db._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(query, params)
                return list(cur.fetchall())

    def update_correction(self, row_id: int, resolution: str) -> None:
        """Инженер вписал фактическое решение по конфигурации — хранится отдельно
        от вывода модели (ai_summary/ai_recommendation), чтобы то и другое
        оставалось видно одновременно и не перезаписывало друг друга."""
        with self.db._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE config_diffs SET resolution = %s, ai_verified = TRUE, "
                    "saved_at = now() WHERE id = %s",
                    (resolution, row_id),
                )
            conn.commit()

    def clear_all(self) -> int:
        with self.db._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM config_diffs")
                deleted = cur.rowcount
            conn.commit()
        return deleted