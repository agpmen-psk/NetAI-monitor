"""
job_worker.py — служба обработки очереди LLM-заданий.

Десктоп-клиент больше не вызывает LLM или Oxidized напрямую (тонкий
клиент, см. api_client.py) — вместо этого API-служба кладёт задание в
таблицу job_queue, а эта служба его забирает, выполняет (может занять
30-120 секунд — аудит конфига, сравнение «с RAG / без RAG» и т.п.) и
записывает результат обратно. Клиент периодически опрашивает статус
задания через API.

Это ВТОРАЯ из двух независимых служб на сервере — первая, realtime_service.py,
опрашивает Zabbix. Разделены специально: долгий LLM-запрос в очереди не
должен задерживать опрос Zabbix, и падение одной не тянет за собой другую.

Типы заданий (job_type → payload → result):
  config_diff_all        {}                              → {"count": N}
  config_diff_single      {"node"}                        → {"row_id", "node", "risk_level"}
  config_full_audit       {"node"}                        → {"row_id", "node", "risk_level"}
  config_submit_external  {"node","review_type","text"}   → {"row_id", "node", "risk_level"}
  incident_submit_external {"id","host","problem_name",
                             "severity","timestamp",
                             "item_key","last_value"}      → {"id", "summary",
                                                                "recommendation", "duplicate_note"}
  rag_comparison_incident {"incident_id"}                 → {with/without summary+recommendation, rag_context}
  rag_comparison_config   {"config_id"}                   → {with/without summary+recommendation, rag_context}
  reindex_embeddings      {}                               → {"incidents": N, "configs": M}

Запуск вручную:
    python job_worker.py
Установка службой — см. tools/install_service.ps1 (тот же скрипт, второй
вызов с именем службы NetAIMonitorJobWorker) и DEPLOYMENT.md.
"""
from __future__ import annotations

import copy
import logging
import os
import signal
import socket
import sys
import time
from datetime import datetime

from models import Incident, Severity
from llm_client import get_analyzer
from rag import EmbeddingClient, IncidentRAG, ConfigRAG
from mock_oxidized_client import MockOxidizedClient
from oxidized_client import OxidizedClient
from service_config import ensure_config_file, load_service_config, resolve_synthetic_mode, twin_topology_path
from topology import Topology
from digital_twin import TwinOxidizedClient
from service_common import (
    setup_logging, acquire_single_instance_lock, connect_db_with_retry, sleep_with_interrupt,
)

log = logging.getLogger("job-worker")
_running = True

POLL_INTERVAL_SECONDS = 3  # очередь заданий — интерактивный сценарий (клиент ждёт), опрашиваем чаще, чем Zabbix
EMPTY_QUEUE_LOG_EVERY = 200  # не засорять лог "очередь пуста" каждые 3 секунды


def _handle_stop(signum, frame) -> None:
    global _running
    log.info("Получен сигнал остановки — завершаюсь после текущего задания...")
    _running = False


def _build_oxidized_client(config: dict):
    mode = resolve_synthetic_mode(config)
    if mode == "twin":
        topology_path = twin_topology_path(config)
        if not topology_path.exists():
            log.error(
                "Режим цифрового двойника включён, но файл топологии не найден: %s",
                topology_path,
            )
            topology = Topology(sites={}, redundancy_groups={}, nodes=[])
        else:
            topology = Topology.load(topology_path)
        return TwinOxidizedClient(topology=topology)
    if mode == "flat":
        return MockOxidizedClient(seed=42)
    return OxidizedClient(base_url=config["oxidized_url"])


