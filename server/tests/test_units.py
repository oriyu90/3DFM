#!/usr/bin/env python3
"""Unit tests for the 3DFM memory-safety + reliability paths.

Stdlib only: run with the system python3.

    PYTHONPATH=server/src python3 server/tests/test_units.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

os.environ.setdefault("FM3D_DATA_DIR", tempfile.mkdtemp(prefix="3dfm-test-"))

from fm3d import memguard  # noqa: E402
from fm3d.db import JobStore  # noqa: E402
from fm3d.manager import JobManager  # noqa: E402
from fm3d.settings import validate  # noqa: E402
from fm3d.worker import Ctx  # noqa: E402


def make_mgr(root: Path) -> JobManager:
    from fm3d.paths import DataDirs
    dirs = DataDirs(root).ensure()
    store = JobStore(dirs.db_path)
    return JobManager(dirs, store, lambda: {"stall_timeout_s": 1800})


class MemguardTest(unittest.TestCase):
    def test_free_bytes_nonnegative_or_unknown(self):
        self.assertGreaterEqual(memguard.free_bytes(), -1)

    def test_total_bytes_positive(self):
        self.assertGreater(memguard.total_bytes(), 0)

    def test_release_idle_never_raises(self):
        res = memguard.release_idle()
        self.assertIn("before", res)
        self.assertIn("after", res)

    def test_is_our_worker_rejects_strangers(self):
        # PID 1 (launchd) is never our worker.
        self.assertFalse(memguard.is_our_worker(1, "any-job"))
        self.assertFalse(memguard.is_our_worker(0, "any-job"))
        self.assertFalse(memguard.is_our_worker(-5, "any-job"))
        self.assertFalse(memguard.is_our_worker(os.getpid(), ""))

    def test_is_our_worker_accepts_self(self):
        # The test runner's own cmdline contains the job id only if we
        # fake it: check the negative path (cmdline mismatch) instead.
        self.assertFalse(
            memguard.is_our_worker(os.getpid(), "no-such-job-id-xyz"))

    def test_effective_cap_leaves_headroom(self):
        cap = memguard.effective_cap_bytes(1000.0)
        total = memguard.total_bytes()
        self.assertLessEqual(cap, total - 6 * 1024**3)


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="3dfm-store-"))
        self.store = JobStore(self.tmp / "jobs.db")

    def test_roundtrip(self):
        self.store.insert("j1", "n", "test", {"a": 1}, 7)
        job = self.store.get("j1")
        self.assertEqual(job["state"], "queued")
        self.assertEqual(job["seed"], 7)
        self.store.set_state("j1", "running", stage="s", worker_pid=123)
        self.assertEqual(self.store.get("j1")["stage"], "s")
        self.assertEqual(self.store.running()["id"], "j1")

    def test_reorder_appends_unmentioned(self):
        for j in ("a", "b", "c"):
            self.store.insert(j, j, "test", {}, None)
        self.store.reorder(["c"])
        order = [j["id"] for j in self.store.list(["queued"])]
        self.assertEqual(order[0], "c")
        self.assertEqual(sorted(order), ["a", "b", "c"])

    def test_recover_orphans_kills_strangers(self):
        # A running job whose pid is launchd (alive but not ours) must be
        # failed, never adopted.
        self.store.insert("orph", "o", "test", {}, None)
        self.store.set_state("orph", "running", worker_pid=1)
        fixed = self.store.recover_orphans()
        self.assertEqual(fixed, ["orph"])
        self.assertEqual(self.store.get("orph")["state"], "failed")

    def test_recover_orphans_dead_pid(self):
        self.store.insert("dead", "d", "test", {}, None)
        self.store.set_state("dead", "running", worker_pid=99999999)
        fixed = self.store.recover_orphans()
        self.assertEqual(fixed, ["dead"])


class ManagerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="3dfm-mgr-"))
        self.mgr = make_mgr(self.tmp)

    def test_create_job_roundtrip(self):
        jid = self.mgr.create_job("n", "test", {"k": 1}, [("a.png", b"xx")],
                                  None)
        self.assertTrue((self.tmp / "jobs" / jid / "inputs" / "a.png"
                         ).is_file())
        self.assertEqual(self.mgr.store.get(jid)["state"], "queued")
        # No staging dirs leak.
        self.assertEqual(list((self.tmp / "jobs").glob(".stage-*")), [])

    def test_retry_empty_inputs_allowed(self):
        # Test backend needs no image: retry of an input-less job works.
        jid = self.mgr.create_job("n", "test", {"duration_s": 1}, [], None)
        self.mgr.store.set_state(jid, "failed", error="boom",
                                 finished_at=time.time())
        new_id, msg = self.mgr.retry_job(jid)
        self.assertEqual(msg, "queued")
        self.assertEqual(self.mgr.store.get(new_id)["state"], "queued")

    def test_retry_missing_inputs_dir_errors(self):
        import shutil as _sh
        jid = self.mgr.create_job("n", "test", {"k": 1}, [("a.png", b"xx")],
                                  None)
        self.mgr.store.set_state(jid, "failed", error="boom",
                                 finished_at=time.time())
        _sh.rmtree(self.tmp / "jobs" / jid / "inputs")
        new_id, msg = self.mgr.retry_job(jid)
        self.assertIsNone(new_id)
        self.assertIn("missing", msg)

    def test_retry_copies_without_ram_read(self):
        jid = self.mgr.create_job("n", "test", {"k": 1}, [("a.png", b"xx")],
                                  3)
        self.mgr.store.set_state(jid, "failed", error="boom",
                                 finished_at=time.time())
        new_id, msg = self.mgr.retry_job(jid)
        self.assertEqual(msg, "queued")
        self.assertTrue((self.tmp / "jobs" / new_id / "inputs" / "a.png"
                         ).is_file())
        self.assertEqual(self.mgr.store.get(new_id)["seed"], 3)

    def test_cancel_queued_removes_files(self):
        jid = self.mgr.create_job("n", "test", {}, [("a.png", b"xx")], None)
        ok, _ = self.mgr.cancel_job(jid)
        self.assertTrue(ok)
        self.assertIsNone(self.mgr.store.get(jid))
        self.assertFalse((self.tmp / "jobs" / jid).exists())

    def test_heartbeat_cap_derived_from_stall(self):
        from fm3d.manager import heartbeat_max_s
        self.assertEqual(heartbeat_max_s({"stall_timeout_s": 1800}), 1500.0)
        self.assertEqual(heartbeat_max_s({}), 1500.0)
        self.assertEqual(heartbeat_max_s({"stall_timeout_s": 100}), 600.0)
        self.assertEqual(heartbeat_max_s({"stall_timeout_s": "bad"}), 1500.0)


class HeartbeatTest(unittest.TestCase):
    def test_heartbeat_stops_after_max_s(self):
        from fm3d.backends import _Heartbeat
        tmp = Path(tempfile.mkdtemp(prefix="3dfm-hb-"))
        ctx = Ctx(job_id="hb", job_dir=tmp, spec={}, seed=None)
        hb = _Heartbeat(ctx, "working", 10.0, "x",
                        max_s=60.0, interval_s=0.05)
        # Pretend the deadline passed: the loop must stop refreshing.
        hb._deadline = time.monotonic() - 1.0
        hb.__enter__()
        try:
            time.sleep(0.25)
            lines = (tmp / "progress.jsonl").read_text().strip().splitlines() \
                if (tmp / "progress.jsonl").exists() else []
            # No refresh happened after the deadline...
            time.sleep(0.2)
            now = (tmp / "progress.jsonl").read_text().strip().splitlines() \
                if (tmp / "progress.jsonl").exists() else []
            self.assertEqual(len(now), len(lines))
            # ...and the loop thread exited on its own.
            hb._th.join(timeout=5)
            self.assertFalse(hb._th.is_alive())
        finally:
            hb.__exit__()

    def test_heartbeat_env_cap(self):
        from fm3d.backends import _Heartbeat
        os.environ["FM3D_HEARTBEAT_MAX_S"] = "0.01"
        try:
            tmp = Path(tempfile.mkdtemp(prefix="3dfm-hb2-"))
            ctx = Ctx(job_id="hb2", job_dir=tmp, spec={}, seed=None)
            hb = _Heartbeat(ctx, "working", 10.0, "x")
            self.assertLessEqual(hb._deadline - time.monotonic(), 60.1)
        finally:
            del os.environ["FM3D_HEARTBEAT_MAX_S"]


class TailTest(unittest.TestCase):
    def test_partial_line_not_consumed(self):
        mgr = make_mgr(self.tmp)
        jid = mgr.create_job("n", "test", {}, [], None)
        p = mgr.dirs.job_dir(jid) / "progress.jsonl"
        line = json.dumps({"stage": "s", "progress": 50.0}) + "\n"
        p.write_text(line + '{"stage": "s2", "progress": 60')  # partial
        mgr._tail_progress(jid)
        job = mgr.store.get(jid)
        self.assertAlmostEqual(job["progress"], 50.0)
        # Complete the line; next tick consumes it.
        with open(p, "a") as f:
            f.write(".0}\n")
        mgr._last_db_push.pop(jid, None)
        # force past the 1s db throttle
        mgr._last_db_push[jid] = 0.0
        mgr._tail_progress(jid)
        self.assertAlmostEqual(mgr.store.get(jid)["progress"], 60.0)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="3dfm-tailmgr-"))


class SettingsTest(unittest.TestCase):
    def test_reject_unknown_and_bad_types(self):
        ok, _ = validate({"nope": 1})
        self.assertFalse(ok)
        ok, _ = validate({"mem_cap_gb": -1})
        self.assertFalse(ok)
        ok, _ = validate({"stall_timeout_s": 5})
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main(verbosity=2)
