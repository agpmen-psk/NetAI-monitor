"""
rag.py — извлечение похожих прошлых случаев (RAG) для обогащения промптов
LLM историческим опытом без дообучения весов модели.
Требует: расширение pgvector в PostgreSQL + модель nomic-embed-text в Ollama.
"""
from __future__ import annotations

import requests
from typing import List


class EmbeddingClient:
    """Генерирует векторные представления текста через локальную модель Ollama."""

    def __init__(self, host, model):
        self.host = host.rstrip("/")
        self.model = model

    def is_available(self) -> bool:
        try:
            resp = requests.post(
                f"{self.host}/api/embeddings",
                json={"model": self.model, "prompt": "test"},
                timeout=5,
            )
            return resp.status_code == 200
        except Exception:
            return False

    def embed(self, text: str) -> List[float]:
        response = requests.post(
            f"{self.host}/api/embeddings",
            json={"model": self.model, "prompt": text},
            timeout=30,
        )
        response.raise_for_status()
        return response.json()["embedding"]


class IncidentRAG:
    """Поиск похожих прошлых инцидентов по косинусному расстоянию (pgvector)."""

    def __init__(self, db, embedder: EmbeddingClient):
        self.db = db
        self.embedder = embedder

    def save_embedding(self, incident_id: str, text: str) -> None:
        vector = self.embedder.embed(text)
        with self.db._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE incidents SET embedding = %s WHERE id = %s", (vector, incident_id))
            conn.commit()

    def find_similar(self, problem_text: str, limit: int = 3, max_distance: float = 0.4) -> List[dict]:
        """ai_verified DESC — исправленные инженером записи всплывают первыми
        при прочих равных (это и есть «обучение на ошибках» через RAG, а не
        через дообучение весов). max_distance отсекает случаи, которые pgvector
        формально считает «ближайшими», но которые на деле почти не похожи —
        раньше в контекст всегда попадали топ-3 записи независимо от того,
        насколько они вообще релевантны, что могло сбивать модель с толку
        при маленькой/разнородной истории."""
        query_vector = self.embedder.embed(problem_text)
        with self.db._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT host, problem_name, ai_summary, ai_recommendation, ai_verified, resolution,
                           embedding <=> %s::vector AS distance
                    FROM incidents
                    WHERE embedding IS NOT NULL
                    ORDER BY ai_verified DESC, distance ASC
                    LIMIT %s
                    """,
                    (query_vector, limit * 3),
                )
                rows = cur.fetchall()
        results = [
            {"host": r[0], "problem_name": r[1], "summary": r[2], "recommendation": r[3],
             "verified": bool(r[4]), "resolution": r[5], "distance": r[6]}
            for r in rows if r[6] <= max_distance
        ]
        return results[:limit]

    def build_context_block(self, problem_text: str) -> str:
        similar = self.find_similar(problem_text)
        if not similar:
            return ""
        lines = ["Похожие случаи из истории (используй как контекст, если релевантно):"]
        for s in similar:
            # Если инженер вписал фактическое решение — используем его как приоритетный
            # ориентир вместо исходной рекомендации модели («обучение на ошибках»).
            if s["verified"] and s.get("resolution"):
                rec = s["resolution"][:200]
                tag = " [фактическое решение инженера — приоритетный ориентир]"
            else:
                rec = (s["recommendation"] or "")[:200]
                tag = ""
            lines.append(f"- {s['problem_name']} на {s['host']} -> решение{tag}: {rec}")
        return "\n".join(lines) + "\n\n"

    def reindex_missing(self, repo) -> int:
        """Считает embedding для всех инцидентов, у которых его ещё нет."""
        pending = repo.get_without_embedding()
        for row in pending:
            self.save_embedding(row["id"], row["problem_name"])
        return len(pending)


class ConfigRAG:
    """Аналогичный поиск похожих случаев, но для проверок конфигураций."""

    def __init__(self, db, embedder: EmbeddingClient):
        self.db = db
        self.embedder = embedder

    def save_embedding(self, config_id: int, text: str) -> None:
        vector = self.embedder.embed(text)
        with self.db._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE config_diffs SET embedding = %s WHERE id = %s", (vector, config_id))
            conn.commit()

    def find_similar(self, config_text: str, limit: int = 3, max_distance: float = 0.4) -> List[dict]:
        query_vector = self.embedder.embed(config_text)
        with self.db._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT node, ai_summary, ai_recommendation, ai_risk_level, ai_verified, resolution,
                           embedding <=> %s::vector AS distance
                    FROM config_diffs
                    WHERE embedding IS NOT NULL
                    ORDER BY ai_verified DESC, distance ASC
                    LIMIT %s
                    """,
                    (query_vector, limit * 3),
                )
                rows = cur.fetchall()
        results = [
            {"node": r[0], "summary": r[1], "recommendation": r[2], "risk_level": r[3],
             "verified": bool(r[4]), "resolution": r[5], "distance": r[6]}
            for r in rows if r[6] <= max_distance
        ]
        return results[:limit]

    def build_context_block(self, config_text: str) -> str:
        similar = self.find_similar(config_text)
        if not similar:
            return ""
        lines = ["Похожие проверки конфигураций из истории (используй как контекст, если релевантно):"]
        for s in similar:
            if s["verified"] and s.get("resolution"):
                rec = s["resolution"][:200]
                tag = " [фактическое решение инженера]"
            else:
                rec = (s["recommendation"] or "")[:200]
                tag = ""
            lines.append(f"- {s['node']} (риск {s['risk_level']}){tag} -> {rec}")
        return "\n".join(lines) + "\n\n"

    def reindex_missing(self, repo) -> int:
        pending = repo.get_without_embedding()
        for row in pending:
            self.save_embedding(row["id"], row["diff_text"])
        return len(pending)