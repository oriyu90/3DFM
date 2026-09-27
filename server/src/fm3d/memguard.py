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


def total_bytes() -> int:
    out = _run(["sysctl", "-n", "hw.memsize"])
    try:
        return int(out.strip())
    except ValueError:
        return -1
