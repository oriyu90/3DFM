"""Server settings with validation. Stored as JSON, written atomically."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    "output_dir": str(Path.home() / "Pictures" / "3DFM"),
    "models_dir": "",               # "" = default <data_dir>/models
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

# Paths that must never become a models/output dir (system roots).
# Compared after expanduser + lexical normalization; subpaths like
# /Users/<name>/... remain allowed.
_BLOCKED_STORAGE_ROOTS = frozenset({
    "/", "/System", "/Library", "/Applications", "/bin", "/sbin",
    "/usr", "/etc", "/var", "/private", "/dev", "/proc",
})


def _validate_storage_path(value: Any, field: str) -> tuple[bool, str]:
    """Light validation for models_dir (heavy checks live in storage.py).

    Empty string means "default" and is always allowed (backward
    compatible with pre-v0.3.0 settings.json that lacks the key).
    Bilingual message (EN + JA) so both UI languages are fully served.
    """
    if not isinstance(value, str):
        return False, (f"{field} must be a string / "
                       f"{field}は文字列で指定してください")
    s = value.strip()
    if s == "":
        return True, ""
    if "\x00" in s:
        return False, (f"{field} must not contain NUL / "
                       f"{field}にNUL文字は使えません")
    if len(s) > 1024:
        return False, (f"{field} is too long (max 1024) / "
                       f"{field}が長すぎます（最大1024文字）")
    try:
        expanded = str(Path(s).expanduser())
    except (RuntimeError, ValueError):
        return False, (f"{field} is not a valid path / "
                       f"{field}は有効なパスではありません")
    if not os.path.isabs(expanded):
        return False, (f"{field} must be an absolute path / "
                       f"{field}は絶対パスで指定してください")
    # Lexical normalization without touching the filesystem (target may
    # not exist yet). Reject ".." to avoid traversal confusion.
    try:
        parts = Path(expanded).parts
    except (ValueError, RuntimeError):
        return False, (f"{field} is not a valid path / "
                       f"{field}は有効なパスではありません")
    if ".." in parts:
        return False, (f"{field} must not contain '..' / "
                       f"{field}に'..'は使えません")
    normalized = os.path.normpath(expanded)
    if normalized in _BLOCKED_STORAGE_ROOTS:
        return False, (f"{field} must not be a system folder ({normalized}) / "
                       f"システムフォルダには設定できません（{normalized}）")
    # External volumes must be mounted; otherwise mkdir -p would create a
    # fake mountpoint on the boot disk and hide the real volume later.
    try:
        _parts = Path(normalized).parts
        if len(_parts) >= 3 and _parts[0] == "/" and _parts[1] == "Volumes":
            _vol = os.path.join("/", "Volumes", _parts[2])
            if not os.path.ismount(_vol):
                return False, (f"{field}: external volume not mounted: {_vol} "
                               f"(connect the drive first) / "
                               f"{field}: 外付けボリュームがマウントされていません: {_vol}（先に接続してください）")
    except (OSError, RuntimeError, ValueError):
        pass
    # Home itself would scatter model files across ~; require a subdir.
    try:
        home = str(Path.home())
        if normalized == home or normalized == os.path.normpath(home):
            return False, (f"{field} must be a subfolder, not home itself / "
                           f"ホーム直下には設定できません（サブフォルダを指定）")
    except (RuntimeError, ValueError):
        pass
    return True, ""


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


def _bilingual(en: str, ja: str) -> str:
    return f"{en} / {ja}"


def validate(patch: dict[str, Any]) -> tuple[bool, str]:
    if not isinstance(patch, dict):
        return False, _bilingual("patch must be an object",
                                 "patchはオブジェクトで指定してください")
    try:
        if "port" in patch and not (1024 <= int(patch["port"]) <= 65535):
            return False, _bilingual("port must be 1024-65535",
                                     "ポートは1024〜65535で指定してください")
        if "texture_default" in patch and int(patch["texture_default"]) not in _TEXTURE_CHOICES:
            return False, _bilingual(
                "texture_default must be one of 1024/2048/4096",
                "既定テクスチャは1024/2048/4096のいずれかで指定してください")
        if "pipeline_default" in patch and patch["pipeline_default"] not in _PIPELINE_CHOICES:
            return False, _bilingual(
                "pipeline_default must be 512, 512->1024 or 512->1536",
                "既定パイプラインは512 / 512->1024 / 512->1536のいずれかで指定してください")
        for k in ("mem_min_free_gb", "mem_cap_gb"):
            if k in patch and float(patch[k]) <= 0:
                return False, _bilingual(f"{k} must be positive",
                                         f"{k}は正の値で指定してください")
        for k in ("stall_timeout_s", "cancel_grace_s", "idle_gc_s"):
            if k in patch and float(patch[k]) < 0:
                return False, _bilingual(f"{k} must be >= 0",
                                         f"{k}は0以上で指定してください")
        if "retention_days" in patch and float(patch["retention_days"]) < 0:
            return False, _bilingual("retention_days must be >= 0",
                                     "履歴保持は0日以上で指定してください")
        if "notify" in patch and not isinstance(patch["notify"], bool):
            return False, _bilingual("notify must be boolean",
                                     "通知はtrue/falseで指定してください")
    except (TypeError, ValueError):
        return False, _bilingual("invalid value type",
                                 "値の型が正しくありません")
    if "output_dir" in patch and not str(patch["output_dir"]).strip():
        return False, _bilingual("output_dir must not be empty",
                                 "出力フォルダは空にできません")
    if "models_dir" in patch:
        ok, msg = _validate_storage_path(patch["models_dir"], "models_dir")
        if not ok:
            return False, msg
    unknown = set(patch) - set(DEFAULTS)
    if unknown:
        return False, _bilingual(f"unknown keys: {sorted(unknown)}",
                                 f"不明なキー: {sorted(unknown)}")
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
