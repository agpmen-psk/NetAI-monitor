"""
llm_client.py — интеллектуальный анализ ТОЛЬКО через локальный LLM (Ollama).
Поддерживает опциональный RAG-контекст (похожие прошлые случаи из PostgreSQL)
для более точных ответов на основе накопленной истории конкретного предприятия.
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections import defaultdict
from datetime import timedelta
from typing import List

from models import Incident, Severity

# Инциденты на одном хосте, случившиеся в пределах этого окна друг от друга,
# считаются одной группой (вероятно один и тот же первопричинный отказ) —
# модели явно указывается на это в промпте, чтобы она не выдавала N
# независимых диагнозов там, где на деле один каскадный сбой.
CORRELATION_WINDOW_MINUTES = 30


INCIDENT_SYSTEM_PROMPT = (
    "Ты — ассистент сетевого инженера NOC на энергосбытовом предприятии. "
    "Тебе передают список активных проблем из системы мониторинга Zabbix. "
    "Для каждой группы связанных проблем дай: "
    "1) краткое объяснение сути проблемы простым языком (1-2 предложения), "
    "2) вероятную причину, "
    "3) конкретные шаги диагностики/устранения. "
    "Отвечай кратко, по-русски, без воды."
)

CONFIG_DIFF_SYSTEM_PROMPT = (
    "Ты — эксперт по информационной безопасности сетевого оборудования "
    "(H3C, Eltex). Тебе передают diff конфигурации устройства (было/стало). "
    "Оцени риск изменения."
)

CONFIG_AUDIT_SYSTEM_PROMPT = (
    "Ты — эксперт по информационной безопасности сетевого оборудования "
    "(H3C, Eltex). Тебе передают полный текущий конфиг устройства. "
    "Проведи аудит на соответствие best practice: "
    "открытые/дефолтные SNMP community, разрешённый telnet вместо SSH, "
    "отсутствие port security на access-портах, слишком широкие ACL "
    "(permit ip any any), отсутствие логирования на syslog, слабые пароли "
    "в конфиге. Оцени общий риск конфигурации."
)

LANGUAGE_INSTRUCTION = (
    "\n\nВАЖНО: отвечай ТОЛЬКО на русском языке. Не используй английский, "
    "китайский или любой другой язык ни в одном слове ответа.\n"
)

RESPONSE_FORMAT_INSTRUCTIONS = (
    LANGUAGE_INSTRUCTION +
    "Ответь строго в формате, каждый пункт рекомендации — не длиннее одного предложения:\n"
    "РИСК: <LOW/MEDIUM/HIGH>\n"
    "СУТЬ: <объяснение, максимум 4 пункта>\n"
    "РЕКОМЕНДАЦИЯ: <шаги, максимум 4 пункта>\n"
)


class BaseAnalyzer(ABC):
    @abstractmethod
    def analyze(self, incidents: List[Incident]) -> List[Incident]:
        ...


def _correlate_incidents(incidents: List[Incident]) -> List[List[Incident]]:
    """Группирует инциденты одного хоста, случившиеся близко по времени.
    Нумерация для парсинга ответа модели строится по позиции в ИСХОДНОМ
    списке incidents (см. index_map в _build_incident_prompt) — группировка
    влияет только на то, как проблемы поданы в тексте промпта, а не на то,
    как парсится ответ, поэтому безопасна для существующего формата."""
    by_host: dict[str, List[Incident]] = defaultdict(list)
    for inc in incidents:
        by_host[inc.host].append(inc)

    groups: List[List[Incident]] = []
    for host_incidents in by_host.values():
        host_incidents = sorted(host_incidents, key=lambda i: i.timestamp)
        current = [host_incidents[0]]
        for inc in host_incidents[1:]:
            if abs((inc.timestamp - current[-1].timestamp)) <= timedelta(minutes=CORRELATION_WINDOW_MINUTES):
                current.append(inc)
            else:
                groups.append(current)
                current = [inc]
        groups.append(current)
    return groups


def _build_incident_prompt(incidents: List[Incident], rag_context: str = "") -> str:
    index_map = {id(inc): i + 1 for i, inc in enumerate(incidents)}
    groups = _correlate_incidents(incidents)

    lines = []
    for group in groups:
        if len(group) > 1:
            lines.append(
                f"— Группа вероятно связанных проблем на {group[0].host} "
                f"({len(group)} подряд в пределах {CORRELATION_WINDOW_MINUTES} мин — "
                f"возможно, это один и тот же первопричинный сбой, а не {len(group)} разных):"
            )
        for inc in group:
            lines.append(f"{index_map[id(inc)]}. {inc.to_prompt_line()}")
    incidents_text = "\n".join(lines)

    return (
        f"{rag_context}"
        f"Вот список активных проблем:\n\n{incidents_text}\n\n"
        "Для каждого номера верни блок в формате:\n"
        "### <номер>\n"
        "СУТЬ: <объяснение>\n"
        "РЕКОМЕНДАЦИЯ: <шаги>\n"
        "Если несколько номеров образуют группу связанных проблем, укажи в СУТИ "
        "каждого из них общую вероятную первопричину, но всё равно верни блок для "
        "каждого номера отдельно.\n"
    )


def _apply_parsed_response(incidents: List[Incident], text: str) -> None:
    clean_text = text.replace("**", "").replace("__", "")
    blocks = re.split(r"#{1,4}\s*", clean_text)
    parsed_any = False

    for block in blocks[1:]:
        block = block.strip()
        if not block:
            continue
        num_match = re.match(r"\D*(\d+)", block)
        if not num_match:
            continue
        idx = int(num_match.group(1)) - 1
        if not (0 <= idx < len(incidents)):
            continue

        summary_match = re.search(r"СУТЬ\s*:?\s*(.*?)(?=РЕКОМЕНДАЦИЯ|\Z)", block, re.IGNORECASE | re.DOTALL)
        rec_match = re.search(r"РЕКОМЕНДАЦИЯ\s*:?\s*(.*)", block, re.IGNORECASE | re.DOTALL)

        summary = summary_match.group(1).strip() if summary_match else ""
        recommendation = rec_match.group(1).strip() if rec_match else ""

        if summary or recommendation:
            incidents[idx].ai_summary = summary or "См. рекомендации."
            incidents[idx].ai_recommendation = recommendation or "См. суть проблемы."
            incidents[idx].ai_analyzed = True
            parsed_any = True

    if not parsed_any and clean_text.strip():
        for inc in incidents:
            if not inc.ai_analyzed:
                inc.ai_summary = "Не удалось разобрать формат ответа модели:"
                inc.ai_recommendation = clean_text.strip()[:1500]
                inc.ai_analyzed = True


class OllamaAnalyzer(BaseAnalyzer):
    """Единственный источник ИИ-анализа в приложении. Полностью локальный."""

    BATCH_SIZE = 10

    def __init__(self, model: str = "gemma4:e4b", host: str = "http://localhost:11434"):
        import requests
        self._requests = requests
        self.model = model
        self.host = host.rstrip("/")

    def is_available(self) -> bool:
        """Проверяет не только что Ollama отвечает, но и что нужная модель
        реально скачана (`ollama pull`). Раньше здесь смотрели только код
        ответа /api/tags — если сервер жив, но модель не скачана, приложение
        считало себя «подключённым к Ollama» и получало ошибку на каждом
        инциденте вместо честного отката на RuleBasedAnalyzer."""
        try:
            resp = self._requests.get(f"{self.host}/api/tags", timeout=3)
            if resp.status_code != 200:
                return False
            names = [m.get("name", "") for m in resp.json().get("models", [])]
            base = self.model.split(":")[0]
            return any(n == self.model or n.split(":")[0] == base for n in names)
        except Exception:
            return False

    # --- Инциденты Zabbix ---------------------------------------------------

    def analyze(self, incidents: List[Incident], rag=None) -> List[Incident]:
        """rag: опциональный IncidentRAG — если передан, в промпт добавляются
        похожие прошлые случаи с их фактическими решениями."""
        if not incidents:
            return incidents
        for start in range(0, len(incidents), self.BATCH_SIZE):
            batch = incidents[start:start + self.BATCH_SIZE]
            rag_context = ""
            if rag is not None:
                try:
                    # Раньше RAG-контекст строился только по batch[0].problem_name —
                    # для остальных ~9 инцидентов в пачке подмешивались случаи, похожие
                    # на совершенно другую проблему. Запрашиваем по всем уникальным
                    # проблемам пачки, чтобы контекст покрывал весь батч, а не первую строку.
                    query_text = "; ".join(dict.fromkeys(inc.problem_name for inc in batch))
                    rag_context = rag.build_context_block(query_text)
                except Exception:
                    rag_context = ""
            try:
                self._call_incidents(_build_incident_prompt(batch, rag_context), INCIDENT_SYSTEM_PROMPT, batch)
            except Exception as e:
                for inc in batch:
                    inc.ai_summary = f"Ошибка анализа: {e}"
                    inc.ai_recommendation = "Проверьте, что Ollama запущена и доступна."
                    inc.ai_analyzed = False
        return incidents

    def _call_incidents(self, prompt: str, system: str, batch: List[Incident]) -> None:
        response = self._requests.post(
            f"{self.host}/api/generate",
            json={
                "model": self.model,
                "system": system,
                "prompt": prompt,
                "stream": False,
                "options": {"num_predict": 1500, "num_ctx": 8192},
            },
            timeout=300,
        )
        response.raise_for_status()
        text = response.json().get("response", "")
        _apply_parsed_response(batch, text)

    # --- Конфигурации (diff и полный аудит) ---------------------------------

    def _review_text(self, text: str, system_prompt: str, label: str, rag_context: str = "") -> dict:
        prompt = f"{system_prompt}{LANGUAGE_INSTRUCTION}\n\n{rag_context}{label}:\n\n{text}{RESPONSE_FORMAT_INSTRUCTIONS}"
        response = self._requests.post(
            f"{self.host}/api/generate",
            json={
                "model": self.model,
                "prompt": prompt,
                "stream": False,
                "options": {
                    "num_predict": 2000,
                    "num_ctx": 6144,
                    "temperature": 0.3,
                },
            },
            timeout=200,
        )
        response.raise_for_status()
        raw_text = response.json().get("response", "")
        clean_text = raw_text.replace("**", "").replace("__", "").strip()

        if not clean_text:
            return {
                "risk_level": "UNKNOWN",
                "summary": "Модель вернула пустой ответ.",
                "recommendation": "Проверьте `ollama ps` и логи сервера.",
            }

        cyrillic_chars = sum(1 for c in clean_text if "а" <= c.lower() <= "я")
        if cyrillic_chars < len(clean_text) * 0.15:
            return {
                "risk_level": "UNKNOWN",
                "summary": "Модель ответила не на русском языке. Смените модель.",
                "recommendation": clean_text[:800],
            }

        risk_match = re.search(r"РИСК\s*:?\s*(LOW|MEDIUM|HIGH)", clean_text, re.IGNORECASE)
        summary_match = re.search(r"СУТЬ\s*:?\s*(.*?)(?=РЕКОМЕНДАЦИЯ|\Z)", clean_text, re.IGNORECASE | re.DOTALL)
        rec_match = re.search(r"РЕКОМЕНДАЦИЯ\s*:?\s*(.*)", clean_text, re.IGNORECASE | re.DOTALL)

        risk_level = risk_match.group(1).upper() if risk_match else "UNKNOWN"
        summary = summary_match.group(1).strip() if summary_match else ""
        recommendation = rec_match.group(1).strip() if rec_match else ""

        if not summary and not recommendation:
            summary = "Не удалось разобрать формат ответа модели, см. сырой текст:"
            recommendation = clean_text[:1500]

        return {"risk_level": risk_level, "summary": summary, "recommendation": recommendation}

    def analyze_config_diff(self, diff_text: str, rag=None) -> dict:
        rag_context = ""
        if rag is not None:
            try:
                rag_context = rag.build_context_block(diff_text)
            except Exception:
                rag_context = ""
        return self._review_text(diff_text, CONFIG_DIFF_SYSTEM_PROMPT, "Diff конфигурации устройства", rag_context)

    def analyze_full_config(self, config_text: str, rag=None) -> dict:
        rag_context = ""
        if rag is not None:
            try:
                rag_context = rag.build_context_block(config_text)
            except Exception:
                rag_context = ""
        return self._review_text(config_text, CONFIG_AUDIT_SYSTEM_PROMPT, "Полный конфиг устройства", rag_context)


class RuleBasedAnalyzer(BaseAnalyzer):
    """Офлайн-заглушка на случай, если Ollama недоступна."""

    RULES = [
        ("link down", "Порт или канал связи перешёл в состояние down.",
         "Проверьте физическое подключение, состояние соседнего устройства, историю port-flap."),
        ("cpu utilization", "Высокая загрузка процессора устройства.",
         "Проверьте top-процессы, исключите broadcast-шторм, проверьте циклы в топологии (STP)."),
        ("memory utilization", "Устройству не хватает оперативной памяти.",
         "Проверьте таблицу MAC/маршрутизации на аномальный рост, рассмотрите перезагрузку."),
        ("lost connection", "Устройство недоступно по ICMP.",
         "Проверьте физическую доступность и электропитание. Приоритет высокий."),
        ("latency", "Возросла задержка до узла.",
         "Проверьте загрузку канала, потери пакетов (mtr), работы у провайдера."),
        ("errors rate", "Растёт число ошибок на интерфейсе.",
         "Проверьте качество оптики/кабеля, уровень сигнала SFP."),
        ("disk space", "Заканчивается свободное место на диске.",
         "Очистите логи/temp, проверьте ротацию логов."),
        ("sip trunk", "Потеряна регистрация SIP-транка.",
         "Проверьте SIP-провайдера, статус Asterisk, NAT/файрвол."),
        ("flapping", "Интерфейс нестабилен.",
         "Проверьте кабель/разъём, настройки duplex/speed."),
        ("agent is not available", "Потеряна связь с Zabbix-агентом.",
         "Проверьте, что хост включён, служба агента и файрвол."),
        ("disk read/write", "Возросло время отклика диска.",
         "Проверьте SMART дисков, нагрузку IOPS, фоновый ребилд RAID."),
    ]

    def analyze(self, incidents: List[Incident], rag=None) -> List[Incident]:
        for inc in incidents:
            name_lower = inc.problem_name.lower()
            matched = False
            for keyword, summary, recommendation in self.RULES:
                if keyword in name_lower:
                    inc.ai_summary = summary
                    inc.ai_recommendation = recommendation
                    inc.ai_analyzed = True
                    matched = True
                    break
            if not matched:
                inc.ai_summary = "Требуется ручной анализ — проблема не распознана правилами."
                inc.ai_recommendation = "Проверьте устройство вручную."
                inc.ai_analyzed = True
            if inc.severity in (Severity.HIGH, Severity.DISASTER):
                inc.ai_recommendation = "⚠ Приоритет высокий. " + inc.ai_recommendation
        return incidents

    def analyze_config_diff(self, diff_text: str, rag=None) -> dict:
        return {"risk_level": "UNKNOWN", "summary": "ИИ недоступен (RuleBased не поддерживает конфиги).",
                "recommendation": "Запустите Ollama для анализа конфигураций."}

    def analyze_full_config(self, config_text: str, rag=None) -> dict:
        return self.analyze_config_diff(config_text)


def get_analyzer(ollama_host: str, ollama_model: str) -> BaseAnalyzer:
    analyzer = OllamaAnalyzer(model=ollama_model, host=ollama_host)
    if analyzer.is_available():
        return analyzer
    return RuleBasedAnalyzer()