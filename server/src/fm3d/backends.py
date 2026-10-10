"""Generation backends.

Each `run_<mode>(ctx)` either produces `artifacts/model.glb` (+ previews)
or raises with a human-readable message. Heavy third-party imports live
INSIDE the functions so the server process and `--help` paths never touch
torch, and a missing runtime becomes a clean job failure — not a crash.

Status:
- test   : plumbing backend (sleep loop + minimal valid GLB). Gated by
           FM3D_TEST=1 on the server side.
- rmbg   : real BiRefNet background removal (torch MPS/CPU).
- normal : TRELLIS.2 image-to-3D (P1 target; faithful call shape, guarded).
- human  : Hunyuan3D Shape + Paint (P2 target; guarded).
- mvadapter synthesis is a stage inside human mode (P3 target; guarded).
"""
from __future__ import annotations

import os
import struct
import time
from pathlib import Path

from .worker import Cancelled, Ctx, atomic_write_bytes


# ------------------------------------------------------------------ registry
def _cleanup_gpu() -> None:
    """Best-effort GPU/CPU memory release for idle transitions.

    Called after each heavy stage and before worker exit so unified
    memory is returned promptly even if the OS keeps the process
    alive briefly (e.g. crash-report suspension). Never raises.
    """
    try:
        import gc as _gc
        _gc.collect()
    except Exception:
        pass
    try:
        import torch as _t
        if getattr(_t.backends, "mps", None) and _t.backends.mps.is_available():
            try:
                _t.mps.empty_cache()
            except Exception:
                pass
    except ImportError:
        pass
    try:
        import gc as _gc2
        _gc2.collect()
    except Exception:
        pass


def run(ctx: Ctx, mode: str, name: str) -> None:
    try:
        if mode == "test":
            run_test(ctx)
        elif mode == "normal":
            run_normal(ctx)
        elif mode == "human":
            run_human(ctx)
        else:
            raise RuntimeError(f"unknown mode: {mode!r}")
    finally:
        # Always release before process exit -> idle memory is freed.
        _cleanup_gpu()


# ------------------------------------------------------------------ helpers
def models_root() -> Path:
    from .paths import resolve_data_dir, resolve_models_dir
    try:
        return resolve_models_dir(resolve_data_dir())
    except Exception:
        from .paths import resolve_data_dir as _dd
        return _dd() / "models"


def require_dir(path: Path, hint: str) -> Path:
    if not path.is_dir():
        raise RuntimeError(
            f"model not found: {path.name} at {path} "
            f"({hint} / 不足モデル: {path} にありません。{hint})")
    return path


SETUP_HINT = "Run Setup (one button) to download runtimes and models."


def minimal_glb() -> bytes:
    """Smallest valid glTF 2.0 GLB: one triangle. Used by test backend and
    as a canary for the export/validation path."""
    positions = struct.pack("<9f", 0, 0, 0, 1, 0, 0, 0, 1, 0)
    indices = struct.pack("<3H", 0, 1, 2)
    pad = b"\x00" * ((4 - (len(indices) % 4)) % 4)
    binary = positions + indices + pad
    gltf = {
        "asset": {"version": "2.0", "generator": "3DFM-test"},
        "scene": 0, "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0}],
        "meshes": [{"primitives": [{
            "attributes": {"POSITION": 0}, "indices": 1, "mode": 4}]}],
        "buffers": [{"byteLength": len(binary)}],
        "bufferViews": [
            {"buffer": 0, "byteOffset": 0, "byteLength": len(positions)},
            {"buffer": 0, "byteOffset": len(positions),
             "byteLength": len(indices)}],
        "accessors": [
            {"bufferView": 0, "componentType": 5126, "count": 3,
             "type": "VEC3", "min": [0, 0, 0], "max": [1, 1, 0]},
            {"bufferView": 1, "componentType": 5123, "count": 3,
             "type": "SCALAR"}],
    }
    import json as _json
    js = _json.dumps(gltf, separators=(",", ":")).encode()
    js += b" " * ((4 - (len(js) % 4)) % 4)
    total = 12 + 8 + len(js) + 8 + len(binary)
    out = struct.pack("<III", 0x46546C67, 2, total)
    out += struct.pack("<II", len(js), 0x4E4F534A) + js
    out += struct.pack("<II", len(binary), 0x004E4942) + binary
    return out


