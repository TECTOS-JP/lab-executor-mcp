# Naming and Repository Strategy (v1.1)

合言葉: **「分離するのではなく、分離できるかを判断できる状態にする」**

## Current status

**Default decision for v1.x**: `visa-mcp` を唯一の正式リポジトリとして継続する。

**Exception**: 後述の「Decision criteria」が複数同時に満たされ、利用者向け
deprecation / migration plan を 1 リリース以上で予告できる場合に限り、
v1.x 内でも分割を **再評価** する余地を残す (v1.1.1 で表現を緩和)。

- v1.0 で Stable / Experimental API 分類、Schema stable 化、reproducibility
  bundle MVP までを安定化済み
- 利用者・docs・registry・CI が `visa-mcp` 名で安定動作

## Why NOT split now

| 理由 | 詳細 |
|------|------|
| 安定化直後の動揺回避 | v1.0 stable core を出した直後にリポジトリ / package 分割すると、互換性対応・docs 移行・registry 二重化で利用者に混乱を与える |
| backend boundary 未証明 | runtime と VISA backend の境界はまだ完全には分離されていない (実装上の coupling が残る) |
| backend abstraction 未実装 | 分離するなら先に Protocol が要る (v1.1 で spike 開始するのみ) |
| docs / registry / schemas / CI が統合済み | 分割すると 2 倍の維持コスト |
| 非 VISA backend の外部需要が未検証 | 仮想需要で先回り分割するのは禁物 |

## Candidate future split (informal, NOT planned for v1.x)

| リポジトリ (仮称) | 役割 |
|-----------------|------|
| `visa-mcp` | VISA / SCPI backend + 共通 schema |
| `lab-executor-mcp` (仮称) | 実験 runtime (Job / DSL / Observation / Benchmark) backend non-dependent |

> ⚠ **v1.1 では実装しない**。これは将来検討用の図示のみ。
> 仮称 `lab-executor-mcp` を予約済みとも未予約とも宣言しない。

## Decision criteria (v1.x 内で再評価する条件)

以下が複数満たされた時点で再評価:

- 非 VISA backend (REST device / simulator / proprietary protocol) の
  **外部需要**が複数件確認できる
- backend abstraction Protocol が **3 以上の実 backend** で動いている
- runtime と backend の coupling が **import / 内部 API 依存ゼロ**に近づく
- registry / docs / CI を分離しても利用者が迷わない構成案がある
- 既存利用者へ **deprecation スケジュール 1 リリース以上** で予告できる

これらが揃わない限り、v1.x 内では分割しない。

## v1.1 decision

| 項目 | 決定 |
|------|------|
| リポジトリ分割 | **行わない** |
| package 分割 | **行わない** |
| 名称変更 | **行わない** |
| backend abstraction 実装 | **spike のみ** (`src/visa_mcp/backends/base.py`) |
| `lab-executor-mcp` 仮称の正式予約 | **行わない** |
| 利用者向けアナウンス | この docs に記載する程度 |

## v1.2+ 候補 (備忘)

- v1.2: Plugin / Extension mechanism (現リポジトリ内で完結)
- v1.3+: Human intent / approval (現リポジトリ内で完結)
- v2.x: 上記条件が揃った場合に限り、リポジトリ分割を検討

## 関連 docs

- [`docs/backend_abstraction.md`](backend_abstraction.md) — backend
  Protocol の責務境界 (v1.1 spike)
- [`docs/v1_stability_policy.md`](v1_stability_policy.md) — Stable / Experimental
  分類
