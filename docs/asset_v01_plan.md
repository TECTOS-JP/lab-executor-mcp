# 実験資産 v0.1 実装計画 (asset export / check + L4/L5 基盤)

作成: 2026-07-06 / 計画: Claude Fable 5 / 実装・テスト: Claude Opus / 最終確認: Claude Fable 5

仕様の正本: `docs/experiment_asset_schema_v0.md`（スキーマ v0.1。本計画はその実装）。
前提: HEAD = 80e30fa (v2.24.0)、テスト 1854 passed / 28 skipped / 0 failed。

## ゴール

1. **Phase A**: 完了 Job から実験資産 zip を生成する `lab-executor asset export` と、
   資産の独立可用性レベルを機械判定する `lab-executor asset check`（L0〜L3）
2. **Phase B**: L4/L5 判定の基盤 — レシピの `requires:`（capability 要件宣言）、
   asset.yaml の `hazards:` / `expected_results:` / `sample:` セクション、
   check の L4/L5 判定

## 絶対制約

1. **MCP ツール面 50 個は不変** — asset は **CLI サブコマンドのみ**。MCP ツールは追加しない
   （市場流通は外部行為であり、AI エージェント向けツール化は将来判断）
2. `RecipeDefinition` への `requires:` 追加は **optional フィールド**（既存 YAML の
   検証結果が 1 件も変わらないこと）
3. 判定ロジックは purity を保つ（`levels.py` は I/O しない純関数群。checker が I/O 担当）
4. 既存の export bundle（`export_experiment_bundle`、bundle_version 1.0）は変更しない。
   asset はそれを **内包** する上位パッケージ
5. LF 改行 / git commit しない / テスト完了を確認してから報告（3分無出力はハング切り分け）

## 新規ファイル構成

```
src/lab_executor/asset/
├── __init__.py
├── manifest.py     # AssetManifest (pydantic) = asset.yaml のスキーマ
├── builder.py      # build_asset(...) -> zip 生成
├── checker.py      # check_asset(zip_path) -> CheckReport (I/O + levels 呼び出し)
├── levels.py       # L0〜L5 判定の純関数群 (dict in -> 判定結果 out)
└── capability.py   # CapabilityRequirements + match_capabilities()
tests/test_asset_v01.py
docs/asset_usage.md  # 利用者向け (export→check→レベルの読み方)
```

cli.py: `asset` サブコマンド群（`export` / `check`）を追加。

## 資産 zip レイアウト (asset_version 0.1)

```
<asset_id>.asset.zip
├── asset.yaml                 # マニフェスト (下記)
├── bundle/                    # export_experiment_bundle の全内容を展開して格納
│   ├── manifest.json / job_record.json / job_summary.json
│   ├── timeline.jsonl / results.jsonl / results.csv / (audit.jsonl)
├── recipe/
│   └── recipe.yaml            # 使用レシピの定義断片 + 解決済みパラメータ
├── instrument/
│   ├── <slug>.yaml            # 装置定義 (実行時に使ったもの)
│   └── _system.yaml           # あれば
└── analysis/
    └── README.md              # 解析手順 (L3 要件。builder は --analysis で受け取る)
```

## asset.yaml (manifest.py の AssetManifest)

`docs/experiment_asset_schema_v0.md` の定義に従う。v0.1 実装での確定形:

```yaml
asset_version: "0.1"
asset_id: <uuid4>
level_declared: <int 0-5>
level_verified: null           # check が書き換えるのではなく、check レポートに出す
title: <str>
created_at: <ISO8601>
license: <str, default "UNLICENSED">
provenance:
  producer: <str, default "">
  runtime: "lab-executor-mcp <version>"
  git_commit: <str | null>     # --git-commit で受け取る (M3 の保存 commit 等)
conditions:                    # L2 用。無い項目は "not_recorded" を明示
  calibration: <str | "not_recorded">
  environment: <str | "not_recorded">
sample:                        # Phase B。省略可
  uuid: <uuid4 | null>
  metadata: {<自由 key-value>}
hazards:                       # Phase B。L5 用
  none_declared: <bool>        # true なら他キー不要
  voltage_max: <float | null>
  temperature_max: <float | null>
  chemicals: [<str>]
  notes: <str>
expected_results:              # Phase B。L5 用
  - {command: <str>, value_min: <float|null>, value_max: <float|null>}
dry_run:                       # Phase B。L5 用
  performed_at: <ISO8601 | null>
  ok: <bool | null>
contents:                      # builder が自動生成。全同梱ファイル
  - {path: <zip内相対>, sha256: <hex>, kind: results|run_metadata|instrument|recipe|analysis|log|other}
```