# ------------------------------------------------------------------ test
def run_test(ctx: Ctx) -> None:
    """Plumbing prover: exercises progress, cancel, ETA, export, crash."""
    spec = ctx.spec
    duration = float(spec.get("duration_s", 8))
    fail = spec.get("fail_at")  # None | "error" | "crash" | "empty"
    steps = max(1, int(duration * 5))
    for i in range(steps + 1):
        ctx.check_cancel()
        ctx.progress("working", i / steps * 90.0, f"step {i}/{steps}")
        if fail == "crash" and i == steps // 2:
            os.kill(os.getpid(), 9)  # simulate native crash; no cleanup
        time.sleep(duration / steps)
    if fail == "error":
        raise RuntimeError("test backend: requested error")
    ctx.check_cancel()
    ctx.progress("export", 96.0, "writing glb")
    if fail != "empty":
        atomic_write_bytes(ctx.artifacts_dir / "model.glb", minimal_glb())
    else:
        atomic_write_bytes(ctx.artifacts_dir / "model.glb", b"")
    atomic_write_bytes(ctx.artifacts_dir / "preview.png",
                       _placeholder_png())
    ctx.progress("export", 100.0, "done")


def _placeholder_png() -> bytes:
    import struct as _s, zlib as _z
    def chunk(t: bytes, d: bytes) -> bytes:
        c = t + d
        return _s.pack(">I", len(d)) + c + _s.pack(">I", _z.crc32(c))
    ihdr = _s.pack(">IIBBBBB", 8, 8, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + bytes([200, 200, 200]) * 8 for _ in range(8))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", _z.compress(raw)) + chunk(b"IEND", b""))


# ------------------------------------------------------------------ rmbg
def run_rembg_file(src: Path, dst: Path, threshold: float,
                   report) -> None:
    """BiRefNet RMBG-2.0. Raises RuntimeError with setup hint if unavailable."""
    import torch
    from PIL import Image
    from torchvision import transforms
    try:
        from transformers import AutoModelForImageSegmentation
    except ImportError as e:
        raise RuntimeError(f"transformers not installed. {SETUP_HINT}") from e
    model_dir = models_root() / "rmbg-2.0"
    if not model_dir.is_dir():
        raise RuntimeError(f"RMBG-2.0 weights missing. {SETUP_HINT}")
    device = "mps" if getattr(torch.backends, "mps", None) \
        and torch.backends.mps.is_available() else "cpu"
    report(f"rembg device={device}")
    model = AutoModelForImageSegmentation.from_pretrained(
        str(model_dir), trust_remote_code=True)
    model.to(device)
    model.eval()
    tf = transforms.Compose([
        transforms.Resize((1024, 1024)),
        transforms.ToTensor(),
        transforms.Normalize([0.485, 0.456, 0.406],
                             [0.229, 0.224, 0.225])])
    img = Image.open(src).convert("RGB")
    inp = tf(img).unsqueeze(0).to(device)
    with torch.no_grad():
        pred = model(inp)[-1].sigmoid().cpu()[0].squeeze()
    mask = transforms.ToPILImage()(pred).resize(img.size)
    if threshold and 0.0 < threshold < 1.0:
        import numpy as _np
        a = _np.array(mask)
        a = ((a > threshold * 255) * 255).astype("uint8")
        from PIL import Image as _I
        mask = _I.fromarray(a, mode="L")
    img.putalpha(mask)
    import io as _io
    buf = _io.BytesIO()
    img.save(buf, format="PNG")
    atomic_write_bytes(dst, buf.getvalue())
    if device == "mps":
        torch.mps.empty_cache()


# ------------------------------------------------------------------ normal
def run_normal(ctx: Ctx) -> None:
    spec = ctx.spec
    inputs = sorted(p for p in ctx.inputs_dir.iterdir() if p.is_file())
    if not inputs:
        raise RuntimeError("no input image")
    src = inputs[0]
    ctx.progress("preprocess", 3.0, f"input {src.name}")
    work = ctx.job_dir / "work"
    work.mkdir(exist_ok=True)
    fg = work / "fg.png"
    if _has_alpha(src):
        ctx.progress("rembg", 8.0, "input has alpha; keeping")
        import shutil as _sh
        _sh.copy(src, fg)
    else:
        ctx.progress("rembg", 6.0, "background removal (RMBG-2.0)")
        run_rembg_file(src, fg, float(spec.get("rembg_threshold", 0.5)),
                       lambda m: ctx.progress("rembg", 10.0, m))
    ctx.check_cancel()
    _require_torch_mps()
    _run_trellis(ctx, fg, spec)


