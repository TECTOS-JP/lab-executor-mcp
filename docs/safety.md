# 安全制約システム (v0.2.0)

visa-mcp v0.2.0 から、YAML 定義に **`safety` セクション**を記述することで、LLM の指示によるハードウェア損傷リスクを多層的に防止できます。

## 設計方針

- **汎用性**: 機器ごと・現場ごとに必要な安全レベルを選択可能
- **情報の最大共有**: LLM に安全制約を可視化し、能動的に守らせる
- **Override 可能**: 必要時には明示的に警告を上書きできる
- **監査追跡**: すべての override は理由付きでログ記録

## 安全モード

サーバー起動時の環境変数 `VISA_MCP_SAFETY_MODE` で選択します。

| モード | 違反時の動作 | Override | 用途 |
|-------|-----------|---------|------|
| **`strict`**（**v0.4.0 からのデフォルト**） | エラー返却、実行ブロック | **不可** | LLM 主体運用・教育・無人運転 |
| `advisory` | 警告返却、要 override | 可（理由必須） | 研究開発（人間が同席） |
| `permissive` | ログ記録のみ、警告も返さない | 不要 | 習熟者の手動操作（非推奨）|

> **v0.3.0 → v0.4.0 の変更点**: デフォルトモードを `advisory` から `strict` に変更しました。
> override を使いたい場合は明示的に `VISA_MCP_SAFETY_MODE=advisory` を指定してください。
> 環境変数未設定時には警告ログが出ます。

## Raw SCPI コマンド (`unsafe_send_command` / `unsafe_query_instrument`)

`execute_named_command` は YAML 定義経由の検証を通りますが、任意 SCPI 文字列を送る生コマンド機能は **デフォルトで無効化**されています。

| 設定 | 動作 |
|------|------|
| 既定 | raw コマンドツールは登録されない |
| `VISA_MCP_ENABLE_RAW_COMMANDS=1` | `unsafe_send_command` / `unsafe_query_instrument` が登録される |
| `strict` モード | 環境変数の有無にかかわらず raw ツールは登録されない |

raw ツール内では SCPI 文字列を簡易解析し、`VOLT` `CURR` `OUTP` `*RST` 等の **危険キーワード** が含まれる場合は警告を返します。`override_safety=True` + `override_reason` の指定で実行できます（advisory モード時のみ）。

すべての raw コマンドは監査ログに記録されます。

```bash
# Windows
set VISA_MCP_SAFETY_MODE=strict
python -m visa_mcp.server

# Linux/macOS
VISA_MCP_SAFETY_MODE=strict python -m visa_mcp.server
```

## 監査ログ

すべての安全制約違反は JSON Lines 形式でログに記録されます。

- 既定パス: `~/.visa-mcp/audit.log`
- カスタマイズ: 環境変数 `VISA_MCP_AUDIT_LOG`

ログ例:

```jsonl
{"ts": "2026-05-19T17:30:00", "resource": "USB0::...", "command": "set_voltage", "parameters": {"voltage": 50.0}, "violations": [{"violation_type": "absolute_max_exceeded", "severity": "high", ...}], "action": "proceed_with_override", "mode": "advisory", "override_safety": true, "override_reason": "ユーザー指示による高電圧テスト"}
```

## YAML スキーマ

### `safety.ratings` — 値の制約

数値パラメータの絶対最大・推奨上限を宣言。**パラメータ名と rating のキーが部分一致**する場合に自動チェック。

```yaml
safety:
  ratings:
    voltage:
      rated: 35.0           # メーカ定格 (情報のみ)
      absolute_max: 36.75   # 絶対最大 (超過 → 高重要度警告)
      recommended_max: 35.0 # 推奨上限 (超過 → 低重要度警告)
      absolute_min: 0       # 絶対最小 (任意)
      unit: "V"
      description: "出力電圧の絶対最大定格"
```

**コマンド `set_voltage` のパラメータ `voltage` 50.0 を送信 → `voltage` という rating キーで照合 → `absolute_max=36.75` 超過で警告**

