#!/bin/bash
# Assemble 3DFM.app from the SwiftPM release binary + bundled payload,
# then ad-hoc sign it. Output: dist/3DFM.app
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DIST="$ROOT/dist"
APP="$DIST/3DFM.app"

echo "[build] swift build -c release..."
swift build --package-path "$ROOT/app" -c release || exit 1
BIN="$ROOT/app/.build/release/ThreeDFM"
[[ -x "$BIN" ]] || { echo "binary missing"; exit 1; }

echo "[build] assembling $APP..."
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BIN" "$APP/Contents/MacOS/ThreeDFM"
cp "$ROOT/scripts/Info.plist" "$APP/Contents/Info.plist"
# Payload the app needs at runtime (server source, CLI, setup scripts)
mkdir -p "$APP/Contents/Resources/server" "$APP/Contents/Resources/cli" \
         "$APP/Contents/Resources/scripts"
cp -R "$ROOT/server/src" "$APP/Contents/Resources/server/src"
cp "$ROOT/server"/requirements-*.txt "$APP/Contents/Resources/server/"
cp "$ROOT/cli/3dfm" "$APP/Contents/Resources/cli/3dfm"
chmod +x "$APP/Contents/Resources/cli/3dfm"
cp "$ROOT/scripts/setup.sh" "$ROOT/scripts/probe.py" \
   "$ROOT/scripts/fetch_models.py" "$ROOT/scripts/trellis_runtime.py" \
   "$ROOT/scripts/hun_runtime.py" "$ROOT/scripts/mv_runtime.py" \
   "$APP/Contents/Resources/scripts/"
chmod +x "$APP/Contents/Resources/scripts/setup.sh"

echo "[build] ad-hoc sign..."
codesign --force -s - --deep "$APP" || exit 1
codesign -v "$APP" && echo "[build] sign ok: $APP"