def _has_alpha(path: Path) -> bool:
    try:
        from PIL import Image
        return "A" in Image.open(path).getbands()
    except Exception:
        return False


def _require_torch_mps() -> str:
    try:
        import torch
    except ImportError as e:
        raise RuntimeError(f"torch not installed. {SETUP_HINT}") from e
    if getattr(torch.backends, "mps", None) \
            and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


PIPELINE_ALIASES = {
    "512": "512",
    "1024": "1024",
    "512->1024": "1024_cascade",
    "1024_cascade": "1024_cascade",
    "512->1536": "1536_cascade",
    "1536_cascade": "1536_cascade",
}

_WATCHDOG_SIGS = ("non-zero size", "BVH needs at least 8 triangles")


class _Heartbeat:
    """Keep progress.jsonl fresh during long native calls.

    The server's stall watchdog kills workers with no output; diffusion
    sampling on MPS can run minutes per stage with zero Python-level
    callbacks, so a thread re-emits the current stage percentage.

    The refresh stops after `max_s` seconds (env FM3D_HEARTBEAT_MAX_S,
    default 1500): without a cap, a truly hung native call would refresh
    forever and the stall watchdog — the only hang detector — could never
    fire, leaving the worker (and its gigabytes) alive indefinitely. The
    manager derives the cap from `stall_timeout_s` so the watchdog always
    gets the last word.
    """

    def __init__(self, ctx: Ctx, stage: str, pct: float, msg: str,
                 max_s: float | None = None, interval_s: float = 20.0):
        import threading
        import time as _t
        if max_s is None:
            try:
                max_s = float(os.environ.get("FM3D_HEARTBEAT_MAX_S",
                                             "1500") or 1500)
            except (TypeError, ValueError):
                max_s = 1500.0
        self._ctx = ctx
        self._args = (stage, pct, msg)
        self._interval = max(0.01, float(interval_s))
        self._deadline = _t.monotonic() + max(60.0, max_s)
        self._stop = threading.Event()
        self._th = threading.Thread(target=self._loop, daemon=True)

    def _loop(self):
        import time as _t
        while not self._stop.wait(self._interval):
            if _t.monotonic() > self._deadline:
                break
            try:
                self._ctx.progress(*self._args)
            except Exception:
                break

    def __enter__(self):
        self._th.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._th.join(timeout=5)
        return False


def _trellis_runtime_dirs():
    from .paths import resolve_data_dir
    tm = resolve_data_dir() / "runtimes" / "trellis" / "trellis-mac"
    return tm, tm / "TRELLIS.2", tm / "stubs"


def _patch_varlen_reduce_mps() -> None:
    """torch<=2.13 MPS lacks segment_reduce AND its CPU fallback raises
    for some dtypes (1024_cascade path). Route that single op via CPU."""
    try:
        from trellis2.modules.sparse import basic as _b
    except ImportError:
        return
    import torch
    if getattr(_b.VarLenTensor.reduce, "_mps_patched", False):
        return

    def reduce(self, op, dim=None, keepdim=False):
        if isinstance(dim, int):
            dim = (dim,)
        feats = self.feats
        if op == "mean":
            red = feats.mean(dim=dim, keepdim=keepdim)
        elif op == "sum":
            red = feats.sum(dim=dim, keepdim=keepdim)
        elif op == "prod":
            red = feats.prod(dim=dim, keepdim=keepdim)
        else:
            raise ValueError(f"Unsupported reduce operation: {op}")
        if dim is None or 0 in dim:
            return red
        if isinstance(feats, torch.Tensor) and feats.device.type == "mps":
            lengths = self.seqlen
            if isinstance(lengths, torch.Tensor):
                lengths = lengths.cpu()
            return torch.segment_reduce(
                red.cpu(), reduce=op, lengths=lengths).to(feats.device)
        return torch.segment_reduce(red, reduce=op, lengths=self.seqlen)

    reduce._mps_patched = True  # type: ignore[attr-defined]
    _b.VarLenTensor.reduce = reduce


