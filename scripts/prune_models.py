#!/usr/bin/env python3
"""Prune unused weight files from an existing 3DFM models dir.

Background: older setups fetched full HF snapshots (all frameworks and
precisions). The pipelines only open a fixed subset:

- sdxl-base-1.0: model_index.json's 7 components as fp32 safetensors.
  Flax/ONNX/OpenVINO copies, fp16 duplicates, single-file checkpoints
  and the standalone vae_* dirs are never opened (~59 GB).
- mv-adapter: only mvadapter_i2mv_sdxl.safetensors is loaded (~15 GB rest).
- rmbg-2.0: transformers prefers model.safetensors; onnx/ + the
  pytorch_model.bin duplicate are unused (~4.2 GB).

Usage:
  prune_models.py --models-dir DIR [--dry-run|--apply]

- Default is --dry-run: prints per-model reclaimable bytes, deletes nothing.
- --apply deletes, but aborts a model unless ALL its critical files exist
  (so a partial download is never made worse).
- rmbg's pytorch_model.bin is kept as fallback unless `safetensors`
  imports in both torch venvs under <data-dir>/venvs.
- Everything deleted can be re-fetched with fetch_models.py (same tier).
- On --apply, writes <models-dir>/pruned.json as an audit record.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import sys
import time
from pathlib import Path

# Exact relative paths (posix) that must survive, per model dir.
SDXL_KEEP_FILES = {
    "model_index.json",
    "text_encoder/config.json",
    "text_encoder/model.safetensors",
    "text_encoder_2/config.json",
    "text_encoder_2/model.safetensors",
    "unet/config.json",
    "unet/diffusion_pytorch_model.safetensors",
    "vae/config.json",
    "vae/diffusion_pytorch_model.safetensors",
}
# Whole small dirs kept (configs + tokenizers, a few MB).
SDXL_KEEP_DIRS = {"scheduler", "tokenizer", "tokenizer_2"}
# Tiny root docs kept for license visibility.
SDXL_KEEP_ROOT_GLOB = {"*.md"}

RMBG_KEEP_FILES = {
    "config.json",
    "preprocessor_config.json",
    "model.safetensors",
    "BiRefNet_config.py",
    "birefnet.py",
}
RMBG_KEEP_ROOT_GLOB = {"*.md", ".gitattributes"}
# Deleted only when safetensors is importable in both torch venvs.
RMBG_BIN_FALLBACK = "pytorch_model.bin"

MV_KEEP_FILES = {"mvadapter_i2mv_sdxl.safetensors"}
MV_KEEP_ROOT_GLOB = {"*.md"}


def _matches(name: str, patterns: set[str]) -> bool:
    return any(fnmatch.fnmatchcase(name, p) for p in patterns)


def sdxl_plan(root: Path) -> tuple[list[Path], list[Path]]:
    keep, drop = [], []
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.is_symlink():
            continue
        rel = p.relative_to(root).as_posix()
        top = rel.split("/")[0]
        if (rel in SDXL_KEEP_FILES or top in SDXL_KEEP_DIRS
                or ("/" not in rel and _matches(rel, SDXL_KEEP_ROOT_GLOB))):
            keep.append(p)
        else:
            drop.append(p)
    return keep, drop


def rmbg_plan(root: Path, bin_deletable: bool) -> tuple[list[Path], list[Path]]:
    keep, drop = [], []
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.is_symlink():
            continue
        rel = p.relative_to(root).as_posix()
        if rel in RMBG_KEEP_FILES or (
                "/" not in rel and _matches(rel, RMBG_KEEP_ROOT_GLOB)):
            keep.append(p)
        elif rel == RMBG_BIN_FALLBACK and not bin_deletable:
            keep.append(p)  # fallback needed; keep it
        elif rel.startswith(".cache/"):
            keep.append(p)  # tiny HF bookkeeping; leave alone
        else:
            drop.append(p)
    return keep, drop


def mv_plan(root: Path) -> tuple[list[Path], list[Path]]:
    keep, drop = [], []
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.is_symlink():
            continue
        rel = p.relative_to(root).as_posix()
        if (rel in MV_KEEP_FILES or (
                "/" not in rel and _matches(rel, MV_KEEP_ROOT_GLOB))):
            keep.append(p)
        else:
            drop.append(p)
    return keep, drop


def _size(files: list[Path]) -> int:
    total = 0
    for p in files:
        try:
            total += p.stat().st_size
        except OSError:
            pass
    return total


def _has_all(root: Path, files: set[str]) -> tuple[bool, list[str]]:
    missing = [f for f in sorted(files) if not (root / f).is_file()]
    return (not missing, missing)


def _safetensors_everywhere(data_dir: Path) -> bool:
    """True only if `import safetensors` works in trellis AND hun-human."""
    import subprocess
    ok = True
    for venv in ("trellis", "hun-human"):
        py = data_dir / "venvs" / venv / "bin" / "python"
        if not py.exists():
            continue  # venv absent: its pipeline can't run anyway
        r = subprocess.run([str(py), "-c", "import safetensors"],
                           capture_output=True, timeout=60)
        if r.returncode != 0:
            ok = False
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models-dir", required=True)
    ap.add_argument("--data-dir", default="",
                    help="runtime root (for the safetensors guard; "
                         "defaults to <models-dir>/..)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=True)
    mode.add_argument("--apply", action="store_true")
    ns = ap.parse_args()

    mdir = Path(ns.models_dir).expanduser()
    data = Path(ns.data_dir).expanduser() if ns.data_dir else mdir.parent
    apply = bool(ns.apply)

    plans: dict[str, tuple[list[Path], list[Path]]] = {}
    guard = _safetensors_everywhere(data)
    print(f"safetensors in torch venvs: {guard} "
          f"(rmbg .bin duplicate {'pruned' if guard else 'kept as fallback'})")

    sdxl = mdir / "sdxl-base-1.0"
    if sdxl.is_dir():
        ok, missing = _has_all(sdxl, SDXL_KEEP_FILES)
        if not ok and apply:
            print(f"ABORT sdxl: missing critical {missing}", file=sys.stderr)
            return 1
        plans["sdxl-base-1.0"] = sdxl_plan(sdxl)
    rmbg = mdir / "rmbg-2.0"
    if rmbg.is_dir():
        ok, missing = _has_all(rmbg, RMBG_KEEP_FILES)
        if not ok and apply:
            print(f"ABORT rmbg: missing critical {missing}", file=sys.stderr)
            return 1
        plans["rmbg-2.0"] = rmbg_plan(rmbg, guard)
    mv = mdir / "mv-adapter"
    if mv.is_dir():
        ok, missing = _has_all(mv, MV_KEEP_FILES)
        if not ok and apply:
            print(f"ABORT mv-adapter: missing critical {missing}",
                  file=sys.stderr)
            return 1
        plans["mv-adapter"] = mv_plan(mv)

    total = 0
    for name, (keep, drop) in plans.items():
        n = _size(drop)
        total += n
        print(f"{name}: keep {len(keep)} files, "
              f"drop {len(drop)} files = {n / 1024**3:.1f} GiB")
        if n and n < 20 * 1024**3:
            for p in drop[:15]:
                print(f"    - {p.relative_to(mdir).as_posix()}")
            if len(drop) > 15:
                print(f"    ... and {len(drop) - 15} more")
    print(f"TOTAL reclaimable: {total / 1024**3:.1f} GiB"
          + ("" if apply else " (dry-run; nothing deleted)"))

    if not apply:
        return 0
    removed = 0
    for name, (_, drop) in plans.items():
        for p in drop:
            try:
                p.unlink()
                removed += 1
            except OSError as e:
                print(f"warn: cannot delete {p}: {e}", file=sys.stderr)
    # Drop dirs left empty by the prune (never the model root itself).
    for name in plans:
        root = mdir / name
        for dirpath, dirnames, filenames in __import__("os").walk(
                root, topdown=False):
            if Path(dirpath) != root and not dirnames and not filenames:
                try:
                    Path(dirpath).rmdir()
                except OSError:
                    pass
    try:
        (mdir / "pruned.json").write_text(json.dumps({
            "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "removed_files": removed,
            "reclaimed_gb": round(total / 1024**3, 1),
        }, indent=2), encoding="utf-8")
    except OSError as e:
        print(f"warn: cannot write pruned.json: {e}", file=sys.stderr)
    print(f"removed {removed} files. Re-fetch anytime with "
          f"fetch_models.py (same tier).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
