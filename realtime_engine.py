"""
realtime_engine.py — логика одного цикла опроса Zabbix в реальном времени:
опрашивает активные проблемы, анализирует новые (и ранее не распарсившиеся)
через LLM, определяет закрытие проблем (пропали из активных) и пишет всё
в репозиторий инцидентов.

Вынесена из gui.py в отдельный модуль без зависимостей от Qt специально
для того, чтобы одна и та же логика работала и в десктоп-приложении, и в
фоновом сервисе (см. realtime_service.py) — раньше опрос жил только внутри
AlertsTab, поэтому анализ происходил только пока у кого-то был открыт
десктоп-клиент. Сервис использует именно этот модуль как основной источник
данных; GUI теперь только читает результат из общей БД (см. AlertsTab).
"""
from __future__ import annotations

from typing import List

from models import Incident


class RealtimeEngine:
    """Один экземпляр — один источник (Zabbix/мок) + один репозиторий
    инцидентов. tick() выполняет один цикл опроса и возвращает сводку
    изменений — вызывающий код (сервис или GUI) решает, что с ней делать
    (залогировать, показать тост и т.п.)."""

    # Сколько ранее не распарсившихся (ai_analyzed=False) инцидентов
    # повторно подавать в анализ за один тик — ограничение, чтобы одна
    # проблемная пачка не растягивала каждый следующий цикл опроса.
    RETRY_LIMIT = 20

    def __init__(self, zabbix_client, analyzer, repo, rag=None):
        self.zabbix_client = zabbix_client
        self.analyzer = analyzer
        self.repo = repo
        self.rag = rag
        # id, которые хоть раз реально приходили из опроса Zabbix — только
        # они кандидаты на автозакрытие, когда пропадают из свежего среза.
        # Вручную добавленные (например, из Центра генерации в десктоп-
        # приложении) никогда сюда не попадают и никогда не закрываются
        # автоматически — Zabbix о них ничего не знает.
        self._polled_ids: set[str] = set()
        self._seeded = False

    def seed(self) -> None:
        """Заполняет множество "уже известных активных" из истории в БД —
        вызывается один раз при старте, чтобы существующие на момент запуска
        активные инциденты тоже были кандидатами на закрытие, а не только
        те, что появятся после этого момента."""
        existing = self.repo.get_history(limit=5000)
        self._polled_ids = {i.id for i in existing if i.resolved_at is None}
        self._seeded = True

    def tick(self) -> dict:
        """Один цикл опроса. Возвращает {"new": int, "closed": int, "retried": int}."""
        if not self._seeded:
            self.seed()

        fetched: List[Incident] = self.zabbix_client.get_active_problems()
        fetched_ids = {i.id for i in fetched}
        self._polled_ids |= fetched_ids

        known = {i.id: i for i in self.repo.get_history(limit=5000)}
        new_incidents = [i for i in fetched if i.id not in known]
        closed_ids = [
            iid for iid, inc in known.items()
            if inc.resolved_at is None and iid in self._polled_ids and iid not in fetched_ids
        ]
        retry_incidents = [inc for inc in known.values() if not inc.ai_analyzed][: self.RETRY_LIMIT]

        if closed_ids:
            self.repo.mark_resolved(closed_ids)

        to_analyze = new_incidents + retry_incidents
        if to_analyze:
            self.analyzer.analyze(to_analyze, rag=self.rag)
            self.repo.save_incidents(to_analyze)
            if self.rag is not None:
                for inc in to_analyze:
                    if inc.ai_analyzed:
                        try:
                            self.rag.save_embedding(inc.id, inc.problem_name)
                        except Exception:
                            pass

        return {"new": len(new_incidents), "closed": len(closed_ids), "retried": len(retry_incidents)}
