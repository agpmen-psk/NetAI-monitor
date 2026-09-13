"""
gui.py — интерфейс NetAI Monitor v2 с RAG.
Пять вкладок: Дашборд / Алерты / Конфигурации / Настройки / Синтетические данные.
Алерты и Конфигурации используют IncidentRAG/ConfigRAG (если доступны) —
похожие прошлые случаи автоматически подмешиваются в промпт LLM.

Стиль — тёмный «glow»-дашборд со скруглёнными карточками, радиальной подсветкой
фона и мини-графиками (линия/бар/донат), построенными на реальных метриках
приложения: инциденты, риск конфигураций, доля разбора ИИ, конвейер анализа.
Слева — постоянный сайдбар: навигация, очередь критичных инцидентов, недавняя
активность ИИ, быстрый доступ к вкладкам.
"""
from __future__ import annotations

import copy
import random
from collections import Counter
from datetime import datetime, timedelta

from PySide6.QtCore import Qt, QThread, Signal, QPointF, QRectF, QTimer
from PySide6.QtGui import (
    QFont, QPainter, QPen, QColor, QPainterPath, QLinearGradient, QRadialGradient,
    QBrush, QTextDocument,
)
from PySide6.QtPrintSupport import QPrinter
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QPushButton, QListWidget, QListWidgetItem, QTextEdit, QLineEdit,
    QSplitter, QStatusBar, QFrame, QButtonGroup, QStackedWidget, QSizePolicy,
    QCheckBox, QFormLayout, QMessageBox, QComboBox, QScrollArea, QDialog, QFileDialog,
)

from models import Incident, Severity
from settings import AppSettings, SettingsManager
from db import Database, IncidentRepository, ConfigDiffRepository
from llm_client import OllamaAnalyzer
from zabbix_client import MockZabbixClient, ZabbixClient
from mock_oxidized_client import MockOxidizedClient
from oxidized_client import OxidizedClient
from rag import EmbeddingClient
from local_config import load_postgres_config, save_postgres_config, build_dsn

# ---------------------------------------------------------------------------
# Токены дизайна
# ---------------------------------------------------------------------------

BG_TOP = "#0E1830"
BG = "#080B14"
SIDEBAR_BG = "#0A0D18"
PANEL_BG = "rgba(255, 255, 255, 0.045)"
PANEL_BORDER = "rgba(255, 255, 255, 0.08)"
TRACK_BG = "#1A1E29"
TEXT_PRIMARY = "#E8EBF2"
TEXT_SECONDARY = "#8891A7"
TEXT_MUTED = "#4E566B"
ACCENT = "#5B8DEF"
ACCENT_LIGHT = "#8FB3FF"
POSITIVE = "#4ADE80"
NEGATIVE = "#F87171"
WARNING = "#FBBF24"

FONT_DATA = "'JetBrains Mono', 'Cascadia Mono', 'Consolas', monospace"
FONT_TEXT = "'Segoe UI', 'Inter', sans-serif"

APP_STYLESHEET = f"""
QMainWindow {{ background-color: {BG}; }}
QWidget {{ color: {TEXT_PRIMARY}; font-family: {FONT_TEXT}; }}
QLabel {{ color: {TEXT_PRIMARY}; }}
QStatusBar {{ background: {BG}; color: {TEXT_MUTED}; border-top: 1px solid {PANEL_BORDER}; font-family: {FONT_DATA}; font-size: 11px; }}
QListWidget {{ background: transparent; border: none; outline: none; }}
QListWidget::item {{ border: none; }}
QListWidget::item:selected {{ background: transparent; }}
QTextEdit {{
    background: transparent; color: {TEXT_PRIMARY}; border: none; padding: 2px;
    font-family: {FONT_DATA}; font-size: 12px;
}}
QLineEdit {{
    background: rgba(255,255,255,0.05); border: 1px solid {PANEL_BORDER};
    border-radius: 8px; padding: 8px; color: {TEXT_PRIMARY};
    font-family: {FONT_DATA}; font-size: 12px;
}}
QLineEdit:focus {{ border: 1px solid {ACCENT}; }}
QCheckBox {{ font-size: 12px; }}
QComboBox {{
    background: rgba(255,255,255,0.05); border: 1px solid {PANEL_BORDER};
    border-radius: 8px; padding: 6px; color: {TEXT_PRIMARY}; font-family: {FONT_DATA};
}}
QComboBox::drop-down {{ border: none; }}
QComboBox QAbstractItemView {{
    background: #10141F; color: {TEXT_PRIMARY}; border: 1px solid {PANEL_BORDER};
    selection-background-color: {ACCENT}; selection-color: #0A0D18;
    outline: none; padding: 4px;
}}
QScrollArea {{ background: transparent; border: none; }}
QScrollBar:vertical {{ background: transparent; width: 6px; }}
QScrollBar::handle:vertical {{ background: {PANEL_BORDER}; border-radius: 3px; min-height: 24px; }}

QMessageBox {{ background-color: #10141F; }}
QMessageBox QLabel {{ color: {TEXT_PRIMARY}; background: transparent; font-size: 13px; }}
QMessageBox QPushButton {{
    background: {ACCENT}; color: #0A0D18; border: none; border-radius: 8px;
    padding: 6px 16px; font-size: 12px; font-weight: 600; min-width: 70px;
}}
QMessageBox QPushButton:hover {{ background: {ACCENT_LIGHT}; }}
"""

PRIMARY_BUTTON_STYLE = f"""
QPushButton {{ background: {ACCENT}; color: #0A0D18; border: none; border-radius: 8px;
    padding: 9px 16px; font-size: 12px; font-weight: 600; }}
QPushButton:disabled {{ background: rgba(255,255,255,0.06); color: {TEXT_MUTED}; }}
QPushButton:hover:!disabled {{ background: {ACCENT_LIGHT}; }}
"""

SECONDARY_BUTTON_STYLE = f"""
QPushButton {{ background: rgba(255,255,255,0.04); color: {TEXT_SECONDARY}; border: 1px solid {PANEL_BORDER};
    border-radius: 8px; padding: 8px 16px; font-size: 12px; font-weight: 600; }}
QPushButton:disabled {{ color: {TEXT_MUTED}; }}
QPushButton:hover:!disabled {{ border: 1px solid {ACCENT}; color: {TEXT_PRIMARY}; }}
"""

NAV_BUTTON_STYLE = f"""
QPushButton {{ background: transparent; color: {TEXT_SECONDARY}; border: none; border-radius: 10px;
    padding: 9px 14px; font-size: 12px; font-weight: 600; text-align: left; }}
QPushButton:checked {{ background: rgba(91,141,239,0.16); color: {TEXT_PRIMARY}; }}
QPushButton:hover:!checked {{ background: rgba(255,255,255,0.04); }}
"""

QUICK_BUTTON_STYLE = f"""
QPushButton {{ background: rgba(255,255,255,0.04); color: {TEXT_SECONDARY}; border: 1px solid {PANEL_BORDER};
    border-radius: 10px; padding: 10px 6px; font-size: 10px; font-weight: 600; }}
QPushButton:hover {{ border: 1px solid {ACCENT}; color: {TEXT_PRIMARY}; }}
"""

RISK_COLORS = {"LOW": POSITIVE, "MEDIUM": WARNING, "HIGH": NEGATIVE, "UNKNOWN": TEXT_MUTED}

MESSAGE_BOX_STYLESHEET = f"""
QMessageBox {{ background-color: #10141F; }}
QMessageBox QLabel {{ color: {TEXT_PRIMARY}; background: transparent; font-size: 13px; }}
QMessageBox QPushButton {{
    background: {ACCENT}; color: #0A0D18; border: none; border-radius: 8px;
    padding: 6px 16px; font-size: 12px; font-weight: 600; min-width: 70px;
}}
QMessageBox QPushButton:hover {{ background: {ACCENT_LIGHT}; }}
"""


def _styled_msgbox(parent, icon, title: str, text: str, buttons=QMessageBox.Ok,
                    default=QMessageBox.Ok) -> QMessageBox:
    """Стиль ставится прямо на экземпляр диалога, а не только на QApplication —
    на некоторых машинах нативный стиль Windows (windowsvista) на модальных
    QMessageBox всё равно рисует кнопки своей темой поверх app-wide QSS
    (кнопка при этом кликабельна, просто не видна). Явный стиль на самом
    экземпляре гарантированно перекрывает это независимо от текущего QStyle."""
    box = QMessageBox(parent)
    box.setIcon(icon)
    box.setWindowTitle(title)
    box.setText(text)
    box.setStandardButtons(buttons)
    box.setDefaultButton(default)
    box.setStyleSheet(MESSAGE_BOX_STYLESHEET)
    return box


def info_box(parent, title: str, text: str) -> None:
    _styled_msgbox(parent, QMessageBox.Information, title, text).exec()


def warn_box(parent, title: str, text: str) -> None:
    _styled_msgbox(parent, QMessageBox.Warning, title, text).exec()


def confirm_box(parent, title: str, text: str) -> bool:
    box = _styled_msgbox(
        parent, QMessageBox.Question, title, text,
        buttons=QMessageBox.Yes | QMessageBox.No, default=QMessageBox.No,
    )
    return box.exec() == QMessageBox.Yes


COMBOBOX_STYLESHEET = f"""
QComboBox {{
    background: rgba(255,255,255,0.05); border: 1px solid {PANEL_BORDER};
    border-radius: 8px; padding: 6px; color: {TEXT_PRIMARY}; font-family: {FONT_DATA};
}}
QComboBox::drop-down {{ border: none; }}
QComboBox QAbstractItemView {{
    background: #10141F; color: {TEXT_PRIMARY}; border: 1px solid {PANEL_BORDER};
    selection-background-color: {ACCENT}; selection-color: #0A0D18;
    outline: none; padding: 4px;
}}
"""


