#!/bin/bash
# Build the ad-hoc DMG: dist/3DFM-<version>-adHoc.dmg
# Ad-hoc signed (codesign -s -): Gatekeeper will block double-click open;
# users right-click -> Open once. That is the agreed deliverable.
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DIST="$ROOT/dist"
STAGE="$DIST/dmg-stage"
VER="$(python3 -c "import re;print(re.search(r'__version__\s*=\s*\"([^\"]+)\"', open('$ROOT/server/src/fm3d/__init__.py').read()).group(1))")"
DMG="$DIST/3DFM-$VER-adHoc.dmg"

bash "$ROOT/scripts/build-app.sh" || exit 1

echo "[dmg] staging..."
rm -rf "$STAGE"
mkdir -p "$STAGE"
cp -R "$DIST/3DFM.app" "$STAGE/"
cat > "$STAGE/README-adHoc.txt" <<EOF
3DFM $VER (ad-hoc build)
=========================
ad-hoc署名のため、初回は右クリック ->「開く」で起動してください。

使い方:
  1. 3DFM.app を /Applications にコピー (任意)
  2. 起動するとメニューバーに cube アイコンが出ます
  3. 初回はセットアップウィザードが開きます:
     出力フォルダを選んで「セットアップ開始」を押すだけです。
     (ランタイム + AIモデルの自動導入。初回は数十分かかります)
  4. 以降はメニューバーから生成・キュー・進捗確認ができます。
  5. CLI (同梱): 3DFM.app/Contents/Resources/cli/3dfm
     GUIと CLI は同じローカルサーバーを使うため操作は等価です。

Dockには表示されません (メニューバー常駐)。
終了はメニューバーの「終了」から。
EOF

echo "[dmg] creating $DMG..."
rm -f "$DMG"
hdiutil create -volname "3DFM" -srcfolder "$STAGE" -ov -format UDZO "$DMG" || exit 1
echo "[dmg] ok: $DMG"
ls -la "$DMG"
