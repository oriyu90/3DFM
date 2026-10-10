"""Filesystem layout for 3DFM runtime data.

All state lives under a single data dir so crash recovery is trivial:
restart the server and it re-reads jobs.db + job dirs. Nothing lives
only in RAM.

Storage configurability (v0.3.0+):

- Data dir (program data root: jobs.db, jobs/, logs/, venvs/, runtimes/,
  settings.json): default ~/Library/Application Support/3DFM.
  Override precedence: FM3D_DATA_DIR env > macOS UserDefaults
  (local.3dfm.app / customDataDir) > pointer file
  (~/Library/Application Support/3DFM.location) > default.
  The Swift app persists the choice in UserDefaults + pointer file;
  the CLI/server resolve it without the GUI running.

- Models dir (AI weights, ~16-60GB): default <data_dir>/models.
  Override precedence: FM3D_MODELS_DIR env > settings.json `models_dir`
  (empty = default) > default. The server propagates the effective
  value to workers via FM3D_MODELS_DIR so per-job processes agree.

- Output dir (generated GLB): settings.json `output_dir` (pre-existing).

venvs/ and runtimes/ always stay under the data dir: venv interpreters
embed absolute paths, so relocating them alone would break Python.
Move the whole data dir instead (documented in Settings).
"""
from __future__ import annotations

import os
import plistlib
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


def default_data_dir() -> Path:
    return Path.home() / "Library" / "Application Support" / "3DFM"


def _user_defaults_plist() -> Path:
    return Path.home() / "Library" / "Preferences" / "local.3dfm.app.plist"


def custom_data_dir_pointer_file() -> Path:
    if sys.platform == "darwin":
        return (Path.home() / "Library" / "Application Support"
                / "3DFM.location")
    return Path.home() / ".config" / "3DFM" / "data_dir.txt"


def read_custom_data_dir_from_plist() -> Optional[Path]:
    """Read Swift @AppStorage("customDataDir") without launching the app.

    Returns None when unset/unreadable/invalid. Never raises.
    """
    try:
        plist = _user_defaults_plist()
        if not plist.is_file():
            return None
        with open(plist, "rb") as f:
            data = plistlib.load(f)
        raw = data.get("customDataDir", "")
        if not isinstance(raw, str) or not raw.strip():
            return None
        return Path(raw.strip()).expanduser()
    except (OSError, ValueError, plistlib.InvalidFileException):
        return None
    except Exception:
        return None


def read_custom_data_dir_from_file() -> Optional[Path]:
    """Read the plain-text pointer file (CLI/GUI shared fallback)."""
    try:
        pointer = custom_data_dir_pointer_file()
        if not pointer.is_file():
            return None
        raw = pointer.read_text(encoding="utf-8").strip()
        if not raw:
            return None
        # First line only; ignore trailing comments/newlines.
        first = raw.splitlines()[0].strip()
        if not first:
            return None
        return Path(first).expanduser()
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    except Exception:
        return None


def resolve_data_dir() -> Path:
    """Effective program-data root. Never raises; defaults win."""
    try:
        override = os.environ.get("FM3D_DATA_DIR")
        if override and override.strip():
            return Path(override.strip()).expanduser()
    except Exception:
        pass
    for reader in (read_custom_data_dir_from_plist,
                   read_custom_data_dir_from_file):
        try:
            candidate = reader()
        except Exception:
            candidate = None
        if candidate is not None and str(candidate).strip():
            try:
                return candidate.expanduser()
            except Exception:
                continue
    try:
        return default_data_dir()
    except Exception:
        return Path("/tmp/3DFM")


