# Цифровой двойник сети — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Заменить плоский случайный генератор синтетики (`MockZabbixClient`/`MockOxidizedClient`) новым источником данных, который знает реальную топологию сети предприятия и умеет каскадные сбои по графу зависимостей — чтобы накопить живой, проверенный инженером датасет для RAG.

**Architecture:** Новый модуль `topology.py` (чистая графовая логика, без БД) + `digital_twin.py` (`TwinZabbixClient`/`TwinOxidizedClient`, тот же интерфейс, что у существующих Mock-классов) + новая таблица `twin_incidents` в PostgreSQL (состояние симуляции переживает перезапуск служб) + новый режим `synthetic_data_mode: "twin"` в `service_config.json`, подключаемый в трёх местах (`realtime_service.py`, `job_worker.py`, `api_server.py`) без изменений в остальном пайплайне анализа.

**Tech Stack:** Python 3.11+, PostgreSQL (psycopg2), без новых зависимостей. Тесты — обычные Python-скрипты с `assert` (в проекте нет pytest, см. `tests/` — этот план заводит первый такой каталог, но не меняет стиль тестирования, принятый в остальном проекте).

**Spec:** `docs/superpowers/specs/2026-09-16-digital-twin-design.md`

## Global Constraints

- Реальные данные сети (`twin_data/network_topology.json`, IP-адреса, адреса площадок) — НИКОГДА не коммитить в git (уже в `.gitignore`: `twin_data/`, `network_topology.json`). Тесты используют отдельный небольшой файл-фикстуру `tests/fixtures/tiny_topology.json` с вымышленными именами — он идёт в git.
- Существующий интерфейс `BaseZabbixClient`/`MockOxidizedClient` не меняется — `TwinZabbixClient`/`TwinOxidizedClient` реализуют те же методы с теми же сигнатурами, никакой код выше по стеку (`realtime_engine.py`, `job_worker.py`: `JobHandlers`) не трогается.
- `depends_on` в топологии — семантика "И": все перечисленные зависимости (узлы или группы резервирования) должны быть живы. Внутри `redundancy_groups` — семантика "ИЛИ": группа жива, пока жив хотя бы один член.
- Коммит после каждой задачи, сообщения на русском (см. стиль существующих коммитов в `git log`).

---

## Обзор структуры файлов

| Файл | Роль |
|---|---|
| `topology.py` (новый) | Граф зависимостей узлов сети: загрузка JSON, валидация, вычисление недоступности/каскадов. Без обращения к БД/сети. |
| `digital_twin.py` (новый) | `TwinZabbixClient`/`TwinOxidizedClient` — генерация инцидентов/конфигов по топологии, тот же интерфейс, что у Mock-классов. |
| `db.py` (изменить) | + таблица `twin_incidents` в `SCHEMA`, + класс `TwinIncidentRepository`. |
| `backend.py` (изменить) | + поле `twin` в `Backend`, инстанцирование `TwinIncidentRepository`. |
| `service_config.py` (изменить) | + `resolve_synthetic_mode()`, `twin_topology_path()`, новые ключи `DEFAULTS`. |
| `realtime_service.py` (изменить) | `_build_clients()` — трёхветочный выбор источника (real/flat/twin). |
| `job_worker.py` (изменить) | `_build_oxidized_client()` — тот же трёхветочный выбор для Oxidized-стороны. |
| `api_server.py` (изменить) | `lifespan()` — тот же выбор для `oxidized_client` (нужен `/configs/nodes`). |
| `tests/fixtures/tiny_topology.json` (новый) | Маленькая топология для тестов (в git, без реальных данных). |
| `tests/test_topology.py` (новый) | Тесты графовой логики. |
| `tests/test_digital_twin.py` (новый) | Тесты `TwinZabbixClient`/`TwinOxidizedClient` (часть требует живой PostgreSQL). |
| `DEPLOYMENT.md` (изменить) | Документация нового режима для администратора. |

---

### Task 1: `topology.py` — граф зависимостей

**Files:**
- Create: `topology.py`
- Create: `tests/fixtures/tiny_topology.json`
- Create: `tests/test_topology.py`

