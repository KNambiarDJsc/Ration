"""Local SQLite store. All data stays on this machine.

Concurrency: Claude Code runs matching hooks in parallel and several sessions
may be live at once, so every connection uses WAL + busy_timeout and short
transactions. Schema changes go through PRAGMA user_version migrations.
"""

from __future__ import annotations

import json
import random
import sqlite3
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from . import config
from .models import (
    AgentEvent,
    AgentTask,
    Confidence,
    EventType,
    ResourceType,
    Source,
    UsageSnapshot,
)

MIGRATIONS: list[str] = [
    # v1
    """
    CREATE TABLE providers (
        name TEXT PRIMARY KEY,
        version TEXT,
        capabilities TEXT,
        detected_at REAL
    );
    CREATE TABLE usage_snapshots (
        id INTEGER PRIMARY KEY,
        provider TEXT NOT NULL,
        window TEXT NOT NULL,
        resource_type TEXT NOT NULL,
        used_percent REAL NOT NULL,
        reset_at REAL,
        window_minutes INTEGER,
        source TEXT NOT NULL,
        confidence TEXT NOT NULL,
        captured_at REAL NOT NULL
    );
    CREATE INDEX ix_usage_provider_time ON usage_snapshots(provider, window, captured_at);
    CREATE TABLE budget_windows (
        provider TEXT NOT NULL,
        window TEXT NOT NULL,
        window_minutes INTEGER,
        reset_at REAL,
        PRIMARY KEY (provider, window)
    );
    CREATE TABLE tasks (
        id TEXT PRIMARY KEY,
        description TEXT NOT NULL,
        repo TEXT,
        task_type TEXT,
        priority REAL,
        estimated_value REAL,
        estimated_difficulty REAL,
        risk REAL,
        features TEXT,
        status TEXT NOT NULL DEFAULT 'queued',
        created_at REAL NOT NULL
    );
    CREATE TABLE runs (
        id TEXT PRIMARY KEY,
        task_id TEXT,
        parent_run_id TEXT,
        provider TEXT NOT NULL,
        session_id TEXT,
        model TEXT,
        model_class TEXT,
        effort TEXT,
        status TEXT NOT NULL DEFAULT 'running',
        started_at REAL NOT NULL,
        ended_at REAL,
        cost_usd REAL,
        input_tokens INTEGER,
        output_tokens INTEGER
    );
    CREATE INDEX ix_runs_task ON runs(task_id);
    CREATE TABLE agent_events (
        id INTEGER PRIMARY KEY,
        timestamp REAL NOT NULL,
        provider TEXT NOT NULL,
        session_id TEXT NOT NULL,
        run_id TEXT,
        task_id TEXT,
        event_type TEXT NOT NULL,
        metadata TEXT NOT NULL DEFAULT '{}'
    );
    CREATE INDEX ix_events_session_time ON agent_events(session_id, timestamp);
    CREATE INDEX ix_events_time ON agent_events(timestamp);
    CREATE TABLE scheduler_decisions (
        id INTEGER PRIMARY KEY,
        task_id TEXT,
        run_id TEXT,
        decided_at REAL NOT NULL,
        state TEXT NOT NULL,
        action TEXT NOT NULL,
        reasons TEXT NOT NULL,
        outcome TEXT
    );
    CREATE TABLE progress_events (
        id INTEGER PRIMARY KEY,
        session_id TEXT,
        run_id TEXT,
        at REAL NOT NULL,
        progress REAL NOT NULL,
        stuck REAL NOT NULL,
        reasons TEXT NOT NULL
    );
    CREATE TABLE interventions (
        id INTEGER PRIMARY KEY,
        session_id TEXT,
        run_id TEXT,
        at REAL NOT NULL,
        intervention TEXT NOT NULL,
        explanation TEXT NOT NULL,
        enforced INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE task_outcomes (
        task_id TEXT PRIMARY KEY,
        completed INTEGER NOT NULL,
        checks_passed INTEGER,
        checks_total INTEGER,
        attempts INTEGER,
        wall_time_s REAL,
        cost_usd REAL,
        human_rating INTEGER,
        recorded_at REAL NOT NULL
    );
    CREATE TABLE workflow_edges (
        parent_run_id TEXT NOT NULL,
        child_run_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        PRIMARY KEY (parent_run_id, child_run_id)
    );
    """,
]

SCHEMA_VERSION = len(MIGRATIONS)


