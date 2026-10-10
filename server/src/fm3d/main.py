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
from .paths import DataDirs, build_data_dirs, resolve_data_dir
from .settings import load as load_settings
from .settings import save as save_settings
from .settings import validate as validate_settings

MAX_UPLOAD_BYTES = 200 * 1024 * 1024
MAX_FILES = 8
MODES = ("normal", "human", "test")
# Uploads are streamed in chunks so a batch of large images never sits
# fully in the long-lived server process (see submit()).
UPLOAD_CHUNK = 1024 * 1024

# 32GB+ memory gates for heavy options (see _validate_job_spec).
# Values in bytes; total unknown (-1) means fail-open (allow).
_MIN_32GB = 32 * 1024**3
_MIN_48GB = 48 * 1024**3
_TEXTURE_CHOICES = (1024, 2048, 4096)
_PIPELINE_CHOICES = ("512", "512->1024", "512->1536")


def _bi(en: str, ja: str) -> str:
    return f"{en} / {ja}"


def _validate_job_spec(body: dict) -> tuple[bool, str]:
    """Validate per-job spec before accepting (memory safety, 32GB+).

    Returns (ok, bilingual_message). Unknown total memory fails open.
    Heavy combos are rejected with 422 (not SIGKILL later) so a 32GB
    Mac never OOMs from a single tap in GUI/CLI.
    """
    try:
        total = memguard.total_bytes()
    except Exception:
        total = -1
    # texture_size
    if "texture_size" in body:
        try:
            tex = int(body["texture_size"])
        except (TypeError, ValueError):
            return False, _bi("texture_size must be 1024/2048/4096",
                               "テクスチャは1024/2048/4096で指定してください")
        if tex not in _TEXTURE_CHOICES:
            return False, _bi(
                "texture_size must be one of 1024/2048/4096",
                "テクスチャは1024/2048/4096のいずれかで指定してください")
        if total > 0 and total < _MIN_32GB and tex == 4096:
            return False, _bi(
                f"texture 4096 needs 32GB+ memory (this Mac: "
                f"{total/1024**3:.0f}GB); use 2048 or lower",
                f"テクスチャ4096は32GB以上のメモリが必要です"
                f"（このMac: {total/1024**3:.0f}GB）。2048以下を使用してください")
    # pipeline_type
    if "pipeline_type" in body:
        pt = str(body.get("pipeline_type", ""))
        if pt not in _PIPELINE_CHOICES:
            return False, _bi(
                "pipeline_type must be 512, 512->1024 or 512->1536",
                "パイプラインは512 / 512->1024 / 512->1536のいずれかで指定してください")
        if total > 0 and total < _MIN_32GB and pt == "512->1536":
            return False, _bi(
                f"pipeline 512->1536 needs 32GB+ memory (this Mac: "
                f"{total/1024**3:.0f}GB); use 512->1024 or lower",
                f"パイプライン512->1536は32GB以上のメモリが必要です"
                f"（このMac: {total/1024**3:.0f}GB）。512->1024以下を使用してください")
    # Heavy combo on 32GB-class: 1536 + 4096 needs 48GB+.
    try:
        _tex = int(body.get("texture_size", 2048))
        _pt = str(body.get("pipeline_type", "512->1024"))
    except (TypeError, ValueError):
        _tex, _pt = 2048, "512->1024"
    if (total > 0 and total < _MIN_48GB and _tex == 4096
            and _pt == "512->1536"):
        return False, _bi(
            "pipeline 512->1536 + texture 4096 needs 48GB+ memory; "
            "lower one of them (e.g. 512->1024 + 2048)",
            "パイプライン512->1536とテクスチャ4096の併用は48GB以上のメモリが"
            "必要です。どちらかを下げてください（例: 512->1024 + 2048）")
    # rembg_threshold
    if "rembg_threshold" in body and body["rembg_threshold"] is not None:
        try:
            rt = float(body["rembg_threshold"])
        except (TypeError, ValueError):
            return False, _bi("rembg_threshold must be 0.0-1.0",
                               "背景除去しきい値は0.0〜1.0で指定してください")
        if not (0.0 <= rt <= 1.0):
            return False, _bi("rembg_threshold must be 0.0-1.0",
                               "背景除去しきい値は0.0〜1.0で指定してください")
    # steps / mv_steps
    for _k in ("steps", "mv_steps"):
        if _k in body and body[_k] is not None:
            try:
                _v = int(body[_k])
            except (TypeError, ValueError):
                return False, _bi(f"{_k} must be a positive int",
                                   f"{_k}は正の整数で指定してください")
            if not (1 <= _v <= 200):
                return False, _bi(f"{_k} must be 1-200",
                                   f"{_k}は1〜200で指定してください")
    # mv_resolution
    if "mv_resolution" in body and body["mv_resolution"] is not None:
        try:
            _r = int(body["mv_resolution"])
        except (TypeError, ValueError):
            return False, _bi("mv_resolution must be 512 or 768",
                               "MV解像度は512または768で指定してください")
        if _r not in (512, 768):
            # Allow 256-1024 range but gate 768 on memory below.
            if not (256 <= _r <= 1024):
                return False, _bi("mv_resolution must be 256-1024",
                                   "MV解像度は256〜1024で指定してください")
        if total > 0 and total < _MIN_48GB and _r == 768:
            return False, _bi(
                f"mv_resolution 768 needs 48GB+ memory (this Mac: "
                f"{total/1024**3:.0f}GB); use 512",
                f"MV解像度768は48GB以上のメモリが必要です"
                f"（このMac: {total/1024**3:.0f}GB）。512を使用してください")
    # decimation_target
    if "decimation_target" in body and body["decimation_target"] is not None:
        try:
            _d = int(body["decimation_target"])
        except (TypeError, ValueError):
            return False, _bi("decimation_target must be a positive int",
                               "decimation_targetは正の整数で指定してください")
        if _d <= 0 or _d > 2000000:
            return False, _bi("decimation_target must be 1-2000000",
                               "decimation_targetは1〜2000000で指定してください")
    return True, ""


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
    data_root = resolve_data_dir().expanduser()
    # Load settings first so a saved models_dir override takes effect.
    # build_data_dirs honors FM3D_MODELS_DIR env > settings value.
    tmp_settings_path = DataDirs(data_root).settings_path
    settings = load_settings(tmp_settings_path)
    dirs = build_data_dirs(
        data_root, str(settings.get("models_dir", "") or "")).ensure()
    # If settings lacked models_dir (pre-v0.3.0), keep it default-empty
    # so old installs behave identically.
    if "models_dir" not in settings:
        settings["models_dir"] = ""
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
            raise HTTPException(
                401, _bi("missing bearer token",
                         "Bearerトークンがありません"))
        if not secrets.compare_digest(authorization[7:], app.state.token):
            raise HTTPException(
                403, _bi("bad token", "トークンが正しくありません"))

    @app.get("/health")
    def health():
        return {"status": "ok", "version": _server_version,
                "gpu": _gpu_kind(app),
                "port": _current_port(app),
                "mem_total_gb": round((memguard.total_bytes() or 0) / 1024**3, 1),
                "mem_free_gb": round((memguard.free_bytes() or 0) / 1024**3, 1)}

    @app.post("/jobs", dependencies=[Depends(authed)])
    async def submit(spec: str = Form(...),
                     images: list[UploadFile] = File(default=[])):
        if len(spec) > 64 * 1024:
            raise HTTPException(
                422, _bi("spec too large (max 64KB)",
                         "specが大きすぎます（最大64KB）"))
        try:
            body = json.loads(spec)
        except ValueError:
            raise HTTPException(
                422, _bi("spec must be JSON", "specはJSONで指定してください"))
        if not isinstance(body, dict):
            raise HTTPException(
                422, _bi("spec must be an object",
                         "specはオブジェクトで指定してください"))
        mode = body.get("mode", "normal")
        if mode not in MODES:
            raise HTTPException(
                422, _bi(f"mode must be one of {MODES}",
                         f"modeは{','.join(MODES)}のいずれかで指定してください"))
        if mode == "test" and os.environ.get("FM3D_TEST") != "1":
            raise HTTPException(
                403, _bi("test backend disabled",
                         "テストバックエンドは無効です"))
        if len(images) > MAX_FILES:
            raise HTTPException(
                422, _bi(f"max {MAX_FILES} images",
                         f"画像は最大{MAX_FILES}枚までです"))
        # Memory-safe spec validation (32GB+ gates, bilingual).
        _ok, _msg = _validate_job_spec(body)
        if not _ok:
            raise HTTPException(422, _msg)
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
                                422, _bi(f"{fname}: file too large (max 200MB)",
                                         f"{fname}: ファイルが大きすぎます（最大200MB）"))
                        if total > MAX_UPLOAD_BYTES * MAX_FILES:
                            raise HTTPException(
                                422, _bi("total upload too large",
                                         "アップロード合計が大きすぎます"))
                        f.write(chunk)
                if size == 0:
                    raise HTTPException(
                        422, _bi(f"{fname}: empty file",
                                 f"{fname}: 空ファイルです"))
                staged.append((dest.name, dest))
        except HTTPException:
            shutil.rmtree(stage, ignore_errors=True)
            raise
        except OSError as e:
            shutil.rmtree(stage, ignore_errors=True)
            raise HTTPException(
                500, _bi(f"cannot store upload: {e}",
                         f"アップロードを保存できません: {e}"))
        n = len(staged)
        if mode == "normal" and n != 1:
            raise HTTPException(
                422, _bi("normal mode needs exactly 1 image",
                         "普通モードは画像1枚が必要です"))
        if mode == "human" and n not in (1, 6):
            raise HTTPException(
                422, _bi("human mode needs 1 or 6 images",
                         "人物モードは1枚または6枚の画像が必要です"))
        if mode == "test" and n > MAX_FILES:
            raise HTTPException(
                422, _bi(f"max {MAX_FILES} images",
                         f"画像は最大{MAX_FILES}枚までです"))
        name = str(body.get("name") or "job")[:80]
        seed = body.get("seed")
        try:
            seed = None if seed is None else int(seed)
        except (TypeError, ValueError):
            raise HTTPException(
                422, _bi("seed must be int",
                         "seedは整数で指定してください"))
        try:
            jid = mgr.create_job_streamed(name, mode, body, seed,
                                          staged, stage)
        except OSError as e:
            raise HTTPException(
                500, _bi(f"cannot store job: {e}",
                         f"ジョブを保存できません: {e}"))
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
            raise HTTPException(
                422, _bi("ids must be a list",
                         "idsはリストで指定してください"))
        if len(ids) > 1000:
            raise HTTPException(
                422, _bi("too many ids (max 1000)",
                         "idsが多すぎます（最大1000）"))
        clean = []
        for i in ids:
            s = str(i)
            if len(s) > 128 or "/" in s or ".." in s:
                raise HTTPException(
                    422, _bi(f"invalid job id: {s[:32]}",
                             f"無効なジョブID: {s[:32]}"))
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
            raise HTTPException(
                422, _bi("tier must be normal|human|full|none",
                         "tierはnormal/human/full/noneで指定してください"))
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
            raise HTTPException(
                504, _bi("model download timed out",
                         "モデルダウンロードがタイムアウトしました"))
        except OSError as e:
            raise HTTPException(
                500, _bi(f"cannot start fetch: {e}",
                         f"ダウンロードを開始できません: {e}"))
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
        # Crash safety: switching the models pointer while jobs are
        # queued/running would split weights mid-pipeline. Require idle
        # like POST /storage/models/move (409 when busy).
        if "models_dir" in patch:
            from . import storage as _storage_gate
            idle_ok, idle_msg = _storage_gate.check_queue_idle(
                app.state.store)
            if not idle_ok:
                raise HTTPException(409, idle_msg)
        app.state.settings.update(patch)
        # When models_dir changes via plain settings (pointer-only, no
        # file move), refresh the resolved dirs so subsequent
        # /models/status and workers use the new location immediately.
        # The caller is responsible for moving files or re-running
        # Setup / models ensure; missing weights surface as usual.
        if "models_dir" in patch:
            from .paths import DataDirs as _DataDirs
            try:
                raw_value = str(app.state.settings.get("models_dir", "") or "")
                override = (Path(raw_value).expanduser()
                            if raw_value.strip() else None)
                new_dirs = _DataDirs(app.state.dirs.root, override)
                new_dirs.ensure()
                app.state.dirs = new_dirs
                try:
                    app.state.mgr.dirs = new_dirs
                except (AttributeError, TypeError):
                    pass
                # Keep process env in sync so DataDirs (env-first when
                # override is None) and future workers agree.
                if override is None:
                    os.environ.pop("FM3D_MODELS_DIR", None)
                else:
                    os.environ["FM3D_MODELS_DIR"] = str(new_dirs.models_dir)
            except (OSError, RuntimeError) as e:
                raise HTTPException(
                    500, _bi(f"cannot prepare models folder: {e}",
                             f"モデルフォルダを準備できません: {e}"))
        try:
            save_settings(app.state.dirs.settings_path, app.state.settings)
        except OSError as e:
            raise HTTPException(
                500, _bi(f"cannot save settings: {e}",
                         f"設定を保存できません: {e}"))
        return app.state.settings

    @app.get("/storage", dependencies=[Depends(authed)])
    def storage_status():
        """Where program files live + usage. GUI and CLI share this."""
        from . import storage as _storage
        from .paths import default_data_dir as _default_root
        dirs = app.state.dirs
        info = _storage.storage_status(
            dirs.root, dirs.models_dir,
            str(app.state.settings.get("output_dir", "")))
        try:
            default_root = str(_default_root())
        except Exception:
            default_root = ""
        try:
            default_models = str(Path(default_root) / "models") \
                if default_root else ""
        except (OSError, RuntimeError):
            default_models = ""
        info["default_data_dir"] = default_root
        info["default_models_dir"] = default_models
        info["is_default_data"] = (
            os.path.normpath(info["data_dir"]) == os.path.normpath(default_root)
            if default_root else True)
        # is_default_models: empty setting + default location, or env unset
        # and path equals <data>/models. Compute lexically (no resolve).
        try:
            configured = str(app.state.settings.get("models_dir", "") or "")
            info["configured_models_dir"] = configured
            info["is_default_models"] = (
                configured.strip() == "" and
                os.path.normpath(info["models_dir"]) ==
                os.path.normpath(os.path.join(info["data_dir"], "models")))
        except (OSError, RuntimeError, ValueError):
            info["configured_models_dir"] = ""
            info["is_default_models"] = True
        return info

    @app.post("/storage/data/validate", dependencies=[Depends(authed)])
    def storage_data_validate(body: dict | None = None):
        """Pre-check a data-dir candidate (no changes)."""
        from . import storage as _storage
        path = ""
        if isinstance(body, dict):
            path = str(body.get("path", "") or "")
        ok, msg, normalized = _storage.validate_data_dir_candidate(
            path, app.state.dirs.root, app.state.dirs.models_dir)
        if not ok:
            # Empty path means "revert to default": report default plan.
            if path.strip() == "":
                from .paths import default_data_dir as _default_root
                try:
                    default_root = _default_root()
                except Exception:
                    raise HTTPException(500, "cannot resolve default")
                return {"ok": True, "path": str(default_root),
                        "mode": "revert-to-default",
                        "note": "Restart the app to apply / "
                                "適用にはアプリの再起動が必要です"}
            raise HTTPException(422, msg)
        assert normalized is not None
        # Space + writability pre-check (no filesystem changes yet except
        # probing the destination parent for writability is avoided here;
        # the offline mover re-checks with a write probe).
        free = _storage.disk_free_bytes(normalized)
        need = _storage.dir_size_bytes(app.state.dirs.root)
        return {"ok": True, "path": str(normalized),
                "mode": "move-offline",
                "data_size_bytes": int(need),
                "free_bytes": int(free),
                "note": "Stop the server, move offline, then restart / "
                        "サーバー停止後にオフライン移動し再起動してください"}

    @app.post("/storage/models/move", dependencies=[Depends(authed)])
    def storage_models_move(body: dict | None = None):
        """Move model weights to a new folder (or switch pointer).

        Body: {"path": "<absolute>", "move_files": true}
        - move_files=true (default): validate, require idle queue,
          move <current>/... contents to the new folder, then switch
          settings pointer atomically. Crash-safe: source entries are
          deleted only after their copy verifies.
        - move_files=false: pointer-only switch (no file move). Use when
          the folder was moved manually or a fresh empty folder + later
          `models ensure` re-download is intended.
        - path="" : revert to default <data_dir>/models (pointer-only).
        """
        from . import storage as _storage
        from .paths import DataDirs as _DataDirs
        path = ""
        move_files = True
        if isinstance(body, dict):
            path = str(body.get("path", "") or "")
            if "move_files" in body:
                move_files = bool(body.get("move_files"))
        idle_ok, idle_msg = _storage.check_queue_idle(app.state.store)
        if not idle_ok:
            raise HTTPException(409, idle_msg)
        ok, msg, normalized = _storage.validate_models_dir_candidate(
            path, app.state.dirs.root)
        if not ok:
            raise HTTPException(422, msg)
        old_models = app.state.dirs.models_dir
        if normalized is None:
            # Revert to default.
            app.state.settings["models_dir"] = ""
            try:
                new_dirs = _DataDirs(app.state.dirs.root, None)
                new_dirs.ensure()
                app.state.dirs = new_dirs
                try:
                    app.state.mgr.dirs = new_dirs
                except (AttributeError, TypeError):
                    pass
                os.environ.pop("FM3D_MODELS_DIR", None)
                save_settings(app.state.dirs.settings_path,
                              app.state.settings)
            except OSError as e:
                raise HTTPException(
                    500, _bi(f"cannot save settings: {e}",
                             f"設定を保存できません: {e}"))
            return {"ok": True, "models_dir": str(new_dirs.models_dir),
                    "mode": "reverted-to-default"}
        assert normalized is not None
        try:
            if os.path.normpath(str(old_models)) == os.path.normpath(str(normalized)):
                return {"ok": True, "models_dir": str(old_models),
                        "mode": "no-change"}
        except (OSError, RuntimeError, ValueError):
            pass
        if not move_files:
            ok_w, msg_w = _storage._ensure_writable_dir(normalized)
            if not ok_w:
                raise HTTPException(422, msg_w)
            app.state.settings["models_dir"] = str(normalized)
            try:
                new_dirs = _DataDirs(app.state.dirs.root, normalized)
                new_dirs.ensure()
                app.state.dirs = new_dirs
                try:
                    app.state.mgr.dirs = new_dirs
                except (AttributeError, TypeError):
                    pass
                os.environ["FM3D_MODELS_DIR"] = str(normalized)
                save_settings(app.state.dirs.settings_path,
                              app.state.settings)
            except OSError as e:
                raise HTTPException(
                    500, _bi(f"cannot save settings: {e}",
                             f"設定を保存できません: {e}"))
            return {"ok": True, "models_dir": str(normalized),
                    "mode": "pointer-only"}
        # move_files=True: space pre-check, then move contents.
        need = _storage.dir_size_bytes(old_models) \
            if old_models.is_dir() else 0
        free = _storage.disk_free_bytes(normalized)
        if free >= 0 and need > 0 and free < need:
            raise HTTPException(
                422,
                f"not enough free space (need {need/1024**3:.1f} GB, "
                f"free {free/1024**3:.1f} GB) / "
                f"空き容量不足（必要 {need/1024**3:.1f} GB、"
                f"空き {free/1024**3:.1f} GB）")
        ok_w, msg_w = _storage._ensure_writable_dir(normalized)
        if not ok_w:
            raise HTTPException(422, msg_w)
        if old_models.is_dir():
            ok_m, msg_m = _storage.move_dir_contents_safe(old_models,
                                                          normalized)
            if not ok_m:
                raise HTTPException(500, msg_m)
        app.state.settings["models_dir"] = str(normalized)
        try:
            new_dirs = _DataDirs(app.state.dirs.root, normalized)
            new_dirs.ensure()
            app.state.dirs = new_dirs
            try:
                app.state.mgr.dirs = new_dirs
            except (AttributeError, TypeError):
                pass
            save_settings(app.state.dirs.settings_path,
                          app.state.settings)
        except OSError as e:
            raise HTTPException(
                500,
                f"files moved but cannot save settings: {e} / "
                f"ファイルは移動済みですが設定を保存できません: {e}")
        # Propagate to subsequently spawned workers via env as well so
        # already-computed absolute paths agree immediately.
        os.environ["FM3D_MODELS_DIR"] = str(normalized)
        return {"ok": True, "models_dir": str(normalized),
                "mode": "moved"}

    return app


def _current_port(app) -> int:
    """Best-effort current port (for /health observability)."""
    try:
        return int(app.state.settings.get("port", 44931))
    except (TypeError, ValueError):
        return 44931


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
            actual = port + attempt
            # Observability: record the actual port so GUI/CLI/logs can
            # show which port won the +1 scan (previously invisible).
            try:
                (app.state.dirs.root / "server.port").write_text(
                    str(actual), encoding="utf-8")
            except OSError:
                pass
            try:
                app.state.settings["port_actual"] = actual
            except Exception:
                pass
            try:
                uvicorn.run(app, host="127.0.0.1", port=actual,
                            log_level="warning", access_log=False)
                return
            except OSError as e:
                if "address" in str(e).lower() or "in use" in str(e).lower():
                    print(f"port {actual} busy, trying next")
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
