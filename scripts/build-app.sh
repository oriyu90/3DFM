#!/bin/bash
# Assemble 3DFM.app from the SwiftPM release binary + bundled payload,
# then ad-hoc sign it. Output: dist/3DFM.app
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DIST="$ROOT/dist"
APP="$DIST/3DFM.app"
VER="$(python3 -c "import re;print(re.search(r'__version__\s*=\s*\"([^\"]+)\"', open('$ROOT/server/src/fm3d/__init__.py').read()).group(1))")"

echo "[build] swift build -c release..."
swift build --package-path "$ROOT/app" -c release || exit 1
BIN="$ROOT/app/.build/release/ThreeDFM"
[[ -x "$BIN" ]] || { echo "binary missing"; exit 1; }

echo "[build] assembling $APP..."
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BIN" "$APP/Contents/MacOS/ThreeDFM"
cp "$ROOT/scripts/Info.plist" "$APP/Contents/Info.plist"
# Stamp the release version so the bundle never reports a stale one.
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $VER" \
  "$APP/Contents/Info.plist" 2>/dev/null || \
  python3 -c "import plistlib; p='$APP/Contents/Info.plist'; d=plistlib.load(open(p,'rb')); d['CFBundleShortVersionString']='$VER'; plistlib.dump(d, open(p,'wb'))"
# Payload the app needs at runtime (server source, CLI, setup scripts)
mkdir -p "$APP/Contents/Resources/server" "$APP/Contents/Resources/cli" \
         "$APP/Contents/Resources/scripts"
cp -R "$ROOT/server/src" "$APP/Contents/Resources/server/src"
cp "$ROOT/server"/requirements-*.txt "$APP/Contents/Resources/server/"
cp "$ROOT/cli/3dfm" "$APP/Contents/Resources/cli/3dfm"
chmod +x "$APP/Contents/Resources/cli/3dfm"
cp "$ROOT/scripts/setup.sh" "$ROOT/scripts/probe.py" \
   "$ROOT/scripts/fetch_models.py" "$ROOT/scripts/prune_models.py" \
   "$ROOT/scripts/trellis_runtime.py" \
   "$ROOT/scripts/hun_runtime.py" "$ROOT/scripts/mv_runtime.py" \
   "$APP/Contents/Resources/scripts/"
chmod +x "$APP/Contents/Resources/scripts/setup.sh"

echo "[build] ad-hoc sign..."
codesign --force -s - --deep "$APP" || exit 1
codesign -v "$APP" && echo "[build] sign ok: $APP"