def connect(path: Path | None = None, attempts: int = 60) -> sqlite3.Connection:
    p = path or config.db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    for i in range(attempts):
        conn = sqlite3.connect(p, timeout=5.0, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=5000")
            # Switching to WAL needs an exclusive lock and returns SQLITE_BUSY without
            # waiting on busy_timeout. WAL is persistent, so only the very first open
            # (possibly several parallel hooks at once) contends; retry briefly.
            if str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() != "wal":
                conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            migrate(conn)
            return conn
        except sqlite3.OperationalError as e:
            conn.close()
            msg = str(e).lower()
            if ("locked" not in msg and "busy" not in msg) or i == attempts - 1:
                raise
            time.sleep(0.02 + random.random() * 0.05)
    raise RuntimeError("unreachable")


def migrate(conn: sqlite3.Connection) -> int:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version >= SCHEMA_VERSION:
        return version
    conn.execute("BEGIN IMMEDIATE")
    try:
        # Re-check under the write lock: a parallel hook may have migrated already.
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        for i in range(version, SCHEMA_VERSION):
            for stmt in MIGRATIONS[i].split(";"):
                if stmt.strip():
                    conn.execute(stmt)
            conn.execute(f"PRAGMA user_version={i + 1}")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return SCHEMA_VERSION


class Store:
    def __init__(self, path: Path | None = None):
        self.path = path or config.db_path()
        self.conn = connect(self.path)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- events -------------------------------------------------------
    def add_event(self, ev: AgentEvent) -> None:
        self.conn.execute(
            "INSERT INTO agent_events(timestamp, provider, session_id, run_id, task_id, event_type, metadata)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                ev.timestamp,
                ev.provider,
                ev.session_id,
                ev.run_id,
                ev.task_id,
                str(ev.event_type),
                json.dumps(ev.metadata, separators=(",", ":"), default=str),
            ),
        )

    def add_events(self, evs: Iterable[AgentEvent]) -> None:
        self.conn.execute("BEGIN")
        try:
            for ev in evs:
                self.add_event(ev)
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def events(
        self,
        session_id: str | None = None,
        run_id: str | None = None,
        since: float | None = None,
        limit: int = 500,
    ) -> list[AgentEvent]:
        q = "SELECT * FROM agent_events WHERE 1=1"
        args: list[Any] = []
        if session_id:
            q += " AND session_id=?"
            args.append(session_id)
        if run_id:
            q += " AND run_id=?"
            args.append(run_id)
        if since is not None:
            q += " AND timestamp>=?"
            args.append(since)
        q += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        rows = self.conn.execute(q, args).fetchall()
        return [
            AgentEvent(
                provider=r["provider"],
                session_id=r["session_id"],
                event_type=EventType(r["event_type"]),
                metadata=json.loads(r["metadata"]),
                run_id=r["run_id"],
                task_id=r["task_id"],
                timestamp=r["timestamp"],
            )
            for r in reversed(rows)
        ]

    def last_event(self, session_id: str | None, event_type: EventType) -> AgentEvent | None:
        q = "SELECT * FROM agent_events WHERE event_type=?"
        args: list[Any] = [str(event_type)]
        if session_id is not None:
            q += " AND session_id=?"
            args.append(session_id)
        r = self.conn.execute(q + " ORDER BY id DESC LIMIT 1", args).fetchone()
        if r is None:
            return None
        return AgentEvent(
            provider=r["provider"],
            session_id=r["session_id"],
            event_type=EventType(r["event_type"]),
            metadata=json.loads(r["metadata"]),
            run_id=r["run_id"],
            task_id=r["task_id"],
            timestamp=r["timestamp"],
        )

    def events_of_type(self, event_type: EventType, since: float = 0.0, limit: int = 500) -> list[AgentEvent]:
        rows = self.conn.execute(
            "SELECT * FROM agent_events WHERE event_type=? AND timestamp>=? ORDER BY id DESC LIMIT ?",
            (str(event_type), since, limit),
        ).fetchall()
        return [
            AgentEvent(r["provider"], r["session_id"], EventType(r["event_type"]), json.loads(r["metadata"]), r["run_id"], r["task_id"], r["timestamp"])
            for r in reversed(rows)
        ]

    def other_session_activity(self, session_id: str, start: float, end: float) -> bool:
        """Did any other session do anything in [start, end]? (Shared quota windows
        can't be attributed to one session if so.)"""
        r = self.conn.execute(
            "SELECT 1 FROM agent_events WHERE session_id!=? AND timestamp BETWEEN ? AND ?"
            " AND event_type NOT IN ('CALIBRATION','ADVICE') LIMIT 1",
            (session_id, start, end),
        ).fetchone()
        return r is not None

    def recent_advice(self, kind: str, since: float, session_id: str | None = None) -> bool:
        q = "SELECT metadata FROM agent_events WHERE event_type='ADVICE' AND timestamp>=?"
        args: list[Any] = [since]
        if session_id is not None:
            q += " AND session_id=?"
            args.append(session_id)
        return any(json.loads(r[0]).get("kind") == kind for r in self.conn.execute(q, args))

    def active_sessions(self, since: float) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT provider, session_id, MIN(timestamp) AS first_at, MAX(timestamp) AS last_at,"
            " COUNT(*) AS n FROM agent_events WHERE timestamp>=? GROUP BY provider, session_id"
            " ORDER BY last_at DESC",
            (since,),
        ).fetchall()

    # ---- usage --------------------------------------------------------
    def add_usage(self, s: UsageSnapshot) -> None:
        self.conn.execute(
            "INSERT INTO usage_snapshots(provider, window, resource_type, used_percent, reset_at,"
            " window_minutes, source, confidence, captured_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                s.provider,
                s.window,
                str(s.resource_type),
                s.used_percent,
                s.reset_at,
                s.window_minutes,
                str(s.source),
                str(s.confidence),
                s.captured_at,
            ),
        )
        self.conn.execute(
            "INSERT INTO budget_windows(provider, window, window_minutes, reset_at) VALUES (?,?,?,?)"
            " ON CONFLICT(provider, window) DO UPDATE SET window_minutes=excluded.window_minutes,"
            " reset_at=excluded.reset_at",
            (s.provider, s.window, s.window_minutes, s.reset_at),
        )

    def latest_usage(self, provider: str | None = None) -> list[UsageSnapshot]:
        q = (
            "SELECT u.* FROM usage_snapshots u JOIN (SELECT provider, window, MAX(captured_at) AS m"
            " FROM usage_snapshots GROUP BY provider, window) l ON u.provider=l.provider"
            " AND u.window=l.window AND u.captured_at=l.m"
        )
        args: list[Any] = []
        if provider:
            q += " WHERE u.provider=?"
            args.append(provider)
        return [_row_to_usage(r) for r in self.conn.execute(q + " ORDER BY u.provider, u.window", args)]

    def usage_history(self, provider: str, window: str, since: float) -> list[UsageSnapshot]:
        rows = self.conn.execute(
            "SELECT * FROM usage_snapshots WHERE provider=? AND window=? AND captured_at>=?"
            " ORDER BY captured_at",
            (provider, window, since),
        ).fetchall()
        return [_row_to_usage(r) for r in rows]

    # ---- tasks / runs / decisions --------------------------------------
    def add_task(self, t: AgentTask, features: dict[str, Any] | None = None) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO tasks(id, description, repo, task_type, priority, estimated_value,"
            " estimated_difficulty, risk, features, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                t.id,
                t.description,
                t.repo,
                str(t.task_type),
                t.priority,
                t.estimated_value,
                t.estimated_difficulty,
                t.risk,
                json.dumps(features or {}),
                "queued",
                t.created_at,
            ),
        )

    def set_task_status(self, task_id: str, status: str) -> None:
        self.conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, task_id))

    def start_run(
        self,
        run_id: str,
        provider: str,
        task_id: str | None = None,
        model: str | None = None,
        model_class: str | None = None,
        effort: str | None = None,
        parent_run_id: str | None = None,
        session_id: str | None = None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO runs(id, task_id, parent_run_id, provider, session_id, model, model_class,"
            " effort, started_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (run_id, task_id, parent_run_id, provider, session_id, model, model_class, effort, time.time()),
        )
        if parent_run_id:
            self.conn.execute(
                "INSERT OR IGNORE INTO workflow_edges(parent_run_id, child_run_id, kind) VALUES (?,?,?)",
                (parent_run_id, run_id, "retry"),
            )

    def end_run(
        self,
        run_id: str,
        status: str,
        cost_usd: float | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        session_id: str | None = None,
    ) -> None:
        self.conn.execute(
            "UPDATE runs SET status=?, ended_at=?, cost_usd=?, input_tokens=?, output_tokens=?,"
            " session_id=COALESCE(?, session_id) WHERE id=?",
            (status, time.time(), cost_usd, input_tokens, output_tokens, session_id, run_id),
        )

    def fail_task_runs(self, task_id: str) -> None:
        """A caller with a stricter verdict than "the checks pass" (autopilot compares with a
        baseline) found the task's result not worth keeping: its runs must not count as
        successes when the scheduler later asks how a model did on this kind of task."""
        self.conn.execute("UPDATE runs SET status='failed' WHERE task_id=? AND status='succeeded'", (task_id,))

    def add_decision(
        self,
        task_id: str | None,
        run_id: str | None,
        state: dict[str, Any],
        action: dict[str, Any],
        reasons: list[str],
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO scheduler_decisions(task_id, run_id, decided_at, state, action, reasons)"
            " VALUES (?,?,?,?,?,?)",
            (task_id, run_id, time.time(), json.dumps(state, default=str), json.dumps(action, default=str), json.dumps(reasons)),
        )
        return int(cur.lastrowid or 0)

    def set_decision_outcome(self, decision_id: int, outcome: dict[str, Any]) -> None:
        self.conn.execute(
            "UPDATE scheduler_decisions SET outcome=? WHERE id=?", (json.dumps(outcome), decision_id)
        )

    def add_progress(self, session_id: str | None, run_id: str | None, progress: float, stuck: float, reasons: list[str]) -> None:
        self.conn.execute(
            "INSERT INTO progress_events(session_id, run_id, at, progress, stuck, reasons) VALUES (?,?,?,?,?,?)",
            (session_id, run_id, time.time(), progress, stuck, json.dumps(reasons)),
        )

    def add_intervention(self, session_id: str | None, run_id: str | None, intervention: str, explanation: str, enforced: bool) -> None:
        self.conn.execute(
            "INSERT INTO interventions(session_id, run_id, at, intervention, explanation, enforced) VALUES (?,?,?,?,?,?)",
            (session_id, run_id, time.time(), intervention, explanation, int(enforced)),
        )

    def last_intervention(self, session_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM interventions WHERE session_id=? ORDER BY id DESC LIMIT 1", (session_id,)
        ).fetchone()

    def record_outcome(
        self,
        task_id: str,
        completed: bool,
        checks_passed: int | None,
        checks_total: int | None,
        attempts: int,
        wall_time_s: float,
        cost_usd: float | None,
    ) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO task_outcomes(task_id, completed, checks_passed, checks_total, attempts,"
            " wall_time_s, cost_usd, recorded_at) VALUES (?,?,?,?,?,?,?,?)",
            (task_id, int(completed), checks_passed, checks_total, attempts, wall_time_s, cost_usd, time.time()),
        )

    def history(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT t.id, t.description, t.task_type, t.status, t.created_at, o.completed, o.attempts,"
            " o.wall_time_s, o.cost_usd, o.checks_passed, o.checks_total FROM tasks t"
            " LEFT JOIN task_outcomes o ON o.task_id=t.id ORDER BY t.created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()

    def provider_success(self, provider: str, model_class: str, task_type: str | None = None) -> tuple[int, int]:
        """(successes, attempts) for runs started by `ration run`."""
        q = (
            "SELECT SUM(CASE WHEN r.status='succeeded' THEN 1 ELSE 0 END), COUNT(*) FROM runs r"
            " LEFT JOIN tasks t ON t.id=r.task_id WHERE r.provider=? AND r.model_class=?"
            " AND r.status IN ('succeeded','failed')"
        )
        args: list[Any] = [provider, model_class]
        if task_type:
            q += " AND t.task_type=?"
            args.append(task_type)
        s, n = self.conn.execute(q, args).fetchone()
        return int(s or 0), int(n or 0)

    def class_success(self, model_class: str, task_type: str) -> tuple[int, int]:
        """(successes, attempts) for this model class and task type, across all providers.
        Feeds the adaptive scheduler policy: whether *this* class has actually
        worked for *this* kind of task on this machine, not a global assumption."""
        s, n = self.conn.execute(
            "SELECT SUM(CASE WHEN r.status='succeeded' THEN 1 ELSE 0 END), COUNT(*) FROM runs r"
            " LEFT JOIN tasks t ON t.id=r.task_id WHERE r.model_class=? AND t.task_type=?"
            " AND r.status IN ('succeeded','failed')",
            (model_class, task_type),
        ).fetchone()
        return int(s or 0), int(n or 0)

    def table_counts(self) -> dict[str, int]:
        names = [
            r[0]
            for r in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        ]
        return {n: self.conn.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0] for n in names}

    def purge(self) -> None:
        for name in self.table_counts():
            self.conn.execute(f'DELETE FROM "{name}"')
        self.conn.execute("VACUUM")


def _row_to_usage(r: sqlite3.Row) -> UsageSnapshot:
    return UsageSnapshot(
        provider=r["provider"],
        window=r["window"],
        resource_type=ResourceType(r["resource_type"]),
        used_percent=r["used_percent"],
        reset_at=r["reset_at"],
        window_minutes=r["window_minutes"],
        source=Source(r["source"]),
        confidence=Confidence(r["confidence"]),
        captured_at=r["captured_at"],
    )
