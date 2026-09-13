"""
settings.py — единая точка чтения/записи настроек приложения.
Все параметры хранятся в PostgreSQL (таблица app_settings), поэтому
GUI-вкладка «Настройки» может их менять без перезапуска и без правки кода.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict

from db import Database, SettingsRepository


@dataclass
class AppSettings:
    zabbix_url: str = ""
    zabbix_user: str = ""
    zabbix_password: str = ""
    oxidized_url: str = ""
    ollama_host: str = "http://localhost:11434"
    ollama_model: str = "gemma4:e4b"
    embedding_model: str = "nomic-embed-text"
    use_synthetic_data: bool = True

    @classmethod
    def from_dict(cls, d: dict) -> "AppSettings":
        return cls(
            zabbix_url=d.get("zabbix_url", ""),
            zabbix_user=d.get("zabbix_user", ""),
            zabbix_password=d.get("zabbix_password", ""),
            oxidized_url=d.get("oxidized_url", ""),
            ollama_host=d.get("ollama_host", "http://localhost:11434"),
            ollama_model=d.get("ollama_model", "gemma4:e4b"),
            embedding_model=d.get("embedding_model", "nomic-embed-text"),
            use_synthetic_data=d.get("use_synthetic_data", "true").lower() == "true",
        )

    def to_dict(self) -> dict:
        d = asdict(self)
        d["use_synthetic_data"] = "true" if self.use_synthetic_data else "false"
        return d


class SettingsManager:
    """Обёртка над SettingsRepository для удобного использования в GUI и main.py."""

    def __init__(self, db: Database):
        self.repo = SettingsRepository(db)

    def load(self) -> AppSettings:
        return AppSettings.from_dict(self.repo.get_all())

    def save(self, settings: AppSettings) -> None:
        self.repo.set_many(settings.to_dict())
