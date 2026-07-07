# 実験資産スキーマ v0.1 — 独立可用性レベルの運用定義

作成: 2026-07-06 / 起草: Claude Fable 5（構想 Phase 2 の要石）
根拠: MaiML（JIS K 0200:2024）の独立可用性概念（一次資料: 顕微鏡 Vol.59 No.1, 2024,
doi:10.11410/kenbikyo.59.1_20）+ 自動実験エコシステム構想メモ（2026-07-02）の L0〜L5 案。

> **実装状況: v0.1 実装済み (v2.25.0)**。`lab-executor asset export` / `asset check`
> と `lab_executor.asset` パッケージで本スキーマの L0〜L5 判定を提供する。利用手順は
> [`asset_usage.md`](asset_usage.md)。実装で確定した細部:
> - `conditions.calibration` / `environment` は **キー必須**（値が `not_recorded`
>   でも可、キー欠落は L2 不成立）。
> - `level_verified` は資産に書き込まず、`asset check` のレポートで返す。
> - `requires:`（L4 の capability 宣言）は `RecipeDefinition` の **optional**
>   フィールド。既存 YAML の検証結果は不変。
> - `dry_run.ok`（L5）は **v0.2 (v2.26.0) で builder が記入できる**。
>   `asset export --dry-run-now` で export 時に同梱レシピを `recipe_to_plan` で
>   コンパイル検証し、同梱装置定義を検証して結果を書き込む（成功 `ok=true` +
>   `step_count`、失敗 `ok=false` + `error`、ただし export 自体は失敗しない）。
>   v0.1 では builder は自動記入せず API 経由 (`build_asset`) でのみ L5 に到達できた。

## 目的

「実験資産（experiment asset）」の品質を **機械判定可能な独立可用性レベル L0〜L5** で
格付けする運用スキーマを定義する。将来の実験資産流通（Phase 3）では本レベルが
品質指標・価格シグナルになる。**公開性を理念でなく経済合理性にする**ための土台。

- **独立可用性**（MaiML 定義）: 十分なメタデータを内包し、追加情報なしに多様な観点から
  実験を再現できること =「自立したデータ」
- MaiML の「限定的独立可用性」（達成可能な範囲から段階的に）を、明示的なレベルとして刻む

## 実験資産の単位

実験資産 = **1つの zip アーカイブ**（lab-executor の `export_experiment_bundle` を基礎に拡張）。
必須の最上位ファイル `asset.yaml`（マニフェスト）を持つ:

```yaml
asset_version: "0.1"
asset_id: <uuid4>            # MaiML の <uuid> に対応。資産の一意性
level_declared: 3            # 作成者の宣言レベル
level_verified: null         # 検証者/検証ツールが確認したレベル (未検証は null)
title: <人間可読なタイトル>
created_at: <ISO8601>
license: <SPDX id または独自>
provenance:
  producer: <匿名化可の作成者識別子>
  runtime: lab-executor-mcp <version>
  git_commit: <レシピ/定義の来歴 commit>   # UI M3 の git 保存が supply する
contents:                    # 同梱物の宣言 (各エントリに sha256)
  - {path: results/..., sha256: ..., kind: results}
  - {path: recipe/...,  sha256: ..., kind: recipe}
```

同梱ファイルはすべて sha256 を持つ（MaiML `<insertion>`+`<hash>` と同型の改ざん検知）。

## レベル定義（機械判定基準付き）

各レベルは**下位レベルの要件をすべて含む**（累積的）。判定は将来の
`lab-executor asset check <zip>` が行う想定（v0.1 では基準の定義のみ）。

### L0 — 生データのみ
- 要件: 測定値の系列（CSV/JSON 等）が存在する
- 判定: `contents` に `kind: results` が1つ以上
- lab-executor 対応: `get_experiment_results` の出力

### L1 — 測定条件が付属
- 要件: 何を・いつ・どのパラメータで測ったか
- 判定: recipe 名 + 解決済みパラメータ + 実行タイムスタンプ + resource 識別子が
  構造化データで存在（`kind: run_metadata`）
