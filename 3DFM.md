# 3DFM.md — 保守メモ (公開サイトには載せない / internal only)

> Rule 6 用: 表 (README/紹介サイト) に出すべきでない次回以降の開発向けメモ。
> 公開Webサイトには記載しないこと。

## 現在の実装 (v0.3.0, 2026-10-07)

- ストレージ配置をアプリ内設定で変更可 (要望対応):
  - データフォルダ: UserDefaults `customDataDir` + pointer file
    `~/Library/Application Support/3DFM.location` + env `FM3D_DATA_DIR`
    (優先度 env > plist > pointer > default)。Swift `Paths.dataDir` と
    Python `resolve_data_dir()` が同一解決。オフライン移動のみ
    (サーバー停止→FileManager/shutilで移動→pointer保存→再起動)。
  - モデルフォルダ: `settings.json` `models_dir` (`""`=既定) + env
    `FM3D_MODELS_DIR` (優先度 env > settings > `<data>/models`)。
    `DataDirs(models_override)` + `build_data_dirs()`。
    サーバーが `POST /storage/models/move` で移動 (キュー空き必須)。
    `PUT /settings` はポインタのみ切替 (ensureで再DL可)。
  - venvs/runtimesはデータフォルダ配下に固定 (venvが絶対パスを含むため)。
- 新規API: `GET /storage`, `POST /storage/data/validate`,
  `POST /storage/models/move`。CLI: `3dfm storage ...` 6種。
  `setup.sh --models-dir`, `probe.py --models-dir` 対応。
- バリデーション: 絶対パス必須、`..`拒否、システムルート拒否、
  home直下拒否、予約フォルダ (`jobs/logs/venvs/runtimes`) 重複拒否、
  `/Volumes` 未マウント拒否、日英バイリンガルメッセージ。
- 移動安全: `move_dir_contents_safe()` (同ボリュームはrename、
  異ボリュームはcopy2+サイズ検証後削除、結合拒否、ネスト拒否、
  statのみでRAM不使用)。`check_queue_idle()` でrunning/queuedを拒否。
  `is_server_live_for_root()` でdata移動時の起動中を拒否。
- UIは日英併記 (Settings > Storage新設、SetupWizardにStorage details、
  Models paneにStorage案内)。既存タブ・キー互換維持。
- バージョン: `server/src/fm3d/__init__.py` が正 (`0.3.0`)。
  `scripts/Info.plist` も同期。build-app.shがバンドル時にstamp。

## バージョン履歴

- v0.1.0 (2026-09-27): P0骨格 + ad-hoc dmg
- v0.2.0 (2026-09-28): idle memory, 40GB+ setup, CLI/GUI parity
- v0.2.1 (2026-10-06): idle memory + reliability hardening
- v0.3.0 (2026-10-07): ストレージ配置設定 (data/modelsフォルダ変更)

## 次回更新すべき箇所 (TODO)

1. 大容量移動の進捗表示: 現状はbusyスピナーのみ。60GB移動で数十分固まる。
   次版で `POST /storage/models/move` をバックグラウンド化し、
   `GET /storage` に `move_state` (progress%) を追加すること。
2. `prune_models.py` は `--models-dir` のみ対応。カスタムmodelsでも
   `--data-dir` なしで動くが、venvsガード (`_safetensors_everywhere`) が
   既定dataのvenvsを見る。カスタムdata時は `--data-dir` を渡す運用を
   ドキュメント化済みだが、自動解決 (settings/env) に寄せると親切。
3. 外付け切断時の起動: modelsが未マウントだと `models/status` が全欠落に
   なる (正しい) が、ジョブ投入時のエラーメッセージが汎用。
   `backends.require_dir` のhintに現在のmodels_dir実パスを含めると
   ユーザーが迷わない (現状は名前のみ)。
4. data移動後の旧フォルダ: 現状は空dirが残る (entries移動後)。次版で
   空になった旧rootの `.moved_to` マーカーを残すと、旧CLIの発見が容易。
5. テスト: `test_units.py` 29件。E2E (実機M-series, TRELLIS/Hunyuan) は
   未再実行 (storage変更が推論に影響しないことはunit+APIで確認済みだが、
   リリース前に1パターンは `test` backendで `submit→done` を推奨)。
6. 署名: ad-hocのまま (Rule 8)。Developer ID移行時は build脚本に
   notarytool/stapler追加 + 本ファイルとREADME/サイトの記述更新。

## 注意 (互換・安全)

- `settings.json` に `models_dir` が無い旧版は `""` (既定) 扱いで完全互換。
- `PUT /settings` の `models_dir` は軽検証のみ。重検証 (書込・容量) は
  move endpoint側。直接PUTで未マウントVolumeを指定しても軽検証で拒否。
- `FM3D_MODELS_DIR` envが残留すると revert後も上書きする。
  move/revert/PUTでenvを同期済みだが、手動exportしている環境では
  `unset FM3D_MODELS_DIR` が必要。トラブル時は `3dfm storage status` の
  `configured_models_dir` と `models_dir` を比較すること。
- CLI `set-data-dir` はpointerを実homeに書く。テスト時は後始末
  (`rm ~/Library/Application\ Support/3DFM.location`) を忘れずに。
- キーストア: macOSアプリのため該当なし (APK Rule 7 対象外)。

## 連絡・公開

- Discord: https://discord.gg/x7KXhNTD8M
- X: https://x.com/InovateofRIZI
- 紹介サイト正規URL: https://studio-rizi.pages.dev/projects/3dfm/
  (中央管理: oriyu90/studio-rizi `website/projects/3dfm/`)