def _trellis_shape(ctx: Ctx, image_path: Path, spec: dict):
    """TRELLIS.2 shape (+voxel attrs). Returns (mesh, verts, faces)."""
    import sys as _sys
    trellis_dir = models_root() / "trellis-2-4b"
    require_dir(trellis_dir, SETUP_HINT)
    tm, t2, stubs = _trellis_runtime_dirs()
    if not (t2 / "trellis2" / "__init__.py").exists():
        raise RuntimeError(
            "TRELLIS runtime code missing "
            f"({tm} not installed). Re-run Setup (normal tier or later).")
    # Env BEFORE torch import (mirrors trellis-mac generate.py).
    os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
    os.environ.setdefault("ATTN_BACKEND", "sdpa")
    os.environ.setdefault("SPARSE_ATTN_BACKEND", "sdpa")
    try:
        import flex_gemm  # noqa: F401
        os.environ["SPARSE_CONV_BACKEND"] = "flex_gemm"
        conv = "flex_gemm(metal)"
    except (ImportError, RuntimeError, OSError):
        # Must assign (not setdefault): the manager pre-seeds flex_gemm,
        # so a failed import would otherwise leave a bogus value.
        os.environ["SPARSE_CONV_BACKEND"] = "none"
        conv = "none(pure-pytorch)"
    for d in (str(t2), str(tm)):
        if d not in _sys.path:
            _sys.path.insert(0, d)
    if str(stubs) not in _sys.path:
        _sys.path.append(str(stubs))

    import torch
    from PIL import Image
    device = _require_torch_mps()
    if device != "mps":
        raise RuntimeError("MPS not available; TRELLIS.2 needs Apple Silicon")
    ctx.progress("shape", 15.0, f"loading TRELLIS.2 (mps, conv={conv})")
    try:
        from trellis2.pipelines import Trellis2ImageTo3DPipeline
    except ImportError as e:
        raise RuntimeError(
            f"TRELLIS.2 code import failed: {e}. Re-run Setup.") from e
    _patch_varlen_reduce_mps()
    t0 = __import__("time").time()
    try:
        pipeline = Trellis2ImageTo3DPipeline.from_pretrained(str(trellis_dir))
    except Exception as e:
        raise RuntimeError(
            f"cannot load TRELLIS.2 weights ({e}). "
            "If this mentions gated access/DINOv3, set HF_TOKEN and "
            "accept the model license, then re-run Setup.") from e
    pipeline.to(torch.device("mps"))
    torch.mps.empty_cache()
    image = Image.open(image_path)
    ptype = PIPELINE_ALIASES.get(str(spec.get("pipeline_type", "512->1024")),
                                 "1024_cascade")
    seed = ctx.seed if isinstance(ctx.seed, int) else 42
    steps = spec.get("steps")
    ov = {"steps": int(steps)} if steps else {}
    ctx.progress("shape", 25.0, f"sampling (pipeline={ptype}, seed={seed})")
    try:
        with _Heartbeat(ctx, "shape", 40.0, f"sampling {ptype}..."):
            outputs = pipeline.run(
                image, seed=seed, pipeline_type=ptype,
                sparse_structure_sampler_params=dict(ov),
                shape_slat_sampler_params=dict(ov),
                tex_slat_sampler_params=dict(ov))
    except (IndexError, AssertionError) as e:
        if any(s in str(e) for s in _WATCHDOG_SIGS):
            raise RuntimeError(
                "empty mesh produced (empty-mesh; macOS GPU watchdog "
                "likely killed a long Metal kernel). Retry, ideally headless "
                "with displays asleep.") from e
        raise
    ctx.check_cancel()
    mesh = outputs[0] if isinstance(outputs, list) else outputs
    import numpy as _np
    verts = mesh.vertices.cpu().numpy()
    faces = mesh.faces.cpu().numpy()
    if verts.shape[0] == 0 or faces.shape[0] == 0:
        raise RuntimeError("empty mesh produced (empty-mesh). "
                           "Retry with a different seed.")
    el = __import__("time").time() - t0
    ctx.progress("shape", 70.0,
                 f"mesh {verts.shape[0]:,}v/{faces.shape[0]:,}f in {el:.0f}s")
    torch.mps.empty_cache()
    return mesh, verts, faces


