"""Filesystem layout for 3DFM runtime data.

All state lives under a single data dir so crash recovery is trivial:
restart the server and it re-reads jobs.db + job dirs. Nothing lives
only in RAM.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def default_data_dir() -> Path:
    return Path.home() / "Library" / "Application Support" / "3DFM"


def resolve_data_dir() -> Path:
    override = os.environ.get("FM3D_DATA_DIR")
    if override:
        return Path(override).expanduser()
    return default_data_dir()


@dataclass(frozen=True)
class DataDirs:
    root: Path

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
        return self.root / "models"

    @property
    def venvs_dir(self) -> Path:
        return self.root / "venvs"

    @property
    def jobs_dir(self) -> Path:
        return self.root / "jobs"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    def ensure(self) -> "DataDirs":
        for p in (self.root, self.models_dir, self.venvs_dir,
                  self.jobs_dir, self.logs_dir):
            p.mkdir(parents=True, exist_ok=True)
        return self

    def job_dir(self, job_id: str) -> Path:
        return self.jobs_dir / job_id
