# MCP ツール API リファレンス

visa-mcp サーバーが提供する 17 個の MCP ツール詳細。全ツールは Claude などの MCP クライアントから自然言語で呼び出せる。

## 全ツール一覧

| カテゴリ | ツール | 用途 |
|---------|--------|------|
| **発見** | `list_resources` | VISA リソースの列挙 |
| | `list_available_definitions` | YAML 定義一覧 |
| | `reload_definitions` | 定義の再読込 |
| **識別** | `identify_instrument` | 単一機器を `*IDN?` で識別 |
| | `identify_all_instruments` | 全機器を一括識別 |
| | `bind_definition` | 手動バインド（非SCPI機器） |
| | `list_identified_instruments` | 識別済みセッション一覧 |
| **情報** | `get_instrument_info` | 機器仕様・安全制約・応答フォーマット・recipes 等を一括取得 (v0.2.0/v0.3.0) |
| | `list_safety_constraints` | 機器の安全制約のみを抽出 (v0.2.0) |
| | `validate_operation` | 実行せずに事前検証 (v0.2.0) |
| **実行** | `list_commands` | 利用可能コマンド表示 |
| | `execute_named_command` | 名前付きコマンド実行 (override_safety 対応 v0.2.0、応答自動構造化 v0.3.0) |
| | `query_instrument` | 任意のクエリ送信 |
| | `send_command` | 任意のコマンド送信（write） |
| **Recipe** | `list_recipes` | 利用可能な典型ワークフロー一覧 (v0.3.0) |
| | `execute_recipe` | 複数コマンドの安全な順次実行 (v0.3.0) |
| **取込** | `extract_pdf_commands` | PDF から候補抽出 |

---

## 発見系ツール

### `list_resources`
接続中の全 VISA リソースを列挙する。

**入力**: なし
**出力**: `{"resources": ["GPIB0::2::INSTR", "USB0::0x..::INSTR", ...], "count": N}`

### `list_available_definitions`
`instruments/` 配下にロードされた機器定義の一覧。

**入力**: なし
**出力**: `[{"manufacturer": ..., "model": ..., "command_count": N}, ...]`

### `reload_definitions`
ファイルを編集した後、サーバー再起動なしで定義を再読込する。識別済みセッションはクリアされる。

**入力**: なし
**出力**: `{"message": "N 件の定義を再ロードしました", "definition_count": N}`

---

## 識別系ツール

### `identify_instrument`
指定リソースに `*IDN?` を送り、応答から該当する定義を見つけてセッションを作成する。

**入力**:
| 引数 | 型 | 説明 |
|------|----|----|
| `resource_name` | string | VISA リソース文字列 |

**出力**: 識別結果（manufacturer / model / serial / firmware / available_commands）

### `identify_all_instruments`
全リソースに対して一括で `*IDN?` を試行する。

### `bind_definition`
`*IDN?` 非対応の旧世代機器に、定義を手動で紐付ける。

**入力**:
| 引数 | 型 | 説明 |
|------|----|----|
| `resource_name` | string | VISA リソース文字列 |
| `manufacturer` | string | `list_available_definitions` で確認できるメーカー名 |
| `model` | string | モデル名 |

**出力**: バインドされたセッション情報

### `list_identified_instruments`
現在識別済みのセッション一覧。

---

## 実行系ツール

### `list_commands`
識別済み機器で利用可能なコマンド一覧。

**入力**: `resource_name`
**出力**: 各コマンドの SCPI 文字列・型・パラメータ・説明

### `execute_named_command`
YAML で定義された名前付きコマンドを型安全に実行。`{placeholder}` は引数で埋められ、`range` / `choices` で検証される。

**入力**:
| 引数 | 型 | 説明 |
|------|----|----|
| `resource_name` | string | リソース名 |
| `command_name` | string | YAML のコマンドキー |
| `parameters` | object | コマンドパラメータ（任意） |

**出力**:
- write 型: `{"command_name": ..., "scpi_sent": "..."}`
- query 型: 上記 + `{"raw_response": "...", "value": ..., "unit": "..."}`

**例**:
```
execute_named_command(
  resource_name="USB0::0x0B3E::0x1029::SERIAL::INSTR",
  command_name="set_voltage",
  parameters={"voltage": 5.0}
)
→ {"scpi_sent": "VOLT 5.0"}
```

### `query_instrument`
任意のクエリを直接送る。YAML 定義不要だが、安全性チェックはされない。

**入力**:
| 引数 | 型 | 説明 |
|------|----|----|
| `resource_name` | string | リソース名 |
| `command` | string | SCPI 文字列。空文字列の場合は read のみ |
| `timeout_ms` | int | タイムアウト（任意） |

### `send_command`
任意の write コマンド送信。応答は読まない。

---

## 取込系ツール

### `extract_pdf_commands`
PDF マニュアルからコマンド候補を正規表現で抽出する。pdfplumber 使用なのでテキストベース PDF のみ対応（スキャン PDF は事前 OCR が必要）。

**入力**:
| 引数 | 型 | 説明 |
|------|----|----|
| `pdf_path` | string | PDF ファイルパス |

**出力**: 抽出されたコマンド候補のリスト（手動レビュー後 YAML に整形する）

---

## 典型的なワークフロー

### SCPI 機器の場合（例: Kikusui 電源）

```
1. list_resources                    # USB が見える
2. identify_instrument               # *IDN? で自動識別
3. list_commands                     # 使えるコマンドを確認
4. execute_named_command set_voltage # 5V 設定
5. execute_named_command set_output  # 出力 ON
6. execute_named_command measure_voltage  # 実測値
```

### 非 SCPI 機器の場合（例: Yokogawa 7563）

```
1. list_resources                    # GPIB が見える
2. list_available_definitions        # 7563 の定義があるか確認
3. bind_definition                   # 手動バインド
4. execute_named_command set_function_and_range  # F12 R2 (K型熱電対)
5. query_instrument ""               # 測定データ読み出し
```

### 新規機器を追加する場合

```
1. extract_pdf_commands                  # PDF から候補抽出
2. (手動) YAML に整形して instruments/ に配置
3. reload_definitions                    # 反映
4. identify_instrument or bind_definition
5. 動作確認
```

---

## エラーレスポンス

全ツールは失敗時に下記形式のレスポンスを返す：

```json
{
  "success": false,
  "error": "ErrorClassName",
  "message": "人間向けエラーメッセージ"
}
```

代表的なエラー:
- `VisaTimeoutError` — 機器応答なし
- `ResourceNotFound` — リソース名が間違い
- `DefinitionNotFound` — YAML 定義が存在しない
- `ParameterValidationError` — 型・範囲・enum 違反
- `CommandNotFound` — その機器に該当コマンドなし