def _run_trellis(ctx: Ctx, image_path: Path, spec: dict) -> None:
    """Full TRELLIS.2 pipeline (shape + PBR bake)."""
    tm, _, _ = _trellis_runtime_dirs()
    mesh, verts, faces = _trellis_shape(ctx, image_path, spec)
    import torch as _t
    _t.mps.empty_cache()
    tex_size = int(spec.get("texture_size", 2048))
    has_voxels = getattr(mesh, "attrs", None) is not None
    if has_voxels:
        ctx.progress("texture", 75.0, f"baking PBR {tex_size}px")
        with _Heartbeat(ctx, "texture", 85.0, "baking textures..."):
            glb_bytes = _bake_trellis(ctx, tm, mesh, verts, faces,
                                      tex_size, spec)
    else:
        ctx.progress("texture", 80.0, "vertex-color export (no voxels)")
        import trimesh
        glb_bytes = trimesh.Trimesh(vertices=verts, faces=faces) \
            .export(file_type="glb")
        if isinstance(glb_bytes, str):
            glb_bytes = glb_bytes.encode()
    atomic_write_bytes(ctx.artifacts_dir / "model.glb", bytes(glb_bytes))
    ctx.progress("export", 100.0, "done")


def _bake_trellis(ctx: Ctx, tm: Path, mesh, verts, faces,
                  tex_size: int, spec: dict) -> bytes:
    """Metal o-voxel bake, else KDTree fallback (trellis-mac logic)."""
    import torch
    use_metal = False
    try:
        import o_voxel.postprocess  # noqa: F401
        import o_voxel as _ov
        backend = getattr(_ov.postprocess, "_BACKEND", None)
        has_dr = getattr(_ov.postprocess, "_HAS_DR", False)
        use_metal = backend == "metal" and has_dr
    except (ImportError, AttributeError, RuntimeError, OSError):
        use_metal = False
    if use_metal:
        try:
            import o_voxel as _ov2
            import fast_simplification
            target = min(200000, len(faces))
            if len(faces) > target:
                ratio = 1.0 - (target / len(faces))
                sv, sf = fast_simplification.simplify(verts, faces, ratio)
                sv_t = torch.from_numpy(sv).float()
                sf_t = torch.from_numpy(sf.astype("int32"))
            else:
                sv_t, sf_t = mesh.vertices.cpu(), mesh.faces.cpu()
            glb = _ov2.postprocess.to_glb(
                vertices=sv_t, faces=sf_t,
                attr_volume=mesh.attrs.cpu(), coords=mesh.coords.cpu(),
                attr_layout=mesh.layout, voxel_size=mesh.voxel_size,
                aabb=[[-0.5, -0.5, -0.5], [0.5, 0.5, 0.5]],
                decimation_target=target, texture_size=tex_size,
                verbose=True)
            import io as _io
            buf = _io.BytesIO()
            # trimesh Scene cannot infer type from a file object
            glb.export(buf, file_type="glb")
            data = buf.getvalue()
            if not data:
                raise RuntimeError("metal bake produced empty GLB")
            return data
        except (RuntimeError, OSError) as e:
            ctx.progress("texture", 82.0, f"metal bake failed, KDTree: {e}")
            use_metal = False
    # KDTree fallback
    import sys as _sys
    if str(tm) not in _sys.path:
        _sys.path.insert(0, str(tm))
    from backends.texture_baker import (bake_texture, export_glb_with_texture,
                                        uv_unwrap)
    from PIL import Image as _PIL
    bake_verts, bake_faces = verts, faces
    target = min(200000, len(faces))
    if len(faces) > target:
        import fast_simplification
        ratio = 1.0 - (target / len(faces))
        bake_verts, bake_faces = fast_simplification.simplify(
            verts, faces, ratio)
    new_verts, new_faces, uvs, _vm = uv_unwrap(bake_verts, bake_faces)
    base_img, mr_img, _mask = bake_texture(
        new_verts, new_faces, uvs,
        mesh.coords.cpu().float().numpy(),
        mesh.attrs.cpu().float().numpy(),
        mesh.origin.cpu().float().numpy(), mesh.voxel_size,
        texture_size=tex_size)
    out = ctx.artifacts_dir / "model.glb"
    out.parent.mkdir(parents=True, exist_ok=True)
    export_glb_with_texture(new_verts, new_faces, uvs, base_img, mr_img,
                            str(out))
    _PIL.fromarray(base_img).save(ctx.artifacts_dir / "preview.png")
    return out.read_bytes()