class ComboBox(QComboBox):
    """QComboBox со стилем прямо на экземпляре. Обычный QSS на QApplication/
    MainWindow не всегда доходит до выпадающего списка — QAbstractItemView
    попапа рисуется отдельным top-level окном и на некоторых машинах
    Windows остаётся белым, несмотря на app-wide стили (тот же случай, что
    и с QMessageBox — см. _styled_msgbox). Явный стиль на самом виджете
    гарантирует тёмный попап независимо от порядка инициализации приложения."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setStyleSheet(COMBOBOX_STYLESHEET)
        self.view().setStyleSheet(COMBOBOX_STYLESHEET)


def _label(text: str, size: int = 12, color: str = TEXT_PRIMARY, weight: int = 400,
           font: str = FONT_TEXT) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet(
        f"color: {color}; font-size: {size}px; font-weight: {weight}; "
        f"font-family: {font}; border: none; background: transparent;"
    )
    return lbl


def _relative_time(ts: datetime) -> str:
    delta = datetime.now() - ts
    minutes = int(delta.total_seconds() // 60)
    if minutes < 1:
        return "только что"
    if minutes < 60:
        return f"{minutes} мин назад"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} ч назад"
    days = hours // 24
    return f"{days} дн назад"


def _hourly_counts(incidents: list[Incident]) -> list[float]:
    buckets = [0] * 12
    now = max((i.timestamp for i in incidents), default=None)
    if not now:
        return [0.0, 0.0]
    for inc in incidents:
        hours_ago = int((now - inc.timestamp).total_seconds() // 3600)
        if 0 <= hours_ago < 12:
            buckets[11 - hours_ago] += 1
    return [float(x) for x in buckets] or [0.0, 0.0]


def _daily_incident_counts(incidents: list[Incident], days: int = 14) -> tuple[list[float], list[str]]:
    now = datetime.now()
    buckets = [0.0] * days
    labels = [(now - timedelta(days=days - 1 - i)).strftime("%d.%m") for i in range(days)]
    for inc in incidents:
        days_ago = (now.date() - inc.timestamp.date()).days
        idx = days - 1 - days_ago
        if 0 <= idx < days:
            buckets[idx] += 1
    return buckets, labels


def _weekly_config_counts(configs: list[dict]) -> list[float]:
    buckets = [0.0] * 7
    now = datetime.now()
    for row in configs:
        saved_at = row.get("saved_at")
        if not saved_at:
            continue
        days_ago = (now.date() - saved_at.date()).days
        idx = 6 - days_ago
        if 0 <= idx < 7:
            buckets[idx] += 1
    return buckets or [0.0]


def _format_duration(seconds: float) -> str:
    if seconds <= 0:
        return "нет данных"
    minutes = seconds / 60
    if minutes < 60:
        return f"{minutes:.0f} мин"
    hours = minutes / 60
    if hours < 24:
        return f"{hours:.1f} ч"
    return f"{hours / 24:.1f} дн"


def _repeat_rate(incidents: list[Incident]) -> float:
    """Доля инцидентов, у которых уже был точно такой же (хост + текст
    проблемы) случай в истории — грубый индикатор повторяющихся отказов,
    которые стоит устранять на уровне инфраструктуры, а не разбирать заново."""
    if not incidents:
        return 0.0
    counts = Counter((i.host, i.problem_name) for i in incidents)
    repeated = sum(c for c in counts.values() if c > 1)
    return repeated / len(incidents) * 100


# ---------------------------------------------------------------------------
# Базовые контейнеры
# ---------------------------------------------------------------------------

class GlowBackground(QWidget):
    """Центральный виджет окна — рисует радиальную подсветку фона."""

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        painter.fillRect(self.rect(), QColor(BG))
        gradient = QRadialGradient(w * 0.34, h * 0.02, w * 0.62)
        gradient.setColorAt(0.0, QColor("#24406F"))
        gradient.setColorAt(0.35, QColor("#152A4D"))
        gradient.setColorAt(0.7, QColor(BG))
        gradient.setColorAt(1.0, QColor(BG))
        painter.fillRect(self.rect(), QBrush(gradient))


class Card(QFrame):
    """Скруглённая карточка со стеклянной заливкой и мягкой границей."""

    def __init__(self, radius: int = 18, accent_left: str | None = None):
        super().__init__()
        border = f"border-left: 3px solid {accent_left};" if accent_left else ""
        self.setStyleSheet(
            f"Card {{ background: {PANEL_BG}; border: 1px solid {PANEL_BORDER}; "
            f"border-radius: {radius}px; {border} }}"
        )


class Divider(QFrame):
    def __init__(self, vertical: bool = False):
        super().__init__()
        self.setFrameShape(QFrame.VLine if vertical else QFrame.HLine)
        if vertical:
            self.setStyleSheet(f"background: {PANEL_BORDER}; max-width: 1px; min-width: 1px; border: none;")
        else:
            self.setStyleSheet(f"background: {PANEL_BORDER}; max-height: 1px; min-height: 1px; border: none;")


# ---------------------------------------------------------------------------
# Кастомные графики (QPainter, без сторонних зависимостей)
# ---------------------------------------------------------------------------

class AreaSparkline(QWidget):
    """Линия с градиентной заливкой под ней — мини-график на карточке."""

    def __init__(self, values: list[float], color: str = ACCENT, height: int = 40):
        super().__init__()
        self._values = values or [0.0, 0.0]
        self._color = QColor(color)
        self.setFixedHeight(height)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def paintEvent(self, event):
        if len(self._values) < 2:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        lo, hi = min(self._values), max(self._values)
        span = (hi - lo) or 1
        points = [
            QPointF(i / (len(self._values) - 1) * (w - 4) + 2, h - 4 - ((v - lo) / span) * (h - 10))
            for i, v in enumerate(self._values)
        ]
        area = QPainterPath()
        area.moveTo(points[0].x(), h)
        for p in points:
            area.lineTo(p)
        area.lineTo(points[-1].x(), h)
        area.closeSubpath()

        fill = QLinearGradient(0, 0, 0, h)
        fill.setColorAt(0.0, QColor(self._color.red(), self._color.green(), self._color.blue(), 90))
        fill.setColorAt(1.0, QColor(self._color.red(), self._color.green(), self._color.blue(), 0))
        painter.fillPath(area, QBrush(fill))

        line = QPainterPath()
        line.moveTo(points[0])
        for p in points[1:]:
            line.lineTo(p)
        painter.setPen(QPen(self._color, 2))
        painter.drawPath(line)


class MiniBarChart(QWidget):
    """Компактный столбчатый график для карточки."""

    def __init__(self, values: list[float], color: str = ACCENT, height: int = 40):
        super().__init__()
        self._values = values or [0.0]
        self._color = QColor(color)
        self.setFixedHeight(height)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        n = len(self._values)
        hi = max(self._values) or 1
        gap = 4
        bar_w = (w - gap * (n - 1)) / n if n else w
        painter.setPen(Qt.NoPen)
        for i, v in enumerate(self._values):
            bar_h = max(2, (v / hi) * (h - 4))
            x = i * (bar_w + gap)
            y = h - bar_h
            color = QColor(self._color)
            color.setAlpha(140 if i < n - 1 else 255)
            painter.setBrush(QBrush(color))
            painter.drawRoundedRect(QRectF(x, y, bar_w, bar_h), 2, 2)


class DonutGauge(QWidget):
    """Круговой индикатор доли (0-100%) с числом в центре."""

    def __init__(self, percent: float, color: str = ACCENT, size: int = 72):
        super().__init__()
        self._percent = max(0.0, min(100.0, percent))
        self._color = QColor(color)
        self.setFixedSize(size, size)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        pad = 6
        rect = QRectF(pad, pad, self.width() - 2 * pad, self.height() - 2 * pad)

        bg_pen = QPen(QColor(TRACK_BG), 7)
        bg_pen.setCapStyle(Qt.RoundCap)
        painter.setPen(bg_pen)
        painter.drawArc(rect, 0, 360 * 16)

        fg_pen = QPen(self._color, 7)
        fg_pen.setCapStyle(Qt.RoundCap)
        painter.setPen(fg_pen)
        span = int(-self._percent / 100 * 360 * 16)
        painter.drawArc(rect, 90 * 16, span)

        painter.setPen(QPen(QColor(TEXT_PRIMARY)))
        f = QFont()
        f.setPointSize(12)
        f.setWeight(QFont.DemiBold)
        painter.setFont(f)
        painter.drawText(self.rect(), Qt.AlignCenter, f"{self._percent:.0f}%")


class DailyLineChart(QWidget):
    """Крупный график «инциденты по дням» с подписями оси X."""

    def __init__(self, values: list[float], labels: list[str], color: str = ACCENT, height: int = 170):
        super().__init__()
        self._values = values or [0.0, 0.0]
        self._labels = labels or []
        self._color = QColor(color)
        self.setMinimumHeight(height)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height() - 20
        lo, hi = min(self._values), max(self._values)
        span = (hi - lo) or 1
        n = len(self._values)

        painter.setPen(QPen(QColor(TRACK_BG)))
        for i in range(4):
            y = h * i / 3
            painter.drawLine(QPointF(0, y), QPointF(w, y))

        points = [
            QPointF(i / (n - 1) * (w - 8) + 4, h - 6 - ((v - lo) / span) * (h - 20))
            for i, v in enumerate(self._values)
        ]
        area = QPainterPath()
        area.moveTo(points[0].x(), h)
        for p in points:
            area.lineTo(p)
        area.lineTo(points[-1].x(), h)
        area.closeSubpath()
        fill = QLinearGradient(0, 0, 0, h)
        fill.setColorAt(0.0, QColor(self._color.red(), self._color.green(), self._color.blue(), 70))
        fill.setColorAt(1.0, QColor(self._color.red(), self._color.green(), self._color.blue(), 0))
        painter.fillPath(area, QBrush(fill))

        line = QPainterPath()
        line.moveTo(points[0])
        for p in points[1:]:
            line.lineTo(p)
        painter.setPen(QPen(self._color, 2))
        painter.drawPath(line)

        painter.setBrush(QBrush(self._color))
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(points[-1], 3.5, 3.5)

        painter.setPen(QPen(QColor(TEXT_MUTED)))
        f = QFont()
        f.setPointSize(8)
        painter.setFont(f)
        step = max(1, n // 7)
        for i in range(0, n, step):
            painter.drawText(QRectF(points[i].x() - 18, h + 4, 36, 16), Qt.AlignCenter, self._labels[i])


class PipelineTrack(QWidget):
    """Горизонтальная дорожка конвейера анализа с узлами-этапами и заливкой прогресса."""

    def __init__(self, stages: list[str], percent: float, color: str = ACCENT, height: int = 34):
        super().__init__()
        self._stages = stages
        self._percent = max(0.0, min(100.0, percent))
        self._color = QColor(color)
        self.setFixedHeight(height)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        w = self.width()
        track_y = 6.0
        track_h = 6.0
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor(TRACK_BG)))
        painter.drawRoundedRect(QRectF(0, track_y, w, track_h), 3, 3)

        fill_w = w * self._percent / 100
        painter.setBrush(QBrush(self._color))
        painter.drawRoundedRect(QRectF(0, track_y, fill_w, track_h), 3, 3)

        n = len(self._stages)
        for i in range(n):
            x = w * i / (n - 1) if n > 1 else 0.0
            done = x <= fill_w + 1
            painter.setBrush(QBrush(self._color if done else QColor("#2A2F3B")))
            painter.setPen(Qt.NoPen)
            painter.drawEllipse(QPointF(x, track_y + track_h / 2), 5, 5)

            painter.setPen(QPen(QColor(TEXT_PRIMARY if done else TEXT_MUTED)))
            f = QFont()
            f.setPointSize(9)
            painter.setFont(f)
            box_w = 150
            if i == 0:
                box_x, align = x, Qt.AlignLeft
            elif i == n - 1:
                box_x, align = x - box_w, Qt.AlignRight
            else:
                box_x, align = x - box_w / 2, Qt.AlignHCenter
            painter.drawText(QRectF(box_x, track_y + 12, box_w, 16), align, self._stages[i])


# ---------------------------------------------------------------------------
# Карточки дашборда
# ---------------------------------------------------------------------------

class StatCard(Card):
    def __init__(self, title: str, value: str, subtitle: str, chart: QWidget):
        super().__init__(radius=18)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(2)
        layout.addWidget(_label(title, size=11, color=TEXT_SECONDARY, weight=600))
        value_label = QLabel(value)
        f = QFont()
        f.setPointSize(21)
        f.setWeight(QFont.DemiBold)
        value_label.setFont(f)
        value_label.setStyleSheet(f"color: {TEXT_PRIMARY}; border: none; background: transparent;")
        layout.addWidget(value_label)
        layout.addWidget(_label(subtitle, size=10, color=TEXT_MUTED))
        layout.addSpacing(6)
        layout.addWidget(chart)


class DonutStatCard(Card):
    def __init__(self, title: str, percent: float, subtitle: str):
        super().__init__(radius=18)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(14)
        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        text_col.addWidget(_label(title, size=11, color=TEXT_SECONDARY, weight=600))
        text_col.addStretch()
        text_col.addWidget(_label(subtitle, size=10, color=TEXT_MUTED))
        layout.addLayout(text_col, stretch=1)
        layout.addWidget(DonutGauge(percent))


class SimpleStatCard(Card):
    """Числовая карточка без графика — для сводных метрик на вкладке Аналитика."""

    def __init__(self, title: str, value: str, subtitle: str):
        super().__init__(radius=18)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(2)
        layout.addWidget(_label(title, size=11, color=TEXT_SECONDARY, weight=600))
        value_label = QLabel(value)
        f = QFont()
        f.setPointSize(21)
        f.setWeight(QFont.DemiBold)
        value_label.setFont(f)
        value_label.setStyleSheet(f"color: {TEXT_PRIMARY}; border: none; background: transparent;")
        layout.addWidget(value_label)
        layout.addWidget(_label(subtitle, size=10, color=TEXT_MUTED))


class HorizontalBarRow(QWidget):
    """Строка «подпись — пропорциональный бар — число» для списков вида
    топ-хостов/распределения риска, без отдельного QPainter-виджета."""

    def __init__(self, label: str, value: int, max_value: int, color: str = ACCENT):
        super().__init__()
        self.setStyleSheet("background: transparent;")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 4)
        layout.setSpacing(4)

        head = QHBoxLayout()
        head.addWidget(_label(label, size=11, weight=700, font=FONT_DATA))
        head.addStretch()
        head.addWidget(_label(str(value), size=11, color=TEXT_SECONDARY))
        layout.addLayout(head)

        track = QFrame()
        track.setFixedHeight(8)
        track.setStyleSheet(f"background: {TRACK_BG}; border-radius: 4px;")
        track_layout = QHBoxLayout(track)
        track_layout.setContentsMargins(0, 0, 0, 0)
        track_layout.setSpacing(0)
        ratio = (value / max_value) if max_value else 0.0
        filled = max(1, round(ratio * 100)) if value else 0
        bar = QFrame()
        bar.setStyleSheet(f"background: {color}; border-radius: 4px;")
        if filled:
            track_layout.addWidget(bar, stretch=filled)
        if 100 - filled:
            track_layout.addStretch(100 - filled)
        layout.addWidget(track)


class PipelineCard(Card):
    def __init__(self, percent: float, total: int):
        super().__init__(radius=18)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 22)
        layout.setSpacing(10)

        header = QHBoxLayout()
        header.addWidget(_label("Конвейер анализа", size=12, weight=700))
        header.addStretch()
        header.addWidget(_label(f"{percent:.0f}% из {total}", size=11, color=ACCENT_LIGHT, weight=700))
        layout.addLayout(header)

        layout.addWidget(_label("Zabbix/Oxidized → Ollama-анализ → RAG-контекст → PostgreSQL",
                                 size=10, color=TEXT_MUTED))
        layout.addSpacing(10)
        layout.addWidget(PipelineTrack(
            ["Сбор данных", "Ollama-анализ", "RAG-контекст", "Сохранено"], percent
        ))


class ChartCard(Card):
    def __init__(self, title: str, chart: QWidget):
        super().__init__(radius=18)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 14)
        layout.setSpacing(10)
        layout.addWidget(_label(title, size=12, weight=700))
        layout.addWidget(chart, stretch=1)


class AttentionRow(QWidget):
    def __init__(self, incident: Incident):
        super().__init__()
        self.setStyleSheet("background: transparent;")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 6, 0, 6)
        layout.setSpacing(10)

        dot = QLabel("●")
        dot.setStyleSheet(f"color: {incident.severity.color}; font-size: 9px; border: none; background: transparent;")
        dot.setFixedWidth(12)
        layout.addWidget(dot)

        text_col = QVBoxLayout()
        text_col.setSpacing(0)
        text_col.addWidget(_label(incident.host, size=11, weight=700, font=FONT_DATA))
        text_col.addWidget(_label(incident.problem_name[:52], size=10, color=TEXT_SECONDARY))
        layout.addLayout(text_col, stretch=1)

        layout.addWidget(_label(_relative_time(incident.timestamp), size=9, color=TEXT_MUTED))


class AttentionCard(Card):
    def __init__(self, incidents: list[Incident], title: str = "Требуют внимания"):
        super().__init__(radius=18)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 12)
        layout.setSpacing(4)
        layout.addWidget(_label(title, size=12, weight=700))
        if not incidents:
            layout.addWidget(_label("Критичных инцидентов нет.", size=11, color=TEXT_MUTED))
        for inc in incidents[:4]:
            layout.addWidget(AttentionRow(inc))
        layout.addStretch()


class RecentActivityCard(Card):
    def __init__(self, incidents: list[Incident]):
        super().__init__(radius=18)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 12)
        layout.setSpacing(8)
        layout.addWidget(_label("Недавняя активность ИИ", size=12, weight=700))
        analyzed = [i for i in incidents if i.ai_analyzed]
        if not analyzed:
            layout.addWidget(_label("Пока нет проанализированных инцидентов.", size=11, color=TEXT_MUTED))
        for inc in analyzed[:3]:
            row = QVBoxLayout()
            row.setSpacing(0)
            head = QHBoxLayout()
            head.addWidget(_label(inc.host, size=11, weight=700, font=FONT_DATA))
            head.addStretch()
            head.addWidget(_label(_relative_time(inc.timestamp), size=9, color=TEXT_MUTED))
            row.addLayout(head)
            row.addWidget(_label(inc.problem_name[:56], size=10, color=TEXT_SECONDARY))
            layout.addLayout(row)
        layout.addStretch()


# ---------------------------------------------------------------------------
# Списки инцидентов/конфигураций (вкладки Алерты / Конфигурации)
# ---------------------------------------------------------------------------

class IncidentRow(QFrame):
    def __init__(self, incident: Incident):
        super().__init__()
        self.incident = incident
        self.setStyleSheet(
            f"IncidentRow {{ background: transparent; border: none; "
            f"border-left: 3px solid {incident.severity.color}; border-bottom: 1px solid {PANEL_BORDER}; }}"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(4)

        header = QHBoxLayout()
        header.addWidget(_label(incident.host, size=12, weight=700, font=FONT_DATA))
        if incident.ai_verified:
            header.addWidget(_label("✓ проверено", size=9, color=POSITIVE, weight=700))
        header.addStretch()
        header.addWidget(_label(incident.severity.label_ru, size=10, color=incident.severity.color, weight=700))
        layout.addLayout(header)

        problem = QLabel(incident.problem_name)
        problem.setWordWrap(True)
        problem.setStyleSheet(f"color: {TEXT_SECONDARY}; font-size: 12px; border: none; background: transparent;")
        layout.addWidget(problem)

        layout.addWidget(_label(
            incident.timestamp.strftime("%d.%m.%Y %H:%M"), size=10, color=TEXT_MUTED, font=FONT_DATA
        ))


class ConfigDiffRow(QFrame):
    def __init__(self, diff_row: dict):
        super().__init__()
        self.diff_row = diff_row
        risk = (diff_row.get("ai_risk_level") or "UNKNOWN").upper()
        color = RISK_COLORS.get(risk, TEXT_MUTED)
        review_type = diff_row.get("review_type", "diff")
        type_label_text = "аудит" if review_type == "full_audit" else "diff"

        self.setStyleSheet(
            f"ConfigDiffRow {{ background: transparent; border: none; "
            f"border-left: 3px solid {color}; border-bottom: 1px solid {PANEL_BORDER}; }}"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(4)

        header = QHBoxLayout()
        header.addWidget(_label(diff_row.get("node", "unknown"), size=12, weight=700, font=FONT_DATA))
        header.addWidget(_label(f"· {type_label_text}", size=10, color=TEXT_MUTED))
        if diff_row.get("ai_verified"):
            header.addWidget(_label("✓ проверено", size=9, color=POSITIVE, weight=700))
        header.addStretch()
        header.addWidget(_label(f"риск: {risk.lower()}", size=10, color=color, weight=700))
        layout.addLayout(header)

        summary = QLabel((diff_row.get("ai_summary") or "Ещё не проанализировано")[:150])
        summary.setWordWrap(True)
        summary.setStyleSheet(f"color: {TEXT_SECONDARY}; font-size: 12px; border: none; background: transparent;")
        layout.addWidget(summary)


class IncidentWorker(QThread):
    """Универсальный воркер анализа: либо тянет пачку из zabbix_client,
    либо (если передан incidents) анализирует уже готовый список — так
    единая логика используется и для «Обновить и проанализировать»,
    и для отправки одного алерта из Центра генерации в реальном времени."""

    finished = Signal(list)
    error = Signal(str)

    def __init__(self, zabbix_client, analyzer, rag=None, incidents: list[Incident] | None = None):
        super().__init__()
        self.zabbix_client = zabbix_client
        self.analyzer = analyzer
        self.rag = rag
        self.incidents = incidents

    def run(self):
        try:
            incidents = self.incidents if self.incidents is not None else self.zabbix_client.get_active_problems()
            incidents = self.analyzer.analyze(incidents, rag=self.rag)
            self.finished.emit(incidents)
        except Exception as e:
            self.error.emit(str(e))


class ConfigTaskWorker(QThread):
    finished = Signal(list)
    error = Signal(str)

    def __init__(self, task_fn):
        super().__init__()
        self.task_fn = task_fn

    def run(self):
        try:
            self.finished.emit(self.task_fn())
        except Exception as e:
            self.error.emit(str(e))


class ComparisonWorker(QThread):
    """Прогоняет один и тот же случай через анализатор дважды — с RAG и без —
    чтобы наглядно показать эффект от RAG-контекста на реальном ответе Ollama."""

    finished = Signal(dict)
    error = Signal(str)

    def __init__(self, task_fn):
        super().__init__()
        self.task_fn = task_fn

    def run(self):
        try:
            self.finished.emit(self.task_fn())
        except Exception as e:
            self.error.emit(str(e))


class ConnectionCheckWorker(QThread):
    """Проверяет доступность одного внешнего сервиса (Zabbix/Oxidized/Ollama/
    RAG-эмбеддинги) в фоне, чтобы сетевой таймаут не подвешивал интерфейс."""

    finished = Signal(bool, str)

    def __init__(self, check_fn):
        super().__init__()
        self.check_fn = check_fn

    def run(self):
        try:
            ok, message = self.check_fn()
        except Exception as e:
            ok, message = False, str(e)[:150]
        self.finished.emit(ok, message)


class ComparisonDialog(QDialog):
    """Модальное окно «с RAG / без RAG»: слева — что именно RAG подмешал в
    промпт, справа — два реальных ответа модели для прямого сравнения."""

    def __init__(self, result: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Эффект RAG на анализ")
        self.resize(920, 600)
        # Стили прописаны на самом диалоге явно (не полагаемся на наследование
        # от MainWindow) — модальные QDialog в Qt не всегда получают QSS родителя.
        self.setStyleSheet(f"""
            QDialog {{ background: {BG}; }}
            QLabel {{ color: {TEXT_PRIMARY}; background: transparent; }}
            QTextEdit {{
                background: rgba(255,255,255,0.03); color: {TEXT_PRIMARY};
                border: 1px solid {PANEL_BORDER}; border-radius: 8px; padding: 8px;
                font-family: {FONT_DATA}; font-size: 12px;
            }}
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        layout.addWidget(_label(result.get("title", ""), size=14, weight=700))
        if result.get("subtitle"):
            layout.addWidget(_label(result["subtitle"], size=11, color=TEXT_SECONDARY))

        context_card = Card(radius=12)
        cl = QVBoxLayout(context_card)
        cl.setContentsMargins(14, 12, 14, 12)
        cl.addWidget(_label("RAG-контекст, отправленный модели в промпте", size=11, color=TEXT_SECONDARY, weight=700))
        ctx_view = QTextEdit()
        ctx_view.setReadOnly(True)
        ctx_view.setPlainText(
            result.get("rag_context")
            or "Похожих случаев в истории не нашлось — RAG ничего не подмешал, "
               "поэтому ответы с RAG и без RAG должны совпадать."
        )
        ctx_view.setFixedHeight(110)
        cl.addWidget(ctx_view)
        layout.addWidget(context_card)

        cols = QHBoxLayout()
        cols.setSpacing(12)
        columns = [
            ("С RAG", POSITIVE, result.get("with_summary", ""), result.get("with_recommendation", "")),
            ("Без RAG", TEXT_MUTED, result.get("without_summary", ""), result.get("without_recommendation", "")),
        ]
        for label, color, summary, recommendation in columns:
            card = Card(radius=12)
            cl2 = QVBoxLayout(card)
            cl2.setContentsMargins(14, 12, 14, 12)
            cl2.addWidget(_label(label, size=12, weight=700, color=color))
            view = QTextEdit()
            view.setReadOnly(True)
            view.setPlainText(f"Суть:\n{summary}\n\nРекомендация:\n{recommendation}")
            cl2.addWidget(view)
            cols.addWidget(card)
        layout.addLayout(cols, stretch=1)

        close_btn = QPushButton("Закрыть")
        close_btn.setStyleSheet(PRIMARY_BUTTON_STYLE)
        close_btn.clicked.connect(self.accept)
        layout.addWidget(close_btn, alignment=Qt.AlignRight)