**Interfaces:**
- Produces: `TopologyNode` (dataclass: `name, role, vendor, model, site, ip, criticality, depends_on: list[str], purpose`), `Topology` (класс с `.sites: dict`, `.redundancy_groups: dict[str, list[str]]`, `.nodes: dict[str, TopologyNode]`, classmethod `.load(path: Path) -> Topology`, методы `.unreachable_given(down_hosts: set[str]) -> set[str]`, `.nearest_down_ancestor(node_name: str, down_hosts: set[str]) -> str | None`).

- [ ] **Step 1: Создать тестовую фикстуру топологии**

Файл `tests/fixtures/tiny_topology.json`:

```json
{
  "sites": {"a": "Site A"},
  "redundancy_groups": {},
  "nodes": [
    {"name": "ROOT", "role": "border_router", "criticality": "HIGH", "depends_on": []},
    {"name": "CORE", "role": "core_switch", "criticality": "HIGH", "depends_on": ["ROOT"]},
    {"name": "LEAF1", "role": "access_switch", "criticality": "LOW", "depends_on": ["CORE"]},
    {"name": "LEAF2", "role": "access_switch", "criticality": "LOW", "depends_on": ["CORE"]},
    {"name": "ORPHAN", "role": "access_switch", "criticality": "LOW", "depends_on": ["MISSING-NODE"]}
  ]
}
```

`ORPHAN` ссылается на несуществующий `MISSING-NODE` намеренно — проверяет, что загрузчик не падает и не создаёт фантомную зависимость.

- [ ] **Step 2: Написать падающий тест**

Файл `tests/test_topology.py`:

```python
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
```

- [ ] **Step 3: Убедиться, что тест падает (модуля ещё нет)**

Run: `python tests/test_topology.py`
Expected: `ModuleNotFoundError: No module named 'topology'`

- [ ] **Step 4: Реализовать `topology.py`**

```python
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
```

- [ ] **Step 5: Запустить тесты и убедиться, что все проходят**

Run: `python tests/test_topology.py`
Expected: `6/6 passed`

- [ ] **Step 6: Commit**

```bash
git add topology.py tests/fixtures/tiny_topology.json tests/test_topology.py
git commit -m "Граф зависимостей топологии сети для цифрового двойника"
```

---

### Task 2: `db.py` — таблица `twin_incidents` и `TwinIncidentRepository`

**Files:**
- Modify: `db.py` (добавить в `SCHEMA` и новый класс рядом с `ServiceStatusRepository`)
- Create: `tests/test_twin_repository.py`

**Interfaces:**
- Consumes: `Database` (существующий класс, `db._connect()`).
- Produces: `TwinIncidentRepository(db: Database)` с методами `create(...)`, `get_open() -> list[dict]`, `get_due_root_causes(now) -> list[dict]`, `resolve(incident_id: str) -> None`, `resolve_by_host(host: str) -> None`, `clear_all() -> int`.

- [ ] **Step 1: Написать падающий тест**

Файл `tests/test_twin_repository.py` (требует доступный PostgreSQL — использует тот же DSN, что и остальное приложение через `service_config.load_service_config()`):

```python
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
```

- [ ] **Step 2: Убедиться, что тест падает (таблицы/атрибута `backend.twin` ещё нет)**

Run: `python tests/test_twin_repository.py`
Expected: `AttributeError: 'Backend' object has no attribute 'twin'` (или похожая ошибка — репозитория ещё нет)

- [ ] **Step 3: Добавить таблицу в `SCHEMA`**

В `db.py`, сразу после блока `job_queue` (после строки с `CREATE INDEX IF NOT EXISTS idx_job_queue_pending`, перед закрывающими `"""` в конце `SCHEMA`):

```python
-- Внутреннее состояние цифрового двойника (см. digital_twin.py) — какие
-- узлы сети сейчас "сломаны" в симуляции. Отдельно от incidents (та
-- хранит уже проанализированные LLM и проверенные инженером записи) —
-- сюда пишет только TwinZabbixClient, наружу (в GUI) не видна напрямую.
CREATE TABLE IF NOT EXISTS twin_incidents (
    id              TEXT PRIMARY KEY,
    host            TEXT NOT NULL,
    problem_name    TEXT NOT NULL,
    severity        INTEGER NOT NULL,
    item_key        TEXT,
    last_value      TEXT,
    started_at      TIMESTAMP NOT NULL DEFAULT now(),
    resolve_at      TIMESTAMP,
    is_root_cause   BOOLEAN NOT NULL DEFAULT true,
    caused_by       TEXT,
    resolved        BOOLEAN NOT NULL DEFAULT false,
    resolved_at     TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_twin_incidents_open ON twin_incidents (resolved) WHERE resolved = false;
```

