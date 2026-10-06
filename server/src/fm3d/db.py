"""Crash-safe SQLite job store.

Safety properties:
- WAL mode + synchronous=FULL: committed state survives `kill -9` / power loss.
- busy_timeout: concurrent readers (REST) never fail while the pump writes.
- Every mutation is a single short transaction; connections are per-call
  (no shared connection across threads).
- On startup, any job left in `running` is requeued as `failed`
  (error=worker-gone) unless its worker PID is still alive and belongs
  to us — verified via a pid marker file. This is the crash-recovery path.
"""
from __future__ import annotations

import contextlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterator, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    mode TEXT NOT NULL,
    spec_json TEXT NOT NULL,
    state TEXT NOT NULL,
    stage TEXT NOT NULL DEFAULT '',
    progress REAL NOT NULL DEFAULT 0,
    eta_s REAL,
    error TEXT DEFAULT '',
    queue_index INTEGER NOT NULL DEFAULT 0,
    seed INTEGER,
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL,
    worker_pid INTEGER
);
CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state, queue_index, created_at);
"""

VALID_STATES = {"queued", "running", "done", "failed", "cancelled"}


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path), timeout=30.0, isolation_level=None,
                          check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous=FULL;")
    con.execute("PRAGMA busy_timeout=30000;")
    con.executescript(SCHEMA)
    return con


def _now() -> float:
    return time.time()


class JobStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        with self._con():
            pass

    @contextlib.contextmanager
    def _con(self) -> Iterator[sqlite3.Connection]:
        """Per-call connection, always closed (no reliance on GC timing).

        Each mutation is a single short transaction (`isolation_level=None`
        autocommit + `with con:` commit scope); the context manager closes
        the handle so WAL readers and page caches never accumulate in a
        long-lived server process.
        """
        con = connect(self.db_path)
        try:
            with con:
                yield con
        finally:
            try:
                con.close()
            except sqlite3.Error:
                pass

    # -- writes ---------------------------------------------------------
    def insert(self, job_id: str, name: str, mode: str, spec: dict,
               seed: Optional[int]) -> None:
        with self._con() as con:
            con.execute(
                "INSERT INTO jobs(id,name,mode,spec_json,state,created_at,seed,"
                "queue_index) VALUES(?,?,?,?,?,?,?,?)",
                (job_id, name, mode, json.dumps(spec), "queued",
                 _now(), seed, int(_now() * 1000)))

    def set_state(self, job_id: str, state: str, **fields: Any) -> None:
        assert state in VALID_STATES, state
        allowed = {"stage", "progress", "eta_s", "error", "started_at",
                   "finished_at", "worker_pid", "seed", "name", "mode",
                   "spec_json", "queue_index"}
        for k in fields:
            if k not in allowed:
                raise ValueError(f"invalid field: {k}")
        cols = ["state=?"]
        vals: list[Any] = [state]
        for k, v in fields.items():
            cols.append(f"{k}=?")
            vals.append(v)
        vals.append(job_id)
        with self._con() as con:
            con.execute(f"UPDATE jobs SET {','.join(cols)} WHERE id=?", vals)

    def update_progress(self, job_id: str, stage: str, progress: float,
                        eta_s: Optional[float]) -> None:
        with self._con() as con:
            con.execute(
                "UPDATE jobs SET stage=?, progress=?, eta_s=? WHERE id=?",
                (stage, progress, eta_s, job_id))

    def reorder(self, ordered_ids: list[str]) -> None:
        """Apply a queue order robustly.

        The ids given first keep their relative order at the front; any
        other queued job not mentioned is appended behind (preserving its
        old relative order) instead of being stranded on a stale index.
        Unknown or finished ids are still stamped but never disturb the
        remaining queue.
        """
        with self._con() as con:
            con.execute("BEGIN")
            pos = 0
            seen: set[str] = set()
            for jid in ordered_ids:
                if jid in seen:
                    continue
                seen.add(jid)
                con.execute("UPDATE jobs SET queue_index=? WHERE id=?",
                            (pos, jid))
                pos += 1
            rows = con.execute(
                "SELECT id FROM jobs WHERE state='queued' "
                "ORDER BY queue_index, created_at").fetchall()
            for r in rows:
                if r["id"] in seen:
                    continue
                con.execute("UPDATE jobs SET queue_index=? WHERE id=?",
                            (pos, r["id"]))
                pos += 1

    def delete(self, job_id: str) -> int:
        with self._con() as con:
            cur = con.execute("DELETE FROM jobs WHERE id=?", (job_id,))
            return cur.rowcount

    # -- reads ----------------------------------------------------------
    def get(self, job_id: str) -> Optional[dict]:
        with self._con() as con:
            row = con.execute("SELECT * FROM jobs WHERE id=?",
                              (job_id,)).fetchone()
            return dict(row) if row else None

    def list(self, states: Optional[list[str]] = None) -> list[dict]:
        with self._con() as con:
            if states:
                q = ",".join("?" for _ in states)
                rows = con.execute(
                    f"SELECT * FROM jobs WHERE state IN ({q}) "
                    "ORDER BY queue_index, created_at", states).fetchall()
            else:
                rows = con.execute(
                    "SELECT * FROM jobs ORDER BY queue_index, "
                    "created_at").fetchall()
            return [dict(r) for r in rows]

    def next_queued(self) -> Optional[dict]:
        with self._con() as con:
            row = con.execute(
                "SELECT * FROM jobs WHERE state='queued' "
                "ORDER BY queue_index, created_at LIMIT 1").fetchone()
            return dict(row) if row else None

    def running(self) -> Optional[dict]:
        with self._con() as con:
            row = con.execute(
                "SELECT * FROM jobs WHERE state='running' LIMIT 1").fetchone()
            return dict(row) if row else None

    def recover_orphans(self) -> list[str]:
        """Mark jobs stuck in `running` from a previous (dead) server run.

        Returns the ids that were failed. A job is only kept as running if
        its recorded worker_pid is still alive *and* the process is really
        our worker for that job (command-line check via a pid marker: PIDs
        get recycled, so signal-0 alone could match a stranger and leave
        the job stuck — or worse, let the watchdog kill someone else's
        process). Otherwise it is failed with error=worker-gone. Called
        once at server startup.
        """
        from . import memguard
        fixed: list[str] = []
        with self._con() as con:
            rows = con.execute(
                "SELECT id, worker_pid FROM jobs WHERE state='running'").fetchall()
            for r in rows:
                alive = memguard.is_our_worker(r["worker_pid"] or 0,
                                               r["id"])
                if not alive:
                    con.execute(
                        "UPDATE jobs SET state='failed', finished_at=?, "
                        "error=?, worker_pid=NULL WHERE id=?",
                        (_now(), "worker-gone (server restarted or worker "
                         "killed)", r["id"]))
                    fixed.append(r["id"])
        return fixed
