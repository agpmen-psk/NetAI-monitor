"""
settings.py — единая точка чтения/записи настроек приложения.
Параметры хранятся в таблице app_settings — в PostgreSQL или, в офлайн-режиме
без развёрнутой БД (см. backend.py), в локальном SQLite. SettingsManager не
завязан на конкретный бэкенд — принимает готовый repo (duck typing: любой
объект с get_all()/set_many()), поэтому GUI-вкладка «Настройки» работает
одинаково в обоих случаях.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict


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
    """Обёртка над репозиторием настроек (Postgres или SQLite — см. backend.py)
    для удобного использования в GUI и main.py."""

    def __init__(self, repo):
        self.repo = repo

    def load(self) -> AppSettings:
        return AppSettings.from_dict(self.repo.get_all())

    def save(self, settings: AppSettings) -> None:
        self.repo.set_many(settings.to_dict())
