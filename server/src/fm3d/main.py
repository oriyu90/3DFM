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
from . import __version__ as _server_version
from .db import JobStore
from .manager import JobManager
from .paths import DataDirs, resolve_data_dir
from .settings import load as load_settings
from .settings import save as save_settings
from .settings import validate as validate_settings

MAX_UPLOAD_BYTES = 200 * 1024 * 1024
MAX_FILES = 8
MODES = ("normal", "human", "test")
# Uploads are streamed in chunks so a batch of large images never sits
# fully in the long-lived server process (see submit()).
UPLOAD_CHUNK = 1024 * 1024


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
    # Remove our liveness marker here (not only in main()'s finally):
    # uvicorn re-kills itself with the received signal AFTER graceful
    # shutdown, so code after uvicorn.run() never executes on SIGTERM.
    _drop_pid_file(dirs)


def _drop_pid_file(dirs: DataDirs) -> None:
    try:
        if dirs.pid_path.read_text(encoding="utf-8").strip() == str(
                os.getpid()):
            dirs.pid_path.unlink()
    except OSError:
        pass


def _apply_retention(dirs: DataDirs, store: JobStore,
                     settings: dict) -> None:
    days = float(settings.get("retention_days", 0) or 0)
    if days <= 0:
        return
    cutoff = time.time() - days * 86400
    for job in store.list(["done", "failed", "cancelled"]):
        fin = job.get("finished_at") or 0
        if fin and fin < cutoff:
            # Only forget the DB record when the files are actually gone:
            # a half-failed rmtree must keep its record, never orphan
            # gigabytes silently.
            errors: list = []
            shutil.rmtree(dirs.job_dir(job["id"]), ignore_errors=False,
                          onerror=lambda *a: errors.append(a))
            if not errors:
                store.delete(job["id"])


