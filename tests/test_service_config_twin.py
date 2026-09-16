"""
test_service_config_twin.py — тесты обратной совместимости и разрешения
режима синтетики. Запуск: python tests/test_service_config_twin.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from service_config import resolve_synthetic_mode, twin_topology_path, config_dir


def test_explicit_mode_wins():
    assert resolve_synthetic_mode({"synthetic_data_mode": "twin", "use_synthetic_data": False}) == "twin"


def test_legacy_true_maps_to_flat():
    assert resolve_synthetic_mode({"use_synthetic_data": True}) == "flat"


def test_legacy_false_maps_to_off():
    assert resolve_synthetic_mode({"use_synthetic_data": False}) == "off"


def test_missing_keys_default_to_off():
    assert resolve_synthetic_mode({}) == "off"


def test_topology_path_relative_is_under_config_dir():
    path = twin_topology_path({"twin_topology_file": "network_topology.json"})
    assert path == config_dir() / "network_topology.json"


def test_topology_path_absolute_is_kept():
    absolute = str(Path("C:/somewhere/custom_topology.json"))
    path = twin_topology_path({"twin_topology_file": absolute})
    assert str(path) == absolute


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
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    run_all()