# ------------------------------------------------------------------ human
def run_human(ctx: Ctx) -> None:
    """Hunyuan3D 2.1 multi-view pipeline (P2) + MV-Adapter synthesis (P3)."""
    spec = ctx.spec
    inputs = sorted(p for p in ctx.inputs_dir.iterdir() if p.is_file())
    if not inputs:
        raise RuntimeError("no input images")
    work = ctx.job_dir / "work"
    mvdir = work / "multiview"
    mvdir.mkdir(parents=True, exist_ok=True)
    if len(inputs) == 1 and spec.get("synthesize_views"):
        ctx.progress("mvadapter", 8.0, "synthesizing views (MV-Adapter)")
        views = _run_mvadapter(ctx, inputs[0], mvdir, spec)
    elif len(inputs) == 6:
        views = []
        for i, src in enumerate(inputs):
            ctx.check_cancel()
            dst = mvdir / f"view{i:02d}.png"
            run_rembg_file(src, dst, float(spec.get("rembg_threshold", 0.4)),
                           lambda m, i=i: ctx.progress(
                               "rembg", 5.0 + i, f"view{i}: {m}"))
            views.append(dst)
    else:
        raise RuntimeError(
            "human mode needs 1 image (+synthesize_views) or 6 images; "
            f"got {len(inputs)}")
    ctx.check_cancel()
    _run_hunyuan(ctx, views, spec)


AZIMUTHS_6V = [0, 45, 90, 180, 225, 270]


