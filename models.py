"""
Модели данных для приложения интеллектуального анализа сетевых инцидентов.
"""
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class Severity(Enum):
    """Уровни критичности инцидента (соответствуют Zabbix severity)."""
    NOT_CLASSIFIED = 0
    INFORMATION = 1
    WARNING = 2
    AVERAGE = 3
    HIGH = 4
    DISASTER = 5

    @property
    def label_ru(self) -> str:
        return {
            Severity.NOT_CLASSIFIED: "Не классифицировано",
            Severity.INFORMATION: "Информация",
            Severity.WARNING: "Предупреждение",
            Severity.AVERAGE: "Средняя",
            Severity.HIGH: "Высокая",
            Severity.DISASTER: "Авария",
        }[self]

    @property
    def color(self) -> str:
        return {
            Severity.NOT_CLASSIFIED: "#97AAB3",
            Severity.INFORMATION: "#7499FF",
            Severity.WARNING: "#FFC859",
            Severity.AVERAGE: "#FFA059",
            Severity.HIGH: "#E97659",
            Severity.DISASTER: "#E45959",
        }[self]


@dataclass
class Incident:
    """Инцидент, полученный из Zabbix (или мок-источника)."""
    id: str
    host: str
    problem_name: str
    severity: Severity
    timestamp: datetime
    item_key: str = ""
    last_value: str = ""
    resolved: bool = False

    # Поля, заполняемые после анализа LLM
    ai_summary: str = ""
    ai_recommendation: str = ""
    ai_analyzed: bool = False

    # True, если инженер вручную вписал фактическое решение — такие записи
    # приоритетно всплывают в RAG-контексте для похожих будущих инцидентов
    # («обучение на ошибках» без дообучения весов модели).
    ai_verified: bool = False
    # Как проблема была решена НА САМОМ ДЕЛЕ, со слов инженера — отдельно от
    # ai_recommendation (вывод модели), чтобы одно не затирало другое.
    resolution: str = ""

    def to_prompt_line(self) -> str:
        """Краткое текстовое представление для передачи в LLM."""
        return (
            f"[{self.severity.label_ru}] {self.host}: {self.problem_name} "
            f"(ключ: {self.item_key}, значение: {self.last_value}, "
            f"время: {self.timestamp.strftime('%Y-%m-%d %H:%M')})"
        )
