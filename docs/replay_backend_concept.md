# Replay Backend Concept (v1.2, design notes — NOT implemented)

合言葉: **「replay backend は概念整理のみ。実装は v1.3+ 候補」**

`export_experiment_bundle` (v1.0) と `validate_experiment_bundle` /
`inspect_experiment_bundle` (v1.1) で実験記録を zip 化・検証できるよう
になった。次の自然な発展として **bundle を replay** したくなるが、
v1.2 では実装しない。本ドキュメントは **何が replay 可能で、何が
不可能か** を整理する。

## Replay backend とは

過去 bundle に記録された応答を deterministic な backend として再生し、
`validate_experiment_plan` / `dry_run_plan` / 限定的な `start_experiment_job`
に対して **実機ノータッチで同じ結果**を返せるようにする概念。

## What replay CAN do (将来 v1.3+ 候補)

- 過去 bundle の `results.jsonl` を順序通り返す
- step_index / target_id でマッチング
- `compatibility.can_be_validated: true` (現状 v1.1)
- 解析・教育・demo・regression test

## What replay CANNOT do (構造的に困難)

- **応答タイミング (latency / jitter) の再現**: bundle に記録されていない
- **副作用の再現**: 機器の物理状態変化 (温度 / 出力電圧の物理値) は記録しない
- **未記録 step のシミュレーション**: 過去にない step を実行できない
- **interactive 動作**: cancel / resume / safety override の挙動
- **firmware bug や hardware quirk** の再現
- **wall-clock time に依存する logic** (`wait_until`)

## 必要な追加情報 (v1.3+ 候補)

deterministic replay を実装するには、bundle に以下が記録されている必要が
ある:

```yaml
replay_metadata:
  recording_version: 1
  step_response_map:
    "steps[0]": { command: "VOLT 5.0", response: null, latency_ms: 12 }
    "steps[1]": { command: "MEAS:VOLT?", response: "5.012", latency_ms: 8 }
  determinism_guarantee: false  # 機器状態 / 時刻依存ありの旨
```

これは **現在の `export_experiment_bundle` には含まれていない**。
v1.3 で bundle layout を `bundle_version=1.1` 等に拡張する場合に検討。

## `inspect_experiment_bundle.compatibility.replay_preconditions`
(v1.3+ 候補)

v1.2 では `can_be_replayed=false` のみ返している。v1.3 で以下のような
詳細を返す候補:

```json
{
  "compatibility": {
    "can_be_replayed": false,
    "replay_preconditions": [
      "Replay backend not implemented in v1.2",
      "Bundle lacks deterministic response map (recording_version<1)",
      "Instrument response timing not recorded",
      "Cancel/safe_shutdown side effects not deterministic"
    ]
  }
}
```

## Why NOT v1.2

- v1.0 stable core を直近 4 リリースで凍結・固めたばかり
- replay は backend abstraction + bundle layout 拡張 + plugin lifecycle が
  揃って初めて意味を持つ
- v1.2 は **definition extension release** に集中する方が安全

## v1.3+ ロードマップ memo

1. v1.3: `bundle_version=1.1` で deterministic response map を optional 追加
2. v1.4: replay backend skeleton (`InstrumentBackend` capability 実装)
3. v1.5: `replay_bundle_with_mock(path)` 検討

これらは正式 commit ではなく、検討候補。

## 関連 docs

- [`bundle_export.md`](bundle_export.md) — bundle layout 現状
- [`backend_abstraction.md`](backend_abstraction.md) — backend Protocol
- [`extension_policy.md`](extension_policy.md) — v1.2 の拡張ポリシー
