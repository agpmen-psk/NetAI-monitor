"""
test_digital_twin.py — тесты TwinZabbixClient/TwinOxidizedClient.
Запуск: python tests/test_digital_twin.py
Тесты TwinZabbixClient используют реальную PostgreSQL (backend.twin) —
см. tests/test_twin_repository.py, тот же принцип очистки по префиксу.
"""
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from digital_twin import TwinZabbixClient, TwinOxidizedClient
from topology import Topology, TopologyNode
from backend import create_postgres_backend

FIXTURE = Path(__file__).parent / "fixtures" / "tiny_topology.json"
TEST_HOST_PREFIX = "twindigtest-"


def _prefixed_topology() -> Topology:
    """Та же структура, что tiny_topology.json, но с именами узлов под
    отдельным префиксом — чтобы тест мог чистить за собой в общей таблице
    twin_incidents, не задевая параллельные тесты/реальные данные."""
    return Topology(
        sites={"a": "Site A"}, redundancy_groups={},
        nodes=[
            TopologyNode(name=f"{TEST_HOST_PREFIX}ROOT", role="border_router",
                         criticality="HIGH", depends_on=[]),
            TopologyNode(name=f"{TEST_HOST_PREFIX}CORE", role="core_switch",
                         criticality="HIGH", depends_on=[f"{TEST_HOST_PREFIX}ROOT"]),
            TopologyNode(name=f"{TEST_HOST_PREFIX}LEAF1", role="access_switch",
                         criticality="LOW", depends_on=[f"{TEST_HOST_PREFIX}CORE"]),
        ],
    )


def _cleanup(backend):
    with backend.db._connect() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM twin_incidents WHERE host LIKE %s", (f"{TEST_HOST_PREFIX}%",))
        conn.commit()


def test_spawn_root_cause_when_rng_forces_it():
    backend = create_postgres_backend()
    _cleanup(backend)
    try:
        topology = _prefixed_topology()
        # seed подобран так, чтобы rng.random() на первом вызове был < вероятности
        # спауна при rate=1_000_000/сутки (практически гарантированный спаун).
        client = TwinZabbixClient(
            repo=backend.twin, topology=topology, incident_rate_per_day=1_000_000,
            poll_interval_seconds=60, rng=random.Random(1),
        )
        incidents = client.get_active_problems()
        assert len(incidents) >= 1
        assert all(i.host.startswith(TEST_HOST_PREFIX) for i in incidents)
    finally:
        _cleanup(backend)


def test_cascade_created_when_root_forced_down():
    backend = create_postgres_backend()
    _cleanup(backend)
    try:
        topology = _prefixed_topology()
        root = f"{TEST_HOST_PREFIX}ROOT"
        client = TwinZabbixClient(
            repo=backend.twin, topology=topology, incident_rate_per_day=0,
            poll_interval_seconds=60, rng=random.Random(1),
        )
        # Форсируем root-cause напрямую через репозиторий (без опоры на RNG
        # спауна) — тест проверяет именно каскад, а не сам спаун.
        backend.twin.create(
            incident_id="twindigtest-forced-root", host=root, problem_name="Forced down",
            severity=5, item_key="", last_value="", resolve_at=None,
            is_root_cause=True, caused_by=None,
        )
        incidents = client.get_active_problems()
        hosts = {i.host for i in incidents}
        assert root in hosts
        assert f"{TEST_HOST_PREFIX}CORE" in hosts
        assert f"{TEST_HOST_PREFIX}LEAF1" in hosts
        # причина каскада должна быть видна в тексте проблемы
        core_incident = next(i for i in incidents if i.host == f"{TEST_HOST_PREFIX}CORE")
        assert root in core_incident.problem_name
    finally:
        _cleanup(backend)


def test_cascade_resolves_when_root_resolves():
    backend = create_postgres_backend()
    _cleanup(backend)
    try:
        topology = _prefixed_topology()
        root = f"{TEST_HOST_PREFIX}ROOT"
        client = TwinZabbixClient(
            repo=backend.twin, topology=topology, incident_rate_per_day=0,
            poll_interval_seconds=60, rng=random.Random(1),
        )
        backend.twin.create(
            incident_id="twindigtest-forced-root-2", host=root, problem_name="Forced down",
            severity=5, item_key="", last_value="", resolve_at=None,
            is_root_cause=True, caused_by=None,
        )
        client.get_active_problems()  # создаёт каскад
        backend.twin.resolve("twindigtest-forced-root-2")
        incidents = client.get_active_problems()  # должен убрать каскад
        hosts = {i.host for i in incidents}
        assert f"{TEST_HOST_PREFIX}CORE" not in hosts
        assert f"{TEST_HOST_PREFIX}LEAF1" not in hosts
    finally:
        _cleanup(backend)


def test_resolve_by_host_does_not_touch_fresh_root_cause():
    backend = create_postgres_backend()
    _cleanup(backend)
    try:
        topology = _prefixed_topology()
        root = f"{TEST_HOST_PREFIX}ROOT"
        core = f"{TEST_HOST_PREFIX}CORE"
        client = TwinZabbixClient(
            repo=backend.twin, topology=topology, incident_rate_per_day=0,
            poll_interval_seconds=60, rng=random.Random(1),
        )
        backend.twin.create(
            incident_id="twindigtest-forced-root-3", host=root, problem_name="Forced down",
            severity=5, item_key="", last_value="", resolve_at=None,
            is_root_cause=True, caused_by=None,
        )
        client.get_active_problems()  # создаёт каскадную запись на CORE

        # Независимая, реальная проблема прилетает на тот же хост, что и
        # каскадная запись.
        backend.twin.create(
            incident_id="twindigtest-forced-core-real", host=core, problem_name="Real independent problem",
            severity=5, item_key="", last_value="", resolve_at=None,
            is_root_cause=True, caused_by=None,
        )
        client.get_active_problems()  # не должен закрыть свежий root-cause на CORE

        open_rows = [r for r in backend.twin.get_open() if r["host"] == core]
        assert len(open_rows) == 1
        assert open_rows[0]["is_root_cause"] is True
    finally:
        _cleanup(backend)


def test_empty_topology_does_not_crash():
    backend = create_postgres_backend()
    empty_topology = Topology(sites={}, redundancy_groups={}, nodes=[])
    client = TwinZabbixClient(
        repo=backend.twin, topology=empty_topology, incident_rate_per_day=50,
        poll_interval_seconds=60, rng=random.Random(1),
    )
    # не должно бросать исключение (zip(*[]) и т.п. на пустой топологии)
    client.get_active_problems()


def test_oxidized_get_nodes_matches_topology():
    topology = Topology.load(FIXTURE)
    client = TwinOxidizedClient(topology=topology, rng=random.Random(1))
    assert set(client.get_nodes()) == set(topology.nodes.keys())


def test_oxidized_get_diff_returns_text():
    topology = Topology.load(FIXTURE)
    client = TwinOxidizedClient(topology=topology, rng=random.Random(1))
    diff = client.get_diff("ROOT", "prev", "latest")
    assert diff.node == "ROOT"
    assert len(diff.diff_text) > 0


def run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"OK   {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"ERROR {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    run_all()
