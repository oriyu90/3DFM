# 3DFM.md — 保守メモ (公開サイトには載せない / internal only)

> Rule 6 用: 表 (README/紹介サイト) に出すべきでない次回以降の開発向けメモ。
> 公開Webサイトには記載しないこと。

## 現在の実装 (v0.3.1, 2026-10-10)

- v0.3.0のストレージ配置設定を継承 (data/modelsフォルダ変更、詳細は下記旧記参照)。
- 32GB+ Apple Silicon保証 (本版の主目的):
  - `POST /jobs` に `_validate_job_spec()` を追加 (日英バイリンガル422)。
    texture/pipeline/steps/mv/rembg/decimationを検証。total<32GBで
    `512->1536`/`4096`を拒否、total<48GBで `1536+4096` 併用とMV `768px` を
    拒否 (不明時はfail-open)。32GB機は `512/512->1024 + 2048 + MV512` が
    必ず通り、heavy指定はOOM前に422で案内。
  - MV合成のメモリ安全: 32GB級では `mv_resolution` を512にclamp、
    `enable_vae_tiling`/`enable_sequential_cpu_offload` をbest-effortで
    有効化。`SPARSE_CONV_BACKEND` のsetdefault残留バグを修正
    (失敗時は `none` に代入)。`_bake_trellis` はOSErrorもKDTree fallback。
  - `PUT /settings` の `models_dir` にidle gate追加 (409、moveと同一)。
    `require_dir` は実パス付き日英メッセージ。
    `DataDirs.ensure()` は `runtimes/` も作成。`worker` はjob_id traversal拒否。
    `manager._diagnose`/`cancel`/`retry` は日英化。`settings.validate` 全面日英化。
    `/health` に `port` 追加、`server.port` ファイルで実ポート可視化。
  - `probe.py`: `flex_gemm`/`o_voxel`/`trimesh`/`scipy`/`cKDTree`/`SDPA` を
    正しくprobe (旧 `mtlgemm` 名は互換alias)。32GB未満の警告を2段階化。
  - `setup.sh`: `tier=none` はdisk need 0GB、32GB+確認ログ追加。
  - `prune_models.py`: `--data-dir` をenv/settingsから自動解決、
    venv皆無時はguard=False (bin重複を保持、安全側)。
  - Swift UI全面日英併記 (menu/queue/detail/NewJob/General/Models/Setup)。
    NewJob/Generalに32GB+/48GB+注意表示 (blockせず警告、serverが422で最終防衛)。
    `Backend` の主要エラー・通知も日英化。
- バージョン: `server/src/fm3d/__init__.py` が正 (`0.3.1`)。
  `scripts/Info.plist` も同期。build-app.shがバンドル時にstamp。
- 検証: `test_units.py` 35件 (spec validation 4件追加)、swift release build、
  TestClient E2E (status/submit/422/idle-409)、probe/prune dry-run。
  実機M1 Max 64GBで確認。32GB logicはtotal_bytes mockで検証。

## 旧実装メモ (v0.3.0, 2026-10-07)

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

## バージョン履歴

- v0.1.0 (2026-09-27): P0骨格 + ad-hoc dmg
- v0.2.0 (2026-09-28): idle memory, 40GB+ setup, CLI/GUI parity
- v0.2.1 (2026-10-06): idle memory + reliability hardening
- v0.3.0 (2026-10-07): ストレージ配置設定 (data/modelsフォルダ変更)
- v0.3.1 (2026-10-10): 32GB+保証 (spec検証・メモリ安全・日英完全対応・port可視化)

## 次回更新すべき箇所 (TODO)

1. 大容量移動の進捗表示: 現状はbusyスピナーのみ。60GB移動で数十分固まる。
   次版で `POST /storage/models/move` をバックグラウンド化し、
   `GET /storage` に `move_state` (progress%) を追加すること。
2. 外付け切断時の投入エラーは `require_dir` に実パス表示済み (v0.3.1)。
   残りは `backends` の汎用RuntimeError全般の日英化率向上 (現状は主要経路のみ)。
3. data移動後の旧フォルダ: 現状は空dirが残る (entries移動後)。次版で
   空になった旧rootの `.moved_to` マーカーを残すと、旧CLIの発見が容易。
4. テスト: `test_units.py` 35件。E2E (実機M-series, TRELLIS/Hunyuan) は
   `test` backendで `submit→422/409` まで確認済み。実重みでの
   `submit→done` は未再実行 (storage変更が推論に影響しないことはunit+APIで
   確認済みだが、リリース前に1パターンは実機推論を推奨)。
5. 署名: ad-hocのまま (Rule 8)。Developer ID移行時は build脚本に
   notarytool/stapler追加 + 本ファイルとREADME/サイトの記述更新。

## 注意 (互換・安全)

- `settings.json` に `models_dir` が無い旧版は `""` (既定) 扱いで完全互換。
- `PUT /settings` の `models_dir` は v0.3.1でidle gate追加 (409)。
  重検証 (書込・容量) は move endpoint側。直接PUTで未マウントVolumeを
  指定しても軽検証で拒否。
- `FM3D_MODELS_DIR` envが残留すると revert後も上書きする。
  move/revert/PUTでenvを同期済みだが、手動exportしている環境では
  `unset FM3D_MODELS_DIR` が必要。トラブル時は `3dfm storage status` の
  `configured_models_dir` と `models_dir` を比較すること。
- CLI `set-data-dir` はpointerを実homeに書く。テスト時は後始末
  (`rm ~/Library/Application\ Support/3DFM.location`) を忘れずに。
- 32GB保証の互換: 旧クライアントのheavy指定 (1536/4096/768) は
  32GB未満/48GB未満で422になる (以前はworker SIGKILLでfailed)。
  デフォルト (512->1024 + 2048 + MV512) は全RAMで通過。意図的な変更。
- キーストア: macOSアプリのため該当なし (APK Rule 7 対象外)。

## 連絡・公開

- Discord: https://discord.gg/x7KXhNTD8M
- X: https://x.com/InovateofRIZI
- 紹介サイト正規URL: https://studio-rizi.pages.dev/projects/3dfm/
  (中央管理: oriyu90/studio-rizi `website/projects/3dfm/`)
