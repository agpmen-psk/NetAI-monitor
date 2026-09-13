"""
db_sqlite.py — офлайн-хранилище на SQLite. Используется автоматически, если
PostgreSQL недоступен (см. backend.py) — например, на компьютере комиссии на
защите диплома, где боевая инфраструктура не развёрнута.

Повторяет публичный интерфейс классов из db.py (Database/IncidentRepository/
ConfigDiffRepository/SettingsRepository), поэтому gui.py работает с любым из
двух бэкендов без изменений. Не поддерживает pgvector — RAG в этом режиме
недоступен (см. backend.py: incident_rag/config_rag остаются None), но
синтетические данные, анализ (Ollama или RuleBasedAnalyzer) и вся остальная
функциональность работают как обычно.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import List

from models import Incident, Severity

SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    id              TEXT PRIMARY KEY,
    host            TEXT NOT NULL,
    problem_name    TEXT NOT NULL,
    severity        INTEGER NOT NULL,
    timestamp       TEXT NOT NULL,
    item_key        TEXT,
    last_value      TEXT,
    ai_summary      TEXT,
    ai_recommendation TEXT,
    ai_verified     INTEGER NOT NULL DEFAULT 0,
    resolution      TEXT,
    saved_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS config_diffs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    node            TEXT NOT NULL,
    review_type     TEXT NOT NULL DEFAULT 'diff',
    version_from    TEXT,
    version_to      TEXT,
    diff_text       TEXT NOT NULL,
    ai_summary      TEXT,
    ai_risk_level   TEXT,
    ai_recommendation TEXT,
    ai_verified     INTEGER NOT NULL DEFAULT 0,
    resolution      TEXT,
    saved_at        TEXT NOT NULL
);

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


def default_sqlite_path() -> str:
    """Кладём файл БД в %APPDATA%/NetAI Monitor — так собранный .exe пишет
    данные в стандартное пользовательское место, а не рядом с собой (куда
    может не быть прав на запись после установки в Program Files)."""
    base = Path(os.environ.get("APPDATA", ".")) / "NetAI Monitor"
    base.mkdir(parents=True, exist_ok=True)
    return str(base / "netai_local.db")


class SQLiteDatabase:
    def __init__(self, path: str | None = None):
        self.path = path or default_sqlite_path()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            for key, value in DEFAULT_SETTINGS.items():
                conn.execute(
                    "INSERT OR IGNORE INTO app_settings (key, value) VALUES (?, ?)",
                    (key, value),
                )
            conn.commit()


class SQLiteSettingsRepository:
    def __init__(self, db: SQLiteDatabase):
        self.db = db

    def get_all(self) -> dict:
        with self.db._connect() as conn:
            rows = conn.execute("SELECT key, value FROM app_settings").fetchall()
        return dict(rows)

    def set(self, key: str, value: str) -> None:
        with self.db._connect() as conn:
            conn.execute(
                "INSERT INTO app_settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            conn.commit()

    def set_many(self, values: dict) -> None:
        for key, value in values.items():
            self.set(key, value)


class SQLiteIncidentRepository:
    def __init__(self, db: SQLiteDatabase):
        self.db = db

    def save_incidents(self, incidents: List[Incident]) -> None:
        with self.db._connect() as conn:
            for inc in incidents:
                conn.execute(
                    """
                    INSERT INTO incidents
                    (id, host, problem_name, severity, timestamp, item_key,
                     last_value, ai_summary, ai_recommendation, saved_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        ai_summary = excluded.ai_summary,
                        ai_recommendation = excluded.ai_recommendation,
                        saved_at = CASE WHEN incidents.ai_verified
                            THEN incidents.saved_at ELSE excluded.saved_at END
                    """,
                    (
                        inc.id, inc.host, inc.problem_name, inc.severity.value,
                        inc.timestamp.isoformat(), inc.item_key, inc.last_value,
                        inc.ai_summary, inc.ai_recommendation, datetime.now().isoformat(),
                    ),
                )
            conn.commit()

    def get_history(self, limit: int = 200) -> List[Incident]:
        with self.db._connect() as conn:
            rows = conn.execute(
                "SELECT id, host, problem_name, severity, timestamp, item_key, "
                "last_value, ai_summary, ai_recommendation, ai_verified, resolution "
                "FROM incidents ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            Incident(
                id=r[0], host=r[1], problem_name=r[2], severity=Severity(r[3]),
                timestamp=datetime.fromisoformat(r[4]), item_key=r[5] or "", last_value=r[6] or "",
                ai_summary=r[7] or "", ai_recommendation=r[8] or "",
                ai_analyzed=bool(r[7]), ai_verified=bool(r[9]), resolution=r[10] or "",
            )
            for r in rows
        ]

    def update_correction(self, incident_id: str, resolution: str) -> None:
        with self.db._connect() as conn:
            conn.execute(
                "UPDATE incidents SET resolution = ?, ai_verified = 1, saved_at = ? WHERE id = ?",
                (resolution, datetime.now().isoformat(), incident_id),
            )
            conn.commit()

    def get_without_embedding(self, limit: int = 500) -> List[dict]:
        return []  # RAG/pgvector недоступны в офлайн-режиме

    def get_stats_by_host(self) -> dict:
        with self.db._connect() as conn:
            rows = conn.execute(
                "SELECT host, COUNT(*) FROM incidents GROUP BY host ORDER BY COUNT(*) DESC"
            ).fetchall()
        return dict(rows)

    def get_verification_stats(self) -> dict:
        with self.db._connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0]
            verified_count, avg_seconds = conn.execute(
                "SELECT COUNT(*), AVG((julianday(saved_at) - julianday(timestamp)) * 86400.0) "
                "FROM incidents WHERE ai_verified = 1"
            ).fetchone()
        return {
            "total": total,
            "verified_count": verified_count or 0,
            "avg_seconds": float(avg_seconds) if avg_seconds is not None else 0.0,
        }

    def clear_all(self) -> int:
        with self.db._connect() as conn:
            cur = conn.execute("DELETE FROM incidents")
            conn.commit()
            return cur.rowcount


class SQLiteConfigDiffRepository:
    def __init__(self, db: SQLiteDatabase):
        self.db = db

    def save_diff(self, node: str, version_from: str, version_to: str, diff_text: str,
                  ai_summary: str = "", ai_risk_level: str = "", ai_recommendation: str = "",
                  review_type: str = "diff") -> int:
        with self.db._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO config_diffs
                (node, review_type, version_from, version_to, diff_text,
                 ai_summary, ai_risk_level, ai_recommendation, saved_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (node, review_type, version_from, version_to, diff_text,
                 ai_summary, ai_risk_level, ai_recommendation, datetime.now().isoformat()),
            )
            conn.commit()
            return cur.lastrowid

    def get_history(self, limit: int = 100, review_type: str | None = None) -> List[dict]:
        query = "SELECT * FROM config_diffs"
        params: tuple = ()
        if review_type:
            query += " WHERE review_type = ?"
            params = (review_type,)
        query += " ORDER BY saved_at DESC LIMIT ?"
        params = params + (limit,)

        with self.db._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(query, params).fetchall()
        results = [dict(r) for r in rows]
        # gui.py ожидает saved_at как datetime (как отдаёт psycopg2), а не строку —
        # приводим тип, чтобы код графиков/аналитики был одинаковым для обоих бэкендов.
        for r in results:
            if isinstance(r.get("saved_at"), str):
                r["saved_at"] = datetime.fromisoformat(r["saved_at"])
            r["ai_verified"] = bool(r.get("ai_verified"))
        return results

    def get_without_embedding(self, limit: int = 500) -> List[dict]:
        return []

    def update_correction(self, row_id: int, resolution: str) -> None:
        with self.db._connect() as conn:
            conn.execute(
                "UPDATE config_diffs SET resolution = ?, ai_verified = 1, saved_at = ? WHERE id = ?",
                (resolution, datetime.now().isoformat(), row_id),
            )
            conn.commit()

    def clear_all(self) -> int:
        with self.db._connect() as conn:
            cur = conn.execute("DELETE FROM config_diffs")
            conn.commit()
            return cur.rowcount
