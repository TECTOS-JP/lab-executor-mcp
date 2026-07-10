# シーケンス処理拡張 SP-1 + SP-2 実装計画 (式統合・compute・実行時引数解決)

作成: 2026-07-08 / 計画: Claude Fable 5 / 実装・テスト: Claude Opus / 最終確認: Claude Fable 5
仕様の正本: AutoLaboKnowlege/sequence_processing_spec.html v1.2（§3 変数モデル・§4 式言語・
§5.1/5.2・§6 実行時引数解決）。本計画はその SP-1 / SP-2 段階の実装。
前提: HEAD = ff4fd80 (v2.27.0)、テスト 1897 passed / 28 skipped / 0 failed。

## 重要な事前調査結果（実装者はこれを前提にすること）

1. **`${steps.x}` の実行時解決は未実装**。`result_as` は schema（models/instrument_def.py:246）と
   IR（experiment_ir/step.py）に宣言され recipe_to_plan が素通しするだけで、
   **消費コードはどこにも無い**（docstring の「v0.6.0+ で実装」は宣言のみ先行）。
   本実装が実行時変数機構の最初の実体となる。既存挙動への影響はこの点で小さい
2. コマンド引数の範囲は `ParameterDefinition.range: [min, max]`（instrument_def.py）に既存。
   recipe 側は v2.25 の `requires.ranges`（"<command>.<arg>" キー）が既存
3. 実行経路は2つ: 同期 `recipe_executor.execute_plan`（execute_recipe ツール）と
   非同期 Job 経路（job/manager.py がステップループを持つ）。**両経路に同じ変数機構を
   通すこと**（実装前に manager 内の recipe 実行ループを特定して読むこと）
4. 式評価器は utils/expression.py（算術のみ）と utils/condition.py（比較・論理）に分裂している

## スコープ

- **SP-1**: 統合式言語 / capture 拡張（value_path・失敗時 failed）/ compute ステップ /
  変数の timeline 記録と result スナップショット
- **SP-2**: `${...}` 実行時引数解決 / 範囲宣言必須の検証 / 実行時範囲執行 /
  dry-run の deferred 表示とテスト値注入
- **スコープ外**（後続 SP）: branch/repeat/guard/pause、array/np、py/dll、message 内の
  文字列補間（pause 用）。numpy 依存は追加しない

## 実装

### 1. 統合式評価器 `utils/seq_expression.py`（新規）

```python
def evaluate(expr: str, ctx: dict[str, dict]) -> Any     # 値モード
def evaluate_condition(expr: str, ctx: dict) -> bool     # 条件モード（結果を bool 強制）
def referenced_names(expr: str) -> set[str]              # "steps.x" 等の参照一覧（検証用）
```

- ctx = `{"params": {...}, "steps": {...}, "vars": {...}, "env": {...}}`
- AST ホワイトリスト: 既存2モジュールの和集合 + `ast.Compare`（連鎖可）/ `BoolOp` / `IfExp` /
  `Call`（関数ホワイトリストのみ: abs, min, max, round, floor, ceil, sqrt, log10, log, exp,
  clamp, len）/ `Attribute`（**Name(params|steps|vars|env) 直下の1段のみ**。それ以外の
  Attribute・全 dunder は拒否）
- 裸の Name は後方互換のため許可（既存 safe_eval の `$target_v` 系。ctx を平坦に探す:
  params → vars → steps の順）。**既存 utils/expression.py・condition.py は変更しない**
  （wait_for_condition 等の既存経路は不変。新機能だけが新評価器を使う）
- ゼロ除算・NaN/inf 生成・型不整合（str と数値の演算）は `SeqExpressionError`

### 2. 変数コンテキスト `experiment_ir/context.py`（新規）

```python
class VariableStore:
    def __init__(self, params: dict, env: dict): ...
    def set_step(self, name, value, *, source_step_path, unit="") -> None
    def set_var(self, name, value, *, source_step_path, expr="", unit="") -> None
    def as_ctx(self) -> dict          # evaluate() に渡す形
    def snapshot(self) -> dict        # {"steps": {...}, "vars": {...}} (result 格納用)
    events: list[dict]                # var_assigned イベント（呼び出し側が timeline へ流す）
```

- 命名規則 `[a-z][a-z0-9_]*`・予約語拒否・型は float/int/bool/str（それ以外は TypeError）
- 代入毎に `{"name", "namespace", "value", "source_step_path", "expr", "unit"}` を events へ。
  Job 経路では JobStore.record_event("var_assigned", payload=...) に接続。
  同期経路（execute_plan）では返り値 dict に `variables` スナップショットを追加するのみ
- env: `job_id`（同期時は ""）, `started_at`。loop_index は SP-3 で追加予定の予約名

### 3. capture 拡張（SP-1）

- schema（models/instrument_def.py の RecipeStep）に `value_path: str = ""` と
  `unit: str = ""` を追加（optional・既存 YAML 不変）。IR CommandStep にも同フィールド
- 実行時: `result_as` があるステップの完了後、result dict から値を抽出して
  `store.set_step()`。抽出規約:
  - `value_path` 指定時: ドットパスで result dict をたどる（例 "parsed.fields.value"）
  - 未指定時: observation の `_value_numeric_from_result` と同じ寛容抽出（import して再利用。
    tools/observation.py の private だが同一プロジェクト内の意図的再利用 — M1 以来の前例に従い
    コメントを残す）
  - **抽出失敗（None）はステップ failed**（error_class="capture_failed"）。
    仕様 §5.1「値が取れないまま先へ進むを許さない」

