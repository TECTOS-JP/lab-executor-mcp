# シーケンス処理拡張 リファレンス (SP-1 〜 SP-4)

v2.28.0 (SP-1/2)・v2.29.0 (SP-3)・v2.30.0 (SP-4) で導入。手動作成シーケンス
(UI で作成 → 投入して放置) の中に「その場の判断」を事前定義するための機構。
仕様正本は `sequence_processing_spec.html` (§3 変数モデル・§4 式言語・
§5.1-5.6・§6)。本書はその **SP-1 〜 SP-4 段階の実装版**リファレンスである。

対象範囲 (この版で実装):

- SP-1: 統合式言語 / capture 拡張 / compute ステップ / 変数の timeline 記録
- SP-2: `${...}` 実行時引数解決 / 範囲宣言必須 / 実行時範囲執行 / dry-run 拡張
- SP-3: branch (条件分岐) / repeat (反復) / guard (範囲検証と安全動作)
- SP-4: pause (人間 / AI の呼び出し。message の `${...}` 補間を含む)

未実装 (後続 SP): array / NumPy / repeat collect (SP-5)、py / dll (SP-6)、
サブシーケンス (SP-7)、args への `${...}` 部分埋め込み。

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

- `${expr}` は **arg 値全体を占める場合のみ**対応。文字列内埋め込みは v2.30.0 (SP-4) で
  **pause の message 等の表示文字列に限り**解禁 (args への部分埋め込みは引き続き禁止)。
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

## 6. branch — 条件判断 (§5.3、v2.29.0 SP-3)

```yaml
- branch:
    - when: "vars.resistivity < 1e-6"
      steps:
        - compute: { set: "meas_current", expr: "0.010" }
    - when: "vars.resistivity < 1e-3"
      steps:
        - compute: { set: "meas_current", expr: "0.001" }
    - else:
      steps:
        - compute: { set: "meas_current", expr: "0.0001" }
```

- 上から評価し、**最初に真になった when の steps のみ**実行 (if/elif/else 相当)。
  `else` は省略可・最後のみ。どの case も採択されない場合は no-op で続行。
- **ネスト最大深さ 3** (`BRANCH_MAX_DEPTH = 3`)。超過はコンパイルエラー。
- 採択された分岐を timeline イベント `branch_taken`
  (case_index / 条件式 / 評価値) に記録。採択なしも case_index=None で記録。
- **全経路定義検証 (§3)**: 分岐内で定義した変数 (result_as / compute set) を
  分岐後に使う場合、**else を含む全 case で定義されていること**。else 無しで
  分岐内定義変数を分岐後に参照するとコンパイルエラー (未定義経路の混入防止)。
- IR はネスト構造を保持 (`BranchStep.cases[].steps` が Step のリスト)。
  ネスト step の step_path は `steps[1]/branch/case[0]/steps[2]` 形式で階層保持。

---

## 7. repeat — 反復 (§5.4、v2.29.0 SP-3。collect は SP-5)

```yaml
- repeat:
    count: "$n_points"            # 固定回数 (コンパイル時解決可)
    steps: [ ... ]                # env.loop_index (0 始まり) を参照可
# または条件反復:
- repeat:
    while: "steps.drift > 0.01"
    max_iterations: 20            # while 使用時は必須 (無限ループ禁止)
    steps: [ ... ]
```

- **count 型**: count 回 body を実行。count はコンパイル時解決 (`$param` 可)、
  1 以上 `REPEAT_MAX_COUNT` (10,000) 以下。
- **while 型**: 条件が真の間実行。`max_iterations` 必須 (1〜10,000)。
  **上限到達は failed にしない** — `repeat_ended` (reason=max_iterations) を
  記録して続行し、後続 guard で扱えるようにする (spec §5.4)。
- 終了は timeline イベント `repeat_ended` に記録。reason は
  `count_completed` / `condition_false` / `max_iterations` の 3 値。
- body 内で `env.loop_index` を参照可。ネスト repeat では内側が外側の
  loop_index を一時的に上書きし、終了時に復元する。
- **while 条件は最初の反復の前に評価される**ため、body 内でのみ定義される変数は
  while 式から参照できない (コンパイルエラー)。ループ前に capture / compute
  しておくこと。
- 展開後総ステップ数の静的見積り (count / max_iterations 乗算) が
  `MAX_TOTAL_STEPS_ESTIMATE` (100,000) を超えるレシピはコンパイルエラー
  (spec §10 展開上限)。