def _run_mvadapter(ctx: Ctx, front: Path, outdir: Path,
                   spec: dict) -> list[Path]:
    """1 image -> 6 views via MV-Adapter SDXL (Apple MPS).

    Views land in outdir as view00..05.png in AZIMUTHS_6V order.
    """
    import sys as _sys
    from .paths import resolve_data_dir as _dd
    mv = _dd() / "runtimes" / "mv" / "MV-Adapter"
    base = models_root() / "sdxl-base-1.0"
    adapter = models_root() / "mv-adapter"
    require_dir(base, SETUP_HINT)
    require_dir(adapter, SETUP_HINT)
    if not (mv / "mvadapter" / "__init__.py").exists():
        raise RuntimeError("MV-Adapter code missing. Re-run Setup.")
    if not (adapter / "mvadapter_i2mv_sdxl.safetensors").exists():
        raise RuntimeError("MV-Adapter weights missing. Re-run Setup.")
    for d in (str(mv), str(mv / "scripts")):
        if d not in _sys.path:
            _sys.path.insert(0, d)
    import torch
    from PIL import Image as _PIL
    if not (getattr(torch.backends, "mps", None)
            and torch.backends.mps.is_available()):
        raise RuntimeError("MPS not available; MV-Adapter needs Apple Silicon")
    # Warm torch's compiler stack BEFORE stubbing triton below: the stubs
    # would otherwise break torch._dynamo/_inductor imports triggered
    # lazily by diffusers (@torch.compiler.disable at class definition).
    try:
        import torch._dynamo  # noqa: F401
    except ImportError:
        pass
    # mesh_utils imports nvdiffrast (CUDA-only) at module scope, but the
    # camera/plucker path we use never calls it. Stub it out.
    import sys as _sys2
    import types as _types
    if "nvdiffrast" not in _sys2.modules:
        try:
            import nvdiffrast  # noqa: F401
        except ImportError:
            _fake = _types.ModuleType("nvdiffrast")
            _fake_torch = _types.ModuleType("nvdiffrast.torch")
            _fake.torch = _fake_torch
            _sys2.modules["nvdiffrast"] = _fake
            _sys2.modules["nvdiffrast.torch"] = _fake_torch
    # Same story for triton (only used by the texture-blend path).
    # It must look like a *package*: torch._inductor.runtime.hints does
    # `import triton.backends.compiler` behind has_triton_package(), and a
    # flat module stub dies with "'triton' is not a package" (fatal inside
    # diffusers' peft loader import). Empty submodules land torch in its
    # pure-python AttrsDescriptor fallback, which our path never executes.
    if "triton" not in _sys2.modules:
        try:
            import triton  # noqa: F401
        except ImportError:
            _tr = _types.ModuleType("triton")
            _tr.__path__ = []
            # @triton.jit is evaluated at import time; identity is fine
            # since the kernel is never launched on our code path.
            _tr.jit = lambda f=None, **kw: (f if callable(f)
                                            else (lambda g: g))
            _tl = _types.ModuleType("triton.language")
            _tl.__path__ = []
            _tl.dtype = type("dtype", (), {})
            _tl.constexpr = lambda f=None, **kw: (f if callable(f)
                                                  else (lambda g: g))
            _tr.language = _tl
            _bc = _types.ModuleType("triton.backends")
            _bc.__path__ = []
            _bcc = _types.ModuleType("triton.backends.compiler")
            _bcc.__path__ = []
            _bc.compiler = _bcc
            _tr.backends = _bc
            _cc = _types.ModuleType("triton.compiler")
            _cc.__path__ = []
            _ccc = _types.ModuleType("triton.compiler.compiler")
            _ccc.__path__ = []
            _cc.compiler = _ccc
            _tr.compiler = _cc
            _sys2.modules["triton"] = _tr
            _sys2.modules["triton.language"] = _tl
            _sys2.modules["triton.backends"] = _bc
            _sys2.modules["triton.backends.compiler"] = _bcc
            _sys2.modules["triton.compiler"] = _cc
            _sys2.modules["triton.compiler.compiler"] = _ccc
    try:
        from scripts.inference_i2mv_sdxl import (prepare_pipeline,
                                                 run_pipeline)
        from mvadapter.pipelines.pipeline_mvadapter_i2mv_sdxl import (
            MVAdapterI2MVSDXLPipeline as _MVPipe)
    except ImportError as e:
        raise RuntimeError(
            f"MV-Adapter import failed: {e}. Re-run Setup.") from e
    # diffusers>=0.40 removed enable_*_slicing; keep old call sites working.
    # On 32-48GB Macs unsliced VAE risks OOM, so prefer tiling/offload
    # when available (best-effort, never fatal).
    for _m in ("enable_vae_slicing", "enable_attention_slicing"):
        if not hasattr(_MVPipe, _m):
            setattr(_MVPipe, _m, lambda self: None)
    ctx.progress("mvadapter", 6.0, "loading SDXL + MV-Adapter (mps)")
    seed = ctx.seed if isinstance(ctx.seed, int) else 42
    try:
        steps = int(spec.get("mv_steps", 50))
    except (TypeError, ValueError):
        steps = 50
    # fp16 on MPS yields NaN in the custom MV attention (verified); fp32 is
    # required. 768px needs ~25GB; smaller Macs drop to 512px.
    from . import memguard as _mg
    total_b = _mg.total_bytes() or 0
    res = 768 if total_b >= 48 * 1024**3 else 512
    try:
        res = int(spec.get("mv_resolution", res))
    except (TypeError, ValueError):
        pass
    # Memory safety: 32GB-class Macs must stay at 512px. The server-side
    # submit validator rejects explicit 768px on <48GB, but clamp here as
    # well so a stale client can never OOM the worker.
    if total_b > 0 and total_b < 48 * 1024**3 and res > 512:
        ctx.progress("mvadapter", 6.0,
                     f"clamping mv_resolution {res}px -> 512px "
                     f"(32GB-class memory safety)")
        res = 512
    pipe = None
    try:
        with _Heartbeat(ctx, "mvadapter", 9.0,
                        f"synthesizing 6 views ({res}px fp32)..."):
            pipe = prepare_pipeline(
                base_model=str(base), vae_model=None, unet_model=None,
                lora_model=None, adapter_path=str(adapter), scheduler=None,
                num_views=6, device="mps", dtype=torch.float32)
            # Memory safety on 32GB-class Macs: enable tiling / sequential
            # offload when the pipeline supports it (best-effort).
            if total_b > 0 and total_b < 48 * 1024**3:
                for _opt in ("enable_vae_tiling",
                             "enable_sequential_cpu_offload"):
                    try:
                        _fn = getattr(pipe, _opt, None)
                        if callable(_fn):
                            _fn()
                            ctx.progress("mvadapter", 7.0,
                                         f"memory-saver {_opt} enabled "
                                         f"(32GB-class)")
                    except Exception:
                        pass
            images = run_pipeline(
                pipe, num_views=6, text=str(spec.get("mv_prompt", "high quality")),
                image=_PIL.open(front).convert("RGB"),
                height=res, width=res,
                num_inference_steps=steps, guidance_scale=5.0, seed=seed,
                remove_bg_fn=None, device="mps", azimuth_deg=list(AZIMUTHS_6V))
    finally:
        # Always release + remove CUDA stubs, even when synthesis fails,
        # so later stages probe real modules and idle memory is freed.
        try:
            if pipe is not None:
                del pipe
        except Exception:
            pass
        _cleanup_gpu()
        for _m in [m for m in _sys2.modules
                   if m == "triton" or m.startswith(("triton.",
                                                     "nvdiffrast"))]:
            try:
                del _sys2.modules[_m]
            except KeyError:
                pass
    ctx.check_cancel()
    outdir.mkdir(parents=True, exist_ok=True)
    outs = []
    # run_pipeline returns (images, reference_image)
    if (isinstance(images, tuple) and len(images) == 2
            and isinstance(images[0], (list, tuple))):
        images = images[0]
    flat = list(images) if isinstance(images, (list, tuple)) else [images]
    # run_pipeline may return a grid; split into 6 tiles if needed
    if len(flat) == 1 and hasattr(flat[0], "size"):
        w, h = flat[0].size
        if w >= h * 5:
            tw = w // 6
            flat = [flat[0].crop((i * tw, 0, (i + 1) * tw, h))
                    for i in range(6)]
    if len(flat) != 6:
        raise RuntimeError(
            f"MV-Adapter returned {len(flat)} views, expected 6")
    for i, im in enumerate(flat):
        p = outdir / f"view{i:02d}.png"
        im.save(str(p))
        outs.append(p)
    _cleanup_gpu()
    ctx.progress("mvadapter", 12.0, "6 views ready")
    return outs