# ---------------------------------------------------------------------------
# Дашборд
# ---------------------------------------------------------------------------

class DashboardTab(QScrollArea):
    def __init__(self, incident_repo: IncidentRepository, config_repo: ConfigDiffRepository):
        super().__init__()
        self.incident_repo = incident_repo
        self.config_repo = config_repo
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        self.viewport().setStyleSheet("background: transparent;")

        inner = QWidget()
        inner.setStyleSheet("background: transparent;")
        self.root = QVBoxLayout(inner)
        self.root.setContentsMargins(28, 24, 28, 24)
        self.root.setSpacing(16)
        self.setWidget(inner)

        header = QHBoxLayout()
        title_col = QVBoxLayout()
        title_col.setSpacing(2)
        title_col.addWidget(_label("Обзор сети", size=18, weight=700))
        title_col.addWidget(_label("NetAI Monitor — АО «Псковэнергосбыт»", size=11, color=TEXT_MUTED))
        header.addLayout(title_col)
        header.addStretch()
        refresh_btn = QPushButton("Обновить сводку")
        refresh_btn.setStyleSheet(SECONDARY_BUTTON_STYLE)
        refresh_btn.clicked.connect(self.refresh)
        header.addWidget(refresh_btn)
        self.root.addLayout(header)

        self.stats_row = QHBoxLayout()
        self.stats_row.setSpacing(16)
        self.root.addLayout(self.stats_row)

        self.pipeline_slot = QVBoxLayout()
        self.root.addLayout(self.pipeline_slot)

        self.bottom_row = QHBoxLayout()
        self.bottom_row.setSpacing(16)
        self.root.addLayout(self.bottom_row)
        self.root.addStretch()

        self.refresh()

    def _clear_layout(self, layout):
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                self._clear_layout(item.layout())

    def refresh(self):
        incidents = self.incident_repo.get_history(limit=200)
        configs = self.config_repo.get_history(limit=100)

        self._clear_layout(self.stats_row)
        self._clear_layout(self.pipeline_slot)
        self._clear_layout(self.bottom_row)

        total = len(incidents)
        critical_24h = sum(
            1 for i in incidents
            if i.severity in (Severity.HIGH, Severity.DISASTER)
            and (datetime.now() - i.timestamp) <= timedelta(hours=24)
        )
        analyzed = sum(1 for i in incidents if i.ai_analyzed)
        analyzed_pct = (analyzed / total * 100) if total else 0.0

        hourly = _hourly_counts(incidents)
        weekly_configs = _weekly_config_counts(configs)

        self.stats_row.addWidget(StatCard(
            "Критичные инциденты (24ч)", str(critical_24h), "Высокая/авария за сутки",
            AreaSparkline(hourly, color=NEGATIVE if critical_24h else POSITIVE),
        ))
        self.stats_row.addWidget(StatCard(
            "Проверок конфигураций", str(len(configs)), "Diff + аудит за неделю",
            MiniBarChart(weekly_configs, color=ACCENT),
        ))
        self.stats_row.addWidget(DonutStatCard(
            "Проанализировано ИИ", analyzed_pct, f"{analyzed} из {total} инцидентов",
        ))

        self.pipeline_slot.addWidget(PipelineCard(analyzed_pct, total))

        daily_values, daily_labels = _daily_incident_counts(incidents, days=14)
        self.bottom_row.addWidget(
            ChartCard("Инциденты по дням (14 дней)", DailyLineChart(daily_values, daily_labels)),
            stretch=2,
        )

        side_col = QVBoxLayout()
        side_col.setSpacing(16)
        critical_unresolved = sorted(
            (i for i in incidents if i.severity in (Severity.HIGH, Severity.DISASTER)),
            key=lambda i: i.timestamp, reverse=True,
        )
        side_col.addWidget(AttentionCard(critical_unresolved))
        side_col.addWidget(RecentActivityCard(sorted(incidents, key=lambda i: i.timestamp, reverse=True)))
        self.bottom_row.addLayout(side_col, stretch=1)


# ---------------------------------------------------------------------------
# Алерты
# ---------------------------------------------------------------------------

