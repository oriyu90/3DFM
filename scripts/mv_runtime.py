#!/usr/bin/env python3
"""Install the MV-Adapter code runtime into <data-dir>/runtimes/mv.

- Code: huanngzh/MV-Adapter @ pinned SHA (Apache-2.0; SDXL base weights
  use Open RAIL++-M — surfaced in Setup/Settings).
- Weights: stabilityai/stable-diffusion-xl-base-1.0 + huanngzh/mv-adapter
  (fetched by fetch_models.py human tier).
- Python extras go into venvs/hun-human (torch MPS + diffusers).
Idempotent.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

MV_PIN = "4277e0018232bac82bb2c103caf0893cedb711be"
MV_URL = "https://github.com/huanngzh/MV-Adapter.git"
NOTICE = """MV-Adapter (https://github.com/huanngzh/MV-Adapter) Apache-2.0.
Base model stabilityai/stable-diffusion-xl-base-1.0 is under
Open RAIL++-M License. Pinned for 3DFM's 1-to-6 view synthesis.
"""


def run(cmd, env=None, cwd=None):
    e = dict(os.environ)
    if env:
        e.update(env)
    print("+", " ".join(str(c) for c in cmd), flush=True)
    subprocess.run(cmd, env=e, cwd=cwd, check=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--reinstall", action="store_true")
    ns = ap.parse_args()
    data = Path(ns.data_dir).expanduser()
    rt = data / "runtimes" / "mv"
    code = rt / "MV-Adapter"
    venv_py = data / "venvs" / "hun-human" / "bin" / "python"
    if not venv_py.exists():
        print("hun-human venv missing", file=sys.stderr)
        return 1
    if ns.reinstall:
        shutil.rmtree(rt, ignore_errors=True)
    rt.mkdir(parents=True, exist_ok=True)
    (rt / "NOTICE").write_text(NOTICE)

    if not (code / ".git").exists():
        run(["git", "clone", "--depth", "1", MV_URL, str(code)])
    try:
        run(["git", "-C", str(code), "fetch", "--depth", "1",
             "origin", MV_PIN])
        run(["git", "-C", str(code), "checkout", MV_PIN])
    except subprocess.CalledProcessError:
        print("warn: pin not fetchable; using HEAD")

    import shutil as _sh
    uv = (_sh.which("uv") or os.path.expanduser("~/.local/bin/uv")
          or "/opt/homebrew/bin/uv")
    run([uv, "pip", "install", "--python", str(venv_py),
         "peft", "controlnet_aux", "sentencepiece", "einops",
         "jaxtyping", "typeguard"])
    probe = ("import importlib.util as u;"
             "mods=['diffusers','peft','mvadapter'];"
             "print({m: bool(u.find_spec(m)) for m in mods})")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(code) + os.pathsep + env.get("PYTHONPATH", "")
    r = subprocess.run([str(venv_py), "-c", probe], capture_output=True,
                       text=True, env=env)
    print("import probe:", (r.stdout or r.stderr)[-500:])
    ok = "'diffusers': True" in r.stdout
    (rt / "manifest.json").write_text(json.dumps({
        "mv_pin": MV_PIN, "diffusers_ready": bool(ok)}, indent=2))
    print(f"mv runtime ready (diffusers={ok})")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