class JobHandlers:
    """Группирует зависимости (репозитории, анализатор, RAG, Oxidized),
    которые нужны всем обработчикам — чтобы не тащить их по одному в
    каждую функцию."""

    def __init__(self, backend, analyzer, incident_rag, config_rag, oxidized_client):
        self.backend = backend
        self.analyzer = analyzer
        self.incident_rag = incident_rag
        self.config_rag = config_rag
        self.oxidized_client = oxidized_client

    def dispatch(self, job: dict) -> dict:
        handler = getattr(self, f"_handle_{job['job_type']}", None)
        if handler is None:
            raise ValueError(f"неизвестный тип задания: {job['job_type']}")
        return handler(job["payload"])

    # --- Конфигурации -------------------------------------------------------

    def _save_config_result(self, node: str, diff_text: str, review_type: str, review: dict,
                             version_from: str = "", version_to: str = "") -> dict:
        row_id = self.backend.configs.save_diff(
            node=node, version_from=version_from, version_to=version_to, diff_text=diff_text,
            ai_summary=review["summary"], ai_risk_level=review["risk_level"],
            ai_recommendation=review["recommendation"], review_type=review_type,
        )
        if self.config_rag is not None:
            try:
                self.config_rag.save_embedding(row_id, diff_text)
            except Exception:
                log.warning("Не удалось сохранить embedding для config_diffs id=%s", row_id, exc_info=True)
        return {"row_id": row_id, "node": node, "risk_level": review["risk_level"]}

    def _handle_config_diff_single(self, payload: dict) -> dict:
        node = payload["node"]
        d = self.oxidized_client.get_diff(node, "prev", "latest")
        review = self.analyzer.analyze_config_diff(d.diff_text, rag=self.config_rag)
        return self._save_config_result(node, d.diff_text, "diff", review,
                                         version_from="prev", version_to="latest")

    def _handle_config_full_audit(self, payload: dict) -> dict:
        node = payload["node"]
        config_text = self.oxidized_client.get_current_config(node)
        review = self.analyzer.analyze_full_config(config_text, rag=self.config_rag)
        return self._save_config_result(node, config_text, "full_audit", review)

    def _handle_config_diff_all(self, payload: dict) -> dict:
        diffs = self.oxidized_client.get_all_diffs()
        count = 0
        for d in diffs:
            review = self.analyzer.analyze_config_diff(d.diff_text, rag=self.config_rag)
            self._save_config_result(d.node, d.diff_text, "diff", review,
                                      version_from="prev", version_to="latest")
            count += 1
        return {"count": count}

    def _handle_config_submit_external(self, payload: dict) -> dict:
        """Из Генератора (десктоп-клиент): текст diff/конфига введён
        оператором вручную, Oxidized не задействован — тот же путь, что
        MockOxidizedClient использовал раньше локально в GUI."""
        node, review_type, text = payload["node"], payload["review_type"], payload["text"]
        analyze_fn = (self.analyzer.analyze_full_config if review_type == "full_audit"
                      else self.analyzer.analyze_config_diff)
        review = analyze_fn(text, rag=self.config_rag)
        return self._save_config_result(node, text, review_type, review)

    def _handle_rag_comparison_config(self, payload: dict) -> dict:
        row = self.backend.configs.get_by_id(payload["config_id"])
        if row is None:
            raise ValueError(f"config_diffs id={payload['config_id']} не найден")
        review_type = row.get("review_type", "diff")
        text = row["diff_text"]
        analyze_fn = (self.analyzer.analyze_full_config if review_type == "full_audit"
                      else self.analyzer.analyze_config_diff)
        with_rag = analyze_fn(text, rag=self.config_rag)
        without_rag = analyze_fn(text, rag=None)
        rag_context = ""
        if self.config_rag is not None:
            try:
                rag_context = self.config_rag.build_context_block(text)
            except Exception:
                rag_context = ""
        return {
            "title": row.get("node", ""),
            "subtitle": f"риск с RAG: {with_rag.get('risk_level', '?')}  ·  "
                        f"риск без RAG: {without_rag.get('risk_level', '?')}",
            "with_summary": with_rag.get("summary", ""),
            "with_recommendation": with_rag.get("recommendation", ""),
            "without_summary": without_rag.get("summary", ""),
            "without_recommendation": without_rag.get("recommendation", ""),
            "rag_context": rag_context,
        }

    # --- Инциденты ------------------------------------------------------------

    # Заметно строже общего RAG-порога релевантности контекста промпта
    # (см. rag.py max_distance=0.4 по умолчанию) — для «это дубликат» нужна
    # почти точная идентичность текста, а не просто тематическая похожесть.
    # Раньше эту проверку делал клиент сам (у него был прямой доступ к RAG);
    # теперь RAG есть только у сервера, поэтому проверка переехала сюда.
    DUPLICATE_MAX_DISTANCE = 0.08

    def _check_duplicate(self, inc: Incident) -> str | None:
        if self.incident_rag is None:
            return None
        try:
            similar = self.incident_rag.find_similar(
                inc.problem_name, limit=3, max_distance=self.DUPLICATE_MAX_DISTANCE,
            )
        except Exception:
            return None
        for s in similar:
            if s["host"] == inc.host:
                return f"похоже на повтор случая «{s['problem_name']}» на {s['host']} из истории"
        return None

    def _handle_incident_submit_external(self, payload: dict) -> dict:
        """Из Генератора: один синтетический инцидент, введённый оператором
        вручную — анализируется и сохраняется так же, как обычный новый
        инцидент из опроса Zabbix (просто минуя realtime_engine)."""
        inc = Incident(
            id=payload["id"], host=payload["host"], problem_name=payload["problem_name"],
            severity=Severity(int(payload["severity"])), timestamp=datetime.fromisoformat(payload["timestamp"]),
            item_key=payload.get("item_key", ""), last_value=payload.get("last_value", ""),
        )
        # До сохранения/эмбеддинга — иначе новый инцидент уже был бы в базе
        # и находил бы сам себя с нулевой дистанцией.
        duplicate_note = self._check_duplicate(inc)
        self.analyzer.analyze([inc], rag=self.incident_rag)
        self.backend.incidents.save_incidents([inc])
        if self.incident_rag is not None and inc.ai_analyzed:
            try:
                self.incident_rag.save_embedding(inc.id, inc.problem_name)
            except Exception:
                log.warning("Не удалось сохранить embedding для инцидента %s", inc.id, exc_info=True)
        return {
            "id": inc.id, "analyzed": inc.ai_analyzed,
            "summary": inc.ai_summary, "recommendation": inc.ai_recommendation,
            "duplicate_note": duplicate_note,
        }

    def _handle_rag_comparison_incident(self, payload: dict) -> dict:
        inc = self.backend.incidents.get_by_id(payload["incident_id"])
        if inc is None:
            raise ValueError(f"инцидент {payload['incident_id']} не найден")
        inc_with = copy.deepcopy(inc)
        inc_without = copy.deepcopy(inc)
        self.analyzer.analyze([inc_with], rag=self.incident_rag)
        self.analyzer.analyze([inc_without], rag=None)
        rag_context = ""
        if self.incident_rag is not None:
            try:
                rag_context = self.incident_rag.build_context_block(inc.problem_name)
            except Exception:
                rag_context = ""
        return {
            "title": inc.host,
            "subtitle": inc.problem_name,
            "with_summary": inc_with.ai_summary,
            "with_recommendation": inc_with.ai_recommendation,
            "without_summary": inc_without.ai_summary,
            "without_recommendation": inc_without.ai_recommendation,
            "rag_context": rag_context,
        }

    # --- RAG-обслуживание -------------------------------------------------

    def _handle_reindex_embeddings(self, payload: dict) -> dict:
        inc_count = cfg_count = 0
        if self.incident_rag is not None:
            for row in self.backend.incidents.get_without_embedding(limit=500):
                try:
                    self.incident_rag.save_embedding(row["id"], row["problem_name"])
                    inc_count += 1
                except Exception:
                    log.warning("Переиндексация: инцидент %s пропущен", row.get("id"), exc_info=True)
        if self.config_rag is not None:
            for row in self.backend.configs.get_without_embedding(limit=500):
                try:
                    self.config_rag.save_embedding(row["id"], row["diff_text"])
                    cfg_count += 1
                except Exception:
                    log.warning("Переиндексация: конфигурация %s пропущена", row.get("id"), exc_info=True)
        return {"incidents": inc_count, "configs": cfg_count}


