"""
mock_oxidized_client.py — расширенный набор синтетических устройств и конфигов
для полноценной демонстрации на защите. Разный уровень риска намеренно
распределён по устройствам (есть и «хорошие», и «плохие» конфиги).
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import List

SYNTHETIC_DIFFS = [
    {"node": "SW-ACCESS-DEMO-07", "diff": (
        "--- before\n+++ after\n@@ interface GigabitEthernet1/0/12 @@\n"
        "-  switchport port-security maximum 2\n-  switchport port-security violation restrict\n"
        "+  no switchport port-security\n"
    )},
    {"node": "RTR-BORDER-DEMO-01", "diff": (
        "--- before\n+++ after\n@@ snmp-agent community @@\n"
        "-  snmp-agent community read cRestricted-2024\n+  snmp-agent community read public\n"
    )},
    {"node": "FW-PERIMETER-DEMO-01", "diff": (
        "--- before\n+++ after\n@@ acl number 3001 @@\n"
        "-  rule 10 deny ip source any destination 203.0.113.0 0.0.0.255\n"
        "+  rule 10 permit ip source any destination any\n"
    )},
    {"node": "SW-CORE-DEMO-01", "diff": (
        "--- before\n+++ after\n@@ line vty 0 4 @@\n"
        "-  transport input ssh\n+  transport input telnet ssh\n"
    )},
    {"node": "SW-DIST-DEMO-03", "diff": (
        "--- before\n+++ after\n@@ interface GigabitEthernet2/0/5 @@\n"
        "-  shutdown\n+  no shutdown\n+  description Unmanaged-new-device\n"
    )},
    {"node": "RTR-BRANCH-DEMO-02", "diff": (
        "--- before\n+++ after\n@@ ip route @@\n"
        "-  ip route 0.0.0.0 0.0.0.0 198.51.100.1\n+  ip route 0.0.0.0 0.0.0.0 198.51.100.254\n"
    )},
    {"node": "SW-ACCESS-DEMO-19", "diff": (
        "--- before\n+++ after\n@@ vlan 100 @@\n"
        "-  vlan 100 name Guest-Isolated\n+  vlan 100 name Guest\n-  ip access-group GUEST-ACL in\n"
    )},
    {"node": "FW-PERIMETER-DEMO-02", "diff": (
        "--- before\n+++ after\n@@ nat @@\n"
        "-  no ip nat inside source static tcp 192.0.2.10 3389 interface GigabitEthernet0/0 3389\n"
        "+  ip nat inside source static tcp 192.0.2.10 3389 interface GigabitEthernet0/0 3389\n"
    )},
]

SYNTHETIC_FULL_CONFIGS = {
    "SW-ACCESS-DEMO-07": (
        "! Демо-конфиг SW-ACCESS-DEMO-07\nhostname SW-ACCESS-DEMO-07\n!\n"
        "snmp-agent community read public\nsnmp-agent community write private\n!\n"
        "interface GigabitEthernet1/0/1\n description Uplink-to-Core\n port link-type trunk\n!\n"
        "interface GigabitEthernet1/0/12\n description User-Port\n port link-type access\n"
        " no switchport port-security\n!\n"
        "line vty 0 4\n transport input telnet ssh\n password 123456\n!\n"
        "acl number 3001\n rule 10 permit ip source any destination any\n!\n"
    ),
    "RTR-BORDER-DEMO-01": (
        "! Демо-конфиг RTR-BORDER-DEMO-01\nhostname RTR-BORDER-DEMO-01\n!\n"
        "snmp-agent community read public\n!\n"
        "interface GigabitEthernet0/0/1\n description WAN-Uplink\n ip address 198.51.100.2 255.255.255.252\n!\n"
        "line vty 0 4\n transport input ssh\n!\nno logging buffered\n!\n"
    ),
    "FW-PERIMETER-DEMO-01": (
        "! Демо-конфиг FW-PERIMETER-DEMO-01\nhostname FW-PERIMETER-DEMO-01\n!\n"
        "acl number 3001\n rule 10 permit ip source any destination any\n"
        " rule 20 deny ip source any destination any\n!\n"
        "snmp-agent community read cRestricted-2024\n!\nline vty 0 4\n transport input ssh\n!\n"
    ),
    "SW-CORE-DEMO-01": (
        "! Демо-конфиг SW-CORE-DEMO-01\nhostname SW-CORE-DEMO-01\n!\n"
        "snmp-agent community read cRestricted-2024\n!\n"
        "interface Vlan-interface1\n ip address 192.0.2.1 255.255.255.0\n!\n"
        "line vty 0 4\n transport input telnet ssh\n!\nlogging host 192.0.2.100\n!\n"
    ),
    "SW-DIST-DEMO-03": (
        "! Демо-конфиг SW-DIST-DEMO-03 (эталонный, best practice)\nhostname SW-DIST-DEMO-03\n!\n"
        "snmp-agent community read cSecure-2026-Rnd\n!\n"
        "interface GigabitEthernet2/0/1\n description Access-Port\n"
        " switchport port-security maximum 2\n switchport port-security violation restrict\n!\n"
        "line vty 0 4\n transport input ssh\n!\nlogging host 192.0.2.150\naaa authentication login default group tacacs+\n!\n"
    ),
    "RTR-BRANCH-DEMO-02": (
        "! Демо-конфиг RTR-BRANCH-DEMO-02\nhostname RTR-BRANCH-DEMO-02\n!\n"
        "snmp-agent community read cRestricted-2024\n!\n"
        "interface GigabitEthernet0/0/1\n ip address 198.51.100.6 255.255.255.252\n!\n"
        "ip route 0.0.0.0 0.0.0.0 198.51.100.254\n!\nline vty 0 4\n transport input ssh\n!\n"
    ),
    "SW-ACCESS-DEMO-19": (
        "! Демо-конфиг SW-ACCESS-DEMO-19\nhostname SW-ACCESS-DEMO-19\n!\n"
        "vlan 100\n name Guest\n!\n"
        "interface GigabitEthernet1/0/8\n port link-type access\n port default vlan 100\n"
        " no switchport port-security\n!\nline vty 0 4\n transport input telnet ssh\n!\n"
    ),
    "FW-PERIMETER-DEMO-02": (
        "! Демо-конфиг FW-PERIMETER-DEMO-02\nhostname FW-PERIMETER-DEMO-02\n!\n"
        "ip nat inside source static tcp 192.0.2.10 3389 interface GigabitEthernet0/0 3389\n!\n"
        "acl number 3002\n rule 10 permit tcp source any destination 192.0.2.10 eq 3389\n!\n"
        "snmp-agent community read cSecure-2026-Rnd\n!\nline vty 0 4\n transport input ssh\n!\n"
    ),
    "SRV-MONITORING-DEMO-01": (
        "! Демо-конфиг SRV-MONITORING-DEMO-01 (эталонный, best practice)\nhostname SRV-MONITORING-DEMO-01\n!\n"
        "snmp-agent community read cSecure-2026-Rnd\n!\nline vty 0 4\n transport input ssh\n!\n"
        "logging host 192.0.2.150\naaa authentication login default group tacacs+\n"
        "ntp server 192.0.2.200\n!\n"
    ),
    "PBX-ASTERISK-DEMO-01": (
        "! Демо-конфиг PBX-ASTERISK-DEMO-01\nhostname PBX-ASTERISK-DEMO-01\n!\n"
        "snmp-agent community read public\n!\nline vty 0 4\n transport input telnet ssh\n password admin\n!\n"
        "sip trunk provider1\n context from-provider\n insecure=port,invite\n!\n"
    ),
}


@dataclass
class ConfigVersion:
    node: str
    version: str
    date: str


@dataclass
class ConfigDiff:
    node: str
    diff_text: str


class MockOxidizedClient:
    """Имитирует REST API Oxidized на расширенном наборе синтетических данных."""

    def __init__(self, seed: int | None = None):
        self._rng = random.Random(seed)

    def get_nodes(self) -> List[str]:
        return list(SYNTHETIC_FULL_CONFIGS.keys())

    def get_current_config(self, node: str) -> str:
        return SYNTHETIC_FULL_CONFIGS.get(
            node, f"! Синтетический демо-конфиг устройства {node}\n! (нет данных)"
        )

    def get_versions(self, node: str) -> List[ConfigVersion]:
        return [
            ConfigVersion(node=node, version="v1", date="2026-09-10 10:00"),
            ConfigVersion(node=node, version="v2", date="2026-09-12 14:30"),
        ]

    def get_diff(self, node: str, version_a: str, version_b: str) -> ConfigDiff:
        entry = next((d for d in SYNTHETIC_DIFFS if d["node"] == node), SYNTHETIC_DIFFS[0])
        return ConfigDiff(node=node, diff_text=entry["diff"])

    def get_all_diffs(self) -> List[ConfigDiff]:
        return [ConfigDiff(node=d["node"], diff_text=d["diff"]) for d in SYNTHETIC_DIFFS]