# 実験資産 v0.2 実装計画 (dry-run 接続 + CLI 単独での L5 到達)

作成: 2026-07-07 / 計画: Claude Fable 5 / 実装・テスト: Claude Opus / 最終確認: Claude Fable 5

前提: HEAD = f5d87cb (v2.25.0)、テスト 1868 passed / 28 skipped / 0 failed。
仕様の正本: docs/experiment_asset_schema_v0.md、v0.1 実装: docs/asset_v01_plan.md。

**注意: 別セッションが export.py の結果抽出修正（レシピジョブ対応）を並行作業中。
本計画は tools/export.py に触れないこと**（builder/cli/asset 配下と docs のみ）。

## ゴール

v0.1 の残ギャップ「L5 は API 経由でのみ到達可能」を解消し、
**CLI だけで L5 資産を作れる**ようにする。

1. `asset export --dry-run-now` — export 時に builder 自身が同梱レシピの
   dry-run（コンパイル検証）を実行し、結果を asset.yaml の dry_run に記録
2. `asset export --meta FILE` — conditions / hazards / expected_results / sample を
   YAML ファイルで一括指定（v0.1 で CLI 未対応だった部分）

## 設計判断（経緯）

dry_run 記録の供給源として「UI M3 の dry-run 実行記録を永続化して接続」も検討したが、
**UI は state DB read-only の絶対制約**があり、別の記録ストアを増やすのは複雑さに
見合わない。export 時に梱包対象そのものを検証する方が、
「この資産に入っているレシピは検証済み」という意味論としても正確
（UI で過去に dry-run した内容と梱包内容が乖離するリスクがない）。

## 仕様

### builder.py への追加

```python
def build_asset(..., dry_run_now: bool = False, meta: dict | None = None) -> dict
```

- `dry_run_now=True` のとき:
  1. 同梱する装置定義（instrument/ に入れるもの）から `InstrumentDefinition` をパース
  2. job_record の recipe 名のレシピを取り出し、job の解決済み parameters で
     `recipe_to_plan(recipe, variables, primary_resource=resource_name)` を実行
  3. 併せて `validate_instrument_file`（非 strict）で定義を検証
  4. 成功 → `dry_run = {"performed_at": <now ISO8601>, "ok": True,
     "method": "recipe_to_plan+validate@export",
     "runtime": "lab-executor-mcp <version>", "step_count": <int>}`
  5. 失敗（コンパイル例外 / 検証 errors / 定義 or レシピ不在）→ `ok: False` +
     `"error": <一行要約>`。**export 自体は失敗させない**（L5 に届かないだけ）
- `meta` dict は asset.yaml の conditions / hazards / expected_results / sample に
  マージ（個別引数 conditions= 等より meta が優先。両方指定時は meta 勝ち）
- 依存方向: asset/builder.py → recipe_executor / registry の import は
  既に v0.1 で checker が validate_instrument_file を使っており問題なし

### cli.py への追加

- `asset export` に `--dry-run-now`（store_true）と `--meta PATH` を追加
- `--meta` の YAML はトップレベルに conditions / hazards / expected_results / sample
  の任意サブセットを持つ。パース失敗・未知のトップレベルキーは明確なエラーで exit 1
  （未知キーを黙って無視しない — 誤記で L5 を逃すのを防ぐ）
- 人間向け出力に dry_run の結果 1 行を追加（ok/false/未実施）

### meta ファイル例（docs/asset_usage.md に記載）

```yaml
conditions:
  calibration: "2026-06 校正証明書 #1234"
  environment: "23±1°C, 45%RH"
hazards:
  none_declared: true
expected_results:
  - {command: measure_voltage, value_min: -0.1, value_max: 0.1}
sample:
  uuid: null
  metadata: {description: "無負荷 (出力OFF基線)"}
```

## テスト (tests/test_asset_v02.py)

fixture は test_asset_v01.py のものを再利用（import またはコピー。requires 付き
レシピ + strict pass する mock 定義が必要 — v0.1 テストで用意済みのものを確認して使う）。

| テスト | 検証 |
|---|---|
| test_dry_run_now_ok | --dry-run-now で dry_run.ok=True、performed_at/step_count 記録 |
| test_dry_run_now_records_failure | 壊れたレシピ参照で ok=False + error、export は成功 |
| test_meta_file_merge | meta YAML の 4 セクションが asset.yaml に反映 |
| test_meta_unknown_key_rejected | 未知トップレベルキーで exit 1 / エラー |
| test_cli_l5_end_to_end | **CLI のみ**（export --dry-run-now --meta ...）で check が L5 を返す |
| test_dry_run_not_requested_unchanged | フラグ無しの asset.yaml が v0.1 と同形（dry_run は null 系のまま） |

リグレッション: tests/test_asset_v01.py（14件）→ 全スイート
（1868+新規 passed / 28 skipped / 0 failed。**export.py に触れないので
export 系 56 件が不変であることも確認**）。

## バージョン・ドキュメント

- v2.26.0（pyproject + __init__ + CHANGELOG 既存書式）
- docs/asset_usage.md に --dry-run-now / --meta と L5 到達手順を追記
- docs/experiment_asset_schema_v0.md の「dry_run.ok は builder が自動記入せず」注記を更新

## 制約（v0.1 と同じ + 追加1）

1. MCP ツール面 50 不変 / 2. tools/export.py に触れない（並行作業との衝突回避）/
3. levels.py の判定基準は変更しない / 4. LF / 5. git commit しない /
6. テスト完了を確認してから報告
