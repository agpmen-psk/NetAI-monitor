"""
topology.py — граф зависимостей узлов сети предприятия для цифрового
двойника (см. digital_twin.py). Отвечает только за структуру сети и вопрос
«жив ли узел при таком-то множестве упавших узлов» — никакой генерации
инцидентов или обращения к БД/сети здесь нет, это чистая графовая логика,
тестируемая без PostgreSQL/Ollama (см. tests/test_topology.py).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class TopologyNode:
    name: str
    role: str
    vendor: str = ""
    model: str = ""
    site: str = ""
    ip: str = ""
    criticality: str = "MEDIUM"
    depends_on: list[str] = field(default_factory=list)
    purpose: str = ""


class Topology:
    def __init__(self, sites: dict[str, str], redundancy_groups: dict[str, list[str]],
                 nodes: list[TopologyNode]):
        self.sites = sites
        self.redundancy_groups = redundancy_groups
        self.nodes: dict[str, TopologyNode] = {n.name: n for n in nodes}

    @classmethod
    def load(cls, path: Path) -> "Topology":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        raw_nodes = data.get("nodes", [])
        redundancy_groups = data.get("redundancy_groups", {})
        valid_refs = {n["name"] for n in raw_nodes} | set(redundancy_groups.keys())

        nodes = []
        for n in raw_nodes:
            # Битая ссылка (опечатка/устройство ещё не добавлено) не должна
            # ронять загрузку всей топологии — просто узел считается не
            # зависящим от неё (см. tests/test_topology.py:test_broken_reference_dropped).
            depends_on = [d for d in n.get("depends_on", []) if d in valid_refs]
            nodes.append(TopologyNode(
                name=n["name"], role=n.get("role", ""), vendor=n.get("vendor", ""),
                model=n.get("model", ""), site=n.get("site", ""), ip=n.get("ip", ""),
                criticality=n.get("criticality", "MEDIUM"), depends_on=depends_on,
                purpose=n.get("purpose", ""),
            ))
        return cls(sites=data.get("sites", {}), redundancy_groups=redundancy_groups, nodes=nodes)

    @classmethod
    def load_or_empty(cls, path, log) -> "Topology":
        """Как .load(), но никогда не бросает исключение: битый/повреждённый
        JSON (или узел без обязательного поля) логируется как ошибка, и
        возвращается пустая топология — так же, как при отсутствующем файле.
        Используется всеми тремя серверными процессами при старте: топологию
        правит вручную администратор на сервере, и один хвостовой JSON/
        пропущенное поле не должны ронять всю службу."""
        if not Path(path).exists():
            log.error(
                "Режим цифрового двойника включён, но файл топологии не найден: %s — "
                "положите network_topology.json рядом с service_config.json.", path,
            )
            return cls(sites={}, redundancy_groups={}, nodes=[])
        try:
            return cls.load(path)
        except Exception as e:
            log.error("Не удалось разобрать файл топологии %s: %s — работаю с пустой топологией.", path, e)
            return cls(sites={}, redundancy_groups={}, nodes=[])

    def _dependency_satisfied(self, dep: str, down_hosts: set[str]) -> bool:
        """dep — имя узла ИЛИ ключ redundancy_groups. Группа резервирования
        жива, пока жив хотя бы один её член (семантика «ИЛИ» внутри группы)."""
        if dep in self.redundancy_groups:
            return any(m not in down_hosts for m in self.redundancy_groups[dep])
        return dep not in down_hosts

    def _node_reachable(self, node: TopologyNode, down_hosts: set[str]) -> bool:
        if node.name in down_hosts:
            return False
        # Список depends_on — семантика «И»: каждая перечисленная зависимость
        # (узел или группа) должна быть жива. Для «любой из двух путей
        # достаточно» используйте redundancy_groups, а не несколько записей
        # в depends_on напрямую.
        return all(self._dependency_satisfied(dep, down_hosts) for dep in node.depends_on)

    def unreachable_given(self, down_hosts: set[str]) -> set[str]:
        """Все узлы, недоступные при заданном множестве упавших узлов — сами
        упавшие плюс всё, для чего каскадом не осталось живого пути наверх.
        Топология — DAG (в реальности почти всегда дерево), поэтому фикс-
        поинт находится за несколько проходов по всем узлам."""
        unreachable = set(down_hosts)
        changed = True
        while changed:
            changed = False
            for node in self.nodes.values():
                if node.name in unreachable:
                    continue
                if not self._node_reachable(node, unreachable):
                    unreachable.add(node.name)
                    changed = True
        return unreachable

    def nearest_down_ancestor(self, node_name: str, down_hosts: set[str]) -> str | None:
        """Ближайший упавший узел выше по цепочке зависимостей — для
        человеко-читаемой пометки причины каскада (см. digital_twin.py)."""
        node = self.nodes.get(node_name)
        if node is None:
            return None
        for dep in node.depends_on:
            candidates = self.redundancy_groups.get(dep, [dep])
            for c in candidates:
                if c in down_hosts:
                    return c
            for c in candidates:
                upstream = self.nearest_down_ancestor(c, down_hosts)
                if upstream:
                    return upstream
        return None