- [ ] **Step 4: Добавить `TwinIncidentRepository`**

В `db.py`, сразу после класса `ServiceStatusRepository` (после его метода `read_all`, перед следующим классом):

```python
class TwinIncidentRepository:
    """Внутреннее состояние симуляции цифрового двойника (см.
    digital_twin.py) — какие узлы сейчас «сломаны» в симулированном мире.
    Резолюция каскадов пересчитывается заново на каждый тик по топологии
    (см. TwinZabbixClient._reconcile_cascades), а не по цепочке caused_by —
    caused_by хранится только для описательного текста."""

    def __init__(self, db: Database):
        self.db = db

    def create(self, incident_id: str, host: str, problem_name: str, severity: int,
               item_key: str, last_value: str, resolve_at, is_root_cause: bool,
               caused_by: str | None) -> None:
        with self.db._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO twin_incidents
                    (id, host, problem_name, severity, item_key, last_value,
                     resolve_at, is_root_cause, caused_by)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (incident_id, host, problem_name, severity, item_key, last_value,
                     resolve_at, is_root_cause, caused_by),
                )
            conn.commit()

    def get_open(self) -> list[dict]:
        with self.db._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT * FROM twin_incidents WHERE resolved = false")
                return [dict(r) for r in cur.fetchall()]

    def get_due_root_causes(self, now) -> list[dict]:
        with self.db._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT * FROM twin_incidents WHERE resolved = false "
                    "AND is_root_cause = true AND resolve_at IS NOT NULL AND resolve_at <= %s",
                    (now,),
                )
                return [dict(r) for r in cur.fetchall()]

    def resolve(self, incident_id: str) -> None:
        with self.db._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE twin_incidents SET resolved = true, resolved_at = now() WHERE id = %s",
                    (incident_id,),
                )
            conn.commit()

    def resolve_by_host(self, host: str) -> None:
        with self.db._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE twin_incidents SET resolved = true, resolved_at = now() "
                    "WHERE host = %s AND resolved = false",
                    (host,),
                )
            conn.commit()

    def clear_all(self) -> int:
        """Полный сброс симуляции — на случай, если мир «заклинило» и проще
        начать с чистого листа, чем разбирать вручную."""
        with self.db._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM twin_incidents")
                deleted = cur.rowcount
            conn.commit()
        return deleted
```

- [ ] **Step 5: Добавить `twin` в `Backend` и `create_postgres_backend` (`backend.py`)**

Это забегание в Task 3 намеренно — тест из Step 1 использует `backend.twin`, без него Step 6 не пройдёт. В `backend.py`:

```python
class Backend(NamedTuple):
    db: object
    incidents: object
    configs: object
    settings: object
    service_status: object
    users: object
    sessions: object
    jobs: object
    twin: object
```

И в `create_postgres_backend`:

```python
from db import (
    Database, IncidentRepository, ConfigDiffRepository, SettingsRepository,
    ServiceStatusRepository, UserRepository, SessionRepository, JobQueueRepository,
    TwinIncidentRepository,
)

db = Database(dsn=_resolve_dsn(dsn))
settings_repo = SettingsRepository(db)
settings_repo.get_all()
return Backend(
    db=db,
    incidents=IncidentRepository(db),
    configs=ConfigDiffRepository(db),
    settings=settings_repo,
    service_status=ServiceStatusRepository(db),
    users=UserRepository(db),
    sessions=SessionRepository(db),
    jobs=JobQueueRepository(db),
    twin=TwinIncidentRepository(db),
)
```

- [ ] **Step 6: Запустить тесты и убедиться, что все проходят**

Run: `python tests/test_twin_repository.py`
Expected: `4/4 passed` (требует доступную PostgreSQL — тот же DSN, что уже настроен для остального приложения)

