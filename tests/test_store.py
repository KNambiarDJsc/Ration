import sqlite3
import threading

from ration import config
from ration.models import AgentEvent, AgentTask, EventType, TaskType
from ration.store import SCHEMA_VERSION, Store, connect, migrate


def test_migrates_fresh_db_and_is_idempotent(tmp_path):
    p = tmp_path / "x.db"
    conn = connect(p)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert migrate(conn) == SCHEMA_VERSION
    conn.close()
    conn2 = connect(p)  # reopen: no error, no duplicate tables
    assert conn2.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_migrates_from_v0_with_existing_file(tmp_path):
    p = tmp_path / "old.db"
    raw = sqlite3.connect(p)
    raw.execute("CREATE TABLE unrelated(x)")
    raw.commit()
    raw.close()
    conn = connect(p)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"agent_events", "usage_snapshots", "scheduler_decisions", "unrelated"} <= names


def test_event_roundtrip(store):
    store.add_event(AgentEvent("claude", "s", EventType.TEST_RESULT, {"passed": 1, "failed": 2}, timestamp=5.0))
    [e] = store.events(session_id="s")
    assert e.event_type == EventType.TEST_RESULT and e.metadata == {"passed": 1, "failed": 2}


def _run(store, provider, model_class, task_type, status):
    task = AgentTask("t", task_type=task_type)
    store.add_task(task)
    store.start_run(f"r-{task.id}", provider, task_id=task.id, model_class=model_class)
    store.end_run(f"r-{task.id}", status)


def test_class_success_aggregates_across_providers(store):
    for provider in ("claude", "codex"):
        _run(store, provider, "cheap", str(TaskType.TESTS), "succeeded")
    _run(store, "claude", "cheap", str(TaskType.TESTS), "failed")
    _run(store, "claude", "strong", str(TaskType.TESTS), "succeeded")  # different class: excluded
    _run(store, "claude", "cheap", str(TaskType.DEBUGGING), "succeeded")  # different task type: excluded

    assert store.class_success("cheap", str(TaskType.TESTS)) == (2, 3)
    assert store.class_success("strong", str(TaskType.TESTS)) == (1, 1)
    assert store.class_success("cheap", str(TaskType.DEBUGGING)) == (1, 1)
    assert store.class_success("premium", str(TaskType.TESTS)) == (0, 0)


def test_concurrent_writers_like_parallel_hooks():
    """Claude Code runs matching hooks in parallel; every writer must succeed."""
    errors = []

    def worker(i):
        try:
            with Store() as s:
                for j in range(25):
                    s.add_event(AgentEvent("claude", f"s{i}", EventType.TOOL_COMPLETED, {"j": j}))
        except Exception as e:  # pragma: no cover - failure path
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    with Store() as s:
        assert s.table_counts()["agent_events"] == 200


def test_purge_removes_everything(store):
    store.add_event(AgentEvent("claude", "s", EventType.TOOL_COMPLETED))
    store.purge()
    assert all(v == 0 for v in store.table_counts().values())
    assert config.db_path().exists()
