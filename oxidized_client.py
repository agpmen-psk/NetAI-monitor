"""
oxidized_client.py — клиент к REST API Oxidized для получения конфигураций
и diff'ов устройств. Сам Oxidized уже опрашивает оборудование и хранит
версии в Git — мы только читаем готовые данные через его API.
"""
from __future__ import annotations

import requests
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class ConfigVersion:
    node: str
    version: str
    date: str


@dataclass
class ConfigDiff:
    node: str
    diff_text: str


class OxidizedClient:
    def __init__(self, base_url: str = "http://10.10.11.25:8888"):
        self.base_url = base_url.rstrip("/")

    def get_nodes(self) -> List[str]:
        """Список всех устройств, которыми управляет Oxidized."""
        resp = requests.get(f"{self.base_url}/nodes", timeout=10)
        resp.raise_for_status()
        return [n["name"] for n in resp.json()]

    def get_current_config(self, node: str) -> str:
        """Текущий (последний) конфиг устройства."""
        resp = requests.get(f"{self.base_url}/node/fetch/{node}", timeout=15)
        resp.raise_for_status()
        return resp.text

    def get_versions(self, node: str) -> List[ConfigVersion]:
        """История версий конфига устройства."""
        resp = requests.get(f"{self.base_url}/node/version/{node}", timeout=10)
        resp.raise_for_status()
        return [
            ConfigVersion(node=node, version=v.get("version", ""), date=v.get("date", ""))
            for v in resp.json()
        ]

    def get_diff(self, node: str, version_a: str, version_b: str) -> ConfigDiff:
        """Diff между двумя версиями конфига — то, что реально интересно ИИ-ревьюеру."""
        resp = requests.get(
            f"{self.base_url}/node/diff",
            params={"node": node, "from": version_a, "to": version_b},
            timeout=15,
        )
        resp.raise_for_status()
        return ConfigDiff(node=node, diff_text=resp.text)