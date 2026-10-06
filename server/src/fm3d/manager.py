"""Job queue manager: the stability core of 3DFM.

Isolation architecture (why a worker crash can never take down the server):

- The server process NEVER imports torch / diffusers / any model code.
  GPU work happens only in a child `python -m fm3d.worker <job>` process.
- The child writes progress to `progress.jsonl` and artifacts via
  tmp-file + atomic rename. The server only tails the file and updates
  SQLite. A `kill -9` on the worker becomes `failed`, never a server crash.
- All server state is in SQLite (WAL + FULL sync). The pump loop is wrapped
  so one bad job cannot kill the loop.

Watchdogs (memory safety):

- Preflight: refuse to start a job unless free memory >= threshold.
- RSS cap: poll worker RSS; SIGKILL past `mem_cap_gb`.
- Stall: SIGKILL when no progress line for `stall_timeout_s`.
- Cancel: flag file -> SIGTERM after grace -> SIGKILL escalation.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Optional

from . import memguard
from .db import JobStore
from .paths import DataDirs

MIN_FREE_GB_BY_MODE = {"normal": 12.0, "human": 20.0, "test": 0.5}
WORKER_MODULE = "fm3d.worker"


def heartbeat_max_s(settings: dict) -> float:
    """Cap for the worker's progress heartbeat (see backends._Heartbeat).

    Derived from `stall_timeout_s` minus a grace margin so the stall
    watchdog always gets the last word on hung native calls.
    """
    try:
        stall = float(settings.get("stall_timeout_s", 1800) or 1800)
    except (TypeError, ValueError):
        stall = 1800.0
    return max(600.0, stall - 300.0)


class AdoptedProc:
    """Minimal process handle for a worker that outlived a server restart."""

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self._exit: Optional[int] = None
        self._gone = False  # dead, but exit code unknowable (not our child)

    def is_alive(self) -> bool:
        if self._gone:
            return False
        if self._exit is not None:
            return False
        try:
            done_pid, status = os.waitpid(self.pid, os.WNOHANG)
        except ChildProcessError:
            # Adopted after a server restart: not our child, so we can
            # never reap it. Fall back to signal-0 liveness probing.
            try:
                os.kill(self.pid, 0)
                return True
            except OSError:
                self._gone = True
                return False
        except OSError:
            return False
        if done_pid == 0:
            try:
                os.kill(self.pid, 0)
                return True
            except OSError:
                self._gone = True
                return False
        self._exit = os.waitstatus_to_exitcode(status)
        return False

    def exit_code(self) -> Optional[int]:
        self.is_alive()
        return self._exit

    def send_signal(self, sig: int) -> None:
        try:
            os.kill(self.pid, sig)
        except OSError:
            pass


def _tail_text(path: Path, max_lines: int = 40) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 65536))
            data = f.read().decode("utf-8", "replace")
        return "\n".join(data.splitlines()[-max_lines:])
    except OSError:
        return ""


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class JobManager:
    def __init__(self, dirs: DataDirs, store: JobStore,
                 get_settings) -> None:
        self.dirs = dirs
        self.store = store
        self.get_settings = get_settings
        self._procs: dict[str, object] = {}   # job_id -> Process/AdoptedProc
        self._offsets: dict[str, int] = {}    # progress.jsonl tail offsets
        self._cancel_at: dict[str, float] = {}
        self._termed: set[str] = set()
        self._last_db_push: dict[str, float] = {}
        self._stop = False
        # Idle-memory state: last time we did real work, and last idle GC.
        self._last_activity = time.time()
        self._last_idle_gc = 0.0
        self._idle_ticks = 0

    # -- public API used by REST --------------------------------------
    def _stage_files(self, files: list[tuple[str, bytes | Path]]
                     ) -> tuple[Path, list[tuple[str, Path]]]:
        """Land upload bytes (or pre-staged paths) in a staging dir.

        Returns (stage_dir, [(safe_name, staged_path)]). The caller moves
        them into the final inputs dir via _commit_new_job, then removes
        the (now empty) stage dir — or drops it on error. Staging lives
        inside jobs_dir so the final move is an atomic same-volume rename.
        """
        stage = self.dirs.jobs_dir / f".stage-{_new_id()}"
        stage.mkdir(parents=True, exist_ok=True)
        staged: list[tuple[str, Path]] = []
        try:
            for fname, payload in files:
                safe = Path(fname).name or "input"
                if not safe or safe in (".", ".."):
                    safe = "input"
                dest = stage / safe
                if isinstance(payload, (bytes, bytearray)):
                    _atomic_write(dest, bytes(payload))
                else:
                    # Already on disk (streamed upload / retry copy):
                    # same-volume atomic move, no RAM involved.
                    os.replace(str(payload), str(dest))
                staged.append((safe, dest))
            return stage, staged
        except BaseException:
            shutil.rmtree(stage, ignore_errors=True)
            raise

    def _commit_new_job(self, name: str, mode: str, spec: dict,
                        seed: Optional[int],
                        staged: list[tuple[str, Path]]) -> str:
        job_id = _new_id()
        jdir = self.dirs.job_dir(job_id)
        idir = jdir / "inputs"
        idir.mkdir(parents=True, exist_ok=True)
        for safe, src in staged:
            os.replace(str(src), str(idir / safe))
        record = {"id": job_id, "name": name, "mode": mode,
                  "spec": spec, "seed": seed,
                  "inputs": sorted(p.name for p in idir.iterdir())}
        _atomic_write(jdir / "spec.json",
                      json.dumps(record, ensure_ascii=False).encode())
        self.store.insert(job_id, name, mode, spec, seed)
        return job_id

    def create_job(self, name: str, mode: str, spec: dict,
                   inputs: list[tuple[str, bytes]],
                   seed: Optional[int]) -> str:
        stage, staged = self._stage_files(inputs)
        try:
            return self._commit_new_job(name, mode, spec, seed, staged)
        finally:
            # Commit moves every staged file out; whatever remains (empty
            # dir on success, leftovers on failure) is dropped here.
            shutil.rmtree(stage, ignore_errors=True)

    def create_job_streamed(self, name: str, mode: str, spec: dict,
                            seed: Optional[int],
                            staged: list[tuple[str, Path]],
                            stage: Path) -> str:
        """Commit files the endpoint already streamed to a staging dir."""
        try:
            return self._commit_new_job(name, mode, spec, seed, staged)
        finally:
            shutil.rmtree(stage, ignore_errors=True)

    def cancel_job(self, job_id: str, force: bool = False) -> tuple[bool, str]:
        job = self.store.get(job_id)
        if not job:
            return False, "not found"
        if job["state"] == "queued":
            self._remove_job_files(job_id)
            self.store.delete(job_id)
            # Queued jobs never spawned; drop any cached tail state.
            self._offsets.pop(job_id, None)
            self._last_db_push.pop(job_id, None)
            self._cancel_at.pop(job_id, None)
            self._termed.discard(job_id)
            return True, "cancelled"
        if job["state"] != "running":
            return False, f"job is {job['state']}"
        (self.dirs.job_dir(job_id) / "cancel.flag").touch(exist_ok=True)
        self._cancel_at[job_id] = time.time()
        if force:
            proc = self._procs.get(job_id)
            self._kill(job_id, signal.SIGKILL)
            if proc is not None:
                self._schedule_reap(proc)
            self._finalize(job_id, cancelled=True,
                           error="cancelled (forced)")
            return True, "cancelled"
        return True, "cancel requested"

    def retry_job(self, job_id: str) -> tuple[Optional[str], str]:
        job = self.store.get(job_id)
        if not job:
            return None, "not found"
        if job["state"] in ("queued", "running"):
            return None, f"job is {job['state']}"
        src = self.dirs.job_dir(job_id)
        try:
            spec_rec = json.loads((src / "spec.json").read_text())
        except (OSError, ValueError):
            return None, "original spec unreadable"
        if not (src / "inputs").is_dir():
            return None, "original inputs missing"
        # Copy (not read) the original inputs through a staging dir: retry
        # of a 6-view human job must not load hundreds of MB into the
        # long-lived server process. An empty inputs dir is legitimate
        # (e.g. test backend needs no image) — only a missing dir is an
        # error.
        stage = self.dirs.jobs_dir / f".stage-{_new_id()}"
        try:
            stage.mkdir(parents=True, exist_ok=True)
            staged: list[tuple[str, Path]] = []
            for p in sorted((src / "inputs").glob("*")):
                if p.is_file() and p.name not in (".", ".."):
                    dest = stage / Path(p.name).name
                    shutil.copyfile(p, dest)
                    staged.append((dest.name, dest))
            return self._commit_new_job(
                job["name"], job["mode"], spec_rec.get("spec", {}),
                job["seed"], staged), "queued"
        except OSError as e:
            return None, f"cannot stage retry inputs: {e}"
        finally:
            shutil.rmtree(stage, ignore_errors=True)

    def reorder(self, ordered_ids: list[str]) -> None:
        self.store.reorder(ordered_ids)

    def log_tail(self, job_id: str, n: int = 200) -> str:
        lines = _tail_text(self.dirs.job_dir(job_id) / "log.txt",
                           max_lines=n)
        if lines:
            return lines
        return _tail_text(self.dirs.job_dir(job_id) / "progress.jsonl",
                          max_lines=n)

    # -- pump loop ------------------------------------------------------
    async def run_forever(self) -> None:
        while not self._stop:
            try:
                idle = await self._tick()
            except Exception:
                traceback.print_exc()
                idle = False
            # Idle backoff: hot loop (0.5s) while work exists, cool
            # loop (2s) when the queue is empty so an idle app holds
            # ~no CPU and lets the OS reclaim pressure.
            await asyncio.sleep(2.0 if idle else 0.5)

    def stop(self) -> None:
        self._stop = True

    async def _tick(self) -> bool:
        """One pump step. Returns True when fully idle (no work)."""
        running = self.store.running()
        if running is not None:
            self._last_activity = time.time()
            self._idle_ticks = 0
            await self._supervise(running["id"])
            return False
        nxt = self.store.next_queued()
        if nxt is None:
            self._idle_ticks += 1
            self._maybe_idle_gc()
            return True
        self._last_activity = time.time()
        self._idle_ticks = 0
        await self._maybe_start(nxt)
        return False

    def _maybe_idle_gc(self) -> None:
        """Release server-side memory when the queue stays empty.

        Called from the idle branch of _tick. Runs at most once per
        `idle_gc_s` (settings, default 60s) and only after the pump
        has observed sustained idleness, so a brief gap between jobs
        does not thrash.
        """
        try:
            settings = self.get_settings()
            interval = float(settings.get("idle_gc_s", 60) or 60)
        except (TypeError, ValueError):
            interval = 60.0
        if interval <= 0:
            return
        now = time.time()
        # Require a few idle ticks first (avoids GC between back-to-back jobs).
        if self._idle_ticks < 3:
            return
        if now - self._last_idle_gc < interval:
            return
        if now - self._last_activity < interval:
            return
        self._last_idle_gc = now
        try:
            import gc as _gc
            _gc.collect()
            res = memguard.release_idle()
            b, a = res.get("before", -1), res.get("after", -1)
            print(f"[idle-gc] released server memory "
                  f"(free {b/1024**3:.1f} -> {a/1024**3:.1f} GiB)",
                  flush=True)
        except Exception:
            traceback.print_exc()

    # -- start ------------------------------------------------------------
    def _worker_python(self, mode: str) -> tuple[Optional[str], str]:
        """Pick the venv python for a mode.

        Torch backends must run under their own venv interpreter.
        Returns (executable, error). Missing runtime -> clean job failure
        with a setup hint (never a crash).
        """
        if mode == "normal":
            cand = self.dirs.venvs_dir / "trellis" / "bin" / "python"
            if cand.exists():
                return str(cand), ""
            return None, ("trellis runtime is not installed "
                          "(venvs/trellis missing). Run Setup first.")
        if mode == "human":
            cand = self.dirs.venvs_dir / "hun-human" / "bin" / "python"
            if cand.exists():
                return str(cand), ""
            return None, ("hun-human runtime is not installed "
                          "(venvs/hun-human missing). Run Setup first.")
        return sys.executable, ""

    async def _maybe_start(self, job: dict) -> None:
        jid = job["id"]
        settings = self.get_settings()
        need = MIN_FREE_GB_BY_MODE.get(job["mode"], 12.0)
        floor = float(settings.get("mem_min_free_gb", need) or need)
        # human jobs are heavier; never go below the mode floor
        floor = max(floor, MIN_FREE_GB_BY_MODE.get(job["mode"], 12.0))
        free = memguard.free_bytes()
        if free >= 0 and free < floor * 1024**3:
            self.store.update_progress(jid, "waiting-memory",
                                       job["progress"], None)
            return
        exe, err = self._worker_python(job["mode"])
        if exe is None:
            self.store.set_state(
                jid, "failed", stage="setup-missing",
                error=err, finished_at=time.time())
            return
        env = dict(os.environ)
        env["FM3D_DATA_DIR"] = str(self.dirs.root)
        # Effective models dir (custom location support): workers resolve
        # the same path via env first, so GUI/CLI/server/worker agree.
        try:
            env["FM3D_MODELS_DIR"] = str(self.dirs.models_dir)
        except (OSError, RuntimeError, ValueError):
            pass
        # Pipeline-internal downloads (DINOv3 etc.) stay inside our data dir
        # and work offline once fetched.
        env["HF_HUB_CACHE"] = str(self.dirs.models_dir / ".hf-cache")
        # Extends the macOS GPU watchdog timeout as a side effect of
        # Metal-debugger mode: prevents kIOGPUCommandBufferCallbackError
        # kills during the long SLat-decoder dispatch (TRELLIS on MPS).
        env["MTL_CAPTURE_ENABLED"] = "1"
        src = Path(__file__).resolve().parents[1]  # .../server/src
        env["PYTHONPATH"] = str(src) + os.pathsep + env.get("PYTHONPATH", "")
        env["PYTHONUNBUFFERED"] = "1"
        # Must be present BEFORE torch/MPS initializes in the worker
        # (backend reads it once). Backends must not rely on setdefault.
        env.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
        env.setdefault("ATTN_BACKEND", "sdpa")
        env.setdefault("SPARSE_ATTN_BACKEND", "sdpa")
        env.setdefault("SPARSE_CONV_BACKEND", "flex_gemm")
        # Bound the worker's progress heartbeat so a hung native call
        # stops refreshing and the stall watchdog below can fire (see
        # backends._Heartbeat). Cap = stall window minus a grace margin.
        env["FM3D_HEARTBEAT_MAX_S"] = str(heartbeat_max_s(settings))
        try:
            proc = await asyncio.create_subprocess_exec(
                exe, "-m", WORKER_MODULE, jid,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                env=env, start_new_session=True)
        except OSError as e:
            self.store.set_state(jid, "failed", stage="spawn",
                                 error=f"cannot start worker: {e}",
                                 finished_at=time.time())
            return
        self._procs[jid] = proc
        self._offsets[jid] = 0
        self.store.set_state(jid, "running", stage="starting",
                             started_at=time.time(), worker_pid=proc.pid,
                             error="")

    # -- supervise ----------------------------------------------------------
    async def _supervise(self, jid: str) -> None:
        proc = self._procs.get(jid)
        if proc is None:
            # Adopted after restart, or record/procs out of sync. Only
            # adopt when the PID really is our worker for this job —
            # PIDs are recycled, and adopting (then possibly SIGKILLing)
            # a stranger would be catastrophic.
            job = self.store.get(jid)
            pid = (job or {}).get("worker_pid") or 0
            if memguard.is_our_worker(pid, jid):
                self._procs[jid] = AdoptedProc(int(pid))
                return
            self._finalize(jid, failed=True,
                           error="worker-gone (no process handle)")
            return
        alive = self._proc_alive(proc)
        self._tail_progress(jid)
        settings = self.get_settings()
        jdir = self.dirs.job_dir(jid)
        cancelled = (jdir / "cancel.flag").exists() or jid in self._cancel_at

        if alive:
            await self._watchdog(jid, proc, settings, cancelled)
            return
        # reaped
        code = self._proc_code(proc)
        self._procs.pop(jid, None)
        if cancelled or (jdir / "cancel.flag").exists():
            self._finalize(jid, cancelled=True, error="cancelled")
            return
        # Adopted workers (server restart) have no knowable exit code;
        # judge by artifacts instead.
        if (code == 0 or code is None) and self._artifacts_ok(jid):
            self._finalize(jid, done=True)
        else:
            self._finalize(jid, failed=True,
                           error=self._diagnose(jid, code))

    async def _watchdog(self, jid, proc, settings, cancelled: bool) -> None:
        now = time.time()
        if cancelled and jid not in self._termed:
            grace = float(settings.get("cancel_grace_s", 10.0))
            since = now - self._cancel_at.get(jid, now)
            if since >= grace:
                self._kill(jid, signal.SIGTERM)
                self._termed.add(jid)
            return
        if cancelled:
            if now - self._cancel_at.get(jid, now) > 20:
                self._kill(jid, signal.SIGKILL)
            return
        # RSS cap (adaptive: never let one worker starve the OS).
        try:
            configured = float(settings.get("mem_cap_gb", 40.0))
        except (TypeError, ValueError):
            configured = 40.0
        eff = memguard.effective_cap_bytes(configured)
        cap = eff if eff > 0 else int(configured * 1024**3)
        rss = memguard.rss_of_pid(self._proc_pid(proc))
        if rss > 0 and rss > cap:
            self._kill(jid, signal.SIGKILL)
            # Schedule a reaper; finalizing now drops the handle.
            self._schedule_reap(proc)
            self._finalize(jid, failed=True,
                           error=f"memory cap exceeded "
                                 f"(RSS {rss/1024**3:.1f} GiB > "
                                 f"{cap/1024**3:.0f} GiB)")
            return
        # stall
        try:
            mtime = (self.dirs.job_dir(jid) / "progress.jsonl").stat().st_mtime
        except OSError:
            job = self.store.get(jid)
            mtime = (job or {}).get("started_at") or now
        try:
            stall_s = float(settings.get("stall_timeout_s", 1800))
        except (TypeError, ValueError):
            stall_s = 1800.0
        if now - mtime > stall_s:
            self._kill(jid, signal.SIGKILL)
            self._schedule_reap(proc)
            self._finalize(jid, failed=True,
                           error=f"stalled: no progress for "
                                 f"{int(now-mtime)}s")

    # -- progress tail --------------------------------------------------------
    def _tail_progress(self, jid: str) -> None:
        p = self.dirs.job_dir(jid) / "progress.jsonl"
        try:
            size = p.stat().st_size
        except OSError:
            return
        off = self._offsets.get(jid, 0)
        if size < off:
            off = 0  # rotated/truncated; reread
        if size == off:
            return
        try:
            with open(p, "rb") as f:
                f.seek(off)
                chunk = f.read()
                end = f.tell()
        except OSError:
            return
        # Only consume complete lines. If the worker was mid-write,
        # the trailing partial line stays for the next tick instead
        # of being dropped (previous code advanced past it and lost
        # the final 100% update).
        last_nl = chunk.rfind(b"\n")
        if last_nl < 0:
            # No complete line yet; wait for the newline.
            # Guard against an unbounded single line.
            if len(chunk) > 1024 * 1024:
                self._offsets[jid] = end
            return
        consumable = chunk[:last_nl + 1]
        self._offsets[jid] = off + len(consumable)
        last = None
        for line in consumable.splitlines():
            if not line.strip():
                continue
            try:
                last = json.loads(line)
            except ValueError:
                continue
        if not isinstance(last, dict):
            return
        now = time.time()
        if now - self._last_db_push.get(jid, 0) < 1.0:
            return
        self._last_db_push[jid] = now
        try:
            prog = float(last.get("progress", 0))
        except (TypeError, ValueError):
            prog = 0.0
        eta = last.get("eta_s")
        self.store.update_progress(jid, str(last.get("stage", "")),
                                   max(0.0, min(100.0, prog)), eta)

    # -- finalize ---------------------------------------------------------------
    def _artifacts_ok(self, jid: str) -> bool:
        glb = self.dirs.job_dir(jid) / "artifacts" / "model.glb"
        try:
            return glb.is_file() and glb.stat().st_size > 0
        except OSError:
            return False

    def _diagnose(self, jid: str, code) -> str:
        tail = _tail_text(self.dirs.job_dir(jid) / "log.txt", 25)
        low = tail.lower()
        if code == -9 or "signal 9" in low or "killed" in low.split()[-10:]:
            return ("worker killed by signal 9 (often the OS out-of-memory "
                    "killer). Try a smaller pipeline/texture size. "
                    + tail[-2000:])
        if "out of memory" in low or "oom" in low:
            return "worker out of memory. " + tail[-2000:]
        if "empty mesh" in low or "empty-mesh" in low:
            return ("empty mesh produced. Retry with a different seed. "
                    + tail[-2000:])
        if code is None:
            return "worker exited (unknown cause). " + tail[-2000:]
        return f"worker exited with code {code}. " + tail[-2000:]

    def _finalize(self, jid: str, done=False, failed=False,
                  cancelled=False, error="") -> None:
        self._procs.pop(jid, None)
        self._cancel_at.pop(jid, None)
        self._termed.discard(jid)
        # Per-job tail state is pure cache: drop it so an idle server
        # does not grow _offsets/_last_db_push without bound.
        self._offsets.pop(jid, None)
        self._last_db_push.pop(jid, None)
        self._last_activity = time.time()
        # Opportunistic server-side GC after each job: the pump may go
        # idle next, and we want file buffers / SQLite pages released.
        try:
            import gc as _gc
            _gc.collect()
        except Exception:
            pass
        now = time.time()
        if done:
            self.store.set_state(jid, "done", stage="done",
                                 progress=100.0, finished_at=now,
                                 worker_pid=None, error="")
            self._write_meta(jid, "done", "")
        elif cancelled:
            self.store.set_state(jid, "cancelled", stage="cancelled",
                                 finished_at=now, worker_pid=None,
                                 error=error or "cancelled")
            self._write_meta(jid, "cancelled", error or "cancelled")
        else:
            self.store.set_state(jid, "failed", finished_at=now,
                                 worker_pid=None, error=error)
            self._write_meta(jid, "failed", error)

    def _write_meta(self, jid: str, state: str, error: str) -> None:
        jdir = self.dirs.job_dir(jid)
        job = self.store.get(jid) or {}
        meta = {
            "id": jid, "name": job.get("name"), "mode": job.get("mode"),
            "state": state, "error": error,
            "seed": job.get("seed"),
            "spec": self._spec_of(jid),
            "started_at": job.get("started_at"),
            "finished_at": job.get("finished_at"),
            "total_mem_gb": round((memguard.total_bytes() or 0) / 1024**3, 1),
        }
        try:
            _atomic_write(jdir / "meta.json",
                          json.dumps(meta, ensure_ascii=False,
                                     indent=2).encode())
        except OSError:
            pass

    def _spec_of(self, jid: str) -> dict:
        try:
            rec = json.loads(
                (self.dirs.job_dir(jid) / "spec.json").read_text())
            return rec.get("spec", {})
        except (OSError, ValueError):
            return {}

    # -- proc helpers -------------------------------------------------------------
    @staticmethod
    def _proc_alive(proc) -> bool:
        if isinstance(proc, AdoptedProc):
            return proc.is_alive()
        return proc.returncode is None

    @staticmethod
    def _proc_code(proc):
        if isinstance(proc, AdoptedProc):
            return proc.exit_code()
        return proc.returncode

    @staticmethod
    def _proc_pid(proc) -> int:
        return proc.pid

    def _kill(self, jid: str, sig: int) -> None:
        proc = self._procs.get(jid)
        if proc is None:
            return
        try:
            if isinstance(proc, AdoptedProc):
                proc.send_signal(sig)
            else:
                proc.send_signal(sig)
        except (OSError, ProcessLookupError, RuntimeError):
            pass

    def _schedule_reap(self, proc) -> None:
        """Reap an asyncio child we are about to drop (anti-zombie).

        The RSS/stall paths finalize immediately, which pops the
        handle from _procs. Without a waiter the PID stays a zombie
        until server exit. Fire-and-forget a waiter instead.
        """
        try:
            import asyncio as _aio
            if hasattr(proc, "wait") and not isinstance(proc, AdoptedProc):
                try:
                    loop = _aio.get_running_loop()
                    loop.create_task(proc.wait())
                except RuntimeError:
                    pass
        except Exception:
            pass

    def _remove_job_files(self, job_id: str) -> None:
        shutil.rmtree(self.dirs.job_dir(job_id), ignore_errors=True)


def _new_id() -> str:
    import secrets
    ts = time.strftime("%Y%m%d%H%M%S", time.gmtime())
    return f"{ts}-{secrets.token_hex(4)}"
