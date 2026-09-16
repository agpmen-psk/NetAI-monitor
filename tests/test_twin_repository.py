"""
test_twin_repository.py — функциональный тест TwinIncidentRepository
против реальной PostgreSQL (в проекте нет мок-слоя для БД — см. остальные
функциональные проверки в истории коммитов). Требует настроенный
service_config.json (или NETAI_DB_DSN) с доступной БД.

Запуск: python tests/test_twin_repository.py
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import create_postgres_backend

TEST_HOST_PREFIX = "twintest-"


def _cleanup(backend):
    with backend.db._connect() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM twin_incidents WHERE host LIKE %s", (f"{TEST_HOST_PREFIX}%",))
        conn.commit()


def test_create_and_get_open():
    backend = create_postgres_backend()
    _cleanup(backend)
    try:
        backend.twin.create(
            incident_id="twintest-1", host=f"{TEST_HOST_PREFIX}A", problem_name="Test problem",
            severity=4, item_key="icmpping", last_value="0", resolve_at=None,
            is_root_cause=True, caused_by=None,
        )
        open_rows = backend.twin.get_open()
        matching = [r for r in open_rows if r["host"] == f"{TEST_HOST_PREFIX}A"]
        assert len(matching) == 1
        assert matching[0]["is_root_cause"] is True
        assert matching[0]["resolved"] is False
    finally:
        _cleanup(backend)


def test_resolve_removes_from_open():
    backend = create_postgres_backend()
    _cleanup(backend)
    try:
        backend.twin.create(
            incident_id="twintest-2", host=f"{TEST_HOST_PREFIX}B", problem_name="Test problem",
            severity=2, item_key="", last_value="", resolve_at=None,
            is_root_cause=True, caused_by=None,
        )
        backend.twin.resolve("twintest-2")
        open_hosts = {r["host"] for r in backend.twin.get_open()}
        assert f"{TEST_HOST_PREFIX}B" not in open_hosts
    finally:
        _cleanup(backend)


def test_resolve_by_host():
    backend = create_postgres_backend()
    _cleanup(backend)
    try:
        backend.twin.create(
            incident_id="twintest-3", host=f"{TEST_HOST_PREFIX}C", problem_name="Cascade problem",
            severity=5, item_key="icmpping", last_value="0", resolve_at=None,
            is_root_cause=False, caused_by=f"{TEST_HOST_PREFIX}A",
        )
        backend.twin.resolve_by_host(f"{TEST_HOST_PREFIX}C")
        open_hosts = {r["host"] for r in backend.twin.get_open()}
        assert f"{TEST_HOST_PREFIX}C" not in open_hosts
    finally:
        _cleanup(backend)


def test_get_due_root_causes():
    backend = create_postgres_backend()
    _cleanup(backend)
    try:
        past = datetime.now() - timedelta(minutes=1)
        future = datetime.now() + timedelta(hours=1)
        backend.twin.create(
            incident_id="twintest-4", host=f"{TEST_HOST_PREFIX}D", problem_name="Due",
            severity=3, item_key="", last_value="", resolve_at=past,
            is_root_cause=True, caused_by=None,
        )
        backend.twin.create(
            incident_id="twintest-5", host=f"{TEST_HOST_PREFIX}E", problem_name="Not due",
            severity=3, item_key="", last_value="", resolve_at=future,
            is_root_cause=True, caused_by=None,
        )
        due_hosts = {r["host"] for r in backend.twin.get_due_root_causes(datetime.now())}
        assert f"{TEST_HOST_PREFIX}D" in due_hosts
        assert f"{TEST_HOST_PREFIX}E" not in due_hosts
    finally:
        _cleanup(backend)


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