- 変数スコープ: count 型 (>= 1 保証) の body 内定義は repeat 後も参照可。
  while 型は 0 回実行があり得るため body 内定義は repeat 後に参照不可。

---

## 8. guard — 範囲検証と安全動作 (§5.5、v2.29.0 SP-3)

```yaml
- guard:
    expr: "1e-8 < vars.resistivity < 1e2"
    on_fail: "safe_shutdown"      # abort | safe_shutdown | warn (既定 abort)
    message: "抵抗率が物理的に妥当な範囲を外れています"
```

- assert 相当。式が真なら通過、偽なら `on_fail`:
  - `abort`: ステップ failed で終端 (`error="guard_failed"`)
  - `safe_shutdown`: 装置の安全停止を実行してから failed
  - `warn`: **続行** + timeline イベント `guard_failed` (warning)
- `on_fail: pause` は未対応 (検証エラー)。pause ステップを guard の後に置くことで同等の
  流れを構成できる (§13)。
- 式評価エラー (未定義参照等) は on_fail に関わらず failed
  (`error="guard_error"`。判定不能のまま通さない)。
- 推奨規約 (spec §5.5): capture / compute の直後に guard を置く。

---

## 9. ネスト実行の制限 (SP-3)

- branch case / repeat body 内に書けるのは: command (deferred / capture 込み)・
  wait・compute・guard・branch・repeat。
- **polling wait (wait_until / wait_for_condition / wait_for_stable) と barrier
  はネスト内では未対応** (コンパイルエラー)。Job manager のトップレベル状態遷移
  (WAITING 等) と密結合のため、対応は後続 SP で検討する。
- Job 経路では cancel / job_timeout がネスト step 境界でも検出される。

---

## 10. 実行経路

同期経路 (`recipe_executor.execute_plan`) と非同期 Job 経路
(`job/manager._run_job_inner`) の**両方**に同じ変数機構を通す。共通ロジックは
`seq_runtime.py` に集約 (`process_command_step` / `process_compute_step` /
`process_guard_step` / `process_branch_step` / `process_repeat_step` /
`execute_step_list` / `extract_capture_value` / `resolve_deferred`)。

- 同期経路: 返り値 dict に `variables` スナップショットを追加。
- Job 経路: `var_assigned` / `deferred_arg_resolved` / `branch_taken` /
  `repeat_ended` / `guard_failed` を timeline に記録し、Job result の
  `variables` に最終スナップショットを格納。
- ネストしたリーフ step (command / wait) の実行は経路側が
  `seq_runtime.NestedExecutors` (run_command / run_wait / cancel_check) として
  注入する。

---

## 11. dry-run (§7)

`/api/edit/dryrun` を拡張:

- 各 deferred 引数の式・範囲宣言の有無 (`range_declared`) を明示。
- `test_values` (`{"steps.thickness": 100, "vars.x": 2.0}`) を渡すと、deferred /
  compute の**解決値**と `in_range` を表示する。名前空間を省いた裸キーは
  `steps.*` とみなす。
- `test_values` 未指定時は deferred を `"deferred"` 表示のまま、検証だけ行う。
- **branch** (SP-3): 全 case を `cases` に展開表示。test_values があれば各 when の
  評価値 (`when_value`) と採択 case (`taken` / `taken_case`) を併記し、採択 case の
  compute 結果だけが後続へ伝播する。
- **repeat** (SP-3): count <= 5 は `iterations` としてイテレーション展開
  (loop_index 毎)、それより大きい場合は `body` を 1 回だけ展開して
  `iterations_omitted: true` を付ける。
- **guard** (SP-3): 式・on_fail・message を表示。test_values があれば `passed`
  (判定結果) を併記。

---

## 12. 記述例

```yaml
recipes:
  film_characterization:
    description: "膜厚 -> 抵抗 -> 抵抗率算出 -> 抵抗率に応じた条件で測定"
    parameters:
      - { name: "n_points", type: "int", default: 5 }
    requires:
      ranges: { "set_current.current": { min: 0.0, max: 0.02 } }
    steps:
      - { command: "measure_thickness", result_as: "thickness", unit: "nm" }
      - guard: { expr: "1 < steps.thickness < 10000",
                 on_fail: "abort", message: "膜厚が想定外です" }
      - { command: "measure_resistance", result_as: "sheet_res", unit: "ohm/sq" }
      - compute: { set: "resistivity",
                   expr: "steps.sheet_res * steps.thickness * 1e-9", unit: "ohm.m" }
      - guard: { expr: "1e-8 < vars.resistivity < 1e2",
                 on_fail: "safe_shutdown",
                 message: "抵抗率が物理範囲外。配線・接触を確認してください" }
      - branch:
          - when: "vars.resistivity < 1e-6"
            steps: [ compute: { set: "meas_current", expr: "0.010" } ]
          - when: "vars.resistivity < 1e-3"
            steps: [ compute: { set: "meas_current", expr: "0.001" } ]
          - else:
            steps: [ compute: { set: "meas_current", expr: "0.0001" } ]
      - { command: "set_current", args: { current: "${vars.meas_current}" } }
      - repeat:
          count: "$n_points"
          steps:
            - { command: "measure_voltage", result_as: "v_point" }
      - { command: "set_current", args: { current: 0.0 } }
```

