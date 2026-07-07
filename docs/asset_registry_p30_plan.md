# P3.0 資産レジストリ実装計画 (asset publish / catalog + 共有ゲート)

作成: 2026-07-07 / 計画: Claude Fable 5 / 実装・テスト: Claude Opus / 最終確認: Claude Fable 5

前提: HEAD = defef81 (v2.26.0 + 共有ポリシー docs)、テスト 1881 passed / 28 skipped / 0 failed。
背景: AutoLaboKnowlege ウィキ concepts/asset-marketplace.md の P3.0 設計と、
docs/asset_usage.md「共有ポリシー (2026-07-07 所有者決定)」。

## ゴール

1. **資産レジストリ**: ディレクトリ 1 つ + INDEX.yaml + 資産 zip 群。
   `asset publish`（掲載ゲート付き）と `asset catalog`（一覧）
2. **共有ゲートの実装**（所有者決定の機械化）:
   - 外部レジストリへの publish は **level_verified <= 3** かつ
     **license が UNLICENSED でない**こと
3. **builder の declare_level 自動宣言の修正**: 計画 v0.1 の仕様
   「None なら check 相当の自動判定値を宣言」を実装（現状 0 固定）

## 絶対制約（従来どおり + 追加）

1. MCP ツール面 50 不変（レジストリも CLI のみ）
2. 既存の extension registry（`registry/INDEX.yaml`、instruments 用）there
   に**触れない** — 資産レジストリは利用者が任意の場所に作る別物
3. publish の掲載ゲートを迂回する経路を作らない（check 失敗 = 掲載不可。
   external の L 上限・license 必須も同様。--force は**重複置換のみ**に許し、
   ゲートのスキップには使えない）
4. LF / git commit しない / テスト完了を確認してから報告

## レジストリのディスク形式

```
<registry_dir>/
├── INDEX.yaml
└── assets/
    └── <asset_id>.asset.zip     # publish 時にコピーされる
```

INDEX.yaml:

```yaml
registry_version: "0.1"
visibility: team          # team | external。作成時に決まり、以後変更は手動編集
name: <表示名>
created_at: <ISO8601>
assets:
  - id: <asset_id (uuid)>
    title: <str>
    level_verified: <int>        # publish 時の check 結果 (自己申告ではない)
    license: <str>
    sha256: <zip 全体の hex>
    path: assets/<asset_id>.asset.zip
    tags: [<str>]
    requires_commands: [<str>]   # 資産内 recipe の requires.commands (発見性用。無ければ [])
    published_at: <ISO8601>
    producer: <asset.yaml provenance.producer>
```

- 並び順の規約: **level_verified 降順 → published_at 降順**（品質を第一ソートに。
  人気指標は持たない — 集中リスク対策の設計原則）

## 実装

### src/lab_executor/asset/registry.py（新規）

```python
class AssetRegistryError(Exception): ...

def init_registry(dir, *, name="", visibility="team") -> dict
    # INDEX.yaml 作成。既存 INDEX があれば AssetRegistryError
def load_index(dir) -> dict           # 無い/壊れは AssetRegistryError
def publish_asset(zip_path, registry_dir, *, tags=None, force=False) -> dict
    # 1) load_index (無ければ AssetRegistryError で「init を先に」と案内)
    # 2) check_asset 実行: schema_ok と checksums_ok が False なら拒否
    # 3) 共有ゲート: index["visibility"] == "external" のとき
    #    - level_verified > 3 → 拒否 (L3 上限ルール。理由文で所有者決定に言及)
    #    - asset.yaml license が "" / "UNLICENSED" → 拒否
    # 4) 重複 asset_id: force=False なら拒否、True なら entry と zip を置換
    # 5) zip を assets/ にコピー、zip 全体 sha256 を計算、INDEX に entry 追記
    #    (規約順にソートして保存)
    # 返り値: {"published": True, "id", "level_verified", "registry", "path"}
def catalog(dir, *, recheck=False) -> list[dict]
    # INDEX の assets を規約順で返す。recheck=True なら各 zip の sha256 を
    #  再計算して INDEX と照合し、不一致 entry に "integrity": "FAILED" を付ける
```

### CLI（cli.py の asset グループに追加）

```
lab-executor asset registry-init --dir DIR [--name STR] [--visibility team|external]
lab-executor asset publish <zip> --registry DIR [--tags a,b] [--force] [--json]
lab-executor asset catalog --registry DIR [--check] [--json]
```

- catalog の人間向け出力: `L4 | <title> | <license> | <tags> | <id 先頭8>` 形式の表 +
  visibility と件数のヘッダ。--check 時は integrity 列を追加
- publish 拒否時は理由を明確に表示して exit 1

### builder の declare_level 自動宣言修正（asset/builder.py）

- `declare_level=None` のとき: zip 化の**前**に、手元にある材料
  （results 行数 / job_record / conditions / instrument 定義の有無 / analysis の有無 /
  requires と capability 照合 / hazards / expected_results / dry_run / strict 検証）で
  levels.py の judge_l0〜l5 を直接呼び、`summarize_verified_level` の値
  （checksums_ok と schema_ok は build 直後ゆえ True 扱い）を level_declared に書く
- 実装後、**export 直後の check で `level_declared == level_verified` になる**こと
  （これを結合テストで固定）
- 明示指定 (`--declare-level N`) は従来どおり優先

## テスト (tests/test_asset_registry_p30.py)

fixture: test_asset_v01/v02 の fixture を再利用し、L3 相当と L4/L5 相当の資産 zip を
生成して使う。

| テスト | 検証 |
|---|---|
| test_registry_init_and_reject_double_init | init 成功 / 二重 init 拒否 |
| test_publish_to_team_registry | team に L4 資産を publish 成功、INDEX entry の中身 |
| test_publish_requires_check_pass | 改ざん zip は拒否 (checksums) |
| test_external_gate_level_cap | external に L4 → 拒否 (理由に L3 上限)。L3 なら成功 |
| test_external_gate_license | external に UNLICENSED L3 → 拒否。CC-BY-4.0 なら成功 |
| test_force_replaces_duplicate | 同 id 再 publish は拒否 / --force で置換 |
| test_force_does_not_bypass_gate | external + --force でも L4 は拒否 |
| test_catalog_order_and_fields | level 降順 → published_at 降順、requires_commands 反映 |
| test_catalog_recheck_detects_tamper | 掲載後に zip 改変 → --check で FAILED |
| test_declare_level_auto | declare_level 省略で export → check の verified と一致 (L2/L3/L4 の 3 ケース) |
| test_declare_level_explicit_wins | --declare-level 1 指定が自動判定に優先 |
| test_cli_roundtrip | CLI で init → publish → catalog の一連 |

リグレッション: test_asset_v01 (14) / v02 (6) / v2_25_1 (並行分) → 全スイート
（1881+新規 passed / 28 skipped / 0 failed）。

## バージョン・ドキュメント

- v2.27.0（pyproject + __init__ + CHANGELOG 既存書式）
- docs/asset_usage.md に registry 節（init/publish/catalog、共有ゲートの説明 —
  既存の「共有ポリシー」節と接続）
- docs/experiment_asset_schema_v0.md の「dry_run 自動記入せず」等は v0.2 で更新済み。
  declare_level 自動宣言の注記があれば実装済みに更新

## スコープ外

- P3.1 以降（git 共有・匿名化・対価）、uri 参照（コピーせず参照のみ）、
  追試報告の紐付け、UI への表示
