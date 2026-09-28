"""Server settings with validation. Stored as JSON, written atomically."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    "output_dir": str(Path.home() / "Pictures" / "3DFM"),
    "port": 44931,
    "pipeline_default": "512->1024",
    "texture_default": 2048,
    "notify": True,
    "retention_days": 0,          # 0 = keep forever
    "mem_min_free_gb": 12.0,      # preflight gate per job class (see manager)
    "mem_cap_gb": 40.0,           # watchdog kill threshold (RSS, adaptive)
    "stall_timeout_s": 1800,      # no-progress watchdog
    "cancel_grace_s": 10.0,
    "idle_gc_s": 60.0,            # idle queue -> server GC interval (0=off)
}

_TEXTURE_CHOICES = (1024, 2048, 4096)
_PIPELINE_CHOICES = ("512", "512->1024", "512->1536")


def load(path: Path) -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    try:
        if path.exists():
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                cfg.update({k: v for k, v in data.items() if k in DEFAULTS})
    except (OSError, ValueError):
        pass  # corrupt settings never crash the server; defaults win
    # environment overrides (headless / CI)
    if os.environ.get("FM3D_PORT"):
        try:
            cfg["port"] = int(os.environ["FM3D_PORT"])
        except ValueError:
            pass
    return cfg


def validate(patch: dict[str, Any]) -> tuple[bool, str]:
    if not isinstance(patch, dict):
        return False, "patch must be an object"
    try:
        if "port" in patch and not (1024 <= int(patch["port"]) <= 65535):
            return False, "port must be 1024-65535"
        if "texture_default" in patch and int(patch["texture_default"]) not in _TEXTURE_CHOICES:
            return False, "texture_default must be one of 1024/2048/4096"
        if "pipeline_default" in patch and patch["pipeline_default"] not in _PIPELINE_CHOICES:
            return False, "pipeline_default must be 512, 512->1024 or 512->1536"
        for k in ("mem_min_free_gb", "mem_cap_gb"):
            if k in patch and float(patch[k]) <= 0:
                return False, f"{k} must be positive"
        for k in ("stall_timeout_s", "cancel_grace_s", "idle_gc_s"):
            if k in patch and float(patch[k]) < 0:
                return False, f"{k} must be >= 0"
        if "retention_days" in patch and float(patch["retention_days"]) < 0:
            return False, "retention_days must be >= 0"
        if "notify" in patch and not isinstance(patch["notify"], bool):
            return False, "notify must be boolean"
    except (TypeError, ValueError):
        return False, "invalid value type"
    if "output_dir" in patch and not str(patch["output_dir"]).strip():
        return False, "output_dir must not be empty"
    unknown = set(patch) - set(DEFAULTS)
    if unknown:
        return False, f"unknown keys: {sorted(unknown)}"
    return True, ""


def save(path: Path, cfg: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".settings-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
