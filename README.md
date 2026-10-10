# 3DFM — メニューバー常駐 3Dモデル生成アプリ / Menu-bar 3D model generator

Author: Yuki_Orita
License: MIT (see LICENSE)

Apple Silicon Mac (M1+) で画像から3Dモデル(GLB)を生成するメニューバー常駐アプリです。
普通モード (TRELLIS.2) と人物モード (6視点 + Hunyuan3D 2.1 PBR + MV-Adapter合成) に対応し、
ローカルサーバー + SQLiteキュー + CLI (`3dfm`) で GUI=CLI パリティを保ちます。

A menu-bar 3D model generator for Apple Silicon Macs. Normal mode (TRELLIS.2)
and human mode (6 views + Hunyuan3D 2.1 PBR + MV-Adapter synthesis). Local
server + SQLite queue + CLI (`3dfm`) with full GUI=CLI parity.

- 日本語 / English 対応UI (設定・メッセージは日英併記 / bilingual JP+EN UI)
- 紹介サイト / Website: https://studio-rizi.pages.dev/projects/3dfm/
- リポジトリ / Repository: https://github.com/oriyu90/3DFM
- 開発者サイト / Developer: https://studio-rizi.pages.dev/
- 連絡 / Contact: https://discord.gg/x7KXhNTD8M / https://x.com/InovateofRIZI

## 動作環境 / Requirements

- Apple Silicon (arm64), macOS 14+ (26+ 推奨 / recommended, Metal MSL 4.0)
- メモリ 16GB最低 / 16 GB minimum, 24GB+推奨 / recommended, 32GB+保証 / 32GB+ verified (v0.3.1), 40GB+快適 (人物 / human)
  - 32GB+ の全Apple Silicon Macでセットアップ完走・通常生成を確認 / Verified setup + standard generation on all 32GB+ Apple Silicon Macs
  - 512 / 512->1024 + 2048 + MV512 は全RAMで動作 / Standard settings work on all RAM sizes
  - 512->1536 / 4096 は32GB+必須、1536+4096併用とMV768は48GB+必須 (不足時は日英422で案内、OOM前に拒否) / 1536/4096 need 32GB+, combo + MV768 need 48GB+ (bilingual 422, never OOM)
- 空き容量: 普通 ~20GB / normal, 人物フル ~60GB / human-full (+作業領域 / work)

## 署名について / Signing (Rule 8)

このアプリは **ad-hoc署名 (`codesign --sign -`) の未公証ビルド**です。
Developer ID署名・公証は行っていません。初回起動時は「右クリック→開く」、
または「システム設定→プライバシーとセキュリティ」からの許可が必要です。

This app is distributed as an **ad-hoc-signed (`codesign --sign -`), unnotarized
build**. No Developer ID signature / notarization. On first launch, use
Right-click → Open, or allow it in System Settings → Privacy & Security.

## セットアップ / Setup

1. `dist/3DFM-*.dmg` を開き `3DFM.app` を起動 / Open the dmg and launch 3DFM.app
2. セットアップウィザードで出力フォルダ・モデルtierを選択 / Choose output folder + model tier
3. ストレージ詳細でデータ/モデルフォルダを変更可 (既定: `~/Library/Application Support/3DFM`, モデル既定 `<data>/models`)
   / Storage details can change data/models folders (defaults as above)
4. Hugging Faceトークン (ゲート付きモデル用・任意) / HF token (gated models, optional)

```bash
# 同内容をCLIで / Same via CLI
cli/3dfm serve --headless
cli/3dfm status
cli/3dfm storage status
cli/3dfm models ensure --tier human
```

## 保存場所 / Storage locations (v0.3.0+)

アプリ内設定「ストレージ / Storage」とCLIで変更できます。
GUI and CLI can change these (Settings > Storage / `3dfm storage ...`).

| 用途 / Purpose | 既定 / Default | 設定キー / Key | 移動 / Move |
|---|---|---|---|
| データフォルダ (venvs・ランタイム・ジョブDB) / Data (venvs, runtimes, job DB) | `~/Library/Application Support/3DFM` | `customDataDir` (UserDefaults + `3DFM.location` pointer) + `FM3D_DATA_DIR` | オフライン移動 (サーバー停止→移動→再起動) / Offline (stop → move → restart). `3dfm storage set-data-dir <path> --move` |
| モデルフォルダ (AI重み ~16–60GB) / Models (weights) | `<data>/models` | `models_dir` (`""`=既定 / default) + `FM3D_MODELS_DIR` | サーバーが移動 (キュー空き必須) / Server-side move (queue must be idle). `3dfm storage move-models <path>` |
| 出力フォルダ (GLB成果物) / Output (GLB) | `~/Pictures/3DFM` | `output_dir` | ポインタのみ (ファイル移動なし) / Pointer only |

- `venvs/` と `runtimes/` は常にデータフォルダ配下 (venvは絶対パスを含むため単独移動不可)。
  venvs/ and runtimes/ always stay under the data folder (venvs embed absolute paths).
- 外付け (`/Volumes/...`) はマウント必須。未マウント時は拒否します (起動ディスクへの誤作成防止)。
  External `/Volumes/...` must be mounted; unmounted targets are rejected.
- 移動は検証後・サイズ確認後に実行し、コピー検証後に元を削除 (クラッシュ安全)。
  Moves verify + size-check, deleting sources only after verified copies.
- メモリ安全: サイズ集計はstatのみ、移動はストリーミング (重みをRAMに読まない)。
  Memory-safe: sizes via stat only; moves stream without RAM-loading weights.

## CLI (GUIと1:1 / GUI=CLI parity)

```
3dfm status
3dfm submit --mode normal -i img.png --texture-size 2048 --name sofa-01
3dfm submit --mode human -i f.png -i r45.png -i r90.png -i b.png -i l45.png -i l90.png
3dfm submit --mode human -i front.png --synthesize-views
3dfm list [--state queued|running|done|failed]
3dfm show <job-id> [--follow]
3dfm cancel <job-id> [--force]
3dfm retry <job-id>
3dfm open <job-id>
3dfm models ensure | status
3dfm settings get|set <key> <value>
3dfm storage status|move-models|reset-models|validate-data-dir|set-data-dir|reset-data-dir
3dfm serve --headless
```

## 開発 / Development

```bash
PYTHONPATH=server/src python3 server/tests/test_units.py
swift build --package-path app -c release
scripts/build-app.sh   # dist/3DFM.app (ad-hoc署名 / ad-hoc sign)
scripts/build-dmg.sh   # dist/3DFM-*.dmg
```

詳細仕様は `設計書・仕様書.md`、手順は `計画書.md`、保守メモは `3DFM.md` を参照。
See `設計書・仕様書.md` for spec, `計画書.md` for plan, `3DFM.md` for maintenance notes.
