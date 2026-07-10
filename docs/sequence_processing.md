# シーケンス処理拡張 リファレンス (SP-1 / SP-2)

v2.28.0 で導入。手動作成シーケンス (UI で作成 → 投入して放置) の中に「その場の
判断」を事前定義するための機構。仕様正本は `sequence_processing_spec.html`
(§3 変数モデル・§4 式言語・§5.1/5.2・§6)。本書はその **SP-1 / SP-2 段階の実装版**
リファレンスである。

対象範囲 (この版で実装):

- SP-1: 統合式言語 / capture 拡張 / compute ステップ / 変数の timeline 記録
- SP-2: `${...}` 実行時引数解決 / 範囲宣言必須 / 実行時範囲執行 / dry-run 拡張

未実装 (後続 SP): branch / repeat / guard / pause (SP-3/4)、array / NumPy /
repeat collect (SP-5)、py / dll (SP-6)、サブシーケンス (SP-7)、文字列補間。

---

## 1. 変数モデル (§3)

| 名前空間 | 内容 | 解決タイミング | 由来 |
| --- | --- | --- | --- |
| `params.*` | レシピパラメータ (`$name`) | コンパイル時 | 現行仕様 |
| `steps.*` | capture (`result_as`) で登録した測定値 | 実行時 | SP-1 |
| `vars.*` | compute で演算した派生値 | 実行時 | SP-1 |
| `env.*` | 実行コンテキスト (`job_id` / `started_at`) 読み取り専用 | 実行時 | SP-1 |

- **型**: float / int / bool / str の 4 型 (array は SP-5)。それ以外の型を代入
  しようとすると `TypeError`。
- **命名**: `[a-z][a-z0-9_]*`。予約語 (`params/steps/vars/env/value`) は不可。
- **スコープ**: 1 Job (= 1 レシピ実行) 内。ステップ順に前方参照のみ可
  (宣言前の参照はコンパイル時エラー)。
- **記録**: すべての代入を timeline イベント `var_assigned` に記録
  (`name` / `namespace` / `value` / `source_step_path` / `expr` / `unit`)。
  Job 終端時に最終スナップショットを result の `variables`
  (`{"steps": {...}, "vars": {...}}`) に格納する。

実装: `experiment_ir/context.py` の `VariableStore`。

---

## 2. 式言語 (§4)

既存 `utils/expression.py` (算術) と `utils/condition.py` (比較・論理) を **1 つの
式言語に統合**した `utils/seq_expression.py` を使う。**既存 2 モジュールは変更して
いない** (wait_for_condition 等の既存経路は不変。新機能だけが新評価器を使う)。

| 区分 | 許可 |
| --- | --- |
| リテラル | 数値 (指数表記可)・文字列・True/False |
| 参照 | `params.x` / `steps.x` / `vars.x` / `env.x` (ドット 1 段のみ) |
| 裸名 (後方互換) | `x` → params → vars → steps の順で平坦探索 |
| 算術 | `+ - * / // % **`、単項 `+ -`、括弧 |
| 比較 (連鎖可) | `== != < <= > >=` (`a < x < b`) |
| 論理 (短絡) | `and or not` |
| 三項 | `A if cond else B` |
| 関数 | `abs, min, max, round, floor, ceil, sqrt, log10, log, exp, clamp(x, lo, hi), len` |

**拒否**: 2 段以上の属性チェーン・dunder (`__`) アクセス・ホワイトリスト外の関数・
添字・内包・ラムダ・代入・import 等。

**数値健全性**: ゼロ除算・NaN/inf の生成・型不整合 (str と数値の演算) は
`SeqExpressionError` (黙って伝播させない)。

API:

```python
evaluate(expr, ctx) -> Any            # 値モード
evaluate_condition(expr, ctx) -> bool # 条件モード (bool 強制)
referenced_names(expr) -> set[str]    # "steps.x" 等の参照一覧 (検証用)
check_expr(expr) -> ast               # 構文 + ノード検証のみ (値評価しない)
```

`ctx = {"params": {...}, "steps": {...}, "vars": {...}, "env": {...}}`。

---

## 3. capture — 測定値の変数登録 (§5.1)

```yaml
- { command: "measure_thickness", result_as: "thickness",
    value_path: "parsed.value_numeric", unit: "nm" }
```

- `value_path` 指定時: result dict をドットパスでたどる (例 `parsed.value_numeric`)。
- 未指定時: observation の `_value_numeric_from_result` と同じ**寛容抽出**。
- **抽出失敗 (None) はステップ failed** (`error="capture_failed"`)。仕様 §5.1
  「値が取れないまま先へ進むを許さない」。

