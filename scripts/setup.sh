#!/bin/bash
# 3DFM one-button setup backend.
# The Swift SetupWizard runs: setup.sh --data-dir ... --output-dir ... --models <tier>
# and parses JSONL progress lines on stdout.
#
#   --data-dir DIR     runtime root (default ~/Library/Application Support/3DFM)
#   --output-dir DIR   model output folder (written to settings.json)
#   --models TIER      none | normal | human | full   (default normal)
#   --hf-token TOKEN   Hugging Face token for gated repos (or HF_TOKEN env).
#                      Never written to logs; prefer env passing.
#   --reinstall        drop and recreate venvs
#   --python VERSION   default 3.11
#
# Idempotent: safe to re-run; finished steps are skipped.
# Exit 0 = setup complete (possibly with warnings), 1 = fatal.
set -u
DATA_DIR="$HOME/Library/Application Support/3DFM"
OUTPUT_DIR="$HOME/Pictures/3DFM"
TIER=normal
REINSTALL=0
PYVER=3.11
HF_TOKEN_ARG=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --data-dir) DATA_DIR="$2"; shift 2;;
    --output-dir) OUTPUT_DIR="$2"; shift 2;;
    --models) TIER="$2"; shift 2;;
    --hf-token) HF_TOKEN_ARG="$2"; shift 2;;
    --reinstall) REINSTALL=1; shift 1;;
    --python) PYVER="$2"; shift 2;;
    *) echo "unknown arg: $1" >&2; exit 1;;
  esac
done

# Token precedence: explicit flag > environment. Exported after LOG setup
# below so the presence note can be logged (value never logged).
HF_EXPORT_LATER="$HF_TOKEN_ARG"
unset HF_TOKEN_ARG

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_DIR="$DATA_DIR/venvs"
MODEL_DIR="$DATA_DIR/models"
LOG="$DATA_DIR/logs/setup.log"
STEP=0
NSTEPS=9
FAILED=0

mkdir -p "$DATA_DIR/logs" "$MODEL_DIR"
exec 3>>"$LOG"

emit() { # phase(0-100 weight) msg
  printf '{"type":"progress","step":%d,"steps":%d,"phase":%s,"msg":%s}\n' \
    "$STEP" "$NSTEPS" "$(printf '%s' "$1" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')" \
    "$(printf '%s' "$2" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')"
}
log() { echo "[setup] $*" >&3; }
die() { emit "error" "$1"; log "FATAL: $1"; echo "{\"type\":\"done\",\"ok\":false,\"error\":$(printf '%s' "$1" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')}" ; exit 1; }

# Export the token once so all child phases (fetch_models, runtime
# installers, pipeline prefetch) authenticate automatically.
# Precedence: explicit --hf-token flag > inherited environment.
# The value itself is never echoed or logged.
if [[ -n "$HF_EXPORT_LATER" ]]; then
  export HF_TOKEN="$HF_EXPORT_LATER"
  unset HF_EXPORT_LATER
fi
if [[ -n "${HF_TOKEN:-}" ]]; then
  log "HF token: present (length ${#HF_TOKEN}, source hidden)"
else
  log "HF token: absent (gated models will report pending)"
fi
next_step() { STEP=$((STEP+1)); emit "$1" "$2"; log "$2"; }

need_tool() {
  if ! command -v "$1" >/dev/null 2>&1; then
    if [[ "$1" == "uv" ]]; then
      log "installing uv..."
      curl -LsSf https://astral.sh/uv/install.sh | sh || die "uv install failed"
      export PATH="$HOME/.local/bin:$PATH"
    else
      die "required tool missing: $1"
    fi
  fi
}

[[ "$(uname -m)" == "arm64" ]] || die "Apple Silicon (arm64) required"

# ---- 1. prerequisites -------------------------------------------------
next_step "prereq" "前提ツールを確認しています"
need_tool uv
need_tool curl
export PATH="$HOME/.local/bin:$PATH"
# Prefer full Xcode (Metal toolchain) when present; no sudo required since
# we only export DEVELOPER_DIR for our own child processes.
if [[ -z "${DEVELOPER_DIR:-}" && -d /Applications/Xcode.app/Contents/Developer ]]; then
  export DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer
fi
if ! xcode-select -p >/dev/null 2>&1; then
  die "Xcode Command Line Tools が必要です: xcode-select --install"
fi
if xcrun -sdk macosx metal --version >/dev/null 2>&1; then
  HAVE_METAL=1
else
  HAVE_METAL=0
fi
log "metal toolchain: $HAVE_METAL"

# ---- 2. python ---------------------------------------------------------
next_step "python" "Python $PYVER を準備しています"
if ! uv python find "$PYVER" >/dev/null 2>&1; then
  uv python install "$PYVER" >>"$LOG" 2>&1 || die "python $PYVER の導入に失敗"
fi
PYBIN="$(uv python find "$PYVER")"
log "python: $PYBIN"

