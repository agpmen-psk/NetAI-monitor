"""
zabbix_client.py — источники данных об инцидентах.
  - ZabbixClient      — боевой клиент через pyzabbix (event.get)
  - MockZabbixClient  — синтетический генератор для демо/защиты диплома

Оба реализуют одинаковый интерфейс get_active_problems().
"""
from __future__ import annotations

import random
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from typing import List

from models import Incident, Severity


class BaseZabbixClient(ABC):
    @abstractmethod
    def get_active_problems(self) -> List[Incident]:
        ...

    @abstractmethod
    def test_connection(self) -> bool:
        ...


class ZabbixClient(BaseZabbixClient):
    """
    Боевой клиент к Zabbix API через pyzabbix.
    Установка: pip install pyzabbix

    Подключение (event.get) выполняется лениво — конструктор НЕ ходит в сеть,
    только сохраняет параметры. Иначе недоступный Zabbix (таймаут, неверный
    хост/порт) валит всё приложение ещё до появления окна: main.py создаёт
    клиент синхронно, в основном потоке, до старта Qt-луп. С таймаутом и
    отложенным login() ошибка сети превращается в понятное исключение,
    которое ловит вызывающий код (GUI-воркеры уже оборачивают все такие
    вызовы в try/except и показывают её пользователю, не роняя программу).
    """

    def __init__(self, url: str, user: str, password: str, timeout: float = 5.0):
        self.url = url
        self.user = user
        self.password = password
        self.timeout = timeout
        self.zapi = None

    def _ensure_login(self):
        if self.zapi is not None:
            return
        from pyzabbix import ZabbixAPI

        zapi = ZabbixAPI(self.url, timeout=self.timeout)
        zapi.login(self.user, self.password)
        self.zapi = zapi

    def test_connection(self) -> bool:
        try:
            self._ensure_login()
            self.zapi.apiinfo.version()
            return True
        except Exception:
            self.zapi = None
            return False

    def get_active_problems(self, limit: int = 10) -> List[Incident]:
        """
        Использует event.get (не problem.get!) — только у event.get есть
        параметр selectHosts, позволяющий сразу получить имя хоста.
        source=0/object=0/value=1 — только активные триггерные проблемы.
        limit ограничивает нагрузку на PHP-сервер Zabbix и объём для LLM.
        """
        try:
            self._ensure_login()
        except Exception as e:
            raise ConnectionError(f"Zabbix недоступен: {e}") from e

        time_from = int((datetime.now() - timedelta(days=7)).timestamp())

        try:
            events = self.zapi.event.get(
                output="extend",
                selectHosts=["host"],
                selectRelatedObject=["expression"],
                source=0,
                object=0,
                value=1,
                time_from=time_from,
                sortfield="clock",
                sortorder="DESC",
                limit=limit,
            )
        except Exception as e:
            self.zapi = None
            raise ConnectionError(f"Zabbix недоступен: {e}") from e

        incidents = []
        for ev in events:
            host_name = ev["hosts"][0]["host"] if ev.get("hosts") else "unknown"
            # ev["object"] — это код типа объекта фильтра event.get (для триггеров
            # всегда "0"), а не ключ item'а — раньше сюда по ошибке попадало
            # именно это поле, и в UI/промпте LLM для каждого инцидента
            # показывался бессмысленный "0". selectRelatedObject возвращает
            # выражение триггера, которое реально содержит имя хоста и ключ item'а.
            item_key = (ev.get("relatedObject") or {}).get("expression", "")
            incidents.append(
                Incident(
                    id=ev["eventid"],
                    host=host_name,
                    problem_name=ev["name"],
                    severity=Severity(int(ev["severity"])),
                    timestamp=datetime.fromtimestamp(int(ev["clock"])),
                    item_key=item_key,
                )
            )
        return incidents