class AlertsTab(QWidget):
    """Работа с инцидентами Zabbix. Использует IncidentRAG для похожих случаев."""

    incident_added = Signal()
    unread_changed = Signal(bool)

    def __init__(self, zabbix_client, analyzer, repo: IncidentRepository, rag=None):
        super().__init__()
        self.zabbix_client = zabbix_client
        self.analyzer = analyzer
        self.repo = repo
        self.rag = rag
        self._live_workers: list[IncidentWorker] = []
        self._unread_ids: set[str] = set()
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(14)

        top_bar = QHBoxLayout()
        self.refresh_btn = QPushButton("Обновить и проанализировать")
        self.refresh_btn.setStyleSheet(PRIMARY_BUTTON_STYLE)
        self.refresh_btn.clicked.connect(self.refresh)
        top_bar.addWidget(self.refresh_btn)

        self.auto_refresh_checkbox = QCheckBox("Автообновление")
        self.auto_refresh_checkbox.toggled.connect(self._toggle_auto_refresh)
        top_bar.addWidget(self.auto_refresh_checkbox)

        self.auto_refresh_interval = ComboBox()
        self.auto_refresh_interval.addItem("каждую минуту", 60_000)
        self.auto_refresh_interval.addItem("каждые 5 минут", 5 * 60_000)
        self.auto_refresh_interval.addItem("каждые 15 минут", 15 * 60_000)
        self.auto_refresh_interval.setCurrentIndex(1)
        self.auto_refresh_interval.currentIndexChanged.connect(self._toggle_auto_refresh)
        top_bar.addWidget(self.auto_refresh_interval)

        top_bar.addStretch()

        self.live_status_label = _label("", size=11, color=TEXT_MUTED)
        top_bar.addWidget(self.live_status_label)

        rag_status = _label(
            "RAG подключён" if self.rag else "RAG недоступен (нет nomic-embed-text)",
            size=11, color=POSITIVE if self.rag else TEXT_MUTED, font=FONT_DATA,
        )
        top_bar.addWidget(rag_status)
        root.addLayout(top_bar)

        self._auto_timer = QTimer(self)
        self._auto_timer.timeout.connect(self._auto_refresh_tick)

        filter_row = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Поиск по хосту или описанию проблемы...")
        self.search_edit.textChanged.connect(self._apply_filters)
        filter_row.addWidget(self.search_edit, stretch=1)

        self.severity_filter = ComboBox()
        self.severity_filter.addItem("Все уровни", None)
        for sev in Severity:
            self.severity_filter.addItem(sev.label_ru, sev)
        self.severity_filter.currentIndexChanged.connect(self._apply_filters)
        filter_row.addWidget(self.severity_filter)

        self.unverified_only_checkbox = QCheckBox("Только непроверенные")
        self.unverified_only_checkbox.toggled.connect(self._apply_filters)
        filter_row.addWidget(self.unverified_only_checkbox)
        root.addLayout(filter_row)

        splitter = QSplitter(Qt.Horizontal)
        self.list_widget = QListWidget()
        self.list_widget.itemClicked.connect(self._on_select)
        splitter.addWidget(self.list_widget)

        detail_card = Card(radius=18)
        detail_layout = QVBoxLayout(detail_card)
        detail_layout.setContentsMargins(20, 16, 20, 16)
        detail_layout.setSpacing(8)
        detail_layout.addWidget(_label("Детали и анализ ИИ", size=12, color=TEXT_SECONDARY, weight=700))
        self.detail_view = QTextEdit()
        self.detail_view.setReadOnly(True)
        detail_layout.addWidget(self.detail_view, stretch=1)

        self.verified_label = _label("", size=10, color=POSITIVE, weight=700)
        detail_layout.addWidget(self.verified_label)

        detail_layout.addWidget(_label(
            "Как инженер реально решил проблему (заполняется вручную) —"
            " это решение приоритетно попадёт в RAG-контекст похожих будущих инцидентов",
            size=10, color=TEXT_MUTED,
        ))
        self.correction_edit = QTextEdit()
        self.correction_edit.setStyleSheet(
            f"QTextEdit {{ background: rgba(255,255,255,0.03); border: 1px solid {PANEL_BORDER}; "
            f"border-radius: 8px; padding: 8px; font-family: {FONT_TEXT}; font-size: 12px; }}"
        )
        self.correction_edit.setPlaceholderText(
            "Например: перезапустили службу мониторинга на хосте, порт заработал "
            "после замены патч-корда..."
        )
        self.correction_edit.setFixedHeight(90)
        self.correction_edit.setEnabled(False)
        detail_layout.addWidget(self.correction_edit)

        action_row = QHBoxLayout()
        self.save_correction_btn = QPushButton("Сохранить решение инженера")
        self.save_correction_btn.setStyleSheet(SECONDARY_BUTTON_STYLE)
        self.save_correction_btn.setEnabled(False)
        self.save_correction_btn.clicked.connect(self._save_correction)
        action_row.addWidget(self.save_correction_btn)

        self.compare_btn = QPushButton("Сравнить: с RAG / без RAG")
        self.compare_btn.setStyleSheet(SECONDARY_BUTTON_STYLE)
        self.compare_btn.setEnabled(False)
        self.compare_btn.clicked.connect(self._run_comparison)
        action_row.addWidget(self.compare_btn)
        action_row.addStretch()
        detail_layout.addLayout(action_row)

        splitter.addWidget(detail_card)
        splitter.setSizes([420, 520])
        root.addWidget(splitter, stretch=1)

        self._selected_incident: Incident | None = None
        self._all_incidents: list[Incident] = []
        self._load_history()
        # то, что уже было в истории при открытии приложения, не считается «новым» —
        # непрочитанным помечается только то, что появится после этого момента
        self._seen_ids = {inc.id for inc in self._all_incidents}

    def _load_history(self):
        self._all_incidents = self.repo.get_history()
        self._apply_filters()

    def _apply_filters(self):
        query = self.search_edit.text().strip().lower()
        severity = self.severity_filter.currentData()
        unverified_only = self.unverified_only_checkbox.isChecked()

        filtered = self._all_incidents
        if query:
            filtered = [i for i in filtered if query in i.host.lower() or query in i.problem_name.lower()]
        if severity is not None:
            filtered = [i for i in filtered if i.severity == severity]
        if unverified_only:
            filtered = [i for i in filtered if not i.ai_verified]

        self.list_widget.clear()
        for inc in filtered:
            item = QListWidgetItem()
            widget = IncidentRow(inc)
            item.setSizeHint(widget.sizeHint())
            item.setData(Qt.UserRole, inc)
            self.list_widget.addItem(item)
            self.list_widget.setItemWidget(item, widget)

    def _toggle_auto_refresh(self):
        if self.auto_refresh_checkbox.isChecked():
            self._auto_timer.start(self.auto_refresh_interval.currentData())
        else:
            self._auto_timer.stop()

    def _auto_refresh_tick(self):
        # Не запускаем новый опрос, если предыдущий (обычный или из
        # Центра генерации) ещё выполняется — иначе воркеры будут копиться.
        if not self.refresh_btn.isEnabled() or self._live_workers:
            return
        self.refresh()

    def refresh(self):
        self.refresh_btn.setEnabled(False)
        self.refresh_btn.setText("Анализирую...")
        self.worker = IncidentWorker(self.zabbix_client, self.analyzer, rag=self.rag)
        self.worker.finished.connect(self._on_finished)
        self.worker.error.connect(self._on_error)
        self.worker.start()

    def _on_finished(self, incidents):
        self._save_and_embed(incidents)
        self._load_history()
        self.refresh_btn.setEnabled(True)
        self.refresh_btn.setText("Обновить и проанализировать")
        self._mark_unread(incidents)
        self.incident_added.emit()

    def _on_error(self, message):
        self.detail_view.setPlainText(f"Ошибка: {message}")
        self.refresh_btn.setEnabled(True)
        self.refresh_btn.setText("Обновить и проанализировать")

    def _save_and_embed(self, incidents: list[Incident]) -> None:
        self.repo.save_incidents(incidents)
        if self.rag is not None:
            for inc in incidents:
                if inc.ai_analyzed:
                    try:
                        self.rag.save_embedding(inc.id, inc.problem_name)
                    except Exception:
                        pass

    def submit_incident(self, incident: Incident) -> None:
        """Принимает один синтетический инцидент из Центра генерации, анализирует
        его в фоне и сразу добавляет в историю — «алерт в реальном времени»,
        без ожидания следующего полного опроса Zabbix."""
        self.live_status_label.setText(f"Получен алерт с {incident.host} — анализирую...")
        self.live_status_label.setStyleSheet(
            f"color: {WARNING}; font-size: 11px; border: none; background: transparent;"
        )
        worker = IncidentWorker(self.zabbix_client, self.analyzer, rag=self.rag, incidents=[incident])
        worker.finished.connect(lambda incidents: self._on_live_finished(worker, incidents))
        worker.error.connect(lambda msg: self._on_live_error(worker, msg))
        self._live_workers.append(worker)
        worker.start()

    def _on_live_finished(self, worker: IncidentWorker, incidents: list[Incident]) -> None:
        # Проверяем на дубликат ДО сохранения/эмбеддинга — иначе новый инцидент
        # уже был бы в базе и находил бы сам себя с нулевой дистанцией.
        duplicate_note = self._check_duplicate(incidents[0]) if incidents else None
        self._save_and_embed(incidents)
        self._load_history()
        if self.list_widget.count():
            self.list_widget.setCurrentRow(0)
            self._on_select(self.list_widget.item(0))
        host = incidents[0].host if incidents else "?"
        if duplicate_note:
            self.live_status_label.setText(f"Алерт с {host} добавлен — {duplicate_note}")
            self.live_status_label.setStyleSheet(
                f"color: {WARNING}; font-size: 11px; border: none; background: transparent;"
            )
        else:
            self.live_status_label.setText(f"Алерт с {host} проанализирован и добавлен")
            self.live_status_label.setStyleSheet(
                f"color: {POSITIVE}; font-size: 11px; border: none; background: transparent;"
            )
        if worker in self._live_workers:
            self._live_workers.remove(worker)
        self._mark_unread(incidents)
        self.incident_added.emit()

    def _check_duplicate(self, inc: Incident) -> str | None:
        """Если в истории уже есть почти идентичный случай на том же хосте —
        предупреждаем оператора, что это может быть повтор, а не новый инцидент."""
        if self.rag is None:
            return None
        try:
            similar = self.rag.find_similar(inc.problem_name, limit=3, max_distance=0.08)
        except Exception:
            return None
        for s in similar:
            if s["host"] == inc.host:
                return f"похоже на повтор случая «{s['problem_name']}» на {s['host']} из истории"
        return None

    def _mark_unread(self, incidents: list[Incident]) -> None:
        """Помечает свежепроанализированные инциденты как непрочитанные — сайдбар
        покажет красную точку у «Алерты», пока оператор не откроет вкладку."""
        new_ids = {inc.id for inc in incidents} - self._seen_ids
        if not new_ids:
            return
        was_empty = not self._unread_ids
        self._unread_ids |= new_ids
        if was_empty:
            self.unread_changed.emit(True)

    def mark_seen(self) -> None:
        """Вызывается при открытии вкладки Алерты — гасит индикатор непрочитанного."""
        if not self._unread_ids:
            return
        self._seen_ids |= self._unread_ids
        self._unread_ids.clear()
        self.unread_changed.emit(False)

    def _on_live_error(self, worker: IncidentWorker, message: str) -> None:
        self.live_status_label.setText(f"Ошибка анализа алерта: {message}")
        self.live_status_label.setStyleSheet(
            f"color: {NEGATIVE}; font-size: 11px; border: none; background: transparent;"
        )
        if worker in self._live_workers:
            self._live_workers.remove(worker)

    def _on_select(self, item):
        inc: Incident = item.data(Qt.UserRole)
        self._selected_incident = inc
        verified_note = f"\n\n--- Как решено фактически (инженер) ---\n{inc.resolution}\n" \
            if inc.ai_verified and inc.resolution else ""
        text = (
            f"Хост: {inc.host}\nПроблема: {inc.problem_name}\nКритичность: {inc.severity.label_ru}\n"
            f"Время: {inc.timestamp.strftime('%d.%m.%Y %H:%M')}\nПараметр: {inc.item_key} = {inc.last_value}\n"
            f"\n--- Суть проблемы (ИИ) ---\n{inc.ai_summary}\n"
            f"\n--- Рекомендация ИИ ---\n{inc.ai_recommendation}\n"
            f"{verified_note}"
        )
        self.detail_view.setPlainText(text)

        # Поле — отдельное хранилище фактического решения (resolution), а не
        # рекомендация ИИ: показываем то, что инженер уже вписывал раньше
        # (можно поправить), иначе поле пустое — сюда не подставляется ai_recommendation.
        self.correction_edit.setPlainText(inc.resolution)
        self.correction_edit.setEnabled(bool(inc.ai_analyzed))
        self.save_correction_btn.setEnabled(bool(inc.ai_analyzed))
        self.compare_btn.setEnabled(bool(inc.ai_analyzed) and self.rag is not None)
        self.verified_label.setText("✓ проверено инженером" if inc.ai_verified else "")

    def _save_correction(self):
        inc = self._selected_incident
        if inc is None:
            return
        resolution = self.correction_edit.toPlainText().strip()
        if not resolution:
            warn_box(
                self, "Пустое решение",
                "Опишите, как проблема была решена на самом деле, прежде чем сохранять.",
            )
            return
        self.repo.update_correction(inc.id, resolution)
        inc.resolution = resolution
        inc.ai_verified = True
        self.verified_label.setText("✓ проверено инженером")
        self._load_history()
        self.live_status_label.setText(f"Решение по {inc.host} сохранено как проверенное")
        self.live_status_label.setStyleSheet(
            f"color: {POSITIVE}; font-size: 11px; border: none; background: transparent;"
        )

    def _run_comparison(self):
        inc = self._selected_incident
        if inc is None or self.rag is None:
            return
        self.compare_btn.setEnabled(False)
        self.compare_btn.setText("Сравниваю...")
        worker = ComparisonWorker(lambda: self._comparison_task(inc))
        worker.finished.connect(lambda result: self._on_comparison_finished(worker, result))
        worker.error.connect(lambda msg: self._on_comparison_error(worker, msg))
        self._live_workers.append(worker)
        worker.start()

    def _comparison_task(self, inc: Incident) -> dict:
        """Прогоняет копию инцидента через анализатор с RAG и без — оригинал
        (и то, что видит пользователь в списке) не трогаем."""
        inc_with = copy.deepcopy(inc)
        inc_without = copy.deepcopy(inc)
        self.analyzer.analyze([inc_with], rag=self.rag)
        self.analyzer.analyze([inc_without], rag=None)
        try:
            rag_context = self.rag.build_context_block(inc.problem_name)
        except Exception:
            rag_context = ""
        return {
            "title": f"{inc.host}",
            "subtitle": inc.problem_name,
            "with_summary": inc_with.ai_summary,
            "with_recommendation": inc_with.ai_recommendation,
            "without_summary": inc_without.ai_summary,
            "without_recommendation": inc_without.ai_recommendation,
            "rag_context": rag_context,
        }

    def _on_comparison_finished(self, worker: ComparisonWorker, result: dict):
        if worker in self._live_workers:
            self._live_workers.remove(worker)
        self.compare_btn.setEnabled(True)
        self.compare_btn.setText("Сравнить: с RAG / без RAG")
        dialog = ComparisonDialog(result, parent=self)
        dialog.exec()

    def _on_comparison_error(self, worker: ComparisonWorker, message: str):
        if worker in self._live_workers:
            self._live_workers.remove(worker)
        self.compare_btn.setEnabled(True)
        self.compare_btn.setText("Сравнить: с RAG / без RAG")
        warn_box(self, "Ошибка сравнения", message)


# ---------------------------------------------------------------------------
# Конфигурации
# ---------------------------------------------------------------------------