- [ ] **Step 7: Commit**

```bash
git add db.py backend.py tests/test_twin_repository.py
git commit -m "Таблица twin_incidents и TwinIncidentRepository для цифрового двойника"
```

---

### Task 3: `service_config.py` — режим `synthetic_data_mode` и путь к топологии

**Files:**
- Modify: `service_config.py`
- Create: `tests/test_service_config_twin.py`

**Interfaces:**
- Produces: `resolve_synthetic_mode(config: dict) -> str` (возвращает `"off" | "flat" | "twin"`), `twin_topology_path(config: dict) -> Path`.

- [ ] **Step 1: Написать падающий тест**

Файл `tests/test_service_config_twin.py`:

```python
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
```

- [ ] **Step 2: Убедиться, что тест падает**

Run: `python tests/test_service_config_twin.py`
Expected: `ImportError: cannot import name 'resolve_synthetic_mode' from 'service_config'`

- [ ] **Step 3: Добавить новые ключи в `DEFAULTS` и функции**

В `service_config.py`, в `DEFAULTS` (после строки `"use_synthetic_data": False,`):

```python
    "use_synthetic_data": False,
    # "" | "off" | "flat" | "twin" — если пусто, режим выводится из
    # старого use_synthetic_data (см. resolve_synthetic_mode), чтобы уже
    # развёрнутые окружения не сломались без ручной правки конфига.
    "synthetic_data_mode": "",
    # Путь к топологии цифрового двойника — относительный ищется рядом с
    # service_config.json (%PROGRAMDATA%\NetAI Monitor\), абсолютный — как есть.
    "twin_topology_file": "network_topology.json",
    "twin_incident_rate_per_day": 50,
```

После функции `config_path()` (перед `log_dir()`), добавить:

```python
def resolve_synthetic_mode(config: dict) -> str:
    """'off' | 'flat' | 'twin'."""
    mode = (config.get("synthetic_data_mode") or "").strip().lower()
    if mode in ("off", "flat", "twin"):
        return mode
    return "flat" if config.get("use_synthetic_data") else "off"


def twin_topology_path(config: dict) -> Path:
    raw = config.get("twin_topology_file") or "network_topology.json"
    path = Path(raw)
    return path if path.is_absolute() else config_dir() / path
```

- [ ] **Step 4: Запустить тесты и убедиться, что все проходят**

Run: `python tests/test_service_config_twin.py`
Expected: `6/6 passed`

- [ ] **Step 5: Commit**

```bash
git add service_config.py tests/test_service_config_twin.py
git commit -m "Режим synthetic_data_mode и путь к топологии в service_config.py"
```

---

### Task 4: `digital_twin.py` — `TwinZabbixClient` и `TwinOxidizedClient`

**Files:**
- Create: `digital_twin.py`
- Create: `tests/test_digital_twin.py`

**Interfaces:**
- Consumes: `Topology`/`TopologyNode` из `topology.py` (Task 1); `TwinIncidentRepository` из `db.py` (Task 2, через `backend.twin`); `Incident`/`Severity` из `models.py`.
- Produces: `TwinZabbixClient(repo, topology, incident_rate_per_day: float, poll_interval_seconds: int, rng=None)` с методами `test_connection() -> bool`, `get_active_problems() -> list[Incident]` (тот же контракт, что `BaseZabbixClient`). `TwinOxidizedClient(topology, rng=None)` с методами `get_nodes() -> list[str]`, `get_current_config(node) -> str`, `get_versions(node) -> list[ConfigVersion]`, `get_diff(node, a, b) -> ConfigDiff`, `get_all_diffs() -> list[ConfigDiff]` (тот же контракт, что `MockOxidizedClient`).

- [ ] **Step 1: Написать падающие тесты**

Файл `tests/test_digital_twin.py` (часть тестов, использующих `TwinZabbixClient`, требует живую PostgreSQL — как и `test_twin_repository.py`; тесты `TwinOxidizedClient` — чистые, без БД):

```python
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
```

- [ ] **Step 2: Убедиться, что тест падает**

Run: `python tests/test_digital_twin.py`
Expected: `ModuleNotFoundError: No module named 'digital_twin'`