### `safety.preconditions` — 順序・状態制約

特定のコマンドを実行する前に、別のコマンドが呼ばれていることを要求する。

```yaml
safety:
  preconditions:
    - command: "set_output"
      when: { state: ["ON", "1"] }       # state=ON のときだけチェック
      requires:
        - { has_been_called: "set_voltage_protection" }
        - { has_been_called: "set_current_protection" }
      severity: "medium"
      reason: "出力 ON 前に過電圧・過電流保護を設定すること"
```

セッション内のコマンド履歴は `SessionManager` が保持。新規セッション（`identify_instrument` / `bind_definition` で作成）ごとにリセットされる。

### `safety.cautions` — 自然言語の注意事項

機械では検知不可能な「禁止行為」を LLM に伝える情報共有。

```yaml
safety:
  cautions:
    - "出力端子を短絡しないこと"
    - "誘導性負荷は OUTP OFF で急遮断しないこと"
```

### `safety.hardware_protections` — 機器側保護機能

LLM が「機器側に既に保護機能がある」ことを知り、適切に利用できるようにする。

```yaml
safety:
  hardware_protections:
    - name: "OVP"
      description: "VOLT:PROT で設定。設定値超過時に出力遮断"
      related_command: "set_voltage_protection"
```

## Override 機構

`advisory` モードで違反が発生した場合、`execute_named_command` に追加引数を指定して再実行できます。

```
# 通常呼び出し（違反あり → ブロック）
execute_named_command(
    resource_name="USB...",
    command_name="set_voltage",
    parameters={"voltage": 50.0}
)
→ {"success": false, "blocked_by_safety": true, "violations": [...]}

# Override（必ず reason を記述）
execute_named_command(
    resource_name="USB...",
    command_name="set_voltage",
    parameters={"voltage": 50.0},
    override_safety=True,
    override_reason="ユーザー確認済み、意図的な絶対最大超過テスト"
)
→ {"success": true, "safety_violations_overridden": [...]}
```

**Override の制約:**
- `strict` モードでは `override_safety=True` を指定しても効果なし、常にブロック
- `advisory` モードでは `override_reason` が空文字列だとブロック
- すべての override は監査ログに記録

## LLM 向け情報提供ツール

### `get_instrument_info(resource_name)`
機器の全情報（仕様・安全制約・応答フォーマット・現在の安全モード等）を一括取得。

### `list_safety_constraints(resource_name)`
特定機器の安全制約のみを抽出。

### `validate_operation(resource_name, command_name, parameters)`
実行せずに事前検証のみ行う dry-run。LLM が「これは安全か？」を判断できる。

```json
{
  "valid": false,
  "scpi_to_send": "VOLT 50.0",
  "parameter_errors": [],
  "safety_violations": [
    {"violation_type": "absolute_max_exceeded", "severity": "high", ...}
  ],
  "safety_mode": "advisory",
  "would_block": true,
  "can_override": true
}
```

## 推奨ワークフロー (LLM 側)

1. **機器識別後すぐに `get_instrument_info` を呼ぶ**
   - 安全制約・推奨手順を context に取り込む
2. **危険そうな操作の前に `validate_operation` で事前確認**
   - 実機を動かす前に違反を発見できる
3. **`execute_named_command` で実行**
   - 違反があれば警告レスポンスを受け取る
4. **必要時のみユーザーに確認を取り override**
   - LLM は勝手に override せず、ユーザー判断を仰ぐ

## トラブルシューティング

| 症状 | 対処 |
|------|------|
| 安全制約が効かない | YAML の `safety.ratings` のキーがパラメータ名と部分一致するか確認 |
| `permissive` モードで実行したつもりが警告される | `VISA_MCP_SAFETY_MODE=permissive` が設定されているか確認 |
| `strict` モードから抜けられない | サーバ再起動 + 環境変数変更が必要 |
| 監査ログが書かれない | `~/.visa-mcp/` ディレクトリの書込権限を確認 |