# ---- 3-4. venvs + deps --------------------------------------------------
mkvenv() { # name requirements-file
  local name="$1" req="$2" venv="$VENV_DIR/$1"
  if [[ $REINSTALL -eq 1 && -d "$venv" ]]; then
    log "reinstall: removing $venv"
    rm -rf "$venv"
  fi
  if [[ ! -x "$venv/bin/python" ]]; then
    log "creating venv $name"
    uv venv "$venv" --python "$PYBIN" >>"$LOG" 2>&1 || die "venv $name の作成に失敗"
  fi
  if [[ -n "$req" && -f "$req" ]]; then
    log "installing deps for $name"
    # shellcheck disable=SC2086
    uv pip install --python "$venv/bin/python" -r "$req" >>"$LOG" 2>&1 \
      || die "依存の導入に失敗 ($name)。ログ: $LOG"
  fi
  "$venv/bin/python" -c "import sys; print('venv ok:', sys.version.split()[0])" >>"$LOG" 2>&1 \
    || die "venv $name の検証に失敗"
}

next_step "venvs" "ランタイム環境を構築しています (初回は数分かかります)"
mkvenv server "$REPO_ROOT/server/requirements-server.txt"
case "$TIER" in
  normal|human|full) mkvenv trellis "$REPO_ROOT/server/requirements-torch.txt";;
esac
case "$TIER" in
  human|full) mkvenv hun-human "$REPO_ROOT/server/requirements-torch-mlx.txt";;
esac

# ---- 5. probes ------------------------------------------------------------
next_step "probes" "GPU・メモリ・容量を診断しています"
"$VENV_DIR/server/bin/python" "$SCRIPT_DIR/probe.py" --data-dir "$DATA_DIR" \
  || die "診断(probe)に失敗。ログ: $LOG"

# ---- 6. models --------------------------------------------------------------
next_step "models" "AIモデルをダウンロードしています (tier=$TIER)"
if [[ "$TIER" != "none" ]]; then
  HF_BIN="$VENV_DIR/server/bin/hf"
  if [[ ! -x "$HF_BIN" ]]; then
    HF_BIN="$VENV_DIR/server/bin/huggingface-cli"
  fi
  "$VENV_DIR/server/bin/python" "$SCRIPT_DIR/fetch_models.py" \
      --tier "$TIER" --models-dir "$MODEL_DIR" --hf-bin "$HF_BIN" \
    || die "モデルのダウンロードに失敗 (一部は後で再試行できます)"
else
  log "tier=none: model download skipped"
fi

# ---- 6b. trellis runtime code (tier normal+) --------------------------------------
next_step "runtime-trellis" "TRELLISランタイムを導入しています"
case "$TIER" in
  normal|human|full)
    "$VENV_DIR/server/bin/python" "$SCRIPT_DIR/trellis_runtime.py" \
      --data-dir "$DATA_DIR" || die "TRELLISランタイムの導入に失敗。ログ: $LOG";;
  *) log "tier=none: trellis runtime skipped";;
esac

# ---- 6c. hun + mv runtimes (tier human+) ---------------------------------------
next_step "runtime-human" "人物モードランタイムを導入しています"
case "$TIER" in
  human|full)
    "$VENV_DIR/server/bin/python" "$SCRIPT_DIR/hun_runtime.py" \
      --data-dir "$DATA_DIR" || die "Hunyuanランタイムの導入に失敗。ログ: $LOG"
    "$VENV_DIR/server/bin/python" "$SCRIPT_DIR/trellis_runtime.py" \
      --data-dir "$DATA_DIR" --venv hun-human || die "human用TRELLIS共存の導入に失敗。ログ: $LOG"
    "$VENV_DIR/server/bin/python" "$SCRIPT_DIR/mv_runtime.py" \
      --data-dir "$DATA_DIR" || die "MV-Adapterランタイムの導入に失敗。ログ: $LOG";;
  *) log "tier=$TIER: human/mv runtimes skipped";;
esac

# ---- 7. settings -------------------------------------------------------------
next_step "settings" "設定を保存しています"
mkdir -p "$OUTPUT_DIR" || die "出力フォルダを作成できません: $OUTPUT_DIR"
"$VENV_DIR/server/bin/python" - "$DATA_DIR" "$OUTPUT_DIR" <<'EOF' >>"$LOG" 2>&1 || exit 1
import json, sys
data_dir, out = sys.argv[1], sys.argv[2]
p = f"{data_dir}/settings.json"
cfg = {}
try:
    with open(p, encoding="utf-8") as f:
        cfg = json.load(f)
except (OSError, ValueError):
    pass
cfg["output_dir"] = out
with open(p, "w", encoding="utf-8") as f:
    json.dump(cfg, f, indent=2, ensure_ascii=False)
print("settings ok:", out)
EOF

# ---- 8. verify ------------------------------------------------------------------
next_step "verify" "最終検証しています"
[[ -x "$VENV_DIR/server/bin/python" ]] || die "server venv が不完全です"
[[ -f "$DATA_DIR/settings.json" ]] || die "settings.json がありません"
[[ -f "$DATA_DIR/probes.json" ]] || die "probes.json がありません"

STEP=$NSTEPS
emit "done" "セットアップが完了しました"
echo '{"type":"done","ok":true}'