- [ ] **Step 3: Реализовать `digital_twin.py`**

```python
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
```

- [ ] **Step 4: Запустить тесты и убедиться, что все проходят**

Run: `python tests/test_digital_twin.py`
Expected: `7/7 passed` (первые 4 требуют доступную PostgreSQL)

- [ ] **Step 5: Commit**

```bash
git add digital_twin.py tests/test_digital_twin.py
git commit -m "TwinZabbixClient/TwinOxidizedClient — цифровой двойник сети"
```

---

### Task 5: Подключить двойник в `realtime_service.py`

**Files:**
- Modify: `realtime_service.py:1-79` (импорты и `_build_clients`), `realtime_service.py:114` (вызов)

**Interfaces:**
- Consumes: `resolve_synthetic_mode`, `twin_topology_path` (Task 3), `Topology` (Task 1), `TwinZabbixClient` (Task 4), `backend.twin` (Task 2).

- [ ] **Step 1: Изменить импорты**

В `realtime_service.py`, заменить блок импортов:

```python
from service_config import ensure_config_file, load_service_config, log_dir, config_path
```

на:

```python
from service_config import (
    ensure_config_file, load_service_config, log_dir, config_path,
    resolve_synthetic_mode, twin_topology_path,
)
from topology import Topology
from digital_twin import TwinZabbixClient
```

- [ ] **Step 2: Переписать `_build_clients`**

Заменить всю функцию `_build_clients` (строки 61-78):

```python
def _build_clients(config: dict, backend):
    mode = resolve_synthetic_mode(config)
    if mode == "twin":
        topology_path = twin_topology_path(config)
        if not topology_path.exists():
            log.error(
                "Режим цифрового двойника включён (synthetic_data_mode=twin), но файл "
                "топологии не найден: %s — положите network_topology.json рядом с "
                "service_config.json. Работаю с пустой топологией (инцидентов не будет).",
                topology_path,
            )
            topology = Topology(sites={}, redundancy_groups={}, nodes=[])
        else:
            topology = Topology.load(topology_path)
            log.info("Цифровой двойник: загружено %d узлов топологии из %s.",
                      len(topology.nodes), topology_path)
        zabbix_client = TwinZabbixClient(
            repo=backend.twin, topology=topology,
            incident_rate_per_day=float(config.get("twin_incident_rate_per_day", 50)),
            poll_interval_seconds=max(10, int(config["poll_interval_seconds"])),
        )
    elif mode == "flat":
        log.info("Режим плоской синтетики — использую MockZabbixClient.")
        zabbix_client = MockZabbixClient(seed=42)
    else:
        if not config["zabbix_url"]:
            log.warning(
                "В %s не заданы параметры Zabbix (zabbix_url пуст) — "
                "опрос работать не будет. Заполните файл и перезапустите службу.",
                config_path(),
            )
        zabbix_client = ZabbixClient(
            url=config["zabbix_url"], user=config["zabbix_user"], password=config["zabbix_password"],
        )

    analyzer = get_analyzer(config["ollama_host"], config["ollama_model"])
    log.info("Анализатор: %s", type(analyzer).__name__)
    return zabbix_client, analyzer
```

- [ ] **Step 3: Обновить вызов в `main()`**

Строка 114, заменить:

```python
    zabbix_client, analyzer = _build_clients(config)
```

на:

```python
    zabbix_client, analyzer = _build_clients(config, backend)
```

- [ ] **Step 4: Проверить компиляцию**

Run: `python -m py_compile realtime_service.py`
Expected: без вывода (успех)

- [ ] **Step 5: Функциональная проверка (режим twin с фикстурой)**

Run:
```bash
python -c "
import service_config
service_config.DEFAULTS['synthetic_data_mode'] = 'twin'
from backend import create_postgres_backend
from realtime_service import _build_clients
config = service_config.load_service_config()
config['synthetic_data_mode'] = 'twin'
config['twin_topology_file'] = 'tests/fixtures/tiny_topology.json'
config['twin_incident_rate_per_day'] = 1000000
backend = create_postgres_backend(config['postgres_dsn'])
zabbix_client, analyzer = _build_clients(config, backend)
print(type(zabbix_client).__name__)
problems = zabbix_client.get_active_problems()
print('problems:', len(problems))
with backend.db._connect() as conn:
    with conn.cursor() as cur:
        cur.execute(\"DELETE FROM twin_incidents WHERE host LIKE 'ROOT' OR host LIKE 'CORE' OR host LIKE 'LEAF%' OR host = 'ORPHAN'\")
    conn.commit()
"
```
Expected: `TwinZabbixClient`, `problems: <какое-то число >= 0>` — без исключений.

