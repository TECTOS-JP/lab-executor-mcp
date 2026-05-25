# Instrument `promote-check` (v1.9, experimental)

合言葉: **「instrument YAML を `tested` / `verified` へ昇格して
よいかを 1 コマンドで診断する」**

v1.8 で `scaffold` / `add-instrument` により draft 定義を作る入口が
揃った。v1.9 では、その draft を次の support_level へ昇格させる前の
**最終チェック**を `promote-check` で提供する。

内部的には [`validate instrument --strict`](./instrument_authoring.md)
の結果を再利用する。

## CLI

```bash
visa-mcp instrument promote-check <path> --target tested [--json]
visa-mcp instrument promote-check <path> --target verified [--json]
visa-mcp instrument promote-check <path> --target draft  # 下方移動
```

`--target` は `draft` / `experimental` / `tested` / `verified` のいずれか。

## support_level 昇格ルール (v1.9)

| 現在 → target | 必要条件 |
|--------------|---------|
| `draft` → `experimental` | strict validation を通る (最低条件) |
| `draft` → `tested` | strict validation を通る |
| `experimental` → `tested` | strict validation を通る |
| `tested` → `verified` | strict validation + `metadata.validation_evidence` 非空 |
| 下方移動 (`verified` → `tested` 等) | **常に eligible** (即 OK) |

### `draft → tested` の最低条件 (strict validation)

| check | 検出 error_class |
|-------|------------------|
| `metadata.manual_ref` に TODO / TBD / FIXME 残存していない | `instrument_manual_ref_todo` |
| 出力系 instrument (`OUTPUT_CAPABLE_CATEGORIES`) は `safe_shutdown` 必須 | `instrument_missing_safe_shutdown` |
| 出力系 instrument は `safety.ratings` 必須 | `instrument_missing_safety_ratings` |
| state 変更系 set command (`set_voltage` 等) に `verify` 必須 | `instrument_missing_verify` |

### `tested → verified` の追加条件

- `metadata.validation_evidence` が **非空 dict** であること
- 推奨 sub-fields:
  - `tested_by` (人名 / 組織)
  - `tested_at` (ISO8601 日付)
  - `interface` (USB / LAN / GPIB / Serial 等)
  - `firmware` (機器 firmware version)
  - `tested_items` (identify / set_voltage / safe_shutdown 等のリスト)

詳細 schema は [`docs/extension_integrity.md`](extension_integrity.md)
の `validation_evidence` section を参照。

### 下方移動を即 eligible にする理由

実機 trouble / regression / 再評価のため、`verified` → `tested` /
`draft` への **下げ**は管理者判断で即実行できるべき。strict 検査を
通す必要は無い (むしろ「下げる時点で問題が見つかった」可能性が高い)。

## 出力 (JSON)

```json
{
  "promote_check": {
    "status": "warning",
    "file": "instruments/kikusui_pmx.yaml",
    "current_support_level": "draft",
    "target_support_level": "tested",
    "eligible": false,
    "blocking_issues": [
      {
        "issue": "instrument_manual_ref_todo",
        "message": "(strict) metadata.manual_ref に TODO 系 placeholder が残存: 'TODO: URL or document title + revision + page range'",
        "field_path": "metadata.manual_ref",
        "details": {"manual_ref": "TODO: ..."}
      }
    ],
    "recommended_actions": [
      {
        "action": "fill_manual_ref",
        "reason": "Replace TODO with actual manual / URL reference"
      }
    ]
  }
}
```

## 終了コード

| 状況 | exit code |
|------|-----------|
| `eligible: true` | 0 |
| `eligible: false` (blocking issues あり) | 1 |
| 致命的 error (file not found / schema invalid / 未知 target) | 1 |

CI で gate として使う場合:

```bash
visa-mcp instrument promote-check my_inst.yaml --target tested
echo "exit=$?"   # 0 ならマージ可
```

## 内部実装

`promote_check_instrument()` (Python) は
`validate_instrument_file(path, strict=True)` の結果を再利用する。
これにより:

- strict ルールが 1 箇所で管理される (registry.py)
- CLI と Python API の判定が必ず一致する
- v1.10 の `instrument review-report` でも同じ strict 結果を集計できる

## `validate instrument --strict` との使い分け

| ユースケース | コマンド |
|-------------|---------|
| 現在の `support_level` が宣言値と矛盾していないか確認 | `validate instrument <file> --strict` |
| 次の `support_level` に昇格してよいかを目標と一緒に判定 | `instrument promote-check <file> --target tested` |
| PR review で複数 instrument を集計 | (v1.10) `instrument review-report` |

将来 (v1.10) では `extension doctor` の `instrument_quality` summary や
`instrument review-report` から `promote-check` を呼び出して、pack 単位
の昇格判定を 1 表で見られるようにする予定。

## v1.9 で対応しないこと (v1.10+ 候補)

- `instrument review-report` (markdown PR 用) — v1.10
- `validation_evidence` template の自動 fill — v1.10
- `support_level=verified` で実機 firmware 違い検出 — v2.x
- 多言語化 — v2.x

## 関連 docs

- [`instrument_authoring.md`](instrument_authoring.md) — scaffold / `add-instrument`
- [`extension_authoring.md`](extension_authoring.md) — pack scaffold / `doctor`
- [`extension_publishing_checklist.md`](extension_publishing_checklist.md)
- [`error_taxonomy.md`](error_taxonomy.md)
- [`category_policy.md`](category_policy.md) — category alias / canonical name