## builder.py

```python
def build_asset(
    *,
    job_id: str,
    db_path: Path,
    instruments_dir: Path,          # 装置定義の取得元
    out_path: Path | None = None,   # default: <exports>/<asset_id>.asset.zip
    title: str = "",
    license_id: str = "UNLICENSED",
    analysis_path: Path | None = None,   # 解析手順 (README.md にコピー)
    declare_level: int | None = None,    # None なら check 相当の自動判定値を宣言
    git_commit: str | None = None,
    conditions: dict | None = None,      # calibration/environment。無指定は not_recorded
    hazards: dict | None = None,
    expected_results: list | None = None,
    sample: dict | None = None,
) -> dict   # {"path", "asset_id", "level_declared", "contents_count", "sha256"}
```

- bundle 部は `tools/export.py` の既存ロジックを **関数として再利用**（MCP ツールを
  経由しない。export の内部関数が tool クロージャ内にあって再利用できない場合は、
  bundle 生成コアを `lab_executor/export_core.py` 等へ**移動ではなく抽出**し、
  tools/export.py はそれを呼ぶ形にリファクタ — MCP レスポンス・挙動不変を担保）
- recipe/recipe.yaml: job_record の recipe 名 + instruments_dir の定義からレシピ断片を
  抽出し、resolved parameters（jobs.parameters_json）と併記
- instrument/: resource に対応する定義 YAML をコピー（特定できない場合は builder が
  エラーではなく `instrument 無し` で続行し、宣言可能レベルが下がるだけ）
- dry_run セクション: builder は自動記入しない（M3 の dry-run 実行記録を将来接続）

## checker.py / levels.py

```python
def check_asset(zip_path: Path) -> CheckReport
# CheckReport: {
#   "asset_id", "schema_ok": bool, "checksums_ok": bool,
#   "level_declared": int, "level_verified": int,   # 判定できた最高レベル
#   "levels": {  # レベル毎の詳細
#     "L0": {"ok": true, "details": [...]},
#     ...
#     "L5": {"ok": false, "missing": ["dry_run.ok", ...]},
#   },
#   "warnings": [...]
# }
```

判定基準（levels.py 純関数。スキーマ文書の基準を実装に落とす）:

- **L0**: results.jsonl または results.csv に 1 行以上
- **L1**: job_record に recipe / parameters / created_at / resource_name が全て非空
- **L2**: instrument/ に定義 YAML が存在 + timeline.jsonl が存在 +
  conditions.calibration / environment が存在（"not_recorded" も可 — **キー欠落は不可**）
- **L3**: L2 + checksums 全一致 + results の各行で raw_response と value_numeric の
  対応が保持（少なくとも 1 系列で両方非空）+ analysis/ に 1 ファイル以上 +
  asset.yaml スキーマ検証 pass
- **L4**: L3 + recipe/recipe.yaml に requires: が存在 +
  `match_capabilities(requires, 同梱 instrument 定義)` が satisfied
- **L5**: L4 + hazards が明示（none_declared=true か、上限値記入）+
  expected_results が 1 件以上 + dry_run.ok == true +
  同梱 instrument 定義が strict 検証 pass（validate_instrument_file(strict=True) の
  errors 0 — safety ratings / safe_shutdown / verify の既存検査を活用）

## capability.py (Phase B)