- [ ] **Step 6: Commit**

```bash
git add realtime_service.py
git commit -m "Подключить TwinZabbixClient в realtime_service.py (synthetic_data_mode=twin)"
```

---

### Task 6: Подключить двойник в `job_worker.py`

**Files:**
- Modify: `job_worker.py:1-70` (импорты и `_build_oxidized_client`)

**Interfaces:**
- Consumes: `resolve_synthetic_mode`, `twin_topology_path` (Task 3), `Topology` (Task 1), `TwinOxidizedClient` (Task 4).

- [ ] **Step 1: Изменить импорты**

Заменить:

```python
from service_config import ensure_config_file, load_service_config
```

на:

```python
from service_config import ensure_config_file, load_service_config, resolve_synthetic_mode, twin_topology_path
from topology import Topology
from digital_twin import TwinOxidizedClient
```

- [ ] **Step 2: Переписать `_build_oxidized_client`**

Заменить функцию (строки 67-69):

```python
def _build_oxidized_client(config: dict):
    mode = resolve_synthetic_mode(config)
    if mode == "twin":
        topology_path = twin_topology_path(config)
        if not topology_path.exists():
            log.error(
                "Режим цифрового двойника включён, но файл топологии не найден: %s",
                topology_path,
            )
            topology = Topology(sites={}, redundancy_groups={}, nodes=[])
        else:
            topology = Topology.load(topology_path)
        return TwinOxidizedClient(topology=topology)
    if mode == "flat":
        return MockOxidizedClient(seed=42)
    return OxidizedClient(base_url=config["oxidized_url"])
```

(Сигнатура и место вызова `oxidized_client = _build_oxidized_client(config)` в `main()` не меняются — `TwinOxidizedClient` не нужен `backend`.)

- [ ] **Step 3: Проверить компиляцию**

Run: `python -m py_compile job_worker.py`
Expected: без вывода

- [ ] **Step 4: Функциональная проверка**

Run:
```bash
python -c "
import service_config
from job_worker import _build_oxidized_client
config = service_config.load_service_config()
config['synthetic_data_mode'] = 'twin'
config['twin_topology_file'] = 'tests/fixtures/tiny_topology.json'
client = _build_oxidized_client(config)
print(type(client).__name__)
print(client.get_nodes())
print(client.get_diff('ROOT', 'prev', 'latest'))
"
```
Expected: `TwinOxidizedClient`, список узлов фикстуры (`ROOT, CORE, LEAF1, LEAF2, ORPHAN`), непустой diff.

- [ ] **Step 5: Commit**

```bash
git add job_worker.py
git commit -m "Подключить TwinOxidizedClient в job_worker.py (synthetic_data_mode=twin)"
```

---

### Task 7: Подключить двойник в `api_server.py`

**Files:**
- Modify: `api_server.py` (импорты и блок построения `oxidized_client` внутри `lifespan()`)

**Interfaces:**
- Consumes: те же, что Task 6.

- [ ] **Step 1: Изменить импорты**

В `api_server.py:38`, заменить:

```python
from service_config import load_service_config, save_service_config
```

на:

```python
from service_config import load_service_config, save_service_config, resolve_synthetic_mode, twin_topology_path
from topology import Topology
from digital_twin import TwinOxidizedClient
```

- [ ] **Step 2: Переписать блок построения `oxidized_client` в `lifespan()`**

Заменить:

```python
    oxidized_client = (
        MockOxidizedClient(seed=42) if config["use_synthetic_data"]
        else OxidizedClient(base_url=config["oxidized_url"])
    )
```

на:

