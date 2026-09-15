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

    def _get(self, path: str, params: dict | None = None, timeout: int = 10) -> requests.Response:
        """Общая точка для всех запросов — раньше каждый метод дублировал
        requests.get()/raise_for_status() без единой обработки ошибок, и
        сетевой сбой (таймаут, DNS, отказ соединения) всплывал как сырое
        исключение requests вместо понятного сообщения — тот же паттерн,
        что уже применён в ZabbixClient."""
        try:
            resp = requests.get(f"{self.base_url}{path}", params=params, timeout=timeout)
            resp.raise_for_status()
            return resp
        except Exception as e:
            raise ConnectionError(f"Oxidized недоступен: {e}") from e

    def get_nodes(self) -> List[str]:
        """Список всех устройств, которыми управляет Oxidized."""
        resp = self._get("/nodes", timeout=10)
        return [n["name"] for n in resp.json()]

    def get_current_config(self, node: str) -> str:
        """Текущий (последний) конфиг устройства."""
        resp = self._get(f"/node/fetch/{node}", timeout=15)
        return resp.text

    def get_versions(self, node: str) -> List[ConfigVersion]:
        """История версий конфига устройства."""
        resp = self._get(f"/node/version/{node}", timeout=10)
        return [
            ConfigVersion(node=node, version=v.get("version", ""), date=v.get("date", ""))
            for v in resp.json()
        ]

    def get_diff(self, node: str, version_a: str, version_b: str) -> ConfigDiff:
        """Diff между двумя версиями конфига — то, что реально интересно ИИ-ревьюеру."""
        resp = self._get(
            "/node/diff", params={"node": node, "from": version_a, "to": version_b}, timeout=15,
        )
        return ConfigDiff(node=node, diff_text=resp.text)

    def get_all_diffs(self) -> List[ConfigDiff]:
        """Diff по всем устройствам сразу — для кнопки «Все устройства (diff)»
        в ConfigsTab. У Oxidized нет отдельного batch-эндпоинта для этого,
        поэтому проходим по всем узлам и берём diff между двумя последними
        версиями каждого. Раньше этого метода не было вовсе на боевом
        клиенте (только на MockOxidizedClient) — кнопка «Все устройства»
        падала с AttributeError при работе с реальным Oxidized. Устройство
        без истории (меньше двух версий) или недоступное — просто
        пропускается, не прерывая остальные."""
        diffs = []
        for node in self.get_nodes():
            try:
                versions = self.get_versions(node)
            except ConnectionError:
                continue
            if len(versions) < 2:
                continue
            prev_version, latest_version = versions[-2].version, versions[-1].version
            try:
                diffs.append(self.get_diff(node, prev_version, latest_version))
            except ConnectionError:
                continue
        return diffs