class ConfigsTab(QWidget):
    """Работа с конфигурациями. Использует ConfigRAG для похожих случаев."""

    ALL_DEVICES = "Все устройства (diff)"

    config_added = Signal()

    def __init__(self, oxidized_client, analyzer, repo: ConfigDiffRepository, rag=None):
        super().__init__()
        self.oxidized_client = oxidized_client
        self.analyzer = analyzer
        self.repo = repo
        self.rag = rag
        self._live_workers: list[ConfigTaskWorker] = []
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(14)

        top_bar = QHBoxLayout()
        self.device_selector = ComboBox()
        self._reload_devices()
        top_bar.addWidget(self.device_selector, stretch=1)

        self.diff_btn = QPushButton("Проверить diff")
        self.diff_btn.setStyleSheet(PRIMARY_BUTTON_STYLE)
        self.diff_btn.clicked.connect(self.run_diff_check)
        top_bar.addWidget(self.diff_btn)

        self.audit_btn = QPushButton("Полный аудит конфига")
        self.audit_btn.setStyleSheet(SECONDARY_BUTTON_STYLE)
        self.audit_btn.clicked.connect(self.run_full_audit)
        top_bar.addWidget(self.audit_btn)

        root.addLayout(top_bar)

        status_row = QHBoxLayout()
        rag_status = _label(
            "RAG подключён" if self.rag else "RAG недоступен (нет nomic-embed-text)",
            size=11, color=POSITIVE if self.rag else TEXT_MUTED, font=FONT_DATA,
        )
        status_row.addWidget(rag_status)
        status_row.addStretch()
        self.live_status_label = _label("", size=11, color=TEXT_MUTED)
        status_row.addWidget(self.live_status_label)
        root.addLayout(status_row)

        filter_row = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Поиск по устройству...")
        self.search_edit.textChanged.connect(self._apply_filters)
        filter_row.addWidget(self.search_edit, stretch=1)

        self.risk_filter = ComboBox()
        self.risk_filter.addItem("Любой риск", None)
        for risk in ("LOW", "MEDIUM", "HIGH", "UNKNOWN"):
            self.risk_filter.addItem(risk, risk)
        self.risk_filter.currentIndexChanged.connect(self._apply_filters)
        filter_row.addWidget(self.risk_filter)

        self.unverified_only_checkbox = QCheckBox("Только непроверенные")
        self.unverified_only_checkbox.toggled.connect(self._apply_filters)
        filter_row.addWidget(self.unverified_only_checkbox)
        root.addLayout(filter_row)

        splitter = QSplitter(Qt.Horizontal)
        self.list_widget = QListWidget()
        self.list_widget.itemClicked.connect(self._on_select)
        splitter.addWidget(self.list_widget)

        detail_card = Card(radius=18)
        detail_layout = QVBoxLayout(detail_card)
        detail_layout.setContentsMargins(20, 16, 20, 16)
        detail_layout.setSpacing(8)
        detail_layout.addWidget(_label("ИИ-ревью конфигурации", size=12, color=TEXT_SECONDARY, weight=700))
        self.detail_view = QTextEdit()
        self.detail_view.setReadOnly(True)
        detail_layout.addWidget(self.detail_view, stretch=1)

        self.verified_label = _label("", size=10, color=POSITIVE, weight=700)
        detail_layout.addWidget(self.verified_label)

        detail_layout.addWidget(_label(
            "Как инженер реально устранил проблему в конфигурации (заполняется вручную)",
            size=10, color=TEXT_MUTED,
        ))
        self.correction_edit = QTextEdit()
        self.correction_edit.setStyleSheet(
            f"QTextEdit {{ background: rgba(255,255,255,0.03); border: 1px solid {PANEL_BORDER}; "
            f"border-radius: 8px; padding: 8px; font-family: {FONT_TEXT}; font-size: 12px; }}"
        )
        self.correction_edit.setPlaceholderText(
            "Например: убрали разрешающее правило ACL, вернули community по умолчанию..."
        )
        self.correction_edit.setFixedHeight(90)
        self.correction_edit.setEnabled(False)
        detail_layout.addWidget(self.correction_edit)

        action_row = QHBoxLayout()
        self.save_correction_btn = QPushButton("Сохранить решение инженера")
        self.save_correction_btn.setStyleSheet(SECONDARY_BUTTON_STYLE)
        self.save_correction_btn.setEnabled(False)
        self.save_correction_btn.clicked.connect(self._save_correction)
        action_row.addWidget(self.save_correction_btn)

        self.compare_btn = QPushButton("Сравнить: с RAG / без RAG")
        self.compare_btn.setStyleSheet(SECONDARY_BUTTON_STYLE)
        self.compare_btn.setEnabled(False)
        self.compare_btn.clicked.connect(self._run_comparison)
        action_row.addWidget(self.compare_btn)
        action_row.addStretch()
        detail_layout.addLayout(action_row)

        splitter.addWidget(detail_card)
        splitter.setSizes([420, 520])
        root.addWidget(splitter, stretch=1)

        self._selected_row: dict | None = None
        self._all_rows: list[dict] = []

        self._load_history()

    def _reload_devices(self):
        self.device_selector.clear()
        self.device_selector.addItem(self.ALL_DEVICES)
        try:
            for node in self.oxidized_client.get_nodes():
                self.device_selector.addItem(node)
        except Exception:
            pass

    def _load_history(self):
        self._all_rows = self.repo.get_history()
        self._apply_filters()

    def _apply_filters(self):
        query = self.search_edit.text().strip().lower()
        risk = self.risk_filter.currentData()
        unverified_only = self.unverified_only_checkbox.isChecked()

        filtered = self._all_rows
        if query:
            filtered = [r for r in filtered if query in (r.get("node") or "").lower()]
        if risk is not None:
            filtered = [r for r in filtered if (r.get("ai_risk_level") or "UNKNOWN").upper() == risk]
        if unverified_only:
            filtered = [r for r in filtered if not r.get("ai_verified")]

        self.list_widget.clear()
        for row in filtered:
            item = QListWidgetItem()
            widget = ConfigDiffRow(row)
            item.setSizeHint(widget.sizeHint())
            item.setData(Qt.UserRole, row)
            self.list_widget.addItem(item)
            self.list_widget.setItemWidget(item, widget)

    def run_diff_check(self):
        selected = self.device_selector.currentText()
        self.diff_btn.setEnabled(False)
        self.diff_btn.setText("Анализирую...")

        if selected == self.ALL_DEVICES:
            task_fn = self._task_diff_all
        else:
            task_fn = lambda: self._task_diff_single(selected)

        self.worker = ConfigTaskWorker(task_fn)
        self.worker.finished.connect(lambda results: self._on_finished(results, self.diff_btn, "Проверить diff"))
        self.worker.error.connect(lambda msg: self._on_error(msg, self.diff_btn, "Проверить diff"))
        self.worker.start()

    def run_full_audit(self):
        selected = self.device_selector.currentText()
        if selected == self.ALL_DEVICES:
            warn_box(
                self, "Полный аудит",
                "Для полного аудита выберите конкретное устройство, не «Все устройства».",
            )
            return

        self.audit_btn.setEnabled(False)
        self.audit_btn.setText("Анализирую...")

        self.worker = ConfigTaskWorker(lambda: self._task_full_audit(selected))
        self.worker.finished.connect(lambda results: self._on_finished(results, self.audit_btn, "Полный аудит конфига"))
        self.worker.error.connect(lambda msg: self._on_error(msg, self.audit_btn, "Полный аудит конфига"))
        self.worker.start()

    def _task_diff_all(self) -> list:
        diffs = self.oxidized_client.get_all_diffs()
        results = []
        for d in diffs:
            review = self.analyzer.analyze_config_diff(d.diff_text, rag=self.rag)
            results.append({"node": d.node, "diff_text": d.diff_text, "review_type": "diff", **review})
        return results

    def _task_diff_single(self, node: str) -> list:
        d = self.oxidized_client.get_diff(node, "prev", "latest")
        review = self.analyzer.analyze_config_diff(d.diff_text, rag=self.rag)
        return [{"node": node, "diff_text": d.diff_text, "review_type": "diff", **review}]

    def _task_full_audit(self, node: str) -> list:
        config_text = self.oxidized_client.get_current_config(node)
        review = self.analyzer.analyze_full_config(config_text, rag=self.rag)
        return [{"node": node, "diff_text": config_text, "review_type": "full_audit", **review}]

    def _save_results(self, results: list[dict]) -> None:
        for r in results:
            new_id = self.repo.save_diff(
                node=r["node"], version_from="prev", version_to="latest",
                diff_text=r["diff_text"], ai_summary=r.get("summary", ""),
                ai_risk_level=r.get("risk_level", "UNKNOWN"),
                ai_recommendation=r.get("recommendation", ""),
                review_type=r.get("review_type", "diff"),
            )
            if self.rag is not None:
                try:
                    self.rag.save_embedding(new_id, r["diff_text"])
                except Exception:
                    pass

    def _on_finished(self, results, btn, btn_text):
        self._save_results(results)
        self._load_history()
        btn.setEnabled(True)
        btn.setText(btn_text)
        self.config_added.emit()

    def _on_error(self, message, btn, btn_text):
        self.detail_view.setPlainText(f"Ошибка: {message}")
        btn.setEnabled(True)
        btn.setText(btn_text)

    def _task_from_external(self, node: str, review_type: str, text: str) -> list:
        if review_type == "full_audit":
            review = self.analyzer.analyze_full_config(text, rag=self.rag)
        else:
            review = self.analyzer.analyze_config_diff(text, rag=self.rag)
        return [{"node": node, "diff_text": text, "review_type": review_type, **review}]

    def submit_config(self, node: str, review_type: str, text: str) -> None:
        """Принимает diff/конфиг из Центра генерации, анализирует в фоне и сразу
        добавляет в историю проверок — без нажатия «Проверить diff»/«Аудит»."""
        self.live_status_label.setText(f"Получена конфигурация {node} — анализирую...")
        self.live_status_label.setStyleSheet(
            f"color: {WARNING}; font-size: 11px; border: none; background: transparent;"
        )
        worker = ConfigTaskWorker(lambda: self._task_from_external(node, review_type, text))
        worker.finished.connect(lambda results: self._on_live_finished(worker, results))
        worker.error.connect(lambda msg: self._on_live_error(worker, msg))
        self._live_workers.append(worker)
        worker.start()

    def _on_live_finished(self, worker: ConfigTaskWorker, results: list[dict]) -> None:
        self._save_results(results)
        self._load_history()
        if self.list_widget.count():
            self.list_widget.setCurrentRow(0)
            self._on_select(self.list_widget.item(0))
        node = results[0]["node"] if results else "?"
        self.live_status_label.setText(f"Конфигурация {node} проанализирована и добавлена")
        self.live_status_label.setStyleSheet(
            f"color: {POSITIVE}; font-size: 11px; border: none; background: transparent;"
        )
        if worker in self._live_workers:
            self._live_workers.remove(worker)
        self.config_added.emit()

    def _on_live_error(self, worker: ConfigTaskWorker, message: str) -> None:
        self.live_status_label.setText(f"Ошибка анализа конфигурации: {message}")
        self.live_status_label.setStyleSheet(
            f"color: {NEGATIVE}; font-size: 11px; border: none; background: transparent;"
        )
        if worker in self._live_workers:
            self._live_workers.remove(worker)

    def _on_select(self, item):
        row = item.data(Qt.UserRole)
        self._selected_row = row
        kind = "Полный аудит" if row.get("review_type") == "full_audit" else "Diff-ревью"
        verified_note = f"\n--- Как решено фактически (инженер) ---\n{row.get('resolution')}\n" \
            if row.get("ai_verified") and row.get("resolution") else ""
        text = (
            f"Устройство: {row.get('node')}\n"
            f"Тип проверки: {kind}\n"
            f"Уровень риска: {row.get('ai_risk_level')}\n\n"
            f"--- Исходные данные ---\n{row.get('diff_text')}\n\n"
            f"--- Суть (ИИ) ---\n{row.get('ai_summary')}\n"
            f"\n--- Рекомендация ИИ ---\n{row.get('ai_recommendation')}\n"
            f"{verified_note}"
        )
        self.detail_view.setPlainText(text)

        self.correction_edit.setPlainText(row.get("resolution") or "")
        has_analysis = bool(row.get("ai_summary"))
        self.correction_edit.setEnabled(has_analysis)
        self.save_correction_btn.setEnabled(has_analysis)
        self.compare_btn.setEnabled(has_analysis and self.rag is not None)
        self.verified_label.setText("✓ проверено инженером" if row.get("ai_verified") else "")

    def _save_correction(self):
        row = self._selected_row
        if row is None:
            return
        resolution = self.correction_edit.toPlainText().strip()
        if not resolution:
            warn_box(
                self, "Пустое решение",
                "Опишите, как проблема была решена на самом деле, прежде чем сохранять.",
            )
            return
        self.repo.update_correction(row["id"], resolution)
        row["resolution"] = resolution
        row["ai_verified"] = True
        self.verified_label.setText("✓ проверено инженером")
        self._load_history()
        self.live_status_label.setText(f"Решение по {row.get('node')} сохранено как проверенное")
        self.live_status_label.setStyleSheet(
            f"color: {POSITIVE}; font-size: 11px; border: none; background: transparent;"
        )

    def _run_comparison(self):
        row = self._selected_row
        if row is None or self.rag is None:
            return
        self.compare_btn.setEnabled(False)
        self.compare_btn.setText("Сравниваю...")
        worker = ComparisonWorker(lambda: self._comparison_task(row))
        worker.finished.connect(lambda result: self._on_comparison_finished(worker, result))
        worker.error.connect(lambda msg: self._on_comparison_error(worker, msg))
        self._live_workers.append(worker)
        worker.start()

    def _comparison_task(self, row: dict) -> dict:
        text = row.get("diff_text") or ""
        review_type = row.get("review_type", "diff")
        analyze_fn = self.analyzer.analyze_full_config if review_type == "full_audit" \
            else self.analyzer.analyze_config_diff
        with_rag = analyze_fn(text, rag=self.rag)
        without_rag = analyze_fn(text, rag=None)
        try:
            rag_context = self.rag.build_context_block(text)
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

    def _on_comparison_finished(self, worker: ComparisonWorker, result: dict):
        if worker in self._live_workers:
            self._live_workers.remove(worker)
        self.compare_btn.setEnabled(True)
        self.compare_btn.setText("Сравнить: с RAG / без RAG")
        dialog = ComparisonDialog(result, parent=self)
        dialog.exec()

    def _on_comparison_error(self, worker: ComparisonWorker, message: str):
        if worker in self._live_workers:
            self._live_workers.remove(worker)
        self.compare_btn.setEnabled(True)
        self.compare_btn.setText("Сравнить: с RAG / без RAG")
        warn_box(self, "Ошибка сравнения", message)


# ---------------------------------------------------------------------------
# Настройки
# ---------------------------------------------------------------------------

class SettingsTab(QScrollArea):
    """QScrollArea (не QWidget) — карточек здесь больше, чем помещается по
    высоте на многих экранах; без прокрутки Qt сжимает layout, чтобы влезть
    в доступную высоту, и поля визуально «плющит» друг в друга."""

    def __init__(self, settings_manager: SettingsManager, on_saved=None, offline_mode: bool = False):
        super().__init__()
        self.settings_manager = settings_manager
        self.on_saved = on_saved
        self.offline_mode = offline_mode

        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        self.viewport().setStyleSheet("background: transparent;")

        inner = QWidget()
        inner.setStyleSheet("background: transparent;")
        root = QVBoxLayout(inner)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(16)
        self.setWidget(inner)

        # --- PostgreSQL: подключение читается ДО выбора бэкенда (local_config.py),
        # поэтому хранится отдельно от остальных настроек (app_settings живёт
        # уже внутри выбранной БД — курица и яйцо).
        pg_card = Card(radius=18, accent_left=(TEXT_MUTED if self.offline_mode else POSITIVE))
        pg_layout = QVBoxLayout(pg_card)
        pg_layout.setContentsMargins(24, 20, 24, 20)
        pg_layout.setSpacing(10)

        pg_header = QHBoxLayout()
        pg_header.addWidget(_label("PostgreSQL", size=14, weight=700))
        pg_header.addStretch()
        current_storage = "сейчас: SQLite (офлайн)" if self.offline_mode else "сейчас: PostgreSQL"
        pg_header.addWidget(_label(
            current_storage, size=11, weight=700,
            color=TEXT_MUTED if self.offline_mode else POSITIVE,
        ))
        pg_layout.addLayout(pg_header)

        pg_layout.addWidget(_label(
            "Если PostgreSQL недоступен при запуске, приложение автоматически "
            "работает на локальном SQLite (без RAG). Укажите параметры ниже и "
            "сохраните, чтобы перейти на PostgreSQL — потребуется перезапуск.",
            size=11, color=TEXT_MUTED,
        ))

        pg_config = load_postgres_config()
        pg_form = QFormLayout()
        pg_form.setSpacing(12)
        self.pg_host = QLineEdit(pg_config["host"])
        self.pg_port = QLineEdit(pg_config["port"])
        self.pg_dbname = QLineEdit(pg_config["dbname"])
        self.pg_user = QLineEdit(pg_config["user"])
        self.pg_password = QLineEdit(pg_config["password"])
        self.pg_password.setEchoMode(QLineEdit.Password)
        for label, widget in [
            ("Хост", self.pg_host), ("Порт", self.pg_port), ("База данных", self.pg_dbname),
            ("Пользователь", self.pg_user), ("Пароль", self.pg_password),
        ]:
            pg_form.addRow(_label(label, size=12, color=TEXT_SECONDARY), widget)
        pg_layout.addLayout(pg_form)

        pg_action_row = QHBoxLayout()
        pg_check_btn = QPushButton("Проверить подключение")
        pg_check_btn.setStyleSheet(SECONDARY_BUTTON_STYLE)
        pg_check_btn.clicked.connect(self._check_postgres_now)
        pg_action_row.addWidget(pg_check_btn)

        pg_save_btn = QPushButton("Сохранить и использовать PostgreSQL")
        pg_save_btn.setStyleSheet(PRIMARY_BUTTON_STYLE)
        pg_save_btn.clicked.connect(self._save_postgres_config)
        pg_action_row.addWidget(pg_save_btn)
        pg_action_row.addStretch()
        pg_layout.addLayout(pg_action_row)

        self.pg_status_label = _label("", size=11, color=TEXT_MUTED)
        pg_layout.addWidget(self.pg_status_label)

        root.addWidget(pg_card)

        card = Card(radius=18)
        form_layout = QVBoxLayout(card)
        form_layout.setContentsMargins(24, 20, 24, 20)
        form = QFormLayout()
        form.setSpacing(12)

        settings = self.settings_manager.load()

        self.zabbix_url = QLineEdit(settings.zabbix_url)
        self.zabbix_user = QLineEdit(settings.zabbix_user)
        self.zabbix_password = QLineEdit(settings.zabbix_password)
        self.zabbix_password.setEchoMode(QLineEdit.Password)
        self.oxidized_url = QLineEdit(settings.oxidized_url)
        self.ollama_host = QLineEdit(settings.ollama_host)
        self.ollama_model = QLineEdit(settings.ollama_model)
        self.embedding_model = QLineEdit(settings.embedding_model)

        for label, widget in [
            ("Zabbix URL", self.zabbix_url),
            ("Zabbix логин", self.zabbix_user),
            ("Zabbix пароль", self.zabbix_password),
            ("Oxidized URL", self.oxidized_url),
            ("Ollama host", self.ollama_host),
            ("Ollama модель", self.ollama_model),
            ("Модель эмбеддингов (RAG)", self.embedding_model),
        ]:
            lbl = _label(label, size=12, color=TEXT_SECONDARY)
            form.addRow(lbl, widget)

        form_layout.addLayout(form)
        root.addWidget(card)

        save_btn = QPushButton("Сохранить настройки")
        save_btn.setStyleSheet(PRIMARY_BUTTON_STYLE)
        save_btn.clicked.connect(self._save)
        root.addWidget(save_btn, alignment=Qt.AlignLeft)

        root.addWidget(_label("Изменения применяются после перезапуска приложения.", size=11, color=TEXT_MUTED))

        # --- Проверка подключений ---
        check_card = Card(radius=18)
        check_layout = QVBoxLayout(check_card)
        check_layout.setContentsMargins(24, 20, 24, 20)
        check_layout.setSpacing(10)
        check_layout.addWidget(_label("Проверка подключений", size=14, weight=700))
        check_layout.addWidget(_label(
            "Проверяет доступность по текущим значениям полей выше (даже если ещё не сохранены).",
            size=11, color=TEXT_MUTED,
        ))

        self._check_workers: list[ConnectionCheckWorker] = []
        self._check_rows: dict[str, tuple[QLabel, QPushButton]] = {}
        services = [
            ("zabbix", "Zabbix", self._check_zabbix),
            ("oxidized", "Oxidized", self._check_oxidized),
            ("ollama", "Ollama (модель анализа)", self._check_ollama),
            ("embedding", "Ollama (модель эмбеддингов / RAG)", self._check_embedding),
        ]
        for key, label, check_fn in services:
            row = QHBoxLayout()
            row.addWidget(_label(label, size=12, font=FONT_DATA), stretch=1)
            status_label = _label("не проверено", size=11, color=TEXT_MUTED)
            row.addWidget(status_label)
            check_btn = QPushButton("Проверить")
            check_btn.setStyleSheet(SECONDARY_BUTTON_STYLE)
            check_btn.clicked.connect(lambda _, k=key, fn=check_fn: self._run_check(k, fn))
            row.addWidget(check_btn)
            check_layout.addLayout(row)
            self._check_rows[key] = (status_label, check_btn)

        root.addWidget(check_card)
        root.addStretch()

    def _run_check(self, key: str, check_fn) -> None:
        status_label, check_btn = self._check_rows[key]
        check_btn.setEnabled(False)
        status_label.setText("проверяю...")
        status_label.setStyleSheet(f"color: {WARNING}; font-size: 11px; border: none; background: transparent;")
        worker = ConnectionCheckWorker(check_fn)
        worker.finished.connect(lambda ok, msg: self._on_check_finished(worker, key, ok, msg))
        self._check_workers.append(worker)
        worker.start()

    def _on_check_finished(self, worker: ConnectionCheckWorker, key: str, ok: bool, message: str) -> None:
        if worker in self._check_workers:
            self._check_workers.remove(worker)
        status_label, check_btn = self._check_rows[key]
        status_label.setText(("✓ " if ok else "✗ ") + (message or ("ок" if ok else "недоступно")))
        status_label.setStyleSheet(
            f"color: {POSITIVE if ok else NEGATIVE}; font-size: 11px; border: none; background: transparent;"
        )
        check_btn.setEnabled(True)

    def _check_zabbix(self):
        try:
            client = ZabbixClient(self.zabbix_url.text(), self.zabbix_user.text(), self.zabbix_password.text())
            ok = client.test_connection()
            return ok, ("подключение успешно" if ok else "не удалось получить версию API")
        except Exception as e:
            return False, str(e)[:150]

    def _check_oxidized(self):
        try:
            client = OxidizedClient(self.oxidized_url.text())
            nodes = client.get_nodes()
            return True, f"{len(nodes)} устройств"
        except Exception as e:
            return False, str(e)[:150]

    def _check_ollama(self):
        try:
            analyzer = OllamaAnalyzer(model=self.ollama_model.text(), host=self.ollama_host.text())
            ok = analyzer.is_available()
            return ok, ("модель доступна" if ok else "сервер отвечает, но модель не скачана (ollama pull)")
        except Exception as e:
            return False, str(e)[:150]

    def _check_embedding(self):
        try:
            client = EmbeddingClient(host=self.ollama_host.text(), model=self.embedding_model.text())
            ok = client.is_available()
            return ok, ("модель доступна" if ok else "не отвечает или модель не скачана")
        except Exception as e:
            return False, str(e)[:150]

    def _current_pg_config(self) -> dict:
        return {
            "host": self.pg_host.text().strip() or "localhost",
            "port": self.pg_port.text().strip() or "5432",
            "dbname": self.pg_dbname.text().strip() or "netai_monitor",
            "user": self.pg_user.text().strip() or "postgres",
            "password": self.pg_password.text(),
        }

    def _check_postgres(self):
        try:
            import psycopg2
            conn = psycopg2.connect(build_dsn(self._current_pg_config()), connect_timeout=5)
            conn.close()
            return True, "подключение успешно"
        except Exception as e:
            return False, str(e)[:150]

    def _check_postgres_now(self):
        self.pg_status_label.setText("проверяю...")
        self.pg_status_label.setStyleSheet(f"color: {WARNING}; font-size: 11px; border: none; background: transparent;")
        worker = ConnectionCheckWorker(self._check_postgres)
        worker.finished.connect(lambda ok, msg: self._on_postgres_check_finished(worker, ok, msg))
        self._check_workers.append(worker)
        worker.start()

    def _on_postgres_check_finished(self, worker, ok: bool, message: str) -> None:
        if worker in self._check_workers:
            self._check_workers.remove(worker)
        self.pg_status_label.setText(("✓ " if ok else "✗ ") + message)
        self.pg_status_label.setStyleSheet(
            f"color: {POSITIVE if ok else NEGATIVE}; font-size: 11px; border: none; background: transparent;"
        )

    def _save_postgres_config(self):
        save_postgres_config(self._current_pg_config())
        info_box(
            self, "PostgreSQL",
            "Параметры подключения сохранены. Перезапустите приложение — оно "
            "попробует подключиться к PostgreSQL и, если получится, перейдёт "
            "с локального SQLite на него автоматически.",
        )

    def _save(self):
        settings = AppSettings(
            zabbix_url=self.zabbix_url.text(),
            zabbix_user=self.zabbix_user.text(),
            zabbix_password=self.zabbix_password.text(),
            oxidized_url=self.oxidized_url.text(),
            ollama_host=self.ollama_host.text(),
            ollama_model=self.ollama_model.text(),
            embedding_model=self.embedding_model.text(),
            use_synthetic_data=self.settings_manager.load().use_synthetic_data,
        )
        self.settings_manager.save(settings)
        info_box(self, "Настройки", "Сохранено. Перезапустите приложение для применения.")
        if self.on_saved:
            self.on_saved()


# ---------------------------------------------------------------------------
# Синтетические данные
# ---------------------------------------------------------------------------

class SyntheticDataTab(QWidget):
    """Демо-режим, очистка БД и переиндексация RAG-эмбеддингов."""

    def __init__(self, settings_manager: SettingsManager, incident_repo: IncidentRepository,
                 config_repo: ConfigDiffRepository, incident_rag=None, config_rag=None):
        super().__init__()
        self.settings_manager = settings_manager
        self.incident_repo = incident_repo
        self.config_repo = config_repo
        self.incident_rag = incident_rag
        self.config_rag = config_rag
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(16)

        card = Card(radius=18)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)

        layout.addWidget(_label("Режим синтетических данных", size=14, weight=700))
        desc = QLabel(
            "Включает вымышленные данные (MockZabbixClient / MockOxidizedClient) вместо "
            "реального подключения к предприятию."
        )
        desc.setWordWrap(True)
        desc.setStyleSheet(f"color: {TEXT_SECONDARY}; font-size: 12px; border: none; background: transparent;")
        layout.addWidget(desc)

        settings = self.settings_manager.load()
        self.checkbox = QCheckBox("Использовать синтетические данные (демо-режим)")
        self.checkbox.setChecked(settings.use_synthetic_data)
        layout.addWidget(self.checkbox)

        save_btn = QPushButton("Применить")
        save_btn.setStyleSheet(PRIMARY_BUTTON_STYLE)
        save_btn.clicked.connect(self._save)
        layout.addWidget(save_btn, alignment=Qt.AlignLeft)

        root.addWidget(card)

        # --- RAG: переиндексация ---
        rag_card = Card(radius=18)
        rag_layout = QVBoxLayout(rag_card)
        rag_layout.setContentsMargins(24, 20, 24, 20)
        rag_layout.setSpacing(12)

        rag_layout.addWidget(_label("RAG — база знаний из истории", size=14, weight=700))
        rag_desc = QLabel(
            "Считает векторные представления (embedding) для записей истории, у которых "
            "их ещё нет. Нужно запускать после накопления новых проанализированных "
            "инцидентов/конфигураций, чтобы RAG видел свежие случаи."
        )
        rag_desc.setWordWrap(True)
        rag_desc.setStyleSheet(f"color: {TEXT_SECONDARY}; font-size: 12px; border: none; background: transparent;")
        rag_layout.addWidget(rag_desc)

        reindex_btn = QPushButton("Переиндексировать историю")
        reindex_btn.setStyleSheet(SECONDARY_BUTTON_STYLE)
        reindex_btn.clicked.connect(self._reindex)
        rag_layout.addWidget(reindex_btn, alignment=Qt.AlignLeft)

        root.addWidget(rag_card)

        # --- Очистка БД ---
        danger_card = Card(radius=18, accent_left=NEGATIVE)
        danger_layout = QVBoxLayout(danger_card)
        danger_layout.setContentsMargins(24, 20, 24, 20)
        danger_layout.setSpacing(12)

        danger_layout.addWidget(_label("Очистка базы данных", size=14, weight=700, color=NEGATIVE))
        danger_desc = QLabel(
            "Удаляет ВСЮ историю инцидентов и все результаты проверок конфигураций "
            "(diff и полный аудит) из PostgreSQL. Действие необратимо."
        )
        danger_desc.setWordWrap(True)
        danger_desc.setStyleSheet(f"color: {TEXT_SECONDARY}; font-size: 12px; border: none; background: transparent;")
        danger_layout.addWidget(danger_desc)

        clear_btn = QPushButton("Очистить все данные (алерты + конфигурации)")
        clear_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {NEGATIVE}; "
            f"border: 1px solid {NEGATIVE}; border-radius: 8px; padding: 9px 16px; "
            f"font-size: 12px; font-weight: 600; }}"
            f"QPushButton:hover {{ background: rgba(248,113,113,0.1); }}"
        )
        clear_btn.clicked.connect(self._clear_database)
        danger_layout.addWidget(clear_btn, alignment=Qt.AlignLeft)

        root.addWidget(danger_card)
        root.addStretch()

    def _save(self):
        settings = self.settings_manager.load()
        settings.use_synthetic_data = self.checkbox.isChecked()
        self.settings_manager.save(settings)
        info_box(
            self, "Синтетические данные", "Сохранено. Перезапустите приложение для применения."
        )

    def _reindex(self):
        if self.incident_rag is None and self.config_rag is None:
            warn_box(
                self, "RAG недоступен",
                "Модель nomic-embed-text не найдена в Ollama. Выполните: ollama pull nomic-embed-text",
            )
            return

        incidents_indexed = self.incident_rag.reindex_missing(self.incident_repo) if self.incident_rag else 0
        configs_indexed = self.config_rag.reindex_missing(self.config_repo) if self.config_rag else 0

        info_box(
            self, "Переиндексация завершена",
            f"Проиндексировано: {incidents_indexed} инцидентов, {configs_indexed} проверок конфигураций.",
        )

    def _clear_database(self):
        confirmed = confirm_box(
            self, "Подтверждение очистки",
            "Удалить ВСЮ историю инцидентов и все проверки конфигураций без возможности отмены?",
        )
        if not confirmed:
            return

        incidents_deleted = self.incident_repo.clear_all()
        configs_deleted = self.config_repo.clear_all()

        info_box(
            self, "Готово",
            f"Удалено: {incidents_deleted} инцидентов, {configs_deleted} проверок конфигураций.\n"
            f"Перезапустите приложение, чтобы вкладки обновились с чистого листа.",
        )


# ---------------------------------------------------------------------------
# Центр синтетических данных (генератор алертов/конфигураций)
# ---------------------------------------------------------------------------

class GeneratedFeedRow(QWidget):
    """Строка в локальной ленте «Отправлено из генератора» — не история БД,
    а быстрая обратная связь оператору, что именно и когда он отправил."""

    def __init__(self, title: str, subtitle: str, color: str):
        super().__init__()
        self.setStyleSheet("background: transparent;")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 4)
        layout.setSpacing(8)
        dot = QLabel("●")
        dot.setStyleSheet(f"color: {color}; font-size: 8px; border: none; background: transparent;")
        dot.setFixedWidth(10)
        layout.addWidget(dot)
        text_col = QVBoxLayout()
        text_col.setSpacing(0)
        text_col.addWidget(_label(title, size=11, weight=700, font=FONT_DATA))
        text_col.addWidget(_label(subtitle, size=10, color=TEXT_MUTED))
        layout.addLayout(text_col, stretch=1)
        layout.addWidget(_label(datetime.now().strftime("%H:%M:%S"), size=9, color=TEXT_MUTED, font=FONT_DATA))