- lab-executor 対応: jobs テーブル（recipe / parameters_json / created_at / resource_name）

### L2 — 装置・校正・環境・解析条件が付属
- 要件: 第三者が「どんな装置構成で得たデータか」を判断できる
- 判定: 装置定義 YAML（コマンド・safety 定義込み）+ SystemConfig + 実行ログ
  （steps/events）+ 校正・環境情報（ある場合。無い場合は `not_recorded` を明示 —
  **欠落の明示**は L2 の要件。暗黙の欠落は不可）
- lab-executor 対応: instrument definition / `_system.yaml` / job_steps / job_events / audit

### L3 — 第三者が再解析可能
- 要件: 資産だけで解析の再実行・検算ができる（MaiML の実用的な独立可用ライン）
- 判定: 生応答（raw_response）↔ 数値化（value_numeric）の対応が全測定点で保持 +
  解析手順（スクリプト or 手順記述）同梱 + `asset.yaml` の sha256 が全て一致 +
  スキーマ検証 pass
- lab-executor 対応: export bundle（決定論的再現性）+ sweep/instrument views

### L4 — 代替装置で追試可能
- 要件: 特定個体・特定型番に依存せず実験を記述できている
- 判定: レシピが**名前付きコマンド（抽象操作）のみ**で記述され、必要 capability の
  一覧（例: `set_voltage`, `measure_voltage`, 範囲・精度要件）が明示されている。
  代替装置の定義 YAML が capability 一覧を満たすかを機械照合できる
- lab-executor 対応: registry の named commands + instrument definition の照合
  （**現状ギャップ**: capability 要件の宣言形式が未実装 → Phase 2 実装課題 #1）

### L5 — AI エージェントが安全確認後に再実行可能
- 要件: 資産単体で自動再実行の可否判断と実行ができる
- 判定: 実行可能レシピ + safety メタデータ（ratings / safe_shutdown / verify 定義）+
  危険性情報（電圧・温度・化学物質等の上限宣言、該当なしの明示）+
  dry-run 記録（検証済みであること）+ 再実行時の期待結果（許容範囲付き）
- lab-executor 対応: safety.py の strict 検査 + recipe verify + UI M3 の dry-run。
  （**現状ギャップ**: 危険性宣言と期待結果の形式が未実装 → Phase 2 実装課題 #2）

## MaiML との関係（採用方針）

**概念は全面採用、ファイル形式は当面は部分採用。**

| 項目 | 方針 |
|---|---|
| 独立可用性の定義・段階論 | 全面採用（本スキーマの根拠） |
| UUID 一意性 / hash 改ざん検知 | 採用（asset_id / contents sha256） |
| XES 的操作ログ | lab-executor の job_events/audit で実質充足 |
| XML（MaiML ファイル）出力 | **将来のコンバータ課題**（export bundle → MaiML 変換器。JIS K 0200 準拠出力は Phase 3 で市場互換性が必要になった時点で実装） |
| `<material>`（試料）モデル | **未対応**。試料管理は lab-executor に概念が無い → Phase 2 実装課題 #3（asset.yaml に `sample:` セクションを予約） |
| キー名前空間 + オントロジー | v0.1 ではキーを固定小語彙で定義し、`x-<namespace>:` プレフィックスで拡張許可 |

## Phase 2 実装課題（優先順）

1. **capability 要件宣言**（L4 の判定基盤）— レシピに `requires:` セクションを追加し、
   装置定義との機械照合を実装
2. **安全メタデータと期待結果**（L5 の判定基盤）— 危険性上限宣言 + 再実行期待値
3. **試料（sample）セクション** — MaiML `<material>` 相当の最小実装（UUID + 自由メタデータ）
4. `lab-executor asset check` — 本スキーマの機械判定ツール（validate/dry-run と同じ
   「保存前ゲート」思想で、資産作成時に L 判定を刻印）
5. export bundle → asset.yaml 生成の自動化（L3 までは既存データでほぼ自動達成可能）

## 改訂方針

- 本書は v0.x の間は破壊的変更可。asset_version で識別
- レベル判定基準の変更は、既存資産の level_verified を無効化する（再検証が必要）