### 4. ComputeStep（SP-1）

- IR: `ComputeStep(type="compute", set: str, expr: str, unit: str = "", on_error: Literal["abort","safe_shutdown"] = "abort", description: str = "")`
  （pause は SP-4 で追加。discriminated union へ追加）
- recipe schema: `- compute: {set, expr, unit?, on_error?, description?}`
  （RecipeStep に compute フィールド追加。wait: と同じ体裁）
- recipe_to_plan: ComputeStep へ変換。**コンパイル時検証**: expr がパース可能・
  参照名（referenced_names）が「その時点までに定義される名前」（params / 先行 result_as /
  先行 compute set / env 予約名）に含まれる。前方参照・未定義参照はコンパイルエラー
- 実行時: evaluate() → set_var()。SeqExpressionError 時は on_error に従う
  （abort=ステップ failed で終了 / safe_shutdown=定義の safe_shutdown レシピを実行してから failed。
  Job 経路の safe_shutdown 実行は manager の既存 cancel(safe_shutdown) 内部ヘルパを特定して再利用）
- execute_plan（同期）と Job 経路の両方でディスパッチを追加

### 5. `${...}` 実行時引数解決（SP-2）

- recipe_to_plan の arg 解決を拡張: 文字列 arg が `${` を含む場合、コンパイル時評価せず
  **DeferredArg として保持**（CommandStep.args の値に `{"__deferred__": "<expr>"}` を格納する
  方式か、CommandStep に `deferred_args: dict[str, str]` を並置する方式か、IR の
  シリアライズ性が良い方を実装時に選び報告すること）。`${expr}` は arg 値全体を占める場合のみ
  対応（文字列内埋め込みは SP-4）。式の抽出は `${` と対応する `}` の間
- **コンパイル時検証（本計画の安全の核）**:
  1. 式パース可能・参照名が定義済み（compute と同じ規則）
  2. **範囲宣言の存在**: 対象コマンドの ParameterDefinition.range があるか、
     recipe.requires.ranges に "<command>.<arg>" があるか。**どちらも無ければ検証エラー**
     （recipe_to_plan で例外 → execute_recipe / start_recipe_job が既存の validation 失敗系で返す。
     validate_instrument_file にも同検査を lint として追加し、UI 保存時にも弾けるように）
- **実行時**: ステップ実行直前に evaluate() で解決 → **範囲執行**
  （ParameterDefinition.range と requires.ranges の両方があれば厳しい方=積集合）。
  範囲外は error_class="range_violation" でステップ failed + 定義に safe_shutdown が
  あれば実行（compute の safe_shutdown と同じヘルパ）。解決値・範囲・執行結果を
  timeline イベント `deferred_arg_resolved` として記録

### 6. dry-run 拡張（SP-2、UI M3 の /api/edit/dryrun）

- dryrun リクエストに `test_values: {"steps.x": 1.0, "vars.y": 2.0}` を追加（optional）
- 応答の各 step に: deferred な引数の一覧・その式・範囲宣言の有無（無ければ error）・
  test_values があれば解決値。compute は test_values で評価した値を表示
- test_values 未指定時は deferred を「未解決 (deferred)」表示のまま検証だけ行う

## テスト（tests/test_seq_processing_sp12.py、目安 20〜24件）

- 評価器: 算術/比較連鎖/論理短絡/三項/関数(clamp含む)/名前空間参照/裸名フォールバック/
  拒否（属性2段・dunder・未知関数・未知ノード）/ゼロ除算・NaN/str混在エラー
- capture: value_path 抽出 / 寛容抽出 / 抽出失敗→failed / unit 注記が var_assigned に載る
- compute: 正常 / 前方参照コンパイルエラー / 実行時エラー on_error=abort /
  safe_shutdown 実行の確認（Mock で safe_shutdown レシピ呼び出しを観測）
- deferred: `${}` 検出 / 範囲宣言なしでコンパイルエラー / requires.ranges だけで OK /
  ParameterDefinition.range だけで OK / 実行時解決の正常値 / 範囲外→range_violation +
  safe_shutdown / timeline に deferred_arg_resolved
- Job 経路: start_recipe_job で compute+capture+deferred を含むレシピ → 変数スナップショットが
  job result に、var_assigned が timeline に載る（MockBackend）
- dry-run: deferred 表示 / test_values 解決 / 範囲宣言なしの検査エラー
- 後方互換: 既存 mock レシピ群の recipe_to_plan 結果が不変（$ 式の従来解決含む）

リグレッション: UI 系（m1〜m4）+ asset 系 + 全スイート（1897+新規 / 28 skipped / 0 failed）。

## バージョン・ドキュメント・制約

- v2.28.0（pyproject + __init__ + CHANGELOG 既存書式）。docs/recipes.md に
  「変数と演算（SP-1/2）」節を追加、docs/dsl か docs/sequence_processing.md 新設で
  仕様書該当節の実装版リファレンスを書く
- MCP ツール面 50 不変 / 既存 YAML・既存レシピの検証結果不変 /
  utils/expression.py・condition.py は不変 / LF / git commit しない /
  テスト完了を確認してから報告（3分無出力はハング切り分け）
