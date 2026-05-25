# Recipe (典型ワークフロー) システム (v0.3.0)

YAML の `recipes` セクションに複数コマンドの安全なシーケンスを宣言することで、LLM が単一のツール呼び出しで複数ステップを一括実行できる仕組み。

## なぜ必要か

電源の出力 ON ひとつをとっても、本来は次の手順が必要：

1. `*RST` で初期化
2. OVP を設定
3. OCP を設定
4. 電圧設定
5. 電流リミット設定
6. 出力 ON

LLM が毎回この順序を覚えていられる保証はない。recipe として「`safe_output_on`」のような名前で宣言しておけば、LLM は `execute_recipe("safe_output_on", target_v=5)` と呼ぶだけで安全な手順が保証される。

## YAML スキーマ

```yaml
recipes:
  safe_output_on:
    description: |
      OVP/OCP を target_v/current_limit の 110% に自動設定してから安全に出力 ON する。
    parameters:
      - { name: "target_v", type: "float", description: "目標出力電圧 (V)" }
      - { name: "current_limit", type: "float", description: "最大電流 (A)" }
    steps:
      - { command: "reset" }
      - { command: "set_voltage_protection", args: { voltage: "$target_v * 1.1 + 0.5" } }
      - { command: "set_current_protection", args: { current: "$current_limit * 1.1 + 0.05" } }
      - { command: "set_voltage", args: { voltage: "$target_v" } }
      - { command: "set_current", args: { current: "$current_limit" } }
      - { command: "set_output", args: { state: "ON" } }
```

## 引数の式評価

steps の `args` に与える値は、文字列で `$` から始まる場合 **安全な式評価**が適用される。

| 構文 | 動作 |
|------|------|
| `"$target_v"` | パラメータ `target_v` の値に置換 |
| `"$target_v * 1.1"` | 1.1 倍した値 |
| `"$target_v * 1.1 + 0.5"` | 110% + 0.5 |
| `"$x ** 2"` | べき乗 |
| `0.5` | 数値リテラル（評価せずそのまま）|
| `"OFF"` | 文字列リテラル（`$` で始まらない）|

### 許可される演算

- 数値リテラル（int, float）
- 変数参照（recipe パラメータ）
- 四則演算 `+ - * /`
- 剰余 `%`、整数除算 `//`、べき乗 `**`
- 単項演算子 `+x -x`
- 括弧によるグループ化

### 禁止されるもの（セキュリティ）

- 関数呼び出し (`__import__`, `exec`, `open` 等)
- 属性アクセス (`obj.attr`)
- インデックスアクセス (`a[0]`)
- 文字列・リスト・辞書リテラル
- 比較・論理演算

実装は `ast.parse(mode='eval')` 後に許可ノード型のみを通すホワイトリスト方式 (`utils/expression.py`)。

## 安全制約との連携

recipe の各ステップは通常の `execute_named_command` と同じ安全制約検証を受ける。

- `advisory` モード時に違反があれば停止、警告レスポンス
- `override_safety=True` と `override_reason` を `execute_recipe` に渡せば、全ステップで override 適用
- 監査ログには各ステップ個別に記録

途中ステップで失敗・違反した場合、以降のステップは実行されない。

```json
{
  "success": false,
  "recipe": "safe_output_on",
  "halted_at_step": 2,
  "steps_executed": [
    {"step": 0, "success": true, "scpi_sent": "*RST"},
    {"step": 1, "success": true, "scpi_sent": "VOLT:PROT 5.5"},
    {"step": 2, "success": false, "blocked_by_safety": true, "violations": [...]}
  ]
}
```

## MCP ツール

### `list_recipes(resource_name)`

利用可能な recipe 一覧を取得。

```json
{
  "recipes": [
    {
      "name": "safe_output_on",
      "description": "...",
      "parameters": [{"name": "target_v", "type": "float"}],
      "step_count": 6,
      "commands_used": ["reset", "set_voltage_protection", ...]
    }
  ]
}
```

### `execute_recipe(resource_name, recipe_name, parameters, override_safety, override_reason)`

recipe を実行。返り値に各ステップの結果が含まれる。

## サンプル recipes

### PMX35-3A
- `safe_output_on(target_v, current_limit)` — 安全な出力 ON シーケンス
- `safe_shutdown()` — 出力 OFF + 電圧 0 設定
- `read_measurement()` — 電圧/電流を測定

### Yokogawa 7563
- `setup_k_thermocouple(range)` — K 型熱電対セットアップ
- `setup_dcv_200mv()` — DC 電圧 200mV レンジ設定
- `trigger_and_read()` — トリガ + 測定値読出 (構造化応答)

## 設計上の注意点

- recipe ステップの `command` は **同じ機器の YAML で定義されたコマンド**を参照する
- 複数機器をまたぐ recipe は v0.3.0 では非対応（将来課題）
- recipe 内で別の recipe を呼ぶ（recipe of recipes）も非対応（同上）