```python
    mode = resolve_synthetic_mode(config)
    if mode == "twin":
        topology_path = twin_topology_path(config)
        if not topology_path.exists():
            log.error("Режим цифрового двойника включён, но файл топологии не найден: %s", topology_path)
            topology = Topology(sites={}, redundancy_groups={}, nodes=[])
        else:
            topology = Topology.load(topology_path)
        oxidized_client = TwinOxidizedClient(topology=topology)
    elif mode == "flat":
        oxidized_client = MockOxidizedClient(seed=42)
    else:
        oxidized_client = OxidizedClient(base_url=config["oxidized_url"])
```

- [ ] **Step 3: Проверить компиляцию**

Run: `python -m py_compile api_server.py`
Expected: без вывода

- [ ] **Step 4: Функциональная проверка (полный прогон сервера)**

Run (фоново, затем проверить и остановить):
```bash
python -c "
import service_config
config = service_config.load_service_config()
config['synthetic_data_mode'] = 'twin'
config['twin_topology_file'] = 'tests/fixtures/tiny_topology.json'
service_config.save_service_config(config)
"
python api_server.py &
sleep 3
curl -s http://127.0.0.1:8000/auth/me
# затем: остановить процесс api_server.py, вернуть service_config.json к прежнему synthetic_data_mode
```
Expected: сервер стартует без трейсбэка в логе (`%PROGRAMDATA%\NetAI Monitor\logs\api_server.log`), `/auth/me` без токена отвечает 401 (сервис жив).

- [ ] **Step 5: Commit**

```bash
git add api_server.py
git commit -m "Подключить TwinOxidizedClient в api_server.py (synthetic_data_mode=twin)"
```

---

### Task 8: Документация

**Files:**
- Modify: `DEPLOYMENT.md`

**Interfaces:** нет (только документация).

- [ ] **Step 1: Добавить раздел про режим цифрового двойника**

В `DEPLOYMENT.md`, в разделе про `service_config.json` (Шаг 4), после описания существующих полей добавить:

```markdown
### Режим «цифровой двойник» (реалистичная синтетика вместо Zabbix)

Помимо `use_synthetic_data` (простая случайная генерация без связи с
реальной топологией), есть режим `synthetic_data_mode: "twin"` —
генератор, который знает реальную топологию вашей сети (граф зависимостей
устройств) и умеет каскадные сбои (упала граница — недоступны все площадки
за ней). Нужен, чтобы накопить обучающий датасет, лично проверяя
сгенерированные алерты как инженер, без подключения к боевому Zabbix.

```json
{
  "synthetic_data_mode": "twin",
  "twin_topology_file": "network_topology.json",
  "twin_incident_rate_per_day": 50
}
```

`twin_topology_file` ищется рядом с `service_config.json`
(`%PROGRAMDATA%\NetAI Monitor\`), если путь не абсолютный. Формат файла и
генератор из реестра оборудования — см.
`twin_data/build_topology.py` в репозитории (сам файл с реальными данными
сети в git не попадает — конфиденциально, копируется на сервер вручную).

`synthetic_data_mode` заменяет старый `use_synthetic_data`
(`true`/`false` по-прежнему работают, если `synthetic_data_mode` не
задан — `true` эквивалентно `"flat"`).
```

- [ ] **Step 2: Commit**

```bash
git add DEPLOYMENT.md
git commit -m "Документация: режим цифрового двойника в service_config.json"
```

---

## Self-Review (выполнено при написании)

- **Покрытие спеки:** архитектура (Task 5-7), формат топологии и загрузчик (Task 1), таблица `twin_incidents` (Task 2), поведение (каскад/резолюция/спаун — Task 4), конфигурация (Task 3), тестирование (во всех задачах), границы MVP (явно не реализуются — Oxidized-корреляция, redundancy_groups для реальных пар, серверы, ручной триггер — оставлены как есть, не требуют кода в этом плане). Все разделы спеки покрыты.
- **Типы/сигнатуры:** `TwinZabbixClient.get_active_problems() -> List[Incident]` используется одинаково в Task 4 и Task 5; `backend.twin` (Task 2) используется в Task 4/5 тестах и коде; `resolve_synthetic_mode`/`twin_topology_path` (Task 3) используются идентично в Task 5/6/7 — сверено.
- **Плейсхолдеров/TBD нет** — каждый шаг содержит готовый код.