---

## 4. compute — 演算処理 (§5.2)

```yaml
- compute:
    set: "resistivity"
    expr: "steps.sheet_res * steps.thickness * 1e-9"
    unit: "ohm.m"
    on_error: "abort"        # abort (既定) | safe_shutdown
```

- 1 ステップ 1 代入 (`vars.<set>` へ)。
- コンパイル時に式をパース・参照名を検証 (前方参照・未定義参照はエラー)。
- 実行時エラー (SeqExpressionError) 時は `on_error`:
  - `abort`: ステップ failed で終了 (`error="compute_error"`)
  - `safe_shutdown`: 定義の `safe_shutdown` を実行してから failed

IR: `experiment_ir/step.py` の `ComputeStep`。

---

## 5. `${...}` 実行時引数解決と範囲執行 (§6)

```yaml
requires:
  ranges: { "set_current.current": { min: 0.0, max: 0.02 } }
steps:
  - { command: "set_current", args: { current: "${vars.meas_current}" } }
```

| 記法 | 解決 | 用途 |
| --- | --- | --- |
| `"$expr"` | コンパイル時 (recipe_to_plan) | params のみからなる式 |
| `"${expr}"` | 実行時 (ステップ実行直前) | steps/vars/env を含む式 |

- `${expr}` は **arg 値全体を占める場合のみ**対応 (文字列内埋め込みは SP-4)。
- コンパイル時、deferred として `CommandStep.deferred_args`
  (`{arg: {"expr", "min", "max"}}`) に保持する。args からは除外される。

**安全要件 (本仕様の核)**:

1. 実行時解決される引数には**範囲宣言が必須**。装置定義の
   `ParameterDefinition.range`、または recipe の `requires.ranges`
   (`"<command>.<arg>"` キー)。**どちらも無ければ検証エラー** (`recipe_to_plan`
   が例外、`validate_instrument_file` も `recipe_deferred_arg_missing_range`
   error を出し UI 保存も弾く)。両方あれば厳しい方 (積集合) を執行する。
2. 実行時に範囲執行 — 解決値が範囲外なら実行せず `range_violation` で failed
   (+ 定義に `safe_shutdown` があれば実行)。
3. 解決値・範囲・執行結果を timeline イベント `deferred_arg_resolved` に記録。

---

## 6. 実行経路

同期経路 (`recipe_executor.execute_plan`) と非同期 Job 経路
(`job/manager._run_job_inner`) の**両方**に同じ変数機構を通す。共通ロジックは
`seq_runtime.py` に集約 (`process_command_step` / `process_compute_step` /
`extract_capture_value` / `resolve_deferred`)。

- 同期経路: 返り値 dict に `variables` スナップショットを追加。
- Job 経路: `var_assigned` / `deferred_arg_resolved` を timeline に記録し、
  Job result の `variables` に最終スナップショットを格納。

---

## 7. dry-run (§7)

`/api/edit/dryrun` を拡張:

- 各 deferred 引数の式・範囲宣言の有無 (`range_declared`) を明示。
- `test_values` (`{"steps.thickness": 100, "vars.x": 2.0}`) を渡すと、deferred /
  compute の**解決値**と `in_range` を表示する。名前空間を省いた裸キーは
  `steps.*` とみなす。
- `test_values` 未指定時は deferred を `"deferred"` 表示のまま、検証だけ行う。

---

## 8. 記述例

```yaml
recipes:
  film_characterization:
    description: "膜厚 -> 抵抗 -> 抵抗率算出 -> 抵抗率に応じた測定電流の注入"
    requires:
      ranges: { "set_current.current": { min: 0.0, max: 0.02 } }
    steps:
      - { command: "measure_thickness", result_as: "thickness", unit: "nm" }
      - { command: "measure_resistance", result_as: "sheet_res", unit: "ohm/sq" }
      - compute: { set: "resistivity",
                   expr: "steps.sheet_res * steps.thickness * 1e-9", unit: "ohm.m" }
      - compute: { set: "meas_current",
                   expr: "0.001 if vars.resistivity < 1e-3 else 0.0001" }
      - { command: "set_current", args: { current: "${vars.meas_current}" } }
```

`branch` / `guard` はまだ無いため、段階的な値マップは三項式 (`compute`) で表現する
(SP-3 で `branch` / `guard` を導入予定)。