```python
class RangeSpec(BaseModel):   # {min: float|None, max: float|None}
class CapabilityRequirements(BaseModel):
    commands: list[str] = []
    ranges: dict[str, RangeSpec] = {}   # key = "<command>.<arg>"

def match_capabilities(req, definition: InstrumentDefinition) -> dict
# {"satisfied": bool, "missing_commands": [...], "range_violations": [...]}
```

- `RecipeDefinition` に `requires: CapabilityRequirements | None = None` を追加
- ranges の照合: 装置定義のコマンド引数に min/max 制約がある場合のみ比較
  （制約情報が無い場合は "unknown" として satisfied は維持しつつ warning）
- registry の mock 定義 1 つに requires: 付きレシピをテスト用に追加してよい
  （既存レシピは変更しない）

## CLI

```
lab-executor asset export --job <id> --db <path> --instruments-dir <dir>
    [--out PATH] [--title STR] [--license STR] [--analysis PATH]
    [--declare-level N] [--git-commit HASH] [--json]
lab-executor asset check <zip> [--json]
```

- check の人間向け出力: レベル表（L0..L5 の ok/ng と不足理由）+ 最終判定 1 行
- 終了コード: check はスキーマ or checksum 破損で 1、それ以外は 0
  （レベルが低いことはエラーではない）

## テスト (tests/test_asset_v01.py)

fixture: tmp DB に JobStore で completed job をシード（steps result に instrument /
command / raw_response / value_numeric、sweep なしで可）+ mock 定義 YAML。

| テスト | 検証 |
|---|---|
| test_build_asset_basic | zip 生成、asset.yaml スキーマ、contents sha256 全一致 |
| test_check_l3_full_asset | analysis 付き・conditions 明示で level_verified == 3 |
| test_check_l2_without_analysis | analysis 無し → 2 |
| test_check_l1_without_instrument | instrument 無し → 1 |
| test_check_l0_minimal | job_record 不完全 (owner だけ等) → 0 |
| test_check_detects_tampering | zip 内 1 ファイル改変 → checksums_ok False + exit 1 |
| test_manifest_schema_rejects_bad | 不正 asset.yaml (level 範囲外等) が検証エラー |
| test_requires_optional_backcompat | requires 無し既存 YAML の検証結果不変 (mock 全定義) |
| test_match_capabilities | 満足 / missing_commands / range violation の3ケース |
| test_check_l4 | requires 付き recipe + 満足する定義 → 4 |
| test_check_l5 | hazards + expected_results + dry_run.ok + strict 定義 → 5 |
| test_check_l5_missing_hazards | hazards 欠落 → 4 止まり + missing に明示 |
| test_cli_export_and_check | CLI 経由の一連 (subprocess or main() 直呼び) |
| test_export_core_refactor_invariant | export_experiment_bundle の MCP レスポンス形が従来と同一 (既存テストで担保されるなら不要) |

リグレッション: 既存 export 系テスト + 全スイート（1854+新規 / 28 skipped / 0 failed）。

## バージョン・ドキュメント

- v2.25.0（pyproject + __init__ + CHANGELOG 既存書式。合言葉は「資産」に絡めて自由に）
- docs/asset_usage.md（export→check の手順、レベル表の読み方、L4/L5 に上げる方法）
- docs/experiment_asset_schema_v0.md に「v0.1 実装済み (v2.25.0)」の注記と、
  実装で確定した細部（conditions の not_recorded 必須化等）を反映

## スコープ外

- MCP ツールとしての asset 操作（stability policy 判断が必要なため見送り）
- import / replay（資産からの再実行。L5 の「再実行可能」は宣言と静的検査まで）
- MaiML XML への変換器（Phase 3）
- UI への asset 表示（次マイルストーン候補）

## 作業手順

1. manifest.py + levels.py + テスト（純関数を先に固める）
2. export core の抽出リファクタ（挙動不変を既存テストで確認）→ builder.py
3. checker.py + capability.py + RecipeDefinition 拡張
4. CLI + docs
5. tests/test_asset_v01.py → export 系既存テスト → 全スイート
