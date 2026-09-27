"""3DFM local server: FastAPI + single-flight GPU queue.

Run: `python -m fm3d.main` (or via the Swift app / `3dfm serve`).
Env: FM3D_DATA_DIR, FM3D_PORT, FM3D_TEST=1 (enable `test` backend).
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import (Depends, FastAPI, File, Form, Header, HTTPException,
                     UploadFile)
from fastapi.responses import FileResponse

from . import memguard
from .db import JobStore
from .manager import JobManager
from .paths import DataDirs, resolve_data_dir
from .settings import load as load_settings
from .settings import save as save_settings
from .settings import validate as validate_settings

MAX_UPLOAD_BYTES = 200 * 1024 * 1024
MAX_FILES = 8
MODES = ("normal", "human", "test")


def _read_token(path: Path) -> str:
    try:
        tok = path.read_text(encoding="utf-8").strip()
        if tok:
            return tok
    except OSError:
        pass
    tok = secrets.token_hex(24)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, tok.encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return tok


@asynccontextmanager
async def lifespan(app: FastAPI):
    dirs: DataDirs = app.state.dirs
    store: JobStore = app.state.store
    mgr: JobManager = app.state.mgr
    adopted = store.recover_orphans()
    if adopted:
        print(f"recovered {len(adopted)} orphan job(s) -> failed")
    _apply_retention(dirs, store, app.state.settings)
    task = asyncio.create_task(mgr.run_forever())
    yield
    mgr.stop()
    await task


def _apply_retention(dirs: DataDirs, store: JobStore,
                     settings: dict) -> None:
    days = float(settings.get("retention_days", 0) or 0)
    if days <= 0:
        return
    cutoff = time.time() - days * 86400
    for job in store.list(["done", "failed", "cancelled"]):
        fin = job.get("finished_at") or 0
        if fin and fin < cutoff:
            shutil.rmtree(dirs.job_dir(job["id"]), ignore_errors=True)
            store.delete(job["id"])


def create_app() -> FastAPI:
    dirs = resolve_data_dir().expanduser()
    dirs = DataDirs(dirs).ensure()
    settings = load_settings(dirs.settings_path)
    token = _read_token(dirs.token_path)
    store = JobStore(dirs.db_path)
    app = FastAPI(title="3DFM", version="0.1.0", lifespan=lifespan)
    app.state.dirs = dirs
    app.state.settings = settings
    app.state.token = token
    app.state.store = store
    app.state.mgr = JobManager(dirs, store, lambda: app.state.settings)

    async def authed(authorization: Optional[str] = Header(None)):
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(401, "missing bearer token")
        if not secrets.compare_digest(authorization[7:], app.state.token):
            raise HTTPException(403, "bad token")

    @app.get("/health")
    def health():
        gpu = "mps" if _has_mps() else "cpu"
        return {"status": "ok", "version": "0.1.0", "gpu": gpu,
                "mem_total_gb": round((memguard.total_bytes() or 0) / 1024**3, 1),
                "mem_free_gb": round((memguard.free_bytes() or 0) / 1024**3, 1)}

    @app.post("/jobs", dependencies=[Depends(authed)])
    async def submit(spec: str = Form(...),
                     images: list[UploadFile] = File(default=[])):
        try:
            body = json.loads(spec)
        except ValueError:
            raise HTTPException(422, "spec must be JSON")
        mode = body.get("mode", "normal")
        if mode not in MODES:
            raise HTTPException(422, f"mode must be one of {MODES}")
        if mode == "test" and os.environ.get("FM3D_TEST") != "1":
            raise HTTPException(403, "test backend disabled")
        if len(images) > MAX_FILES:
            raise HTTPException(422, f"max {MAX_FILES} images")
        blobs: list[tuple[str, bytes]] = []
        for up in images:
            data = await up.read()
            if len(data) > MAX_UPLOAD_BYTES:
                raise HTTPException(422, f"{up.filename}: file too large")
            if not data:
                raise HTTPException(422, f"{up.filename}: empty file")
            blobs.append((Path(up.filename or "input").name, data))
        n = len(blobs)
        if mode == "normal" and n != 1:
            raise HTTPException(422, "normal mode needs exactly 1 image")
        if mode == "human" and n not in (1, 6):
            raise HTTPException(422, "human mode needs 1 or 6 images")
        name = str(body.get("name") or "job")[:80]
        seed = body.get("seed")
        try:
            seed = None if seed is None else int(seed)
        except (TypeError, ValueError):
            raise HTTPException(422, "seed must be int")
        mgr: JobManager = app.state.mgr
        try:
            jid = mgr.create_job(name, mode, body, blobs, seed)
        except OSError as e:
            raise HTTPException(500, f"cannot store job: {e}")
        return {"id": jid}

    @app.get("/jobs", dependencies=[Depends(authed)])
    def list_jobs(state: Optional[str] = None):
        states = state.split(",") if state else None
        return {"jobs": [_public(j) for j in app.state.store.list(states)]}

    @app.get("/jobs/{jid}", dependencies=[Depends(authed)])
    def show_job(jid: str):
        job = app.state.store.get(jid)
        if not job:
            raise HTTPException(404, "not found")
        return _public(job)

    @app.delete("/jobs/{jid}", dependencies=[Depends(authed)])
    def cancel_job(jid: str, force: bool = False):
        ok, msg = app.state.mgr.cancel_job(jid, force=force)
        if not ok:
            raise HTTPException(404 if msg == "not found" else 409, msg)
        return {"ok": True, "message": msg}

    @app.post("/jobs/{jid}/retry", dependencies=[Depends(authed)])
    def retry_job(jid: str):
        new_id, msg = app.state.mgr.retry_job(jid)
        if not new_id:
            raise HTTPException(404 if msg == "not found" else 409, msg)
        return {"id": new_id}

    @app.post("/jobs/reorder", dependencies=[Depends(authed)])
    def reorder(body: dict):
        ids = body.get("ids", [])
        if not isinstance(ids, list):
            raise HTTPException(422, "ids must be a list")
        app.state.mgr.reorder([str(i) for i in ids])
        return {"ok": True}

    @app.get("/jobs/{jid}/log", dependencies=[Depends(authed)])
    def job_log(jid: str, tail: int = 200):
        if not app.state.store.get(jid):
            raise HTTPException(404, "not found")
        return {"log": app.state.mgr.log_tail(jid, min(tail, 2000))}

    @app.get("/jobs/{jid}/artifacts", dependencies=[Depends(authed)])
    def artifacts(jid: str):
        if not app.state.store.get(jid):
            raise HTTPException(404, "not found")
        root = app.state.dirs.job_dir(jid)
        out = []
        for p in sorted(root.rglob("*")):
            if p.is_file() and "inputs" not in p.parts:
                try:
                    out.append({"path": str(p.relative_to(root)),
                                "size": p.stat().st_size})
                except OSError:
                    pass
        return {"artifacts": out}

    @app.get("/jobs/{jid}/file", dependencies=[Depends(authed)])
    def artifact_file(jid: str, path: str):
        if not app.state.store.get(jid):
            raise HTTPException(404, "not found")
        root = app.state.dirs.job_dir(jid)
        target = (root / path)
        try:
            resolved = target.resolve()
        except OSError:
            raise HTTPException(404, "not found")
        if root.resolve() not in resolved.parents and resolved != root.resolve():
            raise HTTPException(403, "path escape denied")
        if not resolved.is_file():
            raise HTTPException(404, "not found")
        return FileResponse(str(resolved))

    @app.get("/models/status", dependencies=[Depends(authed)])
    def models_status():
        from .backends import SETUP_HINT  # noqa: F401
        manifest = _manifest()
        items = []
        for m in manifest:
            p = app.state.dirs.models_dir / m["dir"]
            present = p.is_dir() and any(p.iterdir())
            size = _dir_size(p) if present else 0
            items.append({"id": m["id"], "present": present,
                          "size_gb": round(size / 1024**3, 2),
                          "expected_gb": m["gb"]})
        return {"models": items,
                "runtimes": _runtime_status(app.state.dirs)}

    @app.get("/settings", dependencies=[Depends(authed)])
    def get_settings():
        return app.state.settings

    @app.put("/settings", dependencies=[Depends(authed)])
    def put_settings(patch: dict):
        ok, msg = validate_settings(patch)
        if not ok:
            raise HTTPException(422, msg)
        app.state.settings.update(patch)
        try:
            save_settings(app.state.dirs.settings_path, app.state.settings)
        except OSError as e:
            raise HTTPException(500, f"cannot save settings: {e}")
        return app.state.settings

    return app


def _public(job: dict) -> dict:
    return {k: job.get(k) for k in (
        "id", "name", "mode", "state", "stage", "progress", "eta_s",
        "error", "seed", "created_at", "started_at", "finished_at")}


def _has_mps() -> bool:
    try:
        import torch
        return bool(getattr(torch.backends, "mps", None)
                    and torch.backends.mps.is_available())
    except ImportError:
        return False


def _manifest() -> list[dict]:
    return [
        {"id": "rmbg-2.0", "dir": "rmbg-2.0", "gb": 0.7},
        {"id": "trellis-2-4b", "dir": "trellis-2-4b", "gb": 15},
        {"id": "hunyuan3d-2.1-mlx", "dir": "hunyuan3d-2.1-mlx", "gb": 14},
        {"id": "sdxl-base-1.0", "dir": "sdxl-base-1.0", "gb": 7},
        {"id": "mv-adapter", "dir": "mv-adapter", "gb": 1.5},
        {"id": "realesrgan", "dir": "realesrgan", "gb": 0.06},
    ]


def _dir_size(p: Path) -> int:
    total = 0
    for f in p.rglob("*"):
        try:
            if f.is_file():
                total += f.stat().st_size
        except OSError:
            pass
    return total


def _runtime_status(dirs: DataDirs) -> dict:
    out = {}
    for name in ("server", "trellis", "hun-human"):
        py = dirs.venvs_dir / name / "bin" / "python"
        out[name] = {"present": py.exists()}
    try:
        import torch
        out["server"]["torch"] = torch.__version__
    except ImportError:
        pass
    return out


def main() -> None:
    import uvicorn
    app = create_app()
    port = int(app.state.settings.get("port", 44931))
    for attempt in range(10):
        try:
            uvicorn.run(app, host="127.0.0.1", port=port + attempt,
                        log_level="warning", access_log=False)
            return
        except OSError as e:
            if "address" in str(e).lower() or "in use" in str(e).lower():
                print(f"port {port+attempt} busy, trying next")
                continue
            raise
    raise SystemExit("no free port found")


if __name__ == "__main__":
    main()
