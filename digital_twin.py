"""
digital_twin.py — генератор реалистичных инцидентов/конфигураций по
реальной топологии сети предприятия (см. topology.py), для накопления
живого обучающего датасета без подключения к боевому Zabbix/Oxidized.
Реализует тот же интерфейс, что MockZabbixClient/MockOxidizedClient —
подключается вместо них через synthetic_data_mode="twin" в
service_config.json (см. service_config.py, realtime_service.py,
job_worker.py, api_server.py).

Алгоритм на каждый тик (TwinZabbixClient.get_active_problems):
  1. Закрыть root-cause записи, у которых истекло время restore_at.
  2. С вероятностью, заданной incident_rate_per_day, породить новую
     root-cause проблему на случайном узле (вес по criticality).
  3. Пересчитать множество каскадных сбоев заново по всей топологии
     (Topology.unreachable_given) — а не инкрементально по caused_by:
     это корректно обрабатывает и несколько одновременных root-cause,
     и будущие redundancy_groups, без риска рассинхронизации состояния.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List

from models import Incident, Severity
from topology import Topology

ROLE_PROBLEM_TEMPLATES: dict[str, list[tuple[str, Severity, str, str]]] = {
    "border_router": [
        ("BGP session down on {host}", Severity.DISASTER, "bgp.state", "idle"),
        ("Interface {iface} link down on {host}", Severity.DISASTER, "ifOperStatus", "down(2)"),
        ("High CPU utilization on {host} (>90%)", Severity.AVERAGE, "cpu.util", "94%"),
        ("Lost connection to {host} (ICMP)", Severity.DISASTER, "icmpping", "0 (unreachable)"),
        ("High latency to {host} (>150ms)", Severity.AVERAGE, "icmppingsec", "187ms"),
    ],
    "core_switch": [
        ("High CPU utilization on {host} (>90%)", Severity.HIGH, "cpu.util", "93%"),
        ("Spanning-tree topology change on {host}", Severity.WARNING, "stp.status", "tc-detected"),
        ("Power supply failure on {host}", Severity.DISASTER, "psu.status", "failed"),
        ("Temperature too high on {host} (>75C)", Severity.HIGH, "sensor.temp", "78C"),
        ("Fan speed abnormal on {host}", Severity.WARNING, "fan.speed", "critical low"),
    ],
    "access_switch": [
        ("Interface {iface} link down on {host}", Severity.HIGH, "ifOperStatus", "down(2)"),
        ("Interface {iface} errors rate too high on {host}", Severity.WARNING, "ifOutErrors", "342/min"),
        ("Interface {iface} flapping detected on {host}", Severity.AVERAGE, "ifOperStatus", "flapping x7"),
        ("VLAN mismatch detected on {iface} ({host})", Severity.AVERAGE, "vlan.status", "mismatch"),
    ],
    "edge_router": [
        ("Zabbix agent is not available on {host}", Severity.HIGH, "agent.ping", "unavailable"),
        ("Lost connection to {host} (ICMP)", Severity.HIGH, "icmpping", "0 (unreachable)"),
        ("High latency to {host} (>150ms)", Severity.AVERAGE, "icmppingsec", "187ms"),
    ],
}
DEFAULT_TEMPLATES = ROLE_PROBLEM_TEMPLATES["access_switch"]

IFACES = ["Gi0/1", "Gi0/12", "Te1/0/24", "Gi1/0/3", "Po1", "Gi0/24", "Te2/0/1", "Po2"]

# Сколько минут в среднем живёт root-cause проблема до "само собой решилась".
RESOLVE_MINUTES_RANGE = {
    "border_router": (15, 90), "core_switch": (10, 60),
    "access_switch": (20, 180), "edge_router": (20, 180),
}

# Чем критичнее узел, тем реже там что-то ломается (но тяжелее по
# последствиям через каскад) — HIGH выбирается в 3 раза реже, чем LOW.
CRITICALITY_SPAWN_WEIGHT = {"HIGH": 1, "MEDIUM": 2, "LOW": 3}


class TwinZabbixClient:
    """Тот же интерфейс, что BaseZabbixClient (test_connection,
    get_active_problems) — подставляется вместо ZabbixClient/MockZabbixClient
    (см. realtime_service.py._build_clients)."""

    def __init__(self, repo, topology: Topology, incident_rate_per_day: float,
                 poll_interval_seconds: int, rng: random.Random | None = None):
        self.repo = repo
        self.topology = topology
        self.rng = rng or random.Random()
        ticks_per_day = max(1, 86400 // max(1, poll_interval_seconds))
        self._spawn_probability = incident_rate_per_day / ticks_per_day

    def test_connection(self) -> bool:
        return True

    def get_active_problems(self) -> List[Incident]:
        now = datetime.now()
        self._resolve_due_root_causes(now)
        self._maybe_spawn_root_cause(now)
        self._reconcile_cascades()
        return [self._row_to_incident(r) for r in self.repo.get_open()]

    def _resolve_due_root_causes(self, now: datetime) -> None:
        for row in self.repo.get_due_root_causes(now):
            self.repo.resolve(row["id"])

    def _maybe_spawn_root_cause(self, now: datetime) -> None:
        if not self.topology.nodes:
            return
        if self.rng.random() >= self._spawn_probability:
            return
        population = list(self.topology.nodes.keys())
        weights = [CRITICALITY_SPAWN_WEIGHT.get(self.topology.nodes[n].criticality, 2)
                   for n in population]
        host = self.rng.choices(population, weights=weights, k=1)[0]
        node = self.topology.nodes[host]
        templates = ROLE_PROBLEM_TEMPLATES.get(node.role, DEFAULT_TEMPLATES)
        template, severity, item_key, value = self.rng.choice(templates)
        iface = self.rng.choice(IFACES)
        problem_name = template.format(host=host, iface=iface)
        lo, hi = RESOLVE_MINUTES_RANGE.get(node.role, (20, 120))
        resolve_at = now + timedelta(minutes=self.rng.randint(lo, hi))
        self.repo.create(
            incident_id=self._new_id(), host=host, problem_name=problem_name,
            severity=severity.value, item_key=item_key, last_value=value,
            resolve_at=resolve_at, is_root_cause=True, caused_by=None,
        )

    def _reconcile_cascades(self) -> None:
        open_rows = self.repo.get_open()
        down_roots = {r["host"] for r in open_rows if r["is_root_cause"]}
        should_be_down = self.topology.unreachable_given(down_roots) - down_roots
        current_cascade_hosts = {r["host"] for r in open_rows if not r["is_root_cause"]}

        for host in current_cascade_hosts - should_be_down:
            self.repo.resolve_by_host(host)

        for host in should_be_down - current_cascade_hosts:
            root = self.topology.nearest_down_ancestor(host, down_roots)
            note = f" [вероятно каскад от отказа {root}]" if root else ""
            self.repo.create(
                incident_id=self._new_id(), host=host,
                problem_name=f"Lost connection to {host} (ICMP){note}",
                severity=Severity.DISASTER.value, item_key="icmpping",
                last_value="0 (unreachable)", resolve_at=None,
                is_root_cause=False, caused_by=root,
            )

    def _new_id(self) -> str:
        return f"twin-{self.rng.randrange(10 ** 12)}"

    @staticmethod
    def _row_to_incident(row: dict) -> Incident:
        return Incident(
            id=row["id"], host=row["host"], problem_name=row["problem_name"],
            severity=Severity(row["severity"]), timestamp=row["started_at"],
            item_key=row["item_key"] or "", last_value=row["last_value"] or "",
        )


@dataclass
class ConfigVersion:
    node: str
    version: str
    date: str


@dataclass
class ConfigDiff:
    node: str
    diff_text: str


# Паттерны риска по вендору — тот же стиль, что mock_oxidized_client.py:
# SYNTHETIC_DIFFS, но с реальными вендорами топологии (H3C/Eltex/HPE/Mikrotik).
VENDOR_DIFF_TEMPLATES = {
    "H3C": [
        "--- before\n+++ after\n@@ snmp-agent community @@\n"
        "-  snmp-agent community read cRestricted-2024\n+  snmp-agent community read public\n",
        "--- before\n+++ after\n@@ line vty 0 4 @@\n"
        "-  transport input ssh\n+  transport input telnet ssh\n",
    ],
    "Eltex": [
        "--- before\n+++ after\n@@ interface {iface} @@\n"
        "-  switchport port-security maximum 2\n-  switchport port-security violation restrict\n"
        "+  no switchport port-security\n",
        "--- before\n+++ after\n@@ vlan 100 @@\n"
        "-  vlan 100 name Guest-Isolated\n+  vlan 100 name Guest\n-  ip access-group GUEST-ACL in\n",
    ],
    "HPE": [
        "--- before\n+++ after\n@@ line vty 0 4 @@\n-  transport input ssh\n+  transport input telnet ssh\n",
    ],
    "Mikrotik": [
        "--- before\n+++ after\n@@ /ip firewall filter @@\n-  add chain=input action=drop\n"
        "+  # правило отключено\n",
    ],
}


class TwinOxidizedClient:
    """Тот же интерфейс, что MockOxidizedClient — get_nodes/get_current_config/
    get_versions/get_diff/get_all_diffs. Связь с TwinZabbixClient (конфиг
    как причина инцидента) — намеренно не в MVP, см. спеку, фаза 2."""

    def __init__(self, topology: Topology, rng: random.Random | None = None):
        self.topology = topology
        self.rng = rng or random.Random()

    def get_nodes(self) -> List[str]:
        return list(self.topology.nodes.keys())

    def get_current_config(self, node: str) -> str:
        n = self.topology.nodes.get(node)
        vendor = n.vendor if n else "H3C"
        role = n.role if n else "?"
        return f"! Конфигурация {node} (цифровой двойник)\nhostname {node}\n! vendor: {vendor}, role: {role}\n"

    def get_versions(self, node: str) -> List[ConfigVersion]:
        return [ConfigVersion(node=node, version="v1", date="—"),
                ConfigVersion(node=node, version="v2", date="—")]

    def get_diff(self, node: str, version_a: str, version_b: str) -> ConfigDiff:
        n = self.topology.nodes.get(node)
        vendor = n.vendor if n else "H3C"
        templates = VENDOR_DIFF_TEMPLATES.get(vendor, VENDOR_DIFF_TEMPLATES["H3C"])
        diff_text = self.rng.choice(templates).format(iface=self.rng.choice(IFACES))
        return ConfigDiff(node=node, diff_text=diff_text)

    def get_all_diffs(self) -> List[ConfigDiff]:
        node_names = list(self.topology.nodes.keys())
        sample_size = min(5, len(node_names))
        sample = self.rng.sample(node_names, sample_size) if sample_size else []
        return [self.get_diff(node, "prev", "latest") for node in sample]
