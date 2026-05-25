# 応答フォーマットの構造化パース (v0.3.0)

ベンダ独自のデータフォーマットを正規表現で構造化辞書に変換する仕組み。SCPI 機器は NR1/NR2/NR3 等の標準形式が多いが、旧型機器は独自フォーマットを使うことが多い。

## 動機: Yokogawa 7563 の例

Yokogawa 7563 はトーカ指定されたら自動でデータを送出する。応答例：

```
NTKC+00027.2E+0
```

これだけでは LLM もユーザーも何の数値か分からない。手動でドキュメントを引いて

- `N` = Normal (正常)
- `T` = Temperature (温度測定)
- `K` = K type thermocouple
- `C` = Celsius
- `+00027.2E+0` = 27.2

と読み解く必要がある。

これを visa-mcp が自動で次のように構造化する：

```json
{
  "matched": true,
  "raw": "NTKC+00027.2E+0",
  "fields": {
    "status": "Normal",
    "func": "T",
    "tc_type": "K",
    "unit": "celsius",
    "value": 27.2
  }
}
```

LLM はこの構造化結果から直接「温度は 27.2℃」と判断できる。

## YAML スキーマ

```yaml
response_formats:
  measurement_data:
    pattern: '^(?P<status>[NFOTBC])(?P<func>[NTRKEJSB])(?P<tc_type>[KCFVA])(?P<unit>[CFKVNA])(?P<value>[+-]\d+\.\d+E[+-]\d+)\s*$'
    description: "7563 のデータ応答形式"
    fields:
      status:
        N: "Normal"
        O: "Over range"
        F: "Failure (burnout 等)"
        T: "Trigger waiting"
        B: "Burnout detected"
      unit:
        C: "celsius"
        F: "fahrenheit"
        K: "kelvin"
```

### スキーマ要素

| キー | 説明 |
|------|------|
| `pattern` | Python 正規表現。**名前付きグループ** `(?P<name>...)` 必須 |
| `description` | このフォーマットの説明（情報のみ） |
| `fields` | 名前付きグループ → コード→ヒューマンリーダブル名の辞書 |

### `value` グループの特別扱い

`value` という名前のグループにマッチした文字列は、自動的に `float` への変換が試みられる。失敗した場合は文字列のまま。

## コマンド側からの参照

通常コマンドの `returns.format` フィールドで `response_formats` のキーを参照する。

```yaml
commands:
  read_measurement:
    scpi: ""
    type: "query"
    returns:
      type: "string"
      format: "measurement_data"   # ← response_formats.measurement_data を適用
```

`execute_named_command` の応答に `parsed` フィールドが追加される。

```json
{
  "success": true,
  "data": {
    "command_name": "read_measurement",
    "scpi_sent": "",
    "raw_response": "NTKC+00027.2E+0",
    "value": "NTKC+00027.2E+0",
    "parsed": {
      "matched": true,
      "fields": {"status": "Normal", "value": 27.2, "unit": "celsius", ...}
    }
  }
}
```

## マッチしない場合

正規表現にマッチしない応答（機器がエラー文字列を返した等）は次のように返る：

```json
{
  "matched": false,
  "fields": {},
  "raw": "<original response>"
}
```

これにより LLM はパース失敗を検知して fallback 動作（例: 生応答を文字列として扱う）を選択できる。

## 設計上の注意

- 正規表現は Python の `re` モジュール準拠
- `fields` のマッピングがない名前付きグループはキャプチャ文字列がそのまま入る
- 1 機器に複数の `response_formats` を定義可能（例: 測定データ用とエラー応答用）
- 構造化パーサは現状 read-only（write 系コマンドの引数フォーマットには関与しない）