class GeneratorTab(QScrollArea):
    """Центр синтетических данных: конструктор алертов и конфигураций,
    которые можно отправить напрямую во вкладки Алерты/Конфигурации —
    там они анализируются в реальном времени (см. AlertsTab.submit_incident /
    ConfigsTab.submit_config). Пресеты берутся из тех же таблиц, что и у
    MockZabbixClient/MockOxidizedClient, но поля полностью редактируемые."""

    def __init__(self, on_send_incident, on_send_config):
        super().__init__()
        self.on_send_incident = on_send_incident
        self.on_send_config = on_send_config
        self._preset_oxidized = MockOxidizedClient()
        self._sent_count = 0

        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        self.viewport().setStyleSheet("background: transparent;")

        inner = QWidget()
        inner.setStyleSheet("background: transparent;")
        root = QVBoxLayout(inner)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(16)
        self.setWidget(inner)

        title_col = QVBoxLayout()
        title_col.setSpacing(2)
        title_col.addWidget(_label("Центр синтетических данных", size=18, weight=700))
        title_col.addWidget(_label(
            "Соберите алерт или изменение конфигурации и отправьте его напрямую в анализ — "
            "как будто событие только что пришло из Zabbix/Oxidized.",
            size=11, color=TEXT_MUTED,
        ))
        root.addLayout(title_col)

        columns = QHBoxLayout()
        columns.setSpacing(16)
        columns.addWidget(self._build_incident_card(), stretch=1)
        columns.addWidget(self._build_config_card(), stretch=1)
        root.addLayout(columns)

        feed_card = Card(radius=18)
        feed_layout = QVBoxLayout(feed_card)
        feed_layout.setContentsMargins(20, 16, 20, 16)
        feed_layout.addWidget(_label("Лента отправленных событий", size=12, weight=700))
        self.feed_col = QVBoxLayout()
        self.feed_col.setSpacing(2)
        feed_layout.addLayout(self.feed_col)
        self.feed_empty_label = _label("Пока ничего не отправлено.", size=11, color=TEXT_MUTED)
        self.feed_col.addWidget(self.feed_empty_label)
        root.addWidget(feed_card)
        root.addStretch()

    # -- конструктор алерта -------------------------------------------------

    def _build_incident_card(self) -> Card:
        card = Card(radius=18)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)

        header = QHBoxLayout()
        header.addWidget(_label("Конструктор алерта", size=13, weight=700))
        header.addStretch()
        random_btn = QPushButton("Случайный")
        random_btn.setStyleSheet(SECONDARY_BUTTON_STYLE)
        random_btn.clicked.connect(self._randomize_incident)
        header.addWidget(random_btn)
        layout.addLayout(header)

        form = QFormLayout()
        form.setSpacing(8)

        self.inc_host_combo = ComboBox()
        self.inc_host_combo.setEditable(True)
        self.inc_host_combo.addItems(MockZabbixClient.HOSTS)
        form.addRow(_label("Хост", size=11, color=TEXT_SECONDARY), self.inc_host_combo)

        self.inc_template_combo = ComboBox()
        for template, severity, key, value in MockZabbixClient.PROBLEM_TEMPLATES:
            self.inc_template_combo.addItem(template, (template, severity, key, value))
        self.inc_template_combo.currentIndexChanged.connect(self._apply_incident_template)
        form.addRow(_label("Шаблон проблемы", size=11, color=TEXT_SECONDARY), self.inc_template_combo)

        self.inc_iface_combo = ComboBox()
        self.inc_iface_combo.setEditable(True)
        self.inc_iface_combo.addItems(MockZabbixClient.IFACES)
        self.inc_iface_combo.currentIndexChanged.connect(self._apply_incident_template)
        form.addRow(_label("Интерфейс (если применимо)", size=11, color=TEXT_SECONDARY), self.inc_iface_combo)

        self.inc_problem_edit = QLineEdit()
        form.addRow(_label("Текст проблемы", size=11, color=TEXT_SECONDARY), self.inc_problem_edit)

        self.inc_severity_combo = ComboBox()
        for sev in Severity:
            self.inc_severity_combo.addItem(sev.label_ru, sev)
        form.addRow(_label("Критичность", size=11, color=TEXT_SECONDARY), self.inc_severity_combo)

        self.inc_key_edit = QLineEdit()
        form.addRow(_label("Параметр (item_key)", size=11, color=TEXT_SECONDARY), self.inc_key_edit)

        self.inc_value_edit = QLineEdit()
        form.addRow(_label("Значение", size=11, color=TEXT_SECONDARY), self.inc_value_edit)

        layout.addLayout(form)

        self._apply_incident_template()

        send_btn = QPushButton("Отправить алерт")
        send_btn.setStyleSheet(PRIMARY_BUTTON_STYLE)
        send_btn.clicked.connect(self._send_incident)
        layout.addWidget(send_btn, alignment=Qt.AlignLeft)

        return card

    def _apply_incident_template(self) -> None:
        data = self.inc_template_combo.currentData()
        if not data:
            return
        template, severity, key, value = data
        host = self.inc_host_combo.currentText() or "SW-CORE-01"
        iface = self.inc_iface_combo.currentText() or "Gi0/1"
        self.inc_problem_edit.setText(template.format(host=host, iface=iface))
        index = self.inc_severity_combo.findData(severity)
        if index >= 0:
            self.inc_severity_combo.setCurrentIndex(index)
        self.inc_key_edit.setText(key)
        self.inc_value_edit.setText(value)

    def _randomize_incident(self) -> None:
        host = random.choice(MockZabbixClient.HOSTS)
        template, severity, key, value = random.choice(MockZabbixClient.PROBLEM_TEMPLATES)
        iface = random.choice(MockZabbixClient.IFACES)

        self.inc_host_combo.setCurrentText(host)
        self.inc_iface_combo.setCurrentText(iface)
        index = self.inc_template_combo.findText(template)
        if index >= 0:
            self.inc_template_combo.setCurrentIndex(index)
        else:
            self.inc_problem_edit.setText(template.format(host=host, iface=iface))
            severity_index = self.inc_severity_combo.findData(severity)
            if severity_index >= 0:
                self.inc_severity_combo.setCurrentIndex(severity_index)
            self.inc_key_edit.setText(key)
            self.inc_value_edit.setText(value)

    def _send_incident(self) -> None:
        self._sent_count += 1
        severity: Severity = self.inc_severity_combo.currentData() or Severity.WARNING
        incident = Incident(
            id=f"gen-{datetime.now().strftime('%Y%m%d%H%M%S%f')}",
            host=self.inc_host_combo.currentText().strip() or "SW-CORE-01",
            problem_name=self.inc_problem_edit.text().strip() or "Синтетическая проблема",
            severity=severity,
            timestamp=datetime.now(),
            item_key=self.inc_key_edit.text().strip(),
            last_value=self.inc_value_edit.text().strip(),
        )
        self.on_send_incident(incident)
        self._push_feed(incident.host, incident.problem_name[:48], severity.color)

    # -- конструктор конфигурации -------------------------------------------

    def _build_config_card(self) -> Card:
        card = Card(radius=18)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)

        layout.addWidget(_label("Конструктор конфигурации", size=13, weight=700))

        form = QFormLayout()
        form.setSpacing(8)

        self.cfg_device_combo = ComboBox()
        self.cfg_device_combo.addItems(self._preset_oxidized.get_nodes())
        self.cfg_device_combo.currentIndexChanged.connect(self._load_config_preset)
        form.addRow(_label("Устройство", size=11, color=TEXT_SECONDARY), self.cfg_device_combo)

        self.cfg_type_combo = ComboBox()
        self.cfg_type_combo.addItem("Diff (было/стало)", "diff")
        self.cfg_type_combo.addItem("Полный аудит конфига", "full_audit")
        self.cfg_type_combo.currentIndexChanged.connect(self._load_config_preset)
        form.addRow(_label("Тип проверки", size=11, color=TEXT_SECONDARY), self.cfg_type_combo)

        layout.addLayout(form)

        layout.addWidget(_label("Текст diff/конфига (редактируемый)", size=11, color=TEXT_SECONDARY))
        self.cfg_text_edit = QTextEdit()
        self.cfg_text_edit.setStyleSheet(
            f"QTextEdit {{ background: rgba(255,255,255,0.03); border: 1px solid {PANEL_BORDER}; "
            f"border-radius: 8px; padding: 10px; font-family: {FONT_DATA}; font-size: 12px; }}"
        )
        self.cfg_text_edit.setMinimumHeight(160)
        layout.addWidget(self.cfg_text_edit)

        self._load_config_preset()

        btn_row = QHBoxLayout()
        reset_btn = QPushButton("Сбросить к пресету")
        reset_btn.setStyleSheet(SECONDARY_BUTTON_STYLE)
        reset_btn.clicked.connect(self._load_config_preset)
        btn_row.addWidget(reset_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        send_btn = QPushButton("Отправить в конфигурации")
        send_btn.setStyleSheet(PRIMARY_BUTTON_STYLE)
        send_btn.clicked.connect(self._send_config)
        layout.addWidget(send_btn, alignment=Qt.AlignLeft)

        return card

    def _load_config_preset(self) -> None:
        node = self.cfg_device_combo.currentText()
        review_type = self.cfg_type_combo.currentData()
        if not node:
            return
        try:
            if review_type == "full_audit":
                text = self._preset_oxidized.get_current_config(node)
            else:
                text = self._preset_oxidized.get_diff(node, "prev", "latest").diff_text
        except Exception as e:
            text = f"! Не удалось загрузить пресет: {e}"
        self.cfg_text_edit.setPlainText(text)

    def _send_config(self) -> None:
        self._sent_count += 1
        node = self.cfg_device_combo.currentText().strip() or "SW-CORE-01"
        review_type = self.cfg_type_combo.currentData() or "diff"
        text = self.cfg_text_edit.toPlainText().strip() or "! пустой конфиг"
        self.on_send_config(node, review_type, text)
        kind = "аудит" if review_type == "full_audit" else "diff"
        self._push_feed(node, f"отправлен {kind} на анализ", ACCENT)

    # -- лента отправленного --------------------------------------------------

    def _push_feed(self, title: str, subtitle: str, color: str) -> None:
        if self.feed_empty_label is not None:
            self.feed_empty_label.setParent(None)
            self.feed_empty_label = None
        row = GeneratedFeedRow(title, subtitle, color)
        self.feed_col.insertWidget(0, row)
        while self.feed_col.count() > 6:
            item = self.feed_col.takeAt(self.feed_col.count() - 1)
            if item.widget():
                item.widget().deleteLater()


# ---------------------------------------------------------------------------
# Аналитика и отчёты
# ---------------------------------------------------------------------------

class AnalyticsTab(QScrollArea):
    """Сводные метрики эффективности (аналог MTTR, покрытие ручной проверки,
    повторяемость инцидентов, риск конфигураций) + экспорт полной истории
    в PDF/Excel — для главы про экономический эффект в дипломе."""

    def __init__(self, incident_repo: IncidentRepository, config_repo: ConfigDiffRepository):
        super().__init__()
        self.incident_repo = incident_repo
        self.config_repo = config_repo
        self._incidents_cache: list[Incident] = []
        self._configs_cache: list[dict] = []

        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        self.viewport().setStyleSheet("background: transparent;")

        inner = QWidget()
        inner.setStyleSheet("background: transparent;")
        self.root = QVBoxLayout(inner)
        self.root.setContentsMargins(28, 24, 28, 24)
        self.root.setSpacing(16)
        self.setWidget(inner)

        header = QHBoxLayout()
        title_col = QVBoxLayout()
        title_col.setSpacing(2)
        title_col.addWidget(_label("Аналитика и отчёты", size=18, weight=700))
        title_col.addWidget(_label("Метрики эффективности и экспорт истории", size=11, color=TEXT_MUTED))
        header.addLayout(title_col)
        header.addStretch()

        refresh_btn = QPushButton("Обновить")
        refresh_btn.setStyleSheet(SECONDARY_BUTTON_STYLE)
        refresh_btn.clicked.connect(self.refresh)
        header.addWidget(refresh_btn)

        export_pdf_btn = QPushButton("Экспорт в PDF")
        export_pdf_btn.setStyleSheet(SECONDARY_BUTTON_STYLE)
        export_pdf_btn.clicked.connect(self._export_pdf)
        header.addWidget(export_pdf_btn)

        export_xlsx_btn = QPushButton("Экспорт в Excel")
        export_xlsx_btn.setStyleSheet(PRIMARY_BUTTON_STYLE)
        export_xlsx_btn.clicked.connect(self._export_excel)
        header.addWidget(export_xlsx_btn)
        self.root.addLayout(header)

        self.stats_row = QHBoxLayout()
        self.stats_row.setSpacing(16)
        self.root.addLayout(self.stats_row)

        self.bottom_row = QHBoxLayout()
        self.bottom_row.setSpacing(16)
        self.root.addLayout(self.bottom_row)
        self.root.addStretch()

        self.refresh()

    def _clear_layout(self, layout):
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                self._clear_layout(item.layout())

    def refresh(self):
        incidents = self.incident_repo.get_history(limit=1000)
        configs = self.config_repo.get_history(limit=1000)
        stats = self.incident_repo.get_verification_stats()
        self._incidents_cache = incidents
        self._configs_cache = configs

        self._clear_layout(self.stats_row)
        self._clear_layout(self.bottom_row)

        total = stats["total"]
        verified = stats["verified_count"]
        verified_pct = (verified / total * 100) if total else 0.0
        repeat_pct = _repeat_rate(incidents)
        high_risk_configs = sum(1 for c in configs if (c.get("ai_risk_level") or "").upper() == "HIGH")

        self.stats_row.addWidget(SimpleStatCard(
            "Проверено инженером", f"{verified_pct:.0f}%", f"{verified} из {total} инцидентов",
        ))
        self.stats_row.addWidget(SimpleStatCard(
            "Среднее время до проверки", _format_duration(stats["avg_seconds"]), "аналог MTTR по проверенным",
        ))
        self.stats_row.addWidget(SimpleStatCard(
            "Повторяющихся инцидентов", f"{repeat_pct:.0f}%", "совпадение хоста и проблемы",
        ))
        self.stats_row.addWidget(SimpleStatCard(
            "Высокий риск конфигураций", str(high_risk_configs), f"из {len(configs)} проверок",
        ))

        host_stats = self.incident_repo.get_stats_by_host()
        top_hosts = sorted(host_stats.items(), key=lambda kv: kv[1], reverse=True)[:5]
        hosts_card = Card(radius=18)
        hosts_layout = QVBoxLayout(hosts_card)
        hosts_layout.setContentsMargins(20, 16, 20, 14)
        hosts_layout.setSpacing(4)
        hosts_layout.addWidget(_label("Топ хостов по числу инцидентов", size=12, weight=700))
        max_host = max((v for _, v in top_hosts), default=1) or 1
        if not top_hosts:
            hosts_layout.addWidget(_label("Нет данных.", size=11, color=TEXT_MUTED))
        for host, count in top_hosts:
            hosts_layout.addWidget(HorizontalBarRow(host, count, max_host, color=ACCENT))
        self.bottom_row.addWidget(hosts_card, stretch=1)

        risk_counts = {"HIGH": 0, "MEDIUM": 0, "LOW": 0, "UNKNOWN": 0}
        for c in configs:
            level = (c.get("ai_risk_level") or "UNKNOWN").upper()
            risk_counts[level] = risk_counts.get(level, 0) + 1
        risk_card = Card(radius=18)
        risk_layout = QVBoxLayout(risk_card)
        risk_layout.setContentsMargins(20, 16, 20, 14)
        risk_layout.setSpacing(4)
        risk_layout.addWidget(_label("Риск конфигураций", size=12, weight=700))
        max_risk = max(risk_counts.values(), default=1) or 1
        risk_titles = {"HIGH": "Высокий", "MEDIUM": "Средний", "LOW": "Низкий", "UNKNOWN": "Неизвестно"}
        for level in ("HIGH", "MEDIUM", "LOW", "UNKNOWN"):
            risk_layout.addWidget(HorizontalBarRow(
                risk_titles[level], risk_counts[level], max_risk, color=RISK_COLORS.get(level, TEXT_MUTED),
            ))
        self.bottom_row.addWidget(risk_card, stretch=1)

    # -- экспорт --------------------------------------------------------------

    def _build_report_html(self) -> str:
        incident_rows = "".join(
            f"<tr><td>{inc.timestamp.strftime('%d.%m.%Y %H:%M')}</td><td>{inc.host}</td>"
            f"<td>{inc.problem_name}</td><td>{inc.severity.label_ru}</td>"
            f"<td>{'да' if inc.ai_verified else 'нет'}</td>"
            f"<td>{inc.resolution or inc.ai_recommendation or ''}</td></tr>"
            for inc in self._incidents_cache
        )
        config_rows = "".join(
            f"<tr><td>{c.get('node', '')}</td><td>{c.get('review_type', '')}</td>"
            f"<td>{c.get('ai_risk_level', '')}</td>"
            f"<td>{c.get('resolution') or c.get('ai_recommendation') or ''}</td></tr>"
            for c in self._configs_cache
        )
        return f"""
        <h2>NetAI Monitor — отчёт по инцидентам и конфигурациям</h2>
        <p>Сформирован: {datetime.now().strftime('%d.%m.%Y %H:%M')}</p>
        <h3>Инциденты ({len(self._incidents_cache)})</h3>
        <table border="1" cellspacing="0" cellpadding="4" width="100%">
        <tr><th>Время</th><th>Хост</th><th>Проблема</th><th>Критичность</th>
        <th>Проверено</th><th>Решение</th></tr>
        {incident_rows}
        </table>
        <h3>Конфигурации ({len(self._configs_cache)})</h3>
        <table border="1" cellspacing="0" cellpadding="4" width="100%">
        <tr><th>Узел</th><th>Тип</th><th>Риск</th><th>Решение</th></tr>
        {config_rows}
        </table>
        """

    def _export_pdf(self):
        path, _ = QFileDialog.getSaveFileName(self, "Экспорт в PDF", "netai_report.pdf", "PDF (*.pdf)")
        if not path:
            return
        doc = QTextDocument()
        doc.setHtml(self._build_report_html())
        printer = QPrinter(QPrinter.HighResolution)
        printer.setOutputFormat(QPrinter.PdfFormat)
        printer.setOutputFileName(path)
        doc.print_(printer)
        info_box(self, "Экспорт завершён", f"Отчёт сохранён: {path}")

    def _export_excel(self):
        try:
            import openpyxl
        except ImportError:
            warn_box(
                self, "Excel недоступен",
                "Не установлена библиотека openpyxl. Выполните: pip install openpyxl",
            )
            return
        path, _ = QFileDialog.getSaveFileName(self, "Экспорт в Excel", "netai_report.xlsx", "Excel (*.xlsx)")
        if not path:
            return

        wb = openpyxl.Workbook()
        ws_incidents = wb.active
        ws_incidents.title = "Инциденты"
        ws_incidents.append(["Время", "Хост", "Проблема", "Критичность", "Проверено",
                              "Рекомендация ИИ", "Решение инженера"])
        for inc in self._incidents_cache:
            ws_incidents.append([
                inc.timestamp.strftime("%d.%m.%Y %H:%M"), inc.host, inc.problem_name,
                inc.severity.label_ru, "да" if inc.ai_verified else "нет",
                inc.ai_recommendation, inc.resolution,
            ])

        ws_configs = wb.create_sheet("Конфигурации")
        ws_configs.append(["Узел", "Тип", "Риск", "Рекомендация ИИ", "Решение инженера"])
        for c in self._configs_cache:
            ws_configs.append([
                c.get("node"), c.get("review_type"), c.get("ai_risk_level"),
                c.get("ai_recommendation"), c.get("resolution"),
            ])

        wb.save(path)
        info_box(self, "Экспорт завершён", f"Отчёт сохранён: {path}")


# ---------------------------------------------------------------------------
# Сайдбар
# ---------------------------------------------------------------------------

class SidebarNavItem(QPushButton):
    def __init__(self, text: str):
        super().__init__(text)
        self.setCheckable(True)
        self.setStyleSheet(NAV_BUTTON_STYLE)
        self.setCursor(Qt.PointingHandCursor)


class Sidebar(QWidget):
    def __init__(self, nav_labels: list[str], incident_repo: IncidentRepository,
                 settings_manager: SettingsManager):
        super().__init__()
        self.incident_repo = incident_repo
        self.settings_manager = settings_manager
        self.setFixedWidth(268)
        self.setStyleSheet(f"background: {SIDEBAR_BG}; border-right: 1px solid {PANEL_BORDER};")

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 22, 18, 18)
        root.setSpacing(18)

        brand = QHBoxLayout()
        mark = QLabel()
        mark.setFixedSize(28, 28)
        mark.setStyleSheet(f"background: {ACCENT}; border-radius: 8px;")
        brand.addWidget(mark)
        brand_text = QVBoxLayout()
        brand_text.setSpacing(0)
        brand_text.addWidget(_label("NetAI Monitor", size=13, weight=700))
        brand_text.addWidget(_label("Псковэнергосбыт", size=10, color=TEXT_MUTED))
        brand.addLayout(brand_text)
        brand.addStretch()
        root.addLayout(brand)

        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.buttons: list[SidebarNavItem] = []
        self.nav_badges: dict[int, QLabel] = {}
        nav_col = QVBoxLayout()
        nav_col.setSpacing(2)
        for i, label in enumerate(nav_labels):
            row_widget = QWidget()
            row_widget.setStyleSheet("background: transparent;")
            row = QHBoxLayout(row_widget)
            row.setContentsMargins(0, 0, 8, 0)
            row.setSpacing(0)

            btn = SidebarNavItem(label)
            if i == 0:
                btn.setChecked(True)
            self.group.addButton(btn, i)
            row.addWidget(btn, stretch=1)

            badge = QLabel("●")
            badge.setStyleSheet(f"color: {NEGATIVE}; font-size: 9px; border: none; background: transparent;")
            badge.setFixedWidth(12)
            badge.hide()
            row.addWidget(badge)
            self.nav_badges[i] = badge

            nav_col.addWidget(row_widget)
            self.buttons.append(btn)
        root.addLayout(nav_col)

        root.addWidget(Divider())

        queue_header = QHBoxLayout()
        queue_header.addWidget(_label("Критичные", size=11, color=TEXT_SECONDARY, weight=700))
        self.queue_count_label = _label("0", size=11, color=NEGATIVE, weight=700)
        queue_header.addStretch()
        queue_header.addWidget(self.queue_count_label)
        root.addLayout(queue_header)

        self.queue_col = QVBoxLayout()
        self.queue_col.setSpacing(6)
        root.addLayout(self.queue_col)

        root.addWidget(Divider())

        root.addWidget(_label("Быстрый доступ", size=11, color=TEXT_SECONDARY, weight=700))
        quick_row = QHBoxLayout()
        quick_row.setSpacing(6)
        self.quick_buttons = {}
        for label, target in [("Алерты", 1), ("Конфиги", 2), ("Генератор", 5)]:
            btn = QPushButton(label)
            btn.setStyleSheet(QUICK_BUTTON_STYLE)
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(lambda _, t=target: self.group.button(t).click())
            quick_row.addWidget(btn)
        root.addLayout(quick_row)

        root.addStretch()
        self.refresh_queue()

    def set_badge(self, index: int, visible: bool) -> None:
        """Красная точка рядом с пунктом навигации — сигнал «здесь есть непрочитанное»."""
        badge = self.nav_badges.get(index)
        if badge is not None:
            badge.setVisible(visible)

    def refresh_queue(self):
        # Строки добавлены через addWidget (не addLayout) специально: только тогда
        # takeAt(0).widget() возвращает реальный виджет и deleteLater() его убирает.
        # Раньше строки добавлялись через addLayout — takeAt().widget() был None,
        # старые строки никогда не удалялись и копились друг на друге при каждом
        # обновлении (а refresh_queue теперь дёргается на каждый live-алерт).
        while self.queue_col.count():
            item = self.queue_col.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()

        incidents = self.incident_repo.get_history(limit=200)
        critical = sorted(
            (i for i in incidents if i.severity in (Severity.HIGH, Severity.DISASTER)),
            key=lambda i: i.timestamp, reverse=True,
        )
        self.queue_count_label.setText(str(len(critical)))
        if not critical:
            self.queue_col.addWidget(_label("Нет критичных инцидентов", size=10, color=TEXT_MUTED))
            return
        for inc in critical[:4]:
            row_widget = QWidget()
            row_widget.setStyleSheet("background: transparent;")
            row = QHBoxLayout(row_widget)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(8)
            dot = QLabel("●")
            dot.setStyleSheet(f"color: {inc.severity.color}; font-size: 8px; border: none; background: transparent;")
            dot.setFixedWidth(10)
            row.addWidget(dot)
            text_col = QVBoxLayout()
            text_col.setSpacing(0)
            text_col.addWidget(_label(inc.host, size=10, weight=700, font=FONT_DATA))
            text_col.addWidget(_label(inc.problem_name[:34], size=9, color=TEXT_MUTED))
            row.addLayout(text_col)
            self.queue_col.addWidget(row_widget)


