# Backend abstraction (v1.1 spike, experimental)

合言葉: **「導入ではなく、抽出可能性の確認」**

v1.1 では `InstrumentBackend` Protocol を `src/visa_mcp/backends/base.py`
に **型ヒントとして** 追加する。既存 `VisaManager` / `MockVisaManager` の
動作は **変更しない**。

## 現状の責務 (v1.x で `visa-mcp` が所有)

| layer | 内容 |
|-------|------|
| MCP tool layer | `validate_experiment_plan` / `start_experiment_job` / `get_job_*` / ... |
| DSL validation / dry-run | `validate_and_compile` |
| Job execution | `JobManager` / `RecipeStep` 経路 / state machine |
| Observation / export / bundle | `get_experiment_timeline` / `export_experiment_results` / `export_experiment_bundle` |
| VISA / SCPI 通信 | `VisaManager` / `MockVisaManager` |

## 将来の backend 境界候補 (Protocol)

```python
class InstrumentBackend(Protocol):
    backend_id: str
    async def list_resources(self) -> list[str]: ...
    async def query(self, resource: str, command: str, ...) -> str: ...
    async def write(self, resource: str, command: str, ...) -> None: ...
```

実装候補 (v1.x 期間中は **不実装**、v1.2+ 検討):

| backend | 用途 |
|---------|------|
| `pyvisa` | 既存 `VisaManager` を adapter 化 |
| `mock` | 既存 `MockVisaManager` (benchmark / CI) |
| `replay` | bundle の過去応答を deterministic に再生 |
| `rest` | REST-controlled device adapter |
| `simulator` | 数学モデル backend |

⚠ **LabVIEW backend は候補に含めない** (これまでの方針通り、外部
GUI runtime の影響を `visa-mcp` に持ち込まない)。

## v1.1 spike のスコープ

| 項目 | v1.1 で実装 |
|------|-----------|
| `InstrumentBackend` Protocol 公開 | ✅ |
| `runtime_checkable` 化 | ✅ |
| 既存 `VisaManager` / `MockVisaManager` を Protocol 明示継承 | ❌ (動作変更を避ける) |
| MCP tool 層に backend を注入する API | ❌ |
| backend plugin loader | ❌ (v1.2+) |
| remote backend | ❌ (v1.x 非対応) |
| 非 VISA production support | ❌ (v1.x 非対応) |
| runtime repository 分割 | ❌ ([`docs/naming_and_repository_strategy.md`](naming_and_repository_strategy.md)) |
| Human intent / approval 層 | ❌ (v1.3+、backend と無関係) |

## なぜ Protocol を「導入しない」のか

- v1.0 で API を凍結したばかりで、内部経路を一度に置換すると stable core を
  揺らすリスクがある
- `VisaManager` と `MockVisaManager` は **既に duck-typed に互換**
  (同じ `query` / `write` async API)。実需が確認できた時点で adapter 化すれば
  十分
- 抽象化を先に作ると、後から実装で噛み合わずに **抽象が間違っていた**
  ことに気付くケースが多い

## Open questions (v1.1.1 追記、v1.2+ で再評価)

Protocol を本格化する際の論点。**v1.1 では決定しない** が、検討候補として
記録:

| 論点 | 説明 |
|------|------|
| stateful session | `write` / `query` 単独だけで stateful な機器セッション (e.g. SCPI mode 設定が後続 query に影響する) を扱えるか。`open_resource` / `close_resource` を Protocol に持たせるか |
| timeout / termination | `timeout_ms` / `read_termination` / `write_termination` を毎回引数で渡すのか、`open_resource` 時に固定するのか |
| binary transfer | binary block / arbitrary binary read を Protocol で扱うか (現在は `str` 限定) |
| encoding | UTF-8 以外の encoding (Shift-JIS 等の旧機器対応) を Protocol で表現するか |
| backend capability | 各 backend が何をサポートするか (e.g. mock は polling 模擬可、replay は時系列順しか返せない、rest は async batch 可) を `capabilities` で公開するか |
| mock / replay / simulator の自然な収まり | 同じ Protocol で「mock backend」「過去 bundle replay」「数式 simulator」が無理なく動くか。一部は専用 super-Protocol が要るかも |
| error mapping | 各 backend 固有 error を `error_class=timeout` / `protocol` / `hardware` 等へどう正規化するか |

これらが解決しないと、Protocol を v1.x 内で stable plugin API として
公開するのは早い。**v1.1.1 時点では `InstrumentBackend` は spike / 設計
検討用 public class** であり、stable plugin API ではない
(`docs/v1_stability_policy.md` 参照)。

## Backend capability model (v1.2 design memo, NOT implemented)

将来 Protocol を本格化する場合、backend ごとに **capability** を宣言する
案 (v1.2 では設計メモのみ):

```yaml
backend_capabilities:
  supports_list_resources: true
  supports_query: true
  supports_write: true
  supports_binary_transfer: false
  supports_streaming: false
  supports_replay: false
  supports_safe_shutdown: true
  deterministic: false        # replay backend は true
  max_concurrent_resources: 8
```

これにより、AI エージェントは backend の機能を事前把握でき、
`validate_experiment_plan` 段階で「この plan は現在の backend で実行
不可」と判定できる。

## Error mapping proposal (v1.2 design memo)

backend 固有の error を `error_class` taxonomy へ正規化する方針案:

| Backend error 例 | 推奨 `error_class` |
|----------------|------------------|
| PyVISA `VisaIOError` (timeout) | `timeout` |
| PyVISA `VisaIOError` (connection lost) | `protocol` |
| mock の `MockTimeoutError` | `timeout` |
| mock の `MockProtocolError` | `protocol` |
| backend の機能未対応 (e.g. binary_transfer 要求) | `validation` + `sub_class=backend_unsupported_capability` |
| replay backend の未記録 step | `validation` + `sub_class=replay_step_not_recorded` |

v1.2 では実装しないが、Protocol 本格化時に統一する。

## 将来 backend 切り出しの判断基準

`docs/naming_and_repository_strategy.md` と同じ条件:

- 非 VISA backend の外部需要が複数件
- Protocol が 3 以上の実 backend で動く
- runtime と backend の coupling が import / 内部 API ゼロ
- 利用者向け移行計画 1 リリース以上で予告

これらが揃えば v1.2+ で本格抽象化、v2.x で repo 分割を検討する。

## 関連 docs

- [`docs/naming_and_repository_strategy.md`](naming_and_repository_strategy.md)
- [`docs/v1_stability_policy.md`](v1_stability_policy.md)
