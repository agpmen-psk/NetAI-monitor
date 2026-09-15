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
        """Заполняет множество "уже известных активных" из БД — вызывается
        один раз при старте, чтобы существующие на момент запуска активные
        инциденты тоже были кандидатами на закрытие, а не только те, что
        появятся после этого момента. get_open_ids() — без ограничения на
        количество (раньше был get_history(limit=5000): как только суммарно
        по таблице накапливалось больше 5000 строк — включая уже закрытые,
        которые никогда не чистятся — по-настоящему ещё открытые старые
        инциденты выпадали из этого окна и переставали быть видны логике
        закрытия/повтора анализа ниже)."""
        self._polled_ids = self.repo.get_open_ids()
        self._seeded = True

    def tick(self) -> dict:
        """Один цикл опроса. Возвращает {"new": int, "closed": int, "retried": int}."""
        if not self._seeded:
            self.seed()

        fetched: List[Incident] = self.zabbix_client.get_active_problems()
        fetched_ids = {i.id for i in fetched}
        self._polled_ids |= fetched_ids

        existing_ids = self.repo.get_existing_ids([i.id for i in fetched])
        new_incidents = [i for i in fetched if i.id not in existing_ids]

        open_ids = self.repo.get_open_ids()
        closed_ids = [
            iid for iid in open_ids
            if iid in self._polled_ids and iid not in fetched_ids
        ]
        # Самые старые непроанализированные — первыми (см. get_unanalyzed),
        # иначе при затяжном сбое LLM новые проблемные пачки на каждом тике
        # вытесняли бы старые из окна повтора, и те застревали бы навсегда.
        retry_incidents = self.repo.get_unanalyzed(limit=self.RETRY_LIMIT)

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