def create_app() -> FastAPI:
    dirs = resolve_data_dir().expanduser()
    dirs = DataDirs(dirs).ensure()
    settings = load_settings(dirs.settings_path)
    token = _read_token(dirs.token_path)
    store = JobStore(dirs.db_path)
    app = FastAPI(title="3DFM", version=_server_version,
                  lifespan=lifespan)
    app.state.dirs = dirs
    app.state.settings = settings
    app.state.token = token
    app.state.store = store
    app.state.mgr = JobManager(dirs, store, lambda: app.state.settings)
    app.state.gpu_probe = {"at": 0.0, "value": "unknown"}

    async def authed(authorization: Optional[str] = Header(None)):
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(401, "missing bearer token")
        if not secrets.compare_digest(authorization[7:], app.state.token):
            raise HTTPException(403, "bad token")

    @app.get("/health")
    def health():
        return {"status": "ok", "version": _server_version,
                "gpu": _gpu_kind(app),
                "mem_total_gb": round((memguard.total_bytes() or 0) / 1024**3, 1),
                "mem_free_gb": round((memguard.free_bytes() or 0) / 1024**3, 1)}

    @app.post("/jobs", dependencies=[Depends(authed)])
    async def submit(spec: str = Form(...),
                     images: list[UploadFile] = File(default=[])):
        if len(spec) > 64 * 1024:
            raise HTTPException(422, "spec too large")
        try:
            body = json.loads(spec)
        except ValueError:
            raise HTTPException(422, "spec must be JSON")
        if not isinstance(body, dict):
            raise HTTPException(422, "spec must be an object")
        mode = body.get("mode", "normal")
        if mode not in MODES:
            raise HTTPException(422, f"mode must be one of {MODES}")
        if mode == "test" and os.environ.get("FM3D_TEST") != "1":
            raise HTTPException(403, "test backend disabled")
        if len(images) > MAX_FILES:
            raise HTTPException(422, f"max {MAX_FILES} images")
        # Stream uploads straight to a staging dir in 1 MiB chunks: the
        # server is long-lived, so multi-image batches must never sit
        # fully in RAM (previous code awaited up.read() whole).
        import secrets as _secrets
        mgr: JobManager = app.state.mgr
        stage = mgr.dirs.jobs_dir / f".stage-{_secrets.token_hex(8)}"
        try:
            stage.mkdir(parents=True, exist_ok=True)
            staged: list[tuple[str, Path]] = []
            total = 0
            for up in images:
                fname = Path(up.filename or "input").name or "input"
                if fname in (".", ".."):
                    fname = "input"
                dest = stage / fname
                # Same basename twice: keep both (inputs/ is name-sorted).
                n = 1
                while dest.exists():
                    n += 1
                    dest = stage / f"{dest.stem}-{n}{dest.suffix}"
                size = 0
                with open(dest, "wb") as f:
                    while True:
                        chunk = await up.read(UPLOAD_CHUNK)
                        if not chunk:
                            break
                        size += len(chunk)
                        total += len(chunk)
                        if size > MAX_UPLOAD_BYTES:
                            raise HTTPException(
                                422, f"{fname}: file too large")
                        if total > MAX_UPLOAD_BYTES * MAX_FILES:
                            raise HTTPException(
                                422, "total upload too large")
                        f.write(chunk)
                if size == 0:
                    raise HTTPException(422, f"{fname}: empty file")
                staged.append((dest.name, dest))
        except HTTPException:
            shutil.rmtree(stage, ignore_errors=True)
            raise
        except OSError as e:
            shutil.rmtree(stage, ignore_errors=True)
            raise HTTPException(500, f"cannot store upload: {e}")
        n = len(staged)
        if mode == "normal" and n != 1:
            raise HTTPException(422, "normal mode needs exactly 1 image")
        if mode == "human" and n not in (1, 6):
            raise HTTPException(422, "human mode needs 1 or 6 images")
        if mode == "test" and n > MAX_FILES:
            raise HTTPException(422, f"max {MAX_FILES} images")
        name = str(body.get("name") or "job")[:80]
        seed = body.get("seed")
        try:
            seed = None if seed is None else int(seed)
        except (TypeError, ValueError):
            raise HTTPException(422, "seed must be int")
        try:
            jid = mgr.create_job_streamed(name, mode, body, seed,
                                          staged, stage)
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
        job = app.state.store.get(jid)
        if not job:
            raise HTTPException(404, "not found")
        # Finished history can be deleted (record + files) so GUI and
        # CLI share the same operation. Running/queued go through the
        # cooperative-cancel path.
        if job["state"] in ("done", "failed", "cancelled"):
            shutil.rmtree(app.state.dirs.job_dir(jid), ignore_errors=True)
            app.state.store.delete(jid)
            return {"ok": True, "message": "deleted"}
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
        ids = body.get("ids", []) if isinstance(body, dict) else None
        if not isinstance(ids, list):
            raise HTTPException(422, "ids must be a list")
        if len(ids) > 1000:
            raise HTTPException(422, "too many ids")
        clean = []
        for i in ids:
            s = str(i)
            if len(s) > 128 or "/" in s or ".." in s:
                raise HTTPException(422, f"invalid job id: {s[:32]}")
            clean.append(s)
        app.state.mgr.reorder(clean)
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
            try:
                present = p.is_dir() and any(p.iterdir())
            except OSError:
                present = False
            size = _dir_size(p) if present else 0
            items.append({"id": m["id"], "present": present,
                          "size_gb": round(size / 1024**3, 2),
                          "expected_gb": m["gb"]})
        return {"models": items,
                "runtimes": _runtime_status(app.state.dirs)}

    @app.post("/models/ensure", dependencies=[Depends(authed)])
    def models_ensure(body: dict | None = None):
        """Trigger missing-model download (same script as Setup).

        Body: {"tier": "normal"|"human"|"full"} (default normal).
        Runs fetch_models.py synchronously (small) via subprocess and
        returns per-model ok/pending. GUI and CLI share this endpoint
        so both can perform the identical operation.
        """
        import subprocess as _sp
        import sys as _sys
        tier = "normal"
        if isinstance(body, dict) and body.get("tier"):
            tier = str(body["tier"])
        if tier not in ("normal", "human", "full", "none"):
            raise HTTPException(422, "tier must be normal|human|full|none")
        # Locate fetch_models.py: bundled Resources/scripts or repo scripts.
        cands = [
            Path(__file__).resolve().parents[3] / "scripts" / "fetch_models.py",
            Path(__file__).resolve().parents[2] / "fetch_models.py",
        ]
        script = next((c for c in cands if c.is_file()), None)
        if script is None:
            # Fallback: server/src is <root>/server/src -> scripts at <root>/scripts
            alt = Path(__file__).resolve().parents[3] / "fetch_models.py"
            script = alt if alt.is_file() else None
        if script is None:
            raise HTTPException(500, "fetch_models.py not found in bundle")
        cmd = [_sys.executable, str(script), "--tier", tier,
               "--models-dir", str(app.state.dirs.models_dir),
               "--hf-bin", ""]
        try:
            proc = _sp.run(cmd, capture_output=True, text=True, timeout=3600)
        except _sp.TimeoutExpired:
            raise HTTPException(504, "model download timed out")
        except OSError as e:
            raise HTTPException(500, f"cannot start fetch: {e}")
        # Manifest is the machine-readable result.
        manifest_path = app.state.dirs.models_dir / "manifest.json"
        manifest = {}
        try:
            if manifest_path.is_file():
                manifest = json.loads(manifest_path.read_text())
        except (OSError, ValueError):
            manifest = {}
        return {"ok": proc.returncode == 0, "tier": tier,
                "returncode": proc.returncode,
                "tail": (proc.stdout[-4000:] + proc.stderr[-2000:])[-6000:],
                "manifest": manifest}

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