def main() -> None:
    setup_logging("job-worker", "job_worker.log")

    config_file, created = ensure_config_file()
    if created:
        log.warning("Создан файл настроек службы: %s — заполните и перезапустите.", config_file)

    lock = acquire_single_instance_lock("job_worker.lock")
    if lock is None:
        log.error("Служба уже запущена (блокировка занята другим процессом) — завершаюсь.")
        sys.exit(1)

    signal.signal(signal.SIGINT, _handle_stop)
    signal.signal(signal.SIGTERM, _handle_stop)

    config = load_service_config()
    backend = connect_db_with_retry(config["postgres_dsn"], lambda: _running, log)
    if backend is None:
        log.info("Остановлено до подключения к БД.")
        return

    analyzer = get_analyzer(config["ollama_host"], config["ollama_model"])
    log.info("Анализатор: %s", type(analyzer).__name__)

    incident_rag = config_rag = None
    embedder = EmbeddingClient(host=config["ollama_host"], model=config["embedding_model"])
    if embedder.is_available():
        incident_rag = IncidentRAG(backend.db, embedder)
        config_rag = ConfigRAG(backend.db, embedder)
        log.info("RAG подключён.")
    else:
        log.info("RAG недоступен (нет модели эмбеддингов) — работаю без него.")

    oxidized_client = _build_oxidized_client(config)
    handlers = JobHandlers(backend, analyzer, incident_rag, config_rag, oxidized_client)

    host_name = socket.gethostname()
    pid = os.getpid()

    def heartbeat(status: str, message: str = "") -> None:
        try:
            backend.service_status.write(
                service_id=backend.service_status.JOB_WORKER,
                status=status, message=message, service_host=host_name, pid=pid,
            )
        except Exception as e:
            log.warning("Не удалось записать heartbeat: %s", e)

    heartbeat("idle", "служба запущена, очередь пуста")
    log.info("Job-worker запущен. Опрос очереди каждые %d сек.", POLL_INTERVAL_SECONDS)

    empty_ticks = 0
    while _running:
        job = backend.jobs.claim_next()
        if job is None:
            empty_ticks += 1
            if empty_ticks % EMPTY_QUEUE_LOG_EVERY == 1:
                log.info("Очередь пуста, жду задания...")
                heartbeat("idle", "очередь пуста")
            sleep_with_interrupt(POLL_INTERVAL_SECONDS, lambda: _running)
            continue

        empty_ticks = 0
        log.info("Задание #%d: %s", job["id"], job["job_type"])
        heartbeat("running", f"выполняю задание #{job['id']} ({job['job_type']})")
        try:
            result = handlers.dispatch(job)
            backend.jobs.complete(job["id"], result)
            log.info("Задание #%d выполнено.", job["id"])
            heartbeat("idle", f"задание #{job['id']} выполнено")
        except Exception as e:
            log.exception("Задание #%d провалилось: %s", job["id"], e)
            backend.jobs.fail(job["id"], str(e))
            heartbeat("error", f"задание #{job['id']} провалилось: {str(e)[:200]}")

        # Периодическая чистка старых завершённых заданий — очередь не
        # должна расти бесконечно (та же логика, что и для incidents).
        if job["id"] % 50 == 0:
            try:
                backend.jobs.purge_old()
            except Exception:
                log.warning("Не удалось почистить старые задания.", exc_info=True)

    heartbeat("stopped", "служба остановлена")
    log.info("Job-worker остановлен.")


if __name__ == "__main__":
    main()