def _hun_dirs():
    from .paths import resolve_data_dir
    root = resolve_data_dir()
    code = root / "runtimes" / "hun" / "Hunyuan3D-2.1-mlx"
    weights = root / "models" / "hunyuan3d-2.1-mlx"
    return code, weights


def _run_hunyuan(ctx: Ctx, views: list[Path], spec: dict) -> None:
    """Human mode (hybrid): TRELLIS.2 shape + Hunyuan MLX PBR paint.

    Rationale: the Hunyuan MLX *shape* stage returns empty geometry in
    this environment (upstream port issue under investigation), while
    the MLX *paint* stage is verified working. TRELLIS.2 geometry +
    Hunyuan PBR paint delivers the required 6-view human pipeline.
    Reference view = views[0] (front).
    """
    work = ctx.job_dir / "work"
    work.mkdir(exist_ok=True)

    # Stage 1 — TRELLIS.2 shape from the front view (runs in hun-human
    # venv: trellis stack is installed there by trellis_runtime.py).
    ctx.progress("shape", 15.0, "shape via TRELLIS.2 (front view)")
    mesh, verts, faces = _trellis_shape(ctx, views[0], spec)
    import trimesh as _tr
    shape_glb = work / "shape.glb"
    _tr.Trimesh(vertices=verts, faces=faces).export(str(shape_glb))
    ctx.progress("shape", 55.0,
                 f"shape {verts.shape[0]:,}v/{faces.shape[0]:,}f")
    del mesh
    import gc as _gc
    _gc.collect()
    try:
        import torch as _t2
        if _t2.backends.mps.is_available():
            _t2.mps.empty_cache()
    except ImportError:
        pass

    # Stage 2 — Hunyuan MLX PBR paint.
    import sys as _sys
    code, weights = _hun_dirs()
    for d in (str(code), str(code / "hy3dpaint")):
        if d not in _sys.path:
            _sys.path.insert(0, d)
    os.environ["HUNYUAN3D_MLX_WEIGHTS_DIR"] = str(weights)
    ctx.progress("paint", 60.0, "Hunyuan PBR paint (MLX)")
    try:
        from hy3dpaint.textureGenPipeline_mlx import (
            Hunyuan3DPaintConfigMLX, Hunyuan3DPaintPipelineMLX)
    except ImportError as e:
        raise RuntimeError(
            f"Hunyuan paint code import failed: {e}. Re-run Setup.") from e
    cfg = Hunyuan3DPaintConfigMLX(max_num_view=6, resolution=512)
    tex_size = int(spec.get("texture_size", 2048))
    if hasattr(cfg, "texture_size"):
        cfg.texture_size = tex_size
    paint = Hunyuan3DPaintPipelineMLX(cfg)
    out_base = work / "textured.obj"
    with _Heartbeat(ctx, "paint", 80.0, "paint diffusion (MLX)..."):
        paint(mesh_path=str(shape_glb), image_path=str(views[0]),
              output_mesh_path=str(out_base), save_glb=True)
    ctx.check_cancel()
    glb = work / "textured.glb"
    if not glb.exists():
        cands = [c for c in sorted(work.glob("*.glb"))
                 if c.name != "shape.glb"]
        if not cands:
            raise RuntimeError("paint produced no GLB (empty-mesh?)")
        glb = cands[0]
    atomic_write_bytes(ctx.artifacts_dir / "model.glb", glb.read_bytes())
    ctx.progress("export", 100.0, "done")