def _gpu_kind(app) -> str:
    """Report the real accelerator ("mps"/"cpu"/"unknown").

    The server process itself must stay torch-free (isolation
    architecture), so MPS availability is probed inside the trellis venv
    interpreter instead. The probe imports torch (seconds when cold), so
    the result is cached for 10 minutes.
    """
    import subprocess as _sp
    import sys as _sys
    cache = app.state.gpu_probe
    if time.time() - cache["at"] < 600 and cache["value"] != "unknown":
        return cache["value"]
    value = "unknown"
    try:
        exe = app.state.dirs.venvs_dir / "trellis" / "bin" / "python"
        if exe.exists():
            p = _sp.run(
                [str(exe), "-c",
                 "import torch;print(torch.backends.mps.is_available())"],
                capture_output=True, text=True, timeout=60)
            if p.returncode == 0:
                value = "mps" if p.stdout.strip() == "True" else "cpu"
    except (OSError, _sp.SubprocessError):
        pass
    # Fallback: Apple Silicon without a runtime yet is MPS-capable.
    if value == "unknown" and _sys.platform == "darwin":
        import platform as _pl
        if _pl.machine() == "arm64":
            value = "mps"
    cache["at"], cache["value"] = time.time(), value
    return value


def _manifest() -> list[dict]:
    # expected_gb tracks the *slim* layout (fetch allow_patterns +
    # prune_models.py): fp32 safetensors only, no framework duplicates.
    return [
        {"id": "rmbg-2.0", "dir": "rmbg-2.0", "gb": 0.9},
        {"id": "trellis-2-4b", "dir": "trellis-2-4b", "gb": 15},
        {"id": "hunyuan3d-2.1-mlx", "dir": "hunyuan3d-2.1-mlx", "gb": 14},
        {"id": "sdxl-base-1.0", "dir": "sdxl-base-1.0", "gb": 13},
        {"id": "mv-adapter", "dir": "mv-adapter", "gb": 3.5},
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
    # Liveness marker for `3dfm stop` (and for detecting stale servers).
    # Written before serving, removed on every exit path.
    try:
        app.state.dirs.pid_path.write_text(str(os.getpid()),
                                           encoding="utf-8")
    except OSError:
        pass
    try:
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
    finally:
        # Backup for non-signal exits; the lifespan hook above handles
        # SIGTERM/SIGINT (uvicorn re-raises the signal after graceful
        # shutdown, so this line is skipped on signal death).
        _drop_pid_file(app.state.dirs)


if __name__ == "__main__":
    main()
