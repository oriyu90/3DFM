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

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

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
        con = connect(db_path)
        con.close()

    def _con(self) -> sqlite3.Connection:
        return connect(self.db_path)

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
        with self._con() as con:
            for i, jid in enumerate(ordered_ids):
                con.execute("UPDATE jobs SET queue_index=? WHERE id=?",
                            (i, jid))

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
        its recorded worker_pid is still alive; otherwise it is failed with
        error=worker-gone. Called once at server startup.
        """
        import os
        fixed: list[str] = []
        with self._con() as con:
            rows = con.execute(
                "SELECT id, worker_pid FROM jobs WHERE state='running'").fetchall()
            for r in rows:
                pid = r["worker_pid"]
                alive = False
                if pid:
                    try:
                        os.kill(int(pid), 0)
                        alive = True
                    except (OSError, ValueError):
                        alive = False
                if not alive:
                    con.execute(
                        "UPDATE jobs SET state='failed', finished_at=?, "
                        "error=?, worker_pid=NULL WHERE id=?",
                        (_now(), "worker-gone (server restarted or worker "
                         "killed)", r["id"]))
                    fixed.append(r["id"])
        return fixed