# ---------------------------------------------------------------------------
# Главное окно
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self, incident_repo, config_repo, settings_manager: SettingsManager,
                 zabbix_client, oxidized_client, analyzer, incident_rag=None, config_rag=None,
                 offline_mode: bool = False):
        super().__init__()
        self.setWindowTitle("NetAI Monitor — интеллектуальный анализ сетевой инфраструктуры")
        self.resize(1360, 840)
        self.setStyleSheet(APP_STYLESHEET)
        self.offline_mode = offline_mode

        central = GlowBackground()
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        nav_labels = ["Дашборд", "Алерты", "Конфигурации", "Настройки",
                      "Синтетические данные", "Генератор", "Аналитика"]
        self.sidebar = Sidebar(nav_labels, incident_repo, settings_manager)
        root.addWidget(self.sidebar)

        self.stack = QStackedWidget()
        self.stack.setStyleSheet("background: transparent;")
        self.dashboard_tab = DashboardTab(incident_repo, config_repo)
        self.alerts_tab = AlertsTab(zabbix_client, analyzer, incident_repo, rag=incident_rag)
        self.configs_tab = ConfigsTab(oxidized_client, analyzer, config_repo, rag=config_rag)
        self.generator_tab = GeneratorTab(
            on_send_incident=self.alerts_tab.submit_incident,
            on_send_config=self.configs_tab.submit_config,
        )
        self.stack.addWidget(self.dashboard_tab)
        self.stack.addWidget(self.alerts_tab)
        self.stack.addWidget(self.configs_tab)
        self.stack.addWidget(SettingsTab(settings_manager, offline_mode=offline_mode))
        self.stack.addWidget(SyntheticDataTab(settings_manager, incident_repo, config_repo,
                                               incident_rag=incident_rag, config_rag=config_rag))
        self.stack.addWidget(self.generator_tab)
        self.analytics_tab = AnalyticsTab(incident_repo, config_repo)
        self.stack.addWidget(self.analytics_tab)
        root.addWidget(self.stack, stretch=1)

        self.alerts_tab.incident_added.connect(self._on_live_data_changed)
        self.configs_tab.config_added.connect(self._on_live_data_changed)
        self.alerts_tab.unread_changed.connect(lambda visible: self.sidebar.set_badge(1, visible))

        self.sidebar.group.idClicked.connect(self._on_nav_clicked)
        self.setCentralWidget(central)

        self.setStatusBar(QStatusBar())
        mode = "Синтетические данные (демо)" if settings_manager.load().use_synthetic_data else "Боевые данные"
        analyzer_mode = "Ollama (локально)" if isinstance(analyzer, OllamaAnalyzer) else "Rule-based (офлайн)"
        rag_mode = "RAG включён" if incident_rag else "RAG выключен"
        storage_mode = "БД: SQLite офлайн (PostgreSQL недоступен)" if offline_mode else "БД: PostgreSQL"
        self.statusBar().showMessage(
            f"режим: {mode}  |  ИИ: {analyzer_mode}  |  {rag_mode}  |  {storage_mode}"
        )

    def _on_nav_clicked(self, index: int):
        self.stack.setCurrentIndex(index)
        if index == 0:
            self.dashboard_tab.refresh()
        elif index == 1:
            self.alerts_tab.mark_seen()
        elif index == 6:
            self.analytics_tab.refresh()
        self.sidebar.refresh_queue()

    def _on_live_data_changed(self):
        """Срабатывает при поступлении нового алерта/конфигурации (из Центра
        генерации или обычного «Обновить») — держит сайдбар и дашборд в
        актуальном состоянии без ручного переключения вкладок."""
        self.sidebar.refresh_queue()
        self.dashboard_tab.refresh()


def run_app(incident_repo, config_repo, settings_manager: SettingsManager, zabbix_client,
            oxidized_client, analyzer, incident_rag=None, config_rag=None, offline_mode: bool = False):
    app = QApplication.instance() or QApplication([])
    # Fusion — кроссплатформенный стиль, полностью отрисовываемый через QSS.
    # Нативный стиль Windows (windowsvista) на диалогах (QMessageBox и т.п.)
    # иногда рисует кнопки своей темой поверх наших стилей — кнопка при этом
    # остаётся кликабельной (хитбокс на месте), но визуально не видна.
    app.setStyle("Fusion")
    # Стиль ставится на QApplication (не только на MainWindow) — иначе
    # отдельные QDialog/QMessageBox (например, «Экспорт завершён») не
    # наследуют его надёжно: Qt не всегда прокидывает QSS родительского
    # окна в отдельные top-level диалоги, и кнопки/фон остаются дефолтными.
    app.setStyleSheet(APP_STYLESHEET)
    window = MainWindow(incident_repo, config_repo, settings_manager, zabbix_client, oxidized_client,
                         analyzer, incident_rag=incident_rag, config_rag=config_rag,
                         offline_mode=offline_mode)
    window.show()
    app.exec()
