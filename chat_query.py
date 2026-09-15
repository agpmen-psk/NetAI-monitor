"""
chat_query.py — извлечение контекста для чата «Спросите у NetAI Monitor».

Не полагается на RAG/pgvector (доступны только при PostgreSQL) — вместо
эмбеддингов использует простое распознавание хоста и периода времени прямо
в тексте вопроса плюс агрегированную сводку. Работает одинаково в PostgreSQL-
и SQLite-режимах, не требует Ollama-эмбеддингов для самого поиска (только
для финального ответа, который формирует LLM).
"""
from __future__ import annotations

from datetime import datetime, timedelta


def _parse_days(question: str) -> int:
    q = question.lower()
    if any(k in q for k in ("всю истори", "за всё врем", "за все врем")):
        return 3650
    if "месяц" in q:
        return 30
    if "недел" in q:
        return 7
    if any(k in q for k in ("сутк", "сегодня", "24 час", "24ч")):
        return 1
    return 14  # разумное окно по умолчанию, если период не назван явно


def _find_host(question: str, hosts: list[str]) -> str | None:
    q = question.lower()
    for host in hosts:
        if host and host.lower() in q:
            return host
    return None


def build_context(question: str, incident_repo, config_repo) -> str:
    """Собирает компактный текстовый блок: агрегаты по всей истории +
    отфильтрованные по хосту/периоду записи. Именно этот текст уходит
    в промпт LLM как единственный источник фактов (модели явно
    запрещается выдумывать то, чего здесь нет).

    Раньше и распознавание хоста, и фильтрация по периоду делались
    Python-фильтром по get_history(limit=5000) — хост, все упоминания
    которого старше первых 5000 строк истории, не распознавался бы в
    вопросе вообще, а «за всю историю» могло молча вернуть «не найдено»
    при реально существующих старых данных. Теперь и список хостов, и
    сама фильтрация — прямые запросы к БД (search/get_distinct_*),
    не ограниченные окном выборки."""
    hosts = sorted(set(incident_repo.get_distinct_hosts()) | set(config_repo.get_distinct_nodes()))
    target_host = _find_host(question, hosts)
    days = _parse_days(question)
    cutoff = datetime.now() - timedelta(days=days)

    filtered_incidents = incident_repo.search(since=cutoff, host=target_host, limit=5000)
    filtered_configs = config_repo.search(since=cutoff, node=target_host, limit=5000)

    verification_stats = incident_repo.get_verification_stats()
    total = verification_stats["total"]
    critical = incident_repo.get_critical_count()
    top_hosts = sorted(incident_repo.get_stats_by_host().items(), key=lambda kv: kv[1], reverse=True)[:3]
    risk_counts = config_repo.get_risk_counts()

    lines = [
        f"Сводка по всей истории: {total} инцидентов всего, {critical} критичных (высокая/авария).",
        "Топ хостов по числу инцидентов: "
        + (", ".join(f"{h} ({n})" for h, n in top_hosts) or "нет данных") + ".",
        f"Риск конфигураций по всей истории: HIGH={risk_counts.get('HIGH', 0)}, "
        f"MEDIUM={risk_counts.get('MEDIUM', 0)}, LOW={risk_counts.get('LOW', 0)}.",
        "",
    ]

    scope = f"по хосту {target_host}, " if target_host else ""
    lines.append(f"Далее — данные {scope}за последние {days} дн.:")

    lines.append(f"\nИнциденты ({len(filtered_incidents)}, показаны последние 20):")
    for inc in sorted(filtered_incidents, key=lambda i: i.timestamp, reverse=True)[:20]:
        resolution = f" [решение инженера: {inc.resolution}]" if inc.ai_verified and inc.resolution else ""
        lines.append(
            f"- {inc.timestamp.strftime('%d.%m %H:%M')} {inc.host}: {inc.problem_name} "
            f"({inc.severity.label_ru}){resolution}"
        )
    if not filtered_incidents:
        lines.append("(инцидентов за этот период/хост не найдено)")

    lines.append(f"\nПроверки конфигураций ({len(filtered_configs)}, показаны последние 10):")
    for c in sorted(filtered_configs, key=lambda c: c.get("saved_at") or datetime.min, reverse=True)[:10]:
        resolution = f" [решение инженера: {c.get('resolution')}]" if c.get("ai_verified") and c.get("resolution") else ""
        lines.append(
            f"- {c.get('node')}: риск {c.get('ai_risk_level')}, "
            f"{(c.get('ai_summary') or '')[:100]}{resolution}"
        )
    if not filtered_configs:
        lines.append("(проверок конфигураций за этот период/хост не найдено)")

    return "\n".join(lines)
