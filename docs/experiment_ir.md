# Experiment IR (Intermediate Representation) ── v0.5.0-rc1

visa-mcp が内部で扱う「実験手順 1 単位」の正規表現。Recipe / Group / DSL の各 executor が共有する設計。

v0.5.0-rc1 で導入。v0.8.0 のリポジトリ分割時に `experiment_mcp/ir/` へそのまま移動できるよう、`visa_mcp` 本体の他モジュールへの直接依存を最小化している (疎結合設計)。

## 構成

```
src/visa_mcp/experiment_ir/
├── __init__.py    # CommandStep / WaitStep / Step / Plan を re-export
├── step.py        # 各 Step 型 (Pydantic discriminated union)
└── plan.py        # Plan (Step のシーケンス + parameters + metadata)
```

## Step 型 (v0.5.0-rc1)

### `CommandStep`

機器の名前付きコマンドを 1 回実行。

| フィールド | 型 | 説明 |
|----------|-----|------|
| `type` | `"command"` (literal) | discriminator |
| `command` | `str` | YAML `commands.<name>` を参照 |
| `args` | `dict[str, Any]` | 既に解決済みの引数 (式は recipe_to_plan で評価) |
| `result_as` | `str \| None` | 後続ステップから参照する変数名 (v0.6.0+) |
| `description` | `str` | 説明 |

### `WaitStep`

指定秒数だけ待機。

| フィールド | 型 | 説明 |
|----------|-----|------|
| `type` | `"wait"` (literal) | discriminator |
| `seconds` | `float` | 待機秒数 (0 以上) |
| `description` | `str` | 説明 |

## 今後追加予定の Step 型

| Step | 追加バージョン | 用途 |
|------|--------------|------|
| `QueryStep` | v0.5.0+ 内部 | クエリ専用 (現在は CommandStep に統合) |
| `WaitUntilStep` | v0.5.1 | 絶対時刻まで待機 |
| `WaitForConditionStep` | v0.5.1 | 条件成立まで定期ポーリング |
| `WaitForStableStep` | v0.5.1 | 測定値が安定するまで待機 |
| `GroupStep` | v0.6.0 | 機器グループへの一斉操作 |
| `BarrierStep` | v0.6.1 | 全並列ステップの同期点 |
| `StaggerStep` | v0.6.1 | 段階的起動 (突入電流対策) |
| `SweepStep` | v0.8.0 | パラメータスイープ |
| `ParallelStep` | v0.8.0 | 並列実行ブランチ |
| `LoopStep` | v0.8.0 | 繰り返し |
| `BranchStep` | v0.8.0 | 条件分岐 |
| `SafeShutdownStep` | v0.8.0 | 安全停止シーケンス |

## Plan 型

```python
class Plan(BaseModel):
    name: str = ""
    parameters: dict[str, Any] = {}
    steps: list[Step] = []
    resource_hint: str | None = None  # 主に使用するリソース名
    metadata: dict[str, Any] = {}
```

## Recipe → Plan 変換

`recipe_executor.recipe_to_plan(recipe, variables)` が YAML の `RecipeDefinition` を `Plan` に変換する。

- `args` の文字列値で `$` 始まりのもの (例: `"$target_v * 1.1"`) を **事前評価**して具体値にする
- `wait.seconds` も同様に式評価対応

```python
from visa_mcp.recipe_executor import recipe_to_plan
from visa_mcp.experiment_ir import CommandStep, WaitStep

plan = recipe_to_plan(
    recipe=session.definition.recipes["set_voltage_and_measure_after_settling"],
    variables={"target_v": 5.0, "current_limit": 0.4, "settle_s": 1.5},
)

# plan.steps == [
#   CommandStep(command="reset", args={}),
#   CommandStep(command="set_voltage_protection", args={"voltage": 6.0}),
#   CommandStep(command="set_current_protection", args={"current": 0.49}),
#   CommandStep(command="set_voltage", args={"voltage": 5.0}),
#   CommandStep(command="set_current", args={"current": 0.4}),
#   CommandStep(command="set_output", args={"state": "ON"}),
#   WaitStep(seconds=1.5),
#   CommandStep(command="measure_voltage", args={}),
#   CommandStep(command="measure_current", args={}),
# ]
```

## Plan 実行

`recipe_executor.execute_plan(visa, session, plan, ...)` が Plan を walk して各 Step を dispatch。

- `CommandStep` → 既存の安全制約 + パラメータ検証 + SCPI 送信パス
- `WaitStep` → `asyncio.sleep(step.seconds)`

将来の追加 Step type は `execute_plan` 内に `isinstance(step, NewStepType)` 分岐を追加するだけ。

## 設計上の不変条件

1. **Step は immutable Pydantic モデル** (`Step = Annotated[Union[...], Field(discriminator="type")]`)
2. **Plan は IR の入口かつ出口** ── Recipe / Group / DSL のいずれもまず Plan に変換される
3. **args は事前解決** ── Step に到達した時点で式評価は完了している
4. **疎結合**: experiment_ir は他 visa_mcp モジュールに import しない (`pydantic` のみ依存)
