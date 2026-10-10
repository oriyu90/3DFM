"""Worker entry point: `python -m fm3d.worker <job_id>`.

Runs in an isolated child process so any model crash / OOM / native
segfault affects only the job, never the server. Communicates via:

- spec.json      (read-only input, written by the server)
- progress.jsonl (append-only JSON lines: stage/progress/eta_s/msg)
- cancel.flag    (polled between stages and inside long loops)
- artifacts/     (tmp-file + atomic rename; validated by the server)
- log.txt        (stdout/stderr redirected here)

Exit codes: 0 done · 2 cancelled (ack) · 1 failed.
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path


class Cancelled(Exception):
    pass


@dataclass
class Ctx:
    job_id: str
    job_dir: Path
    spec: dict
    seed: object
    t0: float = field(default_factory=time.time)
    last_emit: float = 0.0

    @property
    def inputs_dir(self) -> Path:
        return self.job_dir / "inputs"

    @property
    def artifacts_dir(self) -> Path:
        return self.job_dir / "artifacts"

    def cancelled(self) -> bool:
        return (self.job_dir / "cancel.flag").exists()

    def check_cancel(self) -> None:
        if self.cancelled():
            raise Cancelled()

    def progress(self, stage: str, pct: float, msg: str = "",
                 eta_s: float | None = None) -> None:
        self.check_cancel()
        if eta_s is None and pct > 1:
            el = time.time() - self.t0
            eta_s = el / pct * (100.0 - pct)
        line = json.dumps({"ts": time.time(), "stage": stage,
                           "progress": round(max(0.0, min(100.0, pct)), 2),
                           "eta_s": eta_s, "msg": msg},
                          ensure_ascii=False)
        p = self.job_dir / "progress.jsonl"
        with open(p, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass


def atomic_write_bytes(path: Path, data: bytes) -> None:
    import tempfile
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        # fsync the directory so the rename survives power loss
        try:
            dfd = os.open(str(path.parent), os.O_RDONLY)
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


def main() -> int:
    from .paths import resolve_data_dir
    if len(sys.argv) != 2:
        print("usage: python -m fm3d.worker <job_id>", file=sys.stderr)
        return 1
    job_id = sys.argv[1]
    # Path safety: job_id becomes a directory name. Reject traversal so
    # a manual `python -m fm3d.worker ../../x` can never escape jobs/.
    if (not job_id or "/" in job_id or "\\" in job_id or ".." in job_id
            or len(job_id) > 128 or job_id in (".", "..")):
        print(f"invalid job_id: {job_id[:32]}", file=sys.stderr)
        return 1
    dirs = resolve_data_dir()
    jdir = dirs / "jobs" / job_id
    log = open(jdir / "log.txt", "a", encoding="utf-8", buffering=1)
    sys.stdout = log
    sys.stderr = log
    try:
        rec = json.loads((jdir / "spec.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"cannot read spec.json: {e}")
        return 1
    ctx = Ctx(job_id=job_id, job_dir=jdir, spec=rec.get("spec", {}),
              seed=rec.get("seed"))
    try:
        from . import backends
        backends.run(ctx, rec.get("mode", ""), rec.get("name", job_id))
    except Cancelled:
        print("cancel acknowledged")
        return 2
    except Exception as e:  # noqa: BLE001 - worker must never crash raw
        print(f"FAILED: {e}")
        traceback.print_exc()
        try:
            ctx.job_dir.joinpath("progress.jsonl").stat()
        except OSError:
            pass
        return 1
    print("DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
