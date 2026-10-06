"""Memory safety helpers (stdlib only, no psutil dependency).

- free_bytes(): usable memory (free + inactive + speculative on macOS)
  parsed from `vm_stat`. Used as a preflight gate before spawning a
  GPU worker.
- rss_of_pid(pid): resident set size via `ps -o rss=`. Used by the
  watchdog to kill runaway workers before they destabilize the OS.
"""
from __future__ import annotations

import re
import subprocess


def _run(cmd: list[str]) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True,
                             timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout if out.returncode == 0 else ""


def page_size() -> int:
    m = re.search(r"page size of (\d+) bytes", _run(["vm_stat"]))
    return int(m.group(1)) if m else 16384


def free_bytes() -> int:
    """Free + inactive + speculative pages in bytes. -1 if unknown."""
    out = _run(["vm_stat"])
    if not out:
        return -1
    ps = page_size()
    total = 0
    for key in ("Pages free", "Pages inactive", "Pages speculative"):
        m = re.search(rf"{key}:\s+(\d+)", out)
        if m:
            total += int(m.group(1)) * ps
    return total if total else -1


def rss_of_pid(pid: int) -> int:
    """RSS in bytes for a pid, -1 if the process is gone."""
    out = _run(["ps", "-o", "rss=", "-p", str(pid)])
    try:
        kb = int(out.strip().split()[0])
        return kb * 1024
    except (ValueError, IndexError):
        return -1


def cmdline_of_pid(pid: int) -> str:
    """Full command line of a pid, "" if the process is gone."""
    out = _run(["ps", "-o", "command=", "-p", str(pid)])
    return out.strip()


def is_our_worker(pid: int, job_id: str) -> bool:
    """True only if pid is still the worker we spawned for job_id.

    `os.kill(pid, 0)` alone is not enough: PIDs are recycled, so a dead
    worker's PID may later belong to an unrelated process. The manager
    and crash recovery must never SIGKILL (or adopt) a stranger, so we
    additionally require the command line to contain our worker module
    marker and the exact job id.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0 or not job_id:
        return False
    cmd = cmdline_of_pid(pid)
    return bool(cmd) and "fm3d.worker" in cmd and str(job_id) in cmd


def total_bytes() -> int:
    out = _run(["sysctl", "-n", "hw.memsize"])
    try:
        return int(out.strip())
    except ValueError:
        return -1


def release_idle() -> dict:
    """Best-effort idle memory release (server is stdlib-only).

    - Python GC (collect cycles holding job dicts / progress buffers).
    - Returns before/after free bytes so callers can log the effect.
    - Never raises: idle GC must not destabilize the pump loop.
    Worker GPU memory is reclaimed by process exit; here we only
    clean the server side plus CPython allocator pressure.
    """
    try:
        import gc as _gc
        before = free_bytes()
        _gc.collect()
        after = free_bytes()
        return {"before": before, "after": after}
    except Exception:
        return {"before": -1, "after": -1}


def effective_cap_bytes(configured_gb: float) -> int:
    """Clamp the RSS kill threshold so the OS keeps headroom.

    On a 40 GiB machine a 40 GiB cap would let one worker starve the
    OS. Keep at least ~6 GiB (or 15%) for the system/WindowServer.
    Returns bytes, -1 if total unknown (caller falls back to config).
    """
    try:
        total = total_bytes()
        if total <= 0:
            return -1
        headroom = max(6 * 1024**3, int(total * 0.15))
        adaptive = total - headroom
        configured = int(float(configured_gb) * 1024**3)
        if configured <= 0:
            return adaptive
        return min(configured, adaptive)
    except (ValueError, TypeError):
        return -1
