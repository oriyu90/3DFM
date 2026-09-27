#!/usr/bin/env python3
"""3DFM environment probe (stdlib only).

Writes <data-dir>/probes.json atomically and prints a human summary.
Exit 0 unless probing itself is impossible.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def run(cmd: list[str], timeout: int = 15) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout)
        return p.returncode, (p.stdout or "").strip()
    except (OSError, subprocess.SubprocessError) as e:
        return 127, str(e)


def vm_free_gb() -> float:
    code, out = run(["vm_stat"])
    if code != 0:
        return -1.0
    import re
    m = re.search(r"page size of (\d+) bytes", out)
    ps = int(m.group(1)) if m else 16384
    total = 0
    for key in ("Pages free", "Pages inactive", "Pages speculative"):
        mm = re.search(rf"{key}:\s+(\d+)", out)
        if mm:
            total += int(mm.group(1)) * ps
    return round(total / 1024**3, 2)


def total_mem_gb() -> float:
    code, out = run(["sysctl", "-n", "hw.memsize"])
    try:
        return round(int(out) / 1024**3, 1)
    except ValueError:
        return -1.0


def venv_probe(python: Path) -> dict:
    info: dict = {"present": python.exists()}
    if not info["present"]:
        return info
    code, out = run([str(python), "-c",
                     "import sys; print(sys.version.split()[0])"])
    info["python"] = out if code == 0 else f"broken: {out}"
    code, out = run([str(python), "-c",
                     "import torch; print(torch.__version__)"])
    info["torch"] = out if code == 0 else None
    if info["torch"]:
        code, out = run([str(python), "-c",
                         "import torch; print(torch.backends.mps.is_available()"
                         " if hasattr(torch.backends,'mps') else False)"])
        info["mps"] = (out == "True") if code == 0 else False
    code, out = run([str(python), "-c", "import mlx; print('1')"])
    info["mlx"] = (code == 0)
    for mod in ("mtlgemm", "mtldiffrast", "fast_simplification", "xatlas"):
        code, _ = run([str(python), "-c", f"import {mod}"])
        info[f"mod_{mod}"] = (code == 0)
    return info


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", required=True)
    ns = ap.parse_args()
    data = Path(ns.data_dir).expanduser()
    data.mkdir(parents=True, exist_ok=True)
    venvs = data / "venvs"

    code, metal = run(["xcrun", "-sdk", "macosx", "metal", "--version"])
    result = {
        "arch": platform.machine(),
        "macos": platform.mac_ver()[0],
        "mem_total_gb": total_mem_gb(),
        "mem_free_gb": vm_free_gb(),
        "disk_free_gb": round((shutil.disk_usage(str(data)).free) / 1024**3, 1),
        "metal_toolchain": code == 0,
        "metal_version": metal.splitlines()[0] if metal else "",
        "venvs": {name: venv_probe(venvs / name / "bin" / "python")
                  for name in ("server", "trellis", "hun-human")},
    }
    warnings: list[str] = []
    if result["arch"] != "arm64":
        warnings.append("Apple Silicon required")
    if result["mem_total_gb"] > 0 and result["mem_total_gb"] < 16:
        warnings.append(f"only {result['mem_total_gb']} GiB memory; "
                        "16 GiB minimum, generation will be limited")
    if result["disk_free_gb"] < 40:
        warnings.append(f"only {result['disk_free_gb']} GiB disk free; "
                        "full models need ~35 GiB")
    if not result["metal_toolchain"]:
        warnings.append("Metal toolchain missing: texture baking falls back "
                        "to slower KDTree path")
    result["warnings"] = warnings

    fd, tmp = tempfile.mkstemp(dir=str(data), prefix=".probes-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, data / "probes.json")
    except OSError as e:
        print(f"cannot write probes.json: {e}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
