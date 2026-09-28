#!/usr/bin/env python3
"""Download model weights with resume + verification (stdlib + huggingface_hub).

Tier -> repos (mirrors 設計書 §5 manifest):
  normal: rmbg-2.0, trellis-2-4b
  human:  + hunyuan3d-2.1, sdxl-base-1.0, mv-adapter, realesrgan
  full:   human set (alias; turbos/MLX variants arrive in P2/P3)
  none:   nothing

Gated repos (RMBG-2.0 etc.) need HF_TOKEN and prior license acceptance.
A gated failure is recorded as `pending` with a message — setup still
exits 0 so the app opens; the job fails cleanly later with a hint.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

TIERS: dict[str, list[dict]] = {
    "normal": [
        {"id": "rmbg-2.0", "repo": "briaai/RMBG-2.0", "gated": True,
         "allow": ["*.json", "*.py", "*.md", ".gitattributes",
                   "model.safetensors", "preprocessor_config.json"]},
        {"id": "trellis-2-4b", "repo": "microsoft/TRELLIS.2-4B",
         "gated": False},
    ],
    "human": [
        {"id": "rmbg-2.0", "repo": "briaai/RMBG-2.0", "gated": True,
         "allow": ["*.json", "*.py", "*.md", ".gitattributes",
                   "model.safetensors", "preprocessor_config.json"]},
        {"id": "trellis-2-4b", "repo": "microsoft/TRELLIS.2-4B",
         "gated": False},
        {"id": "hunyuan3d-2.1-mlx", "repo": "dgrauet/hunyuan3d-2.1-mlx",
         "gated": False},
        {"id": "sdxl-base-1.0",
         "repo": "stabilityai/stable-diffusion-xl-base-1.0",
         "gated": False,
         # Slim set: exactly what MVAdapterI2MVSDXLPipeline.from_pretrained
         # resolves (model_index.json's 7 components, fp32 safetensors).
         # Skips flax/onnx/openvino duplicates, fp16 copies, single-file
         # checkpoints and standalone vae_* dirs (~59 GB saved).
         "allow": ["model_index.json", "*.md",
                   "scheduler/*", "tokenizer/*", "tokenizer_2/*",
                   "text_encoder/config.json",
                   "text_encoder/model.safetensors",
                   "text_encoder_2/config.json",
                   "text_encoder_2/model.safetensors",
                   "unet/config.json",
                   "unet/diffusion_pytorch_model.safetensors",
                   "vae/config.json",
                   "vae/diffusion_pytorch_model.safetensors"]},
        {"id": "mv-adapter", "repo": "huanngzh/mv-adapter", "gated": False,
         # Only the adapter the code loads (backends checks this exact
         # file). Other task variants + SD2.1 files are unused.
         "allow": ["*.md", "mvadapter_i2mv_sdxl.safetensors"]},
        {"id": "realesrgan",
         "url": "https://github.com/xinntao/Real-ESRGAN/releases/download/"
                "v0.1.0/RealESRGAN_x4plus.pth",
         "file": "RealESRGAN_x4plus.pth"},
    ],
}
TIERS["full"] = TIERS["human"]


def snapshot(repo: str, dest: Path, allow: list[str] | None = None) -> None:
    from huggingface_hub import snapshot_download
    dest.mkdir(parents=True, exist_ok=True)
    kw: dict = {"repo_id": repo, "local_dir": str(dest),
                "local_dir_use_symlinks": False, "resume_download": True}
    if allow:
        kw["allow_patterns"] = allow
    snapshot_download(**kw)


def fetch_url(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "3DFM-setup/1.0"})
    with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as f:
        while True:
            chunk = r.read(1024 * 1024)
            if not chunk:
                break
            f.write(chunk)
    os.replace(tmp, dest)


def looks_complete(d: Path) -> bool:
    try:
        return d.is_dir() and any(d.iterdir())
    except OSError:
        return False


def _auth_status() -> str:
    if not os.environ.get("HF_TOKEN"):
        return "auth: none (public repos only; gated -> pending)"
    try:
        from huggingface_hub import HfApi
        me = HfApi().whoami()
        name = me.get("name", "?") if isinstance(me, dict) else "?"
        return f"auth: ok ({name})"
    except Exception as e:  # noqa: BLE001 - report, don't fail
        return f"auth: token invalid ({e})"


def main() -> int:
    print(_auth_status(), flush=True)
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier", default="normal")
    ap.add_argument("--models-dir", required=True)
    ap.add_argument("--hf-bin", default="")
    ns = ap.parse_args()
    items = TIERS.get(ns.tier, [])
    mdir = Path(ns.models_dir).expanduser()
    mdir.mkdir(parents=True, exist_ok=True)
    manifest_path = mdir / "manifest.json"
    manifest: dict = {}
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text())
        except ValueError:
            manifest = {}
    ok, pending = [], []
    for it in items:
        dest = mdir / it["id"]
        if "url" in it:
            target = dest / it["file"]
            if target.exists() and target.stat().st_size > 0:
                print(f"skip {it['id']} (present)")
                manifest[it["id"]] = {"status": "ok"}
                continue
            try:
                print(f"download {it['id']} ...")
                fetch_url(it["url"], target)
                manifest[it["id"]] = {"status": "ok"}
                ok.append(it["id"])
            except Exception as e:  # noqa: BLE001 - record and continue
                manifest[it["id"]] = {"status": "pending", "error": str(e)}
                pending.append(it["id"])
                print(f"pending {it['id']}: {e}")
            continue
        # Always run snapshot_download(): it resumes partial downloads
        # and is a cheap no-op when complete. Never skip on listing alone.
        # NOTE: allow_patterns only restricts what is *fetched*; files
        # already on disk from an older full snapshot are left alone.
        # Use scripts/prune_models.py to slim an existing install.
        try:
            print(f"download {it['repo']} -> {it['id']} ...")
            snapshot(it["repo"], dest, it.get("allow"))
            manifest[it["id"]] = {"status": "ok", "repo": it["repo"]}
            ok.append(it["id"])
        except Exception as e:  # noqa: BLE001 - record and continue
            msg = str(e)
            if "401" in msg or "403" in msg or "gated" in msg.lower():
                msg += (" (gated repo: accept the license on Hugging Face "
                        "and set HF_TOKEN, then re-run Setup)")
            manifest[it["id"]] = {"status": "pending", "error": msg,
                                  "repo": it["repo"]}
            pending.append(it["id"])
            print(f"pending {it['id']}: {msg}", file=sys.stderr)
    try:
        manifest_path.write_text(json.dumps(manifest, indent=2),
                                 encoding="utf-8")
    except OSError as e:
        print(f"cannot write manifest: {e}", file=sys.stderr)
        return 1
    print(f"models ok={ok} pending={pending}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
