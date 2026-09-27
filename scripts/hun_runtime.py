#!/usr/bin/env python3
"""Install the Hunyuan3D MLX runtime into <data-dir>/runtimes/hun.

- Code: dgrauet/Hunyuan3D-2.1-mlx @ pinned SHA (Apple MLX port of
  Tencent Hunyuan3D-2.1; see NOTICE. Upstream license: Tencent Hunyuan
  3D 2.1 Community License Agreement — NOT for commercial use without
  Tencent's terms; the app surfaces this in Setup/Settings.)
- Weights: dgrauet/hunyuan3d-2.1-mlx (MLX safetensors, fetched by
  fetch_models.py human tier).
- Python extras (MLX-side) go into venvs/hun-human.
Idempotent via marker files.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

MLX_PORT_PIN = "5fe21945b790fbb7fb28c510e89babd7b9feabe6"
MLX_PORT_URL = "https://github.com/dgrauet/Hunyuan3D-2.1-mlx.git"
MLX_WEIGHTS_REPO = "dgrauet/hunyuan3d-2.1-mlx"
NOTICE = """Hunyuan3D-2.1-mlx (https://github.com/dgrauet/Hunyuan3D-2.1-mlx):
Apple MLX port of Tencent Hunyuan3D-2.1. Upstream works governed by the
Tencent Hunyuan 3D 2.1 Community License Agreement (see LICENSE in the
cloned tree). Weights: dgrauet/hunyuan3d-2.1-mlx (same license).
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
    rt = data / "runtimes" / "hun"
    code = rt / "Hunyuan3D-2.1-mlx"
    venv_py = data / "venvs" / "hun-human" / "bin" / "python"
    if not venv_py.exists():
        print("hun-human venv missing", file=sys.stderr)
        return 1
    if ns.reinstall:
        shutil.rmtree(rt, ignore_errors=True)
    rt.mkdir(parents=True, exist_ok=True)
    (rt / "NOTICE").write_text(NOTICE)

    if not (code / ".git").exists():
        run(["git", "clone", "--depth", "1", MLX_PORT_URL, str(code)])
    try:
        run(["git", "-C", str(code), "fetch", "--depth", "1",
             "origin", MLX_PORT_PIN])
        run(["git", "-C", str(code), "checkout", MLX_PORT_PIN])
    except subprocess.CalledProcessError:
        print("warn: pin not fetchable; using HEAD")

    import shutil as _sh
    uv = (_sh.which("uv") or os.path.expanduser("~/.local/bin/uv")
          or "/opt/homebrew/bin/uv")
    run([uv, "pip", "install", "--python", str(venv_py),
         "mlx-arsenal", "PyMCubes", "pymeshlab", "pygltflib"])
    probe = ("import importlib.util as u;"
             "mods=['mlx','mlx_arsenal','trimesh','mcubes','cv2','xatlas'];"
             "print({m: bool(u.find_spec(m)) for m in mods})")
    r = subprocess.run([str(venv_py), "-c", probe], capture_output=True,
                       text=True)
    print("import probe:", (r.stdout or r.stderr)[-800:])
    ok = "'mlx': True" in r.stdout and "'mlx_arsenal': True" in r.stdout
    (rt / "manifest.json").write_text(json.dumps({
        "mlx_port_pin": MLX_PORT_PIN,
        "mlx_weights_repo": MLX_WEIGHTS_REPO,
        "mlx_ready": bool(ok),
    }, indent=2))
    print(f"hun runtime ready (mlx={ok})")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
