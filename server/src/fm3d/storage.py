"""Storage-location management for 3DFM (v0.3.0+).

Lets users choose where program files live (data dir + models dir) via
in-app Settings / CLI, without breaking existing installs:

- Compatibility: empty `models_dir` means the historical default
  <data_dir>/models. Old settings.json files keep working.
- Crash safety: every JSON/pointer write is tmp-file + fsync + atomic
  rename (+ dir fsync). Moves verify before deleting the source and
  refuse when jobs are queued/running or the server is live (data move).
- Memory safety: sizes are computed via stat only; moves stream files
  (shutil copy, 1 MiB-ish kernel buffers) and never read model weights
  into Python RAM.

All user-facing messages are bilingual (EN + JA) so JP and EN UIs are
both fully served.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path


_BLOCKED_EXACT = frozenset({
    "/", "/System", "/Library", "/Applications", "/bin", "/sbin",
    "/usr", "/etc", "/var", "/private", "/dev", "/proc",
})


def _bilingual(en: str, ja: str) -> str:
    return f"{en} / {ja}"


def normalize_path(raw: str) -> Path:
    return Path(raw.strip()).expanduser()


def _lexical_ok(p: Path) -> tuple[bool, str]:
    try:
        parts = p.parts
    except (ValueError, RuntimeError):
        return False, _bilingual("not a valid path", "有効なパスではありません")
    if ".." in parts:
        return False, _bilingual("must not contain '..'", "'..'は使えません")
    return True, ""


def _is_blocked_root(normalized: str) -> bool:
    return normalized in _BLOCKED_EXACT


def dir_size_bytes(path: Path) -> int:
    """Sum of file sizes under path (stat only, never reads contents)."""
    total = 0
    try:
        root = Path(path)
        if root.is_file():
            try:
                return root.stat().st_size
            except OSError:
                return 0
        if not root.is_dir():
            return 0
        # os.walk streams directory entries; no full file list in RAM.
        for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
            for name in filenames:
                try:
                    st = os.lstat(os.path.join(dirpath, name))
                    # Skip symlinks themselves (don't follow, don't double-count).
                    if os.path.islink(os.path.join(dirpath, name)):
                        continue
                    total += st.st_size
                except OSError:
                    continue
    except (OSError, RuntimeError):
        pass
    return total


def disk_free_bytes(path: Path) -> int:
    """Free bytes on the volume containing path (nearest existing parent)."""
    try:
        candidate = Path(path).expanduser()
        while not candidate.exists():
            parent = candidate.parent
            if parent == candidate:
                break
            candidate = parent
        return shutil.disk_usage(str(candidate)).free
    except (OSError, RuntimeError, ValueError):
        return -1


def _nearest_existing(path: Path) -> Path:
    candidate = Path(path).expanduser()
    while True:
        try:
            if candidate.exists():
                return candidate
        except (OSError, RuntimeError):
            pass
        parent = candidate.parent
        if parent == candidate:
            return Path("/")
        candidate = parent


def _ensure_writable_dir(path: Path) -> tuple[bool, str]:
    """Create dir (parents) + probe with a temp file. Returns (ok, msg)."""
    try:
        path.mkdir(parents=True, exist_ok=True)
    except (OSError, RuntimeError) as e:
        return False, _bilingual(f"cannot create folder: {e}",
                                 f"フォルダを作成できません: {e}")
    probe = path / ".3dfm-write-test"
    try:
        fd = os.open(str(probe), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, b"ok")
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            probe.unlink()
        except OSError:
            pass
    except (OSError, RuntimeError) as e:
        return False, _bilingual(f"folder is not writable: {e}",
                                 f"フォルダに書き込めません: {e}")
    return True, ""


def _external_volume_mounted(normalized_str: str) -> tuple[bool, str]:
    """Require /Volumes/<name> to be mounted (avoid creating a fake mountpoint).

    Returns (ok, volume_root). When the path is not under /Volumes,
    returns (True, "").
    """
    try:
        parts = Path(normalized_str).parts
    except (ValueError, RuntimeError):
        return True, ""
    if len(parts) >= 3 and parts[0] == "/" and parts[1] == "Volumes":
        vol = os.path.join("/", "Volumes", parts[2])
        try:
            # ismount is True for mounted volumes; a missing volume would
            # otherwise be silently created as a plain folder on the boot
            # disk by mkdir -p (hiding the real mount later).
            if not os.path.ismount(vol):
                # Allow the volume root itself when it exists as a dir?
                # No: without a mount, writing there goes to the boot disk.
                if not os.path.isdir(vol):
                    return False, vol
                # Directory exists but not a mount (e.g. stale mountpoint):
                # still refuse to avoid shadowing.
                return False, vol
        except (OSError, RuntimeError):
            return False, vol
    return True, ""


def validate_models_dir_candidate(raw: str, data_root: Path) -> tuple[bool, str, Path | None]:
    """Validate a new models_dir value (light + filesystem checks).

    Returns (ok, message, normalized). Empty string means "default" and
    is always valid (normalized=None signals default).
    """
    if not isinstance(raw, str):
        return False, _bilingual("models_dir must be a string",
                                 "モデルフォルダは文字列で指定してください"), None
    s = raw.strip()
    if s == "":
        return True, "", None
    if "\x00" in s:
        return False, _bilingual("models_dir must not contain NUL",
                                 "モデルフォルダにNUL文字は使えません"), None
    if len(s) > 1024:
        return False, _bilingual("models_dir is too long (max 1024)",
                                 "モデルフォルダのパスが長すぎます（最大1024文字）"), None
    try:
        expanded = normalize_path(s)
    except (RuntimeError, ValueError):
        return False, _bilingual("models_dir is not a valid path",
                                 "モデルフォルダは有効なパスではありません"), None
    if not expanded.is_absolute():
        return False, _bilingual("models_dir must be an absolute path",
                                 "モデルフォルダは絶対パスで指定してください"), None
    ok, msg = _lexical_ok(expanded)
    if not ok:
        return False, _bilingual(f"models_dir {msg}", f"モデルフォルダ{msg}"), None
    normalized_str = os.path.normpath(str(expanded))
    normalized = Path(normalized_str)
    if _is_blocked_root(normalized_str):
        return False, _bilingual(
            f"models_dir must not be a system folder ({normalized_str})",
            f"システムフォルダには設定できません（{normalized_str}）"), None
    try:
        home = os.path.normpath(str(Path.home()))
        if normalized_str == home:
            return False, _bilingual(
                "models_dir must be a subfolder, not home itself",
                "ホーム直下には設定できません（サブフォルダを指定）"), None
    except (RuntimeError, ValueError):
        pass
    try:
        root_norm = os.path.normpath(str(Path(data_root).expanduser()))
    except (RuntimeError, ValueError):
        root_norm = ""
    # Must not equal the data root itself (would mix DB with weights).
    if normalized_str == root_norm:
        return False, _bilingual(
            "models_dir must not be the data folder itself (choose a subfolder or an outside folder)",
            "データフォルダ自体には設定できません（サブフォルダまたは外部フォルダを選択）"), None
    # Must not collide with reserved data-root children (except models).
    for reserved in ("jobs", "logs", "venvs", "runtimes"):
        if normalized_str == os.path.join(root_norm, reserved) or \
                normalized_str.startswith(os.path.join(root_norm, reserved) + os.sep):
            return False, _bilingual(
                f"models_dir must not overlap the reserved folder '{reserved}'",
                f"予約フォルダ '{reserved}' と重なる場所には設定できません"), None
    ok_vol, vol = _external_volume_mounted(normalized_str)
    if not ok_vol:
        return False, _bilingual(
            f"external volume not mounted: {vol} (connect the drive first)",
            f"外付けボリュームがマウントされていません: {vol}（先に接続してください）"), None
    return True, "", normalized


def validate_data_dir_candidate(raw: str, current_root: Path,
                                current_models: Path) -> tuple[bool, str, Path | None]:
    """Validate a new data_dir value. Empty means default (clears custom)."""
    if not isinstance(raw, str):
        return False, _bilingual("data_dir must be a string",
                                 "データフォルダは文字列で指定してください"), None
    s = raw.strip()
    if s == "":
        # Empty = revert to default. Always structurally valid; the caller
        # decides whether a move is needed.
        return True, "", None
    if "\x00" in s:
        return False, _bilingual("data_dir must not contain NUL",
                                 "データフォルダにNUL文字は使えません"), None
    if len(s) > 1024:
        return False, _bilingual("data_dir is too long (max 1024)",
                                 "データフォルダのパスが長すぎます（最大1024文字）"), None
    try:
        expanded = normalize_path(s)
    except (RuntimeError, ValueError):
        return False, _bilingual("data_dir is not a valid path",
                                 "データフォルダは有効なパスではありません"), None
    if not expanded.is_absolute():
        return False, _bilingual("data_dir must be an absolute path",
                                 "データフォルダは絶対パスで指定してください"), None
    ok, msg = _lexical_ok(expanded)
    if not ok:
        return False, _bilingual(f"data_dir {msg}", f"データフォルダ{msg}"), None
    normalized_str = os.path.normpath(str(expanded))
    normalized = Path(normalized_str)
    if _is_blocked_root(normalized_str):
        return False, _bilingual(
            f"data_dir must not be a system folder ({normalized_str})",
            f"システムフォルダには設定できません（{normalized_str}）"), None
    try:
        home = os.path.normpath(str(Path.home()))
        if normalized_str == home:
            return False, _bilingual(
                "data_dir must be a subfolder, not home itself",
                "ホーム直下には設定できません（サブフォルダを指定）"), None
    except (RuntimeError, ValueError):
        pass
    try:
        cur_root = os.path.normpath(str(Path(current_root).expanduser()))
        cur_models = os.path.normpath(str(Path(current_models).expanduser()))
    except (RuntimeError, ValueError):
        cur_root, cur_models = "", ""
    if normalized_str == cur_root:
        return False, _bilingual(
            "data_dir is already this folder (no change)",
            "すでにこのフォルダです（変更なし）"), None
    # New data root must not live inside the current models dir (would
    # nest the DB inside weights and break moves).
    if cur_models and (normalized_str == cur_models or
                       normalized_str.startswith(cur_models + os.sep)):
        return False, _bilingual(
            "data_dir must not be inside the models folder",
            "モデルフォルダの内側には設定できません"), None
    ok_vol, vol = _external_volume_mounted(normalized_str)
    if not ok_vol:
        return False, _bilingual(
            f"external volume not mounted: {vol} (connect the drive first)",
            f"外付けボリュームがマウントされていません: {vol}（先に接続してください）"), None
    return True, "", normalized


def check_queue_idle(store) -> tuple[bool, str]:
    """Refuse storage moves while work exists (crash-safety gate)."""
    try:
        running = store.running()
        if running is not None:
            return False, _bilingual(
                "stop the running job before moving storage (job is running)",
                "生成中のジョブがあるため移動できません（先に停止してください）")
        nxt = store.next_queued()
        if nxt is not None:
            return False, _bilingual(
                "clear the queue before moving storage (queued jobs exist)",
                "待機中のジョブがあるため移動できません（キューを空にしてください）")
    except Exception as e:
        return False, _bilingual(f"cannot check queue: {e}",
                                 f"キューを確認できません: {e}")
    return True, ""


def is_server_live_for_root(root: Path) -> bool:
    """True when a server pid file points at a live process.

    Used to block offline data-dir moves while the server runs.
    Conservative: unreadable pid file means "not live" (caller still
    requires an idle queue via the API when the server is up).
    """
    try:
        pid_file = Path(root).expanduser() / "server.pid"
        if not pid_file.is_file():
            return False
        raw = pid_file.read_text(encoding="utf-8").strip()
        pid = int(raw)
        if pid <= 0:
            return False
        os.kill(pid, 0)
        return True
    except (OSError, ValueError, UnicodeDecodeError):
        return False
    except Exception:
        return False


def _same_volume(a: Path, b_existing_parent: Path) -> bool:
    try:
        return os.stat(a).st_dev == os.stat(b_existing_parent).st_dev
    except OSError:
        return False


def move_dir_contents_safe(src: Path, dst: Path) -> tuple[bool, str]:
    """Move all entries of src into dst (dst created if missing).

    - Refuses when dst exists and is non-empty with colliding names.
    - Same-volume: atomic os.rename per entry (fast, crash-safe).
    - Cross-volume: copy2 per file + size verify, then delete source
      entry only after its copy verifies. Never loads file contents
      into Python RAM (shutil streams).
    Returns (ok, bilingual message).
    """
    try:
        src = Path(src).expanduser()
        dst = Path(dst).expanduser()
    except (RuntimeError, ValueError) as e:
        return False, _bilingual(f"invalid path: {e}", f"無効なパス: {e}")
    try:
        if not src.is_dir():
            return False, _bilingual(
                f"source folder not found: {src}",
                f"移動元のフォルダがありません: {src}")
        dst.mkdir(parents=True, exist_ok=True)
        if not dst.is_dir():
            return False, _bilingual(
                f"destination is not a folder: {dst}",
                f"移動先がフォルダではありません: {dst}")
        # Resolve to detect same-folder / nesting mistakes.
        try:
            src_res = src.resolve()
            dst_res = dst.resolve()
        except OSError:
            src_res, dst_res = src.absolute(), dst.absolute()
        if src_res == dst_res:
            return False, _bilingual(
                "source and destination are the same folder",
                "移動元と移動先が同じフォルダです")
        # Nesting either way would recurse or orphan data.
        if str(dst_res).startswith(str(src_res) + os.sep) or \
                str(src_res).startswith(str(dst_res) + os.sep):
            # Allow the default case dst == src/models? No: that case is
            # same-folder handled above. Any other nesting is refused.
            # Exception: moving <root>/models contents when dst is a fresh
            # subfolder of src is still dangerous -> refuse.
            return False, _bilingual(
                "nested folders cannot be moved into each other",
                "入れ子フォルダ間の移動はできません")
        entries = list(src.iterdir())
        if not entries:
            return True, _bilingual("nothing to move (source is empty)",
                                    "移動する内容がありません（元が空です）")
        same_vol = _same_volume(src, _nearest_existing(dst))
        for entry in sorted(entries, key=lambda p: p.name):
            target = dst / entry.name
            if target.exists() or target.is_symlink():
                return False, _bilingual(
                    f"destination already has '{entry.name}' (refusing to merge)",
                    f"移動先に '{entry.name}' が既にあります（上書き統合しません）")
        moved: list[Path] = []
        try:
            for entry in sorted(entries, key=lambda p: p.name):
                target = dst / entry.name
                if same_vol:
                    try:
                        os.rename(str(entry), str(target))
                    except OSError:
                        # Fall back to copy path for this entry.
                        if entry.is_dir() and not entry.is_symlink():
                            shutil.copytree(str(entry), str(target),
                                            symlinks=True,
                                            copy_function=shutil.copy2)
                            if dir_size_bytes(target) != dir_size_bytes(entry):
                                raise OSError(f"size mismatch: {entry.name}")
                            shutil.rmtree(str(entry))
                        else:
                            shutil.copy2(str(entry), str(target))
                            try:
                                if target.stat().st_size != entry.stat().st_size:
                                    raise OSError(
                                        f"size mismatch: {entry.name}")
                            except OSError:
                                raise
                            entry.unlink()
                    moved.append(target)
                else:
                    if entry.is_dir() and not entry.is_symlink():
                        shutil.copytree(str(entry), str(target),
                                        symlinks=True,
                                        copy_function=shutil.copy2)
                        if dir_size_bytes(target) != dir_size_bytes(entry):
                            raise OSError(f"size mismatch: {entry.name}")
                        shutil.rmtree(str(entry))
                    elif entry.is_symlink():
                        # Recreate symlink as-is (never follow).
                        try:
                            linkto = os.readlink(str(entry))
                            os.symlink(linkto, str(target))
                        except OSError as e:
                            raise OSError(f"symlink copy failed: {e}")
                        entry.unlink()
                    else:
                        shutil.copy2(str(entry), str(target))
                        try:
                            if target.stat().st_size != entry.stat().st_size:
                                raise OSError(
                                    f"size mismatch: {entry.name}")
                        except OSError:
                            raise
                        entry.unlink()
                    moved.append(target)
        except (OSError, RuntimeError) as e:
            return False, _bilingual(
                f"move stopped after {len(moved)} item(s): {e}. "
                "Already-moved items stay at the destination; re-run to continue.",
                f"{len(moved)}件の移動後に中断: {e}。"
                "移動済みは移動先に残ります。再実行で続行できます。")
        return True, _bilingual(f"moved {len(moved)} item(s)",
                                f"{len(moved)}件を移動しました")
    except (OSError, RuntimeError) as e:
        return False, _bilingual(f"move failed: {e}", f"移動に失敗: {e}")
    except Exception as e:
        return False, _bilingual(f"move failed: {e}", f"移動に失敗: {e}")


def storage_status(data_root: Path, models_dir: Path,
                   output_dir: str) -> dict:
    """Point-in-time storage report (stat only, no file reads)."""
    try:
        data_root = Path(data_root).expanduser()
    except Exception:
        data_root = Path.home()
    try:
        models_dir = Path(models_dir).expanduser()
    except Exception:
        models_dir = data_root / "models"
    try:
        out = str(output_dir)
    except Exception:
        out = ""
    data_size = dir_size_bytes(data_root) if data_root.exists() else 0
    models_size = dir_size_bytes(models_dir) if models_dir.exists() else 0
    # When models live inside data root, report non-models data separately
    # so the UI can show "app data" vs "models" without double counting.
    try:
        inside = str(models_dir.resolve()).startswith(
            str(data_root.resolve()) + os.sep)
    except (OSError, RuntimeError):
        inside = False
    free = disk_free_bytes(models_dir if models_dir.exists() else data_root)
    free_data = disk_free_bytes(data_root)
    return {
        "data_dir": str(data_root),
        "models_dir": str(models_dir),
        "output_dir": out,
        "models_inside_data": bool(inside),
        "data_size_bytes": int(data_size),
        "models_size_bytes": int(models_size),
        "data_size_gb": round(data_size / 1024**3, 2),
        "models_size_gb": round(models_size / 1024**3, 2),
        "free_bytes": int(free),
        "free_gb": round(free / 1024**3, 2) if free >= 0 else -1,
        "data_free_bytes": int(free_data),
        "data_free_gb": round(free_data / 1024**3, 2) if free_data >= 0 else -1,
    }