class MockZabbixClient(BaseZabbixClient):
    """Синтетический генератор инцидентов — для демо и защиты без боевого Zabbix."""

    HOSTS = [
        "SW-CORE-01", "SW-CORE-02", "SW-ACCESS-14", "SW-ACCESS-22", "SW-ACCESS-31",
        "SW-ACCESS-45", "RTR-BORDER-01", "RTR-BORDER-02", "RTR-BRANCH-PSKOV-02",
        "RTR-BRANCH-VELIKIE-LUKI-01", "SRV-1C-DB", "SRV-1C-APP", "SRV-FILE-01",
        "SRV-FILE-02", "SRV-DHCP-DNS", "SRV-WEB-01", "SRV-BACKUP-01",
        "PBX-ASTERISK-01", "PBX-ASTERISK-02", "FW-PERIMETER-01", "FW-PERIMETER-02",
        "SRV-MONITORING-01", "SRV-TERMINAL-01", "SW-DIST-07", "SW-DIST-12",
    ]

    PROBLEM_TEMPLATES = [
        ("Interface {iface} link down on {host}", Severity.HIGH, "ifOperStatus", "down(2)"),
        ("High CPU utilization on {host} (>90%)", Severity.AVERAGE, "cpu.util", "94%"),
        ("High memory utilization on {host} (>85%)", Severity.WARNING, "vm.memory.util", "88%"),
        ("Lost connection to {host} (ICMP)", Severity.DISASTER, "icmpping", "0 (unreachable)"),
        ("High latency to {host} (>150ms)", Severity.AVERAGE, "icmppingsec", "187ms"),
        ("Interface {iface} errors rate too high on {host}", Severity.WARNING, "ifOutErrors", "342/min"),
        ("Disk space is low on {host} (<10% free)", Severity.WARNING, "vfs.fs.size", "6.2% free"),
        ("SIP trunk registration failed on {host}", Severity.HIGH, "sip.status", "unregistered"),
        ("Interface {iface} flapping detected on {host}", Severity.AVERAGE, "ifOperStatus", "flapping x7"),
        ("Zabbix agent is not available on {host}", Severity.HIGH, "agent.ping", "unavailable"),
        ("Disk read/write request responses are too high on {host}", Severity.WARNING, "disk.io", "24ms"),
        ("Power supply failure on {host}", Severity.DISASTER, "psu.status", "failed"),
        ("Temperature too high on {host} (>75C)", Severity.HIGH, "sensor.temp", "78C"),
        ("BGP session down on {host}", Severity.DISASTER, "bgp.state", "idle"),
        ("OSPF neighbor down on {host}", Severity.HIGH, "ospf.neighbor", "down"),
        ("Certificate expiring soon on {host}", Severity.WARNING, "cert.expiry", "7 days left"),
        ("Backup job failed on {host}", Severity.AVERAGE, "backup.status", "failed"),
        ("DNS resolution failing on {host}", Severity.HIGH, "dns.status", "timeout"),
        ("License expiration warning on {host}", Severity.INFORMATION, "license.status", "expiring"),
        ("Fan speed abnormal on {host}", Severity.WARNING, "fan.speed", "critical low"),
        ("VLAN mismatch detected on {iface} ({host})", Severity.AVERAGE, "vlan.status", "mismatch"),
        ("Spanning-tree topology change on {host}", Severity.WARNING, "stp.status", "tc-detected"),
        ("NTP synchronization lost on {host}", Severity.WARNING, "ntp.status", "unsynchronized"),
        ("RAID degraded on {host}", Severity.HIGH, "raid.status", "degraded"),
    ]

    IFACES = ["Gi0/1", "Gi0/12", "Te1/0/24", "Gi1/0/3", "Po1", "Gi0/24", "Te2/0/1", "Po2"]

    def __init__(self, seed: int | None = None):
        self._rng = random.Random(seed)
        # Пул "сейчас активных" проблем, персистентный между вызовами —
        # ключ для демонстрации реального жизненного цикла алерта (см.
        # AlertsTab: реальное время + вкладка «Закрытые»). Раньше каждый
        # вызов пересобирал ПОЛНОСТЬЮ новый случайный список с новыми id
        # (через _call_seq) — идея была просто не путать одинаковые id между
        # вызовами, но побочный эффект: "закрытие" алерта было неотличимо от
        # того, что это просто новый случайный набор — id никогда не
        # пропадали и не появлялись повторно, только рождались новые. Теперь
        # пул реально живёт между опросами: часть проблем закрывается
        # (пропадает из активных), часть появляется — как в настоящем Zabbix.
        self._active: dict[str, Incident] = {}
        self._next_id = 100000

    def test_connection(self) -> bool:
        return True

    def _spawn(self) -> Incident:
        host = self._rng.choice(self.HOSTS)
        template, severity, key, value = self._rng.choice(self.PROBLEM_TEMPLATES)
        iface = self._rng.choice(self.IFACES)
        problem_name = template.format(host=host, iface=iface)
        inc_id = str(self._next_id)
        self._next_id += 1
        return Incident(
            id=inc_id, host=host, problem_name=problem_name, severity=severity,
            timestamp=datetime.now(), item_key=key, last_value=value,
        )

    def get_active_problems(self, count: int = 40) -> List[Incident]:
        """Первый вызов засевает пул из `count` "активных" проблем (как будто
        Zabbix только что подключили — сразу видно накопленную историю).
        Каждый следующий вызов имитирует естественный оборот: часть активных
        проблем случайно "решается" (пропадает из пула — обнаруживается как
        закрытие при следующем опросе AlertsTab), несколько новых появляется."""
        if not self._active:
            for _ in range(count):
                inc = self._spawn()
                self._active[inc.id] = inc
        else:
            for inc_id in list(self._active.keys()):
                if self._rng.random() < 0.12:
                    del self._active[inc_id]
            for _ in range(self._rng.randint(0, 3)):
                inc = self._spawn()
                self._active[inc.id] = inc

        result = list(self._active.values())
        result.sort(key=lambda x: x.severity.value, reverse=True)
        return result