系列の収集 (`collect:` で array に蓄積) は SP-5 で対応予定。それまでは各反復の
capture は同名変数への上書きになる (最後の値のみ残る)。

---

## 13. pause — 人間 / AI の呼び出し (§5.6、v2.30.0 SP-4)

```yaml
- pause:
    message: "抵抗率 ${vars.resistivity} Ω·m。続行してよいですか?"
    timeout_s: 3600                # 必須 (既定 3600)
    on_timeout: "safe_shutdown"    # abort | safe_shutdown (既定)
    expose: ["vars.resistivity", "steps.thickness"]   # 確認画面に表示する値
```

「常時監視が難しい」問題への直接の答え: 監視は不要、**呼ばれたときだけ人が見る**。
応答が無ければ timeout の既定は safe_shutdown (安全側)。

### 状態機械の扱い (実装方針)

- **JobStatus の 8 状態は変更しない** (v1 stability policy)。pause 中は
  **status=WAITING のまま**。
- 「pause 要求中」は `job_pauses` テーブルの未解決レコード (resolution IS NULL)
  + timeline イベント `pause_requested` (message・expose 値・応答期限) で表現する。
- observation 層 (`compute_current_phase`) が未解決 pause を検出したら
  **phase="paused"** を返す (`PHASE_ENUM` に追加。get_experiment_timeline 等の
  MCP observation・UI 詳細画面の両方に現れる)。

### message の `${...}` 補間

- SP-4 で文字列内埋め込みを解禁したのは **表示文字列 (message) のみ**。
  装置に届く args への部分埋め込みは引き続き禁止 (SP-2 の安全要件のまま)。
- 補間式はコンパイル時に参照検証される。実行時の評価エラーは fail-soft
  (未解決のまま表示。表示専用のため failed にはしない)。

### 応答経路 (3 つ)

1. **UI**: ジョブ詳細画面の pause パネル (「続行 / 中止」ボタン。M4 の cancel と
   同じ available 条件・confirm)。`/api/control/jobs/{id}/pause-response`
   プロキシ経由。
2. **control plane**: `POST /control/jobs/{job_id}/pause-response
   {action: continue|abort, responder}` (X-Control-Token 認証・audit 記録は
   cancel と同じ流儀)。
3. **CLI**: `lab-executor job respond-pause <job_id> --action continue|abort
   [--responder NAME] [--db PATH]` — control plane が使えない環境 (AI が MCP +
   CLI のみ持つ場合等) のための直接応答 (state DB の resolution を更新)。
   **MCP ツールは追加しない** (tool surface 50 不変)。

### 実行時の挙動

- 待機は 200ms slice で `resolution` をポーリング (job cancel / job_timeout_s
  にも即応)。
- `continue`: step success として再開。result に resolution / responder が残る。
- `abort`: step failed (`error="pause_aborted"`) → job failed。
- `timeout_s` 超過: `on_timeout` に従う。`safe_shutdown` なら装置の安全停止を
  実行してから failed (`error="pause_timeout"`)。timeline に `pause_timeout`。
- 応答は timeline イベント `pause_resolved` (action / responder) に記録される。

### 制限 (SP-4 スコープ)

- **Job 経路のみ対応**。同期 execute_recipe は `AsyncStepRequiresJob` で
  start_recipe_job への切替を促す (wait 系の前例)。
- branch / repeat 内の pause は未対応 (コンパイルエラー)。
- guard の `on_fail: pause` は未対応 (guard の直後に pause ステップを置く)。
- M-1 通知エンジンは未実装のため、通知は timeline イベント + UI 表示 +
  control.json 経由の既存監視で行う (通知フックの将来接続点は
  `JobManager._run_pause_step` の TODO(M-1) コメント)。
