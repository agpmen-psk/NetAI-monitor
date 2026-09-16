"""
test_topology.py — тесты graph-логики topology.py. В проекте нет pytest —
запуск: python tests/test_topology.py (обычные функции + assert, тот же
стиль, что использовался для функциональных проверок в этой сессии).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from topology import Topology, TopologyNode

FIXTURE = Path(__file__).parent / "fixtures" / "tiny_topology.json"


def test_load_basic():
    topo = Topology.load(FIXTURE)
    assert set(topo.nodes.keys()) == {"ROOT", "CORE", "LEAF1", "LEAF2", "ORPHAN"}
    assert topo.nodes["CORE"].depends_on == ["ROOT"]
    assert topo.nodes["CORE"].role == "core_switch"


def test_broken_reference_dropped():
    topo = Topology.load(FIXTURE)
    assert topo.nodes["ORPHAN"].depends_on == []


def test_unreachable_given_root_down():
    topo = Topology.load(FIXTURE)
    unreachable = topo.unreachable_given({"ROOT"})
    assert unreachable == {"ROOT", "CORE", "LEAF1", "LEAF2"}
    assert "ORPHAN" not in unreachable


def test_unreachable_given_leaf_down_does_not_cascade_up():
    topo = Topology.load(FIXTURE)
    unreachable = topo.unreachable_given({"LEAF1"})
    assert unreachable == {"LEAF1"}


def test_nearest_down_ancestor():
    topo = Topology.load(FIXTURE)
    assert topo.nearest_down_ancestor("LEAF1", {"ROOT"}) == "ROOT"
    assert topo.nearest_down_ancestor("LEAF1", set()) is None


def test_redundancy_group_suppresses_cascade():
    topo = Topology(
        sites={}, redundancy_groups={"pair": ["A1", "A2"]},
        nodes=[
            TopologyNode(name="A1", role="border_router", depends_on=[]),
            TopologyNode(name="A2", role="border_router", depends_on=[]),
            TopologyNode(name="B", role="access_switch", depends_on=["pair"]),
        ],
    )
    # один из пары упал — B всё ещё жив (группа держится на A2)
    assert topo.unreachable_given({"A1"}) == {"A1"}
    # оба упали — только тогда B каскадит
    assert topo.unreachable_given({"A1", "A2"}) == {"A1", "A2", "B"}


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
