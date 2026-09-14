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

    def get_history(self, limit: int = 200) -> List[Incident]:
        with self.db._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, host, problem_name, severity, timestamp, item_key, "
                    "last_value, ai_summary, ai_recommendation, ai_verified, resolution, "
                    "resolved_at, opened_at FROM incidents "
                    "ORDER BY timestamp DESC LIMIT %s",
                    (limit,),
                )
                rows = cur.fetchall()

        return [
            Incident(
                id=r[0], host=r[1], problem_name=r[2], severity=Severity(r[3]),
                timestamp=r[4], item_key=r[5] or "", last_value=r[6] or "",
                ai_summary=r[7] or "", ai_recommendation=r[8] or "",
                ai_analyzed=bool(r[7]), ai_verified=bool(r[9]), resolution=r[10] or "",
                resolved_at=r[11], opened_at=r[12],
            )
            for r in rows
        ]

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
        return {
            "total": total,
            "verified_count": verified_count or 0,
            "avg_seconds": float(avg_seconds) if avg_seconds is not None else 0.0,
        }

    def get_stats_by_host(self) -> dict:
        with self.db._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT host, COUNT(*) FROM incidents GROUP BY host ORDER BY COUNT(*) DESC"
                )
                return dict(cur.fetchall())

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

    def get_without_embedding(self, limit: int = 500) -> List[dict]:
        with self.db._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT id, diff_text FROM config_diffs "
                    "WHERE embedding IS NULL AND ai_summary IS NOT NULL LIMIT %s",
                    (limit,),
                )
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