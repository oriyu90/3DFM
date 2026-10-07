#!/bin/bash
# Build the ad-hoc DMG: dist/3DFM-<version>-adHoc.dmg (+ .sha256)
# Ad-hoc signed (codesign -s -), unnotarized: Gatekeeper blocks a plain
# double-click; users Right-click -> Open once (documented in README +
# release notes). That is the agreed deliverable (Rule 8: no Developer ID
# claims).
#
# Distribution-friendly layout:
#   3DFM.app + /Applications symlink (drag-to-install) + bilingual README.
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DIST="$ROOT/dist"
STAGE="$DIST/dmg-stage"
VER="$(python3 -c "import re;print(re.search(r'__version__\s*=\s*\"([^\"]+)\"', open('$ROOT/server/src/fm3d/__init__.py').read()).group(1))")"
DMG="$DIST/3DFM-$VER-adHoc.dmg"
SHA="$DMG.sha256"

bash "$ROOT/scripts/build-app.sh" || exit 1

echo "[dmg] staging..."
rm -rf "$STAGE"
mkdir -p "$STAGE"
cp -R "$DIST/3DFM.app" "$STAGE/"
# Standard drag-to-install target.
ln -s /Applications "$STAGE/Applications"
cat > "$STAGE/README-first.txt" <<EOF
3DFM $VER (ad-hoc build / ad-hocビルド)
========================================
EN: Ad-hoc-signed and unnotarized. On first launch: Right-click 3DFM.app -> Open, then click Open. Or allow it in System Settings -> Privacy & Security.
JA: ad-hoc署名・未公証ビルドです。初回は 3DFM.app を右クリック ->「開く」->「開く」。または「システム設定 -> プライバシーとセキュリティ」で許可してください。

Install / インストール:
  1. Drag 3DFM.app to Applications (or run it from the DMG to try).
     3DFM.app を Applications にドラッグ (試すだけならDMGのままでも可)。
  2. Launch: a cube icon appears in the menu bar (not the Dock).
     起動するとメニューバーに cube アイコンが出ます (Dockには出ません)。
  3. First-run Setup wizard: pick an output folder + model tier, press Start.
     初回セットアップ: 出力フォルダとモデルtierを選び「セットアップ開始」。
     (Runtimes + AI models auto-install. First run may take tens of minutes.)
  4. New Generation from the menu bar or the main window. Track queue/progress/logs there.
     以降はメニューバーやメインウィンドウから生成・キュー・進捗確認ができます。
  5. Bundled CLI (same server, same operations):
     同梱CLI (同一サーバー・同一操作):
       3DFM.app/Contents/Resources/cli/3dfm status
       3DFM.app/Contents/Resources/cli/3dfm storage status

Storage / 保存場所 (v0.3.0+):
  Settings > Storage (設定>ストレージ) can move the data folder and the
  models folder (~16-60GB) to an external SSD. External volumes must be mounted.
  CLI: 3dfm storage move-models /Volumes/SSD/3DFM-models

Requirements / 動作環境:
  Apple Silicon (M1+), macOS 14+ (26+ recommended),
  16GB min / 24GB+ recommended / 40GB+ for human mode,
  disk ~20GB (normal) / ~60GB (human-full).

Models / モデル:
  TRELLIS.2 MIT / MV-Adapter Apache-2.0 / RMBG-2.0 CC BY-NC 4.0 (non-commercial, 非商用) /
  Hunyuan3D Community License / SDXL Open RAIL++-M.
  Commercial use needs care (esp. RMBG-2.0/Hunyuan). Details in Settings > Models.
  商用利用は要注意 (特にRMBG-2.0/Hunyuan)。詳細は設定>モデル。

Support / 連絡:
  https://github.com/oriyu90/3DFM
  https://discord.gg/x7KXhNTD8M / https://x.com/InovateofRIZI
EOF
# Keep the old filename as an alias for existing docs/links.
ln -sf README-first.txt "$STAGE/README-adHoc.txt"

echo "[dmg] creating $DMG..."
rm -f "$DMG" "$SHA"
hdiutil create -volname "3DFM $VER" -srcfolder "$STAGE" -ov -format UDZO "$DMG" || exit 1
echo "[dmg] verifying..."
hdiutil verify "$DMG" || exit 1
(cd "$DIST" && shasum -a 256 "$(basename "$DMG")" > "$(basename "$SHA")")
echo "[dmg] ok: $DMG"
ls -la "$DMG" "$SHA"
cat "$SHA"