def write_custom_data_dir_pointer(path: Path) -> None:
    """Persist a custom data dir (pointer file + UserDefaults plist).

    Atomic for the pointer file (tmp + fsync + rename). The plist update
    is best-effort and preserves other keys. Raises OSError on failure
    so callers can report it (never silently half-written).
    """
    target = Path(str(path)).expanduser()
    pointer = custom_data_dir_pointer_file()
    pointer.parent.mkdir(parents=True, exist_ok=True)
    import tempfile
    fd, tmp = tempfile.mkstemp(dir=str(pointer.parent),
                               prefix=".3DFM-location-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(str(target) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, pointer)
        try:
            dfd = os.open(str(pointer.parent), os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    # Mirror into UserDefaults plist so the Swift UI shows the same value
    # even if the pointer file is removed manually.
    try:
        plist = _user_defaults_plist()
        data: dict = {}
        if plist.is_file():
            try:
                with open(plist, "rb") as f:
                    loaded = plistlib.load(f)
                if isinstance(loaded, dict):
                    data = loaded
            except (OSError, ValueError, plistlib.InvalidFileException):
                data = {}
        data["customDataDir"] = str(target)
        plist.parent.mkdir(parents=True, exist_ok=True)
        fd2, tmp2 = tempfile.mkstemp(dir=str(plist.parent),
                                     prefix=".local-3dfm-")
        try:
            with os.fdopen(fd2, "wb") as f:
                plistlib.dump(data, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp2, plist)
        except BaseException:
            try:
                os.unlink(tmp2)
            except OSError:
                pass
            # Plist mirror failure must not fail the whole operation when
            # the pointer file already succeeded.
            pass
    except OSError:
        pass


def clear_custom_data_dir_pointer() -> None:
    """Remove custom data-dir persistence (revert to default). Best-effort."""
    try:
        pointer = custom_data_dir_pointer_file()
        try:
            pointer.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
    except Exception:
        pass
    try:
        plist = _user_defaults_plist()
        if plist.is_file():
            try:
                with open(plist, "rb") as f:
                    data = plistlib.load(f)
            except (OSError, ValueError, plistlib.InvalidFileException):
                return
            if isinstance(data, dict) and "customDataDir" in data:
                data.pop("customDataDir", None)
                try:
                    import tempfile
                    fd, tmp = tempfile.mkstemp(dir=str(plist.parent),
                                               prefix=".local-3dfm-")
                    with os.fdopen(fd, "wb") as f:
                        plistlib.dump(data, f)
                        f.flush()
                        os.fsync(f.fileno())
                    os.replace(tmp, plist)
                except (OSError, ValueError):
                    try:
                        os.unlink(tmp)
                    except (OSError, NameError):
                        pass
    except Exception:
        pass


def read_configured_models_dir(data_root: Path) -> str:
    """Read settings.json `models_dir` for a data root. "" when unset."""
    try:
        import json
        settings_path = Path(data_root).expanduser() / "settings.json"
        if not settings_path.is_file():
            return ""
        with open(settings_path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return ""
        value = data.get("models_dir", "")
        return value if isinstance(value, str) else ""
    except (OSError, ValueError):
        return ""
    except Exception:
        return ""


def resolve_models_dir(data_root: Path | None = None,
                       configured: str = "") -> Path:
    """Effective models dir. Never raises; defaults win.

    Precedence: FM3D_MODELS_DIR env > `configured`/settings.json value
    (when non-empty) > <data_root>/models.
    """
    try:
        env = os.environ.get("FM3D_MODELS_DIR")
        if env and env.strip():
            return Path(env.strip()).expanduser()
    except Exception:
        pass
    cfg = configured
    if not cfg:
        try:
            root = data_root if data_root is not None else resolve_data_dir()
            cfg = read_configured_models_dir(root)
        except Exception:
            cfg = ""
    if cfg and cfg.strip():
        try:
            return Path(cfg.strip()).expanduser()
        except Exception:
            pass
    try:
        root = data_root if data_root is not None else resolve_data_dir()
        return Path(root).expanduser() / "models"
    except Exception:
        return Path.home() / "Library" / "Application Support" / "3DFM" / "models"


@dataclass(frozen=True)
class DataDirs:
    root: Path
    models_override: Optional[Path] = field(default=None)

    @property
    def db_path(self) -> Path:
        return self.root / "jobs.db"

    @property
    def token_path(self) -> Path:
        return self.root / "server.token"

    @property
    def pid_path(self) -> Path:
        """Liveness marker for `3dfm stop` (written at serve, removed at exit)."""
        return self.root / "server.pid"

    @property
    def settings_path(self) -> Path:
        return self.root / "settings.json"

    @property
    def models_dir(self) -> Path:
        if self.models_override is not None:
            return self.models_override
        # Env still wins over a constructed override of None so workers
        # spawned with FM3D_MODELS_DIR agree even when the server was
        # built before the env was set.
        try:
            env = os.environ.get("FM3D_MODELS_DIR")
            if env and env.strip():
                return Path(env.strip()).expanduser()
        except Exception:
            pass
        return self.root / "models"

    @property
    def venvs_dir(self) -> Path:
        return self.root / "venvs"

    @property
    def runtimes_dir(self) -> Path:
        return self.root / "runtimes"

    @property
    def jobs_dir(self) -> Path:
        return self.root / "jobs"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    def ensure(self) -> "DataDirs":
        for p in (self.root, self.models_dir, self.venvs_dir,
                  self.runtimes_dir, self.jobs_dir, self.logs_dir):
            p.mkdir(parents=True, exist_ok=True)
        return self

    def job_dir(self, job_id: str) -> Path:
        return self.jobs_dir / job_id


def build_data_dirs(root: Path | None = None,
                    settings_models_dir: str = "") -> DataDirs:
    """Construct DataDirs honoring models override (env > settings)."""
    effective_root = (Path(root).expanduser() if root is not None
                      else resolve_data_dir().expanduser())
    override: Optional[Path] = None
    try:
        env = os.environ.get("FM3D_MODELS_DIR")
        if env and env.strip():
            override = Path(env.strip()).expanduser()
        elif settings_models_dir and settings_models_dir.strip():
            override = Path(settings_models_dir.strip()).expanduser()
        else:
            # Fall back to whatever settings.json already stores so a
            # server restarted without explicit settings still agrees
            # with the last saved choice.
            stored = read_configured_models_dir(effective_root)
            if stored and stored.strip():
                override = Path(stored.strip()).expanduser()
    except Exception:
        override = None
    return DataDirs(effective_root, override)
