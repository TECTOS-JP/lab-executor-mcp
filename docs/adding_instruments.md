# 機器定義の追加ガイド

新しい計測機器を visa-mcp で扱うには、`instruments/` 配下に YAML ファイルを追加します。本ガイドではテンプレートを起点にした作成手順を説明します。

## 全体の流れ

1. **テンプレートをコピー**: `instruments/_template.yaml` を `<vendor>_<model>.yaml` という名前でコピー
2. **メタデータ・接続設定を埋める**
3. **コマンドを定義**: マニュアルから SCPI/独自コマンドを抜き出して定義
4. **サーバー再起動 or `reload_definitions` ツールを呼ぶ**

サンプルとして `examples/instruments/` に下記が同梱されています：

- `kikusui_pmx35_3a.yaml` — 標準 SCPI の電源（`*IDN?` 自動識別）
- `yokogawa_7563.yaml` — 旧世代の非 SCPI 温度計（手動バインド）

## YAML スキーマ

### `metadata` セクション

```yaml
metadata:
  manufacturer: "Kikusui"        # メーカー名（自由表記）
  model: "PMX35-3A"              # モデル番号
  description: "直流安定化電源"   # 一行説明
  manual_ref: "PMX_IF_J6.pdf"    # 参照したマニュアル（任意）
```

### `identification` セクション

`*IDN?` の応答から本機器を識別するためのパターン。

```yaml
identification:
  manufacturer_match: "KIKUSUI"   # *IDN? の第1フィールドに含まれる文字列
  model_regex: "PMX35-3A"         # 第2フィールドの正規表現
```

`*IDN?` 非対応機器の場合もこの項目は記述しますが、識別には使われず `bind_definition` MCP ツールで手動紐付けします。

### `connection` セクション

```yaml
connection:
  default_timeout_ms: 3000          # VISA タイムアウト
  read_termination: "\n"            # 受信終端
  write_termination: "\n"           # 送信終端
  serial:                           # RS-232 接続時のみ
    baud_rate: 9600
    data_bits: 8
    parity: "N"
    stop_bits: 1
```

### `commands` セクション

コマンドは「キー（実装側で呼ぶ名前）」と「scpi（実際に送信される文字列）」のペア。

#### write コマンド（パラメータなし）

```yaml
reset:
  scpi: "*RST"
  type: "write"
  description: "パネル設定を初期化"
  parameters: []
```

#### write コマンド（パラメータあり）

`{name}` プレースホルダで動的に埋め込み。`parameters` で型・範囲を宣言。

```yaml
set_voltage:
  scpi: "VOLT {voltage}"
  type: "write"
  description: "出力電圧を設定"
  parameters:
    - name: voltage
      type: "float"
      range: [0, 36.75]      # 範囲外はバリデーションエラー
      description: "電圧値 (V)"
```

#### query コマンド（応答あり）

```yaml
measure_voltage:
  scpi: "MEAS:VOLT?"
  type: "query"
  description: "出力電圧の実測値"
  parameters: []
  returns:
    type: "float"
    unit: "V"
```

### パラメータ型

| `type` | 用途 | 追加プロパティ |
|--------|------|--------------|
| `integer` | 整数 | `range: [min, max]` |
| `float` | 浮動小数点 | `range: [min, max]` |
| `enum` | 列挙値 | `choices: ["A", "B"]` |
| `string` | 任意文字列 | 通常不要 |

## マニュアル PDF からの自動抽出

テキストベース PDF の場合、MCP ツール `extract_pdf_commands` でコマンド候補を抽出できます。

```
あなた: PMX_IF_J6.pdf からコマンドを抽出してください
Claude: [extract_pdf_commands を呼び出し] → 抽出された候補一覧を YAML 形式で提示
```

抽出結果は **必ず手動でレビュー** してください。マニュアル PDF が画像ベース（スキャン PDF）の場合は事前に OCR が必要です。

## 非 SCPI 機器の扱い

`*IDN?` が実装されていない／応答が不正な機器の場合：

1. YAML の `identification` は形だけ書いておけば OK
2. クライアントは `bind_definition` ツールでリソース名と定義を手動紐付け：

```
あなた: GPIB0::5::INSTR を Yokogawa の 7563 として登録してください
Claude: [bind_definition を呼び出し] → セッション作成
```

3. 以後は通常の `execute_named_command` で操作可能

## サンプル: 最小定義

```yaml
metadata:
  manufacturer: "Acme"
  model: "Widget-1"
  description: "Acme Widget"

identification:
  manufacturer_match: "ACME"
  model_regex: "Widget-1"

connection:
  default_timeout_ms: 3000
  read_termination: "\n"
  write_termination: "\n"

commands:
  identify:
    scpi: "*IDN?"
    type: "query"
    description: "機器識別"
    parameters: []
    returns:
      type: "string"

  set_value:
    scpi: "VAL {x}"
    type: "write"
    description: "値設定"
    parameters:
      - name: x
        type: "float"
        range: [0, 100]
```

## トラブルシューティング

| 症状 | 原因の候補 |
|------|----------|
| YAML が読み込まれない | スキーマ違反（pydantic エラーログ確認）／拡張子 `.yaml` でない |
| パラメータ検証エラー | `range` の境界外、`choices` 外の値 |
| コマンド送信成功するが応答が変 | `read_termination` / `write_termination` が機器と不一致 |
| 識別されない | `manufacturer_match` の大文字小文字を確認、`model_regex` を緩める |
