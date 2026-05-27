# 変更履歴

## v2.10.0 — Rollback / Cleanup Plan Refinement

合言葉: **「削除実行に進む前に、plan の精度と UX を上げる」**

v2.9 で `rollback-plan` / `cleanup-plan` が入った次の段階。**実削除
には進まず**、分類整理 / verify 統合 / `--latest` UX / status semantics
を改善する。`--apply` は v2.11+ で慎重に検討。

### `--latest` flag (P0)

`migration-log {inspect,verify,rollback-plan,cleanup-plan}` 全てで
`--latest` を導入:

```bash
lab-executor extension migration-log verify --latest
lab-executor extension migration-log rollback-plan --latest
lab-executor extension migration-log cleanup-plan --latest
```

- `operation == extension_copy_apply` の最新 manifest を自動選択
- 明示 manifest path との併用は usage error (exit 2)
- 該当 manifest が無ければ exit 1

新 API: `find_latest_extension_copy_manifest(log_dir=None) -> Path | None`

### Rollback plan 分類改善 (P0)

`target_missing` を `blocked_reasons` から **`already_absent` リスト
に分離**。「削除対象が既に無い = 異常ではなく対象外」を明示。

```python
@dataclass
class ExtensionRollbackPlan:
    status: str
    candidates: list[...]
    already_absent: list[dict]   # ← v2.10 新規
    blocked_reasons: list[dict]  # ← legacy_source_missing 等の real block のみ
    warnings: list[dict]
```

summary に `already_absent` カウントを追加。schema_version=`v2.10`。

### Cleanup plan 改善 (P0)

1. **verify 統合**: 内部で `verify_extension_migration_log()` を呼び、
   verify の error/warning を cleanup-plan の blocked/warning に変換。
   verify 条件が一元化される。
2. **`already_cleaned_or_missing` warning を `legacy_source_missing`
   リストに分離**。v2.10 時点では実 cleanup が無いため「既に整理済」
   と断定できない。構造化して報告するに留める。
3. `delete_performed_unexpected` / `overwrite_performed_unexpected`
   等の overall meta error は cleanup-plan を全体 block する。

```python
@dataclass
class ExtensionCleanupPlan:
    status: str
    candidates: list[...]
    legacy_source_missing: list[dict]   # ← v2.10 新規
    blocked_reasons: list[dict]
    warnings: list[dict]
```

### Plan-only warning と status の分離 (P0、案 A)

v2.9 では `plan_only` warning を常に追加するため status が常に
warning になっていた。v2.10 で **案 A** を採用:

- 実 problem が無ければ `status="ok"`
- plan-only warning は `warnings[]` に残るが status を格上げしない
- `--strict` は real problem だけで exit 1 化 (plan-only では exit 0)

これにより CI で `--strict` を安全に使えるようになる (plan-only
状態で false fail しない)。

### CLI 挙動

```bash
# 正常系: status=ok / exit 0 / --strict でも 0
lab-executor extension migration-log rollback-plan --latest --strict

# duplicate / target missing for cleanup 等の real problem
# -> status=warning|error / --strict で exit 1
```

### v2.10 で **やらないこと**

- rollback `--apply` / cleanup `--apply`
- target 削除 / legacy source 削除 / source 復元
- overwrite / install default 変更
- manifest schema 破壊変更
- extension pack / `.install_meta.json` schema 変更
- remote registry / signing / trust store

### Tests (181 件 pass)

`tests/test_v2_10_rollback_cleanup_refinement.py`: 18 件

- `find_latest_extension_copy_manifest` (順序 / empty)
- `--latest` CLI (verify / inspect / rollback-plan / cleanup-plan)
- `--latest` と明示 path の併用は exit 2
- 該当 manifest 無し → exit 1
- cleanup-plan が verify 結果を使うこと (extension_id_mismatch /
  delete_performed_unexpected 全体 block)
- plan-only warning だけなら status=ok / `--strict` でも exit 0
- rollback の already_absent / blocked 分離
- no_file_changes (snapshot 比較)
- Boundary: PyVISA / `visa_mcp` 非依存
- 回帰: install_default / tool surface 不変

既存 v2.9 tests を v2.10 schema へ更新 (status=ok 期待、
already_absent / legacy_source_missing リスト、schema_version=
`v2.10`)。

### docs / cli docstring

- `docs/extension_path_migration.md`: v2.10 セクション追加 (`--latest`
  / 案 A status semantics / 状態別ふるまい表 / Command matrix)
- `cli.py` module docstring を v2.9.x → v2.10.x

### 互換性

- `ExtensionRollbackPlan` / `ExtensionCleanupPlan` の dict 表現に
  `already_absent` / `legacy_source_missing` field が追加 (v2.9 では
  存在しなかった)
- schema_version は v2.9 → v2.10 に上がる
- status 値の semantics が変化 (v2.9 までは plan-only でも warning、
  v2.10 から ok)。CI で `--strict` を使っていた箇所は安全側に動く
- MCP tool / DSL / extension pack 形式 / `.install_meta.json` /
  `default_extensions_dir()` 返り値、すべて不変

---

## v2.9.0 — Extension Rollback / Cleanup Planning

合言葉: **「v2.7 で copy、v2.8 で verify、v2.9 で戻すか進めるかの
計画。まだ削除しない」**

v2.8 で `verify_extension_migration_log()` を使い copy 結果を検証
できるようになった次の段階。**rollback (取り消し)** と **cleanup
(legacy 整理)** の方向性が逆の 2 種類の計画を CLI / API で出せる
ようにする。実削除は v2.9 でも一切しない (`apply_available=False`
固定)。

### 新規 API (`extension_migration_log.py`)

```python
@dataclass(frozen=True)
class ExtensionRollbackCandidate:
    extension_id: str
    target: Path                  # 取り消し時に削除候補
    legacy_source: Path | None    # 戻る先 (存在必須)
    target_exists: bool
    legacy_source_exists: bool
    safe_to_plan: bool
    apply_available: bool = False

@dataclass
class ExtensionRollbackPlan:
    status: str / candidates / blocked_reasons / warnings
    apply_available: bool = False

@dataclass(frozen=True)
class ExtensionCleanupCandidate:
    extension_id: str
    legacy_source: Path           # 整理時に削除候補
    copied_target: Path           # verify ok 前提
    target_verified: bool
    legacy_source_exists: bool
    safe_to_plan: bool
    apply_available: bool = False

@dataclass
class ExtensionCleanupPlan:
    status: str / candidates / blocked_reasons / warnings
    apply_available: bool = False

def plan_extension_rollback_from_log(manifest_path) -> ExtensionRollbackPlan
def plan_extension_cleanup_from_log(manifest_path)  -> ExtensionCleanupPlan
```

### rollback-plan 条件

**candidate**:
- manifest 読める / schema 対応
- copied[] に target がある、target が存在する
- legacy source が存在する (戻る先が必要)

**blocked**:
- `target_missing` (既に消えている)
- `legacy_source_missing` (戻す先が無い)
- `delete_performed_unexpected` / `overwrite_performed_unexpected`
  (manifest 改ざん)
- `manifest_schema_unsupported` / `manifest_not_found`

### cleanup-plan 条件

**candidate**:
- manifest 読める / schema 対応
- target が存在し `extension.yaml` が読め `extension_id` 一致 (verify
  ok 相当)
- legacy source が存在する

**blocked**:
- `target_missing` / `target_extension_yaml_missing` /
  `target_extension_yaml_unreadable` / `extension_id_mismatch`
- `delete_performed_unexpected` / `overwrite_performed_unexpected`
- `manifest_schema_unsupported`

**warning** (candidate にしない):
- `already_cleaned_or_missing` (legacy source が既に無い → 整理不要)

### rollback ↔ cleanup の **方向が逆**

| Plan | 目的 | 削除候補 | 必要な前提 |
|------|------|----------|------------|
| rollback-plan | migration を **取り消す** | target | legacy source あり |
| cleanup-plan  | migration を **進める**   | legacy source | target が verify ok |

混同すると危険なため、docs に command matrix を追加した。

### 新規 CLI

```bash
lab-executor extension migration-log rollback-plan <manifest> [--json] [--strict]
lab-executor extension migration-log cleanup-plan  <manifest> [--json] [--strict]
```

Exit code は既存の `verify` と同じ (ok=0、warning=0/`--strict`で1、
error=1、usage=2)。candidate あり時は `plan-only` warning が必ず
入るため、default の status は warning になる (実 apply はまだ
できない、という reminder)。

### v2.9 で **やらないこと**

- rollback `--apply` / cleanup `--apply`
- target 削除 / legacy source 削除 / source 復元
- overwrite / install default 変更
- active_read_paths 優先順位変更
- extension pack / `.install_meta.json` schema 変更

### Tests (163 件 pass)

`tests/test_v290_rollback_cleanup_plan.py`: 22 件

- rollback: ok / target_missing / legacy_source_missing /
  schema_unsupported / delete_performed_unexpected / **does not
  delete files** (snapshot 比較で固定)
- cleanup: ok / target_missing / extension_id_mismatch /
  source_missing → already_cleaned warning / overwrite_unexpected /
  **does not delete files**
- CLI: rollback-plan / cleanup-plan の help / JSON / `--strict`
- Boundary: PyVISA / `visa_mcp` 非依存 subprocess gate
- 回帰: install_default / tool surface 不変

### docs / cli docstring

- `docs/extension_path_migration.md`: command matrix 追加 (rollback
  と cleanup の方向の違いを明示)、ロードマップ表に v2.9 を実装済と
  して追加
- `cli.py` module docstring を v2.8.x → v2.9.x

### 互換性

- `ExtensionCopyApplyResult` / `MigrationLogVerificationResult` 等
  既存 API は無変更
- MCP tool / DSL / extension pack 形式 / `.install_meta.json` /
  `default_extensions_dir()` 返り値、すべて不変

---

## v2.8.0 — Migration Log Inspection + Copied Pack Verification

合言葉: **「v2.7 で copy した結果を追跡・検証できるようにする。
rollback も delete もまだしない」**

v2.7 で `~/.lab-executor/migration_logs/extension-copy-<stamp>.json`
として残し始めた apply manifest を、CLI / API で読み返し、copied
target が現在も健全かを検証する段階。実 rollback / target 削除 /
overwrite は v2.8 でも一切しない。

### 新規 module: `lab_executor.extension_migration_log`

```python
@dataclass(frozen=True)
class MigrationLogSummary: ...
@dataclass(frozen=True)
class ExtensionCopyApplyManifest: ...
@dataclass
class MigrationLogVerificationResult: ...

def list_extension_migration_logs(*, log_dir=None) -> list[MigrationLogSummary]
def load_extension_migration_log(path) -> ExtensionCopyApplyManifest
def verify_extension_migration_log(path) -> MigrationLogVerificationResult
```

`operation == extension_copy_apply` のみを対象 (将来別 operation が
増えても混在しない)。`schema_version == "v2.7"` を必須 (将来後方
互換のため SUPPORTED_MANIFEST_SCHEMAS で扱う)。

### `verify_extension_migration_log()` の検出項目

**error**:

- `target_missing`
- `target_extension_yaml_missing` / `target_extension_yaml_unreadable`
- `extension_id_mismatch`
- `delete_performed_unexpected` (manifest が改ざんで `delete_performed
  =true` になっている)
- `overwrite_performed_unexpected`
- `manifest_schema_unsupported` / `manifest_not_found`

**warning**:

- `source_missing` (source は将来整理される可能性があるため warning)

### manifest 保存失敗時 → `partial_failure` 格上げ (P0)

v2.7.1 で予約した **案 A** を実装。`apply_extension_copy_plan()`
内で `_write_manifest()` が例外を出した場合:

```
status = "partial_failure"
manifest_path = None
failed[] に {"error_class": "manifest_write_failed", "message": ...}
```

実 copy は完了していても audit 上「成功」扱いしない。manifest なしの
copy は後から検証・説明できないため。

### 新規 CLI subcommands

```bash
lab-executor extension migration-log list [--json]
lab-executor extension migration-log inspect <manifest> [--json]
lab-executor extension migration-log verify <manifest> [--json] [--strict]
```

- `list`: timestamp 降順で表示
- `inspect`: `delete_performed=false` / `overwrite_performed=false`
  を目立たせる (ユーザーが「このマイグレーションは削除も上書きも
  していない」と確認できる)
- `verify`: exit code は `check` / `migration-plan` と整合
  (ok=0、warning=0/`--strict`で1、error=1、usage=2)

### v2.8 で **やらないこと**

- rollback `--apply` / target 削除 / legacy source 削除
- overwrite / `--force`
- install default 変更
- active_read_paths 優先順位変更
- extension pack / `.install_meta.json` schema 変更
- remote registry / signing / trust store

### Tests (141 件 pass)

`tests/test_v280_migration_log.py`: 22 件

- list (empty / after apply)
- load + schema rejection (unsupported schema_version / operation)
- verify (ok / target_missing / extension_id_mismatch /
  source_missing warning / delete_performed_unexpected /
  overwrite_performed_unexpected)
- `manifest_write_failure_marks_partial_failure` (P0 の核 -
  `_write_manifest` を monkeypatch で例外化)
- CLI: list/inspect/verify JSON + verify strict 挙動
- Boundary: PyVISA / `visa_mcp` 非依存 subprocess gate
- 回帰: install_default 不変 / Stable 43 + Experimental 7 = 50 不変

### docs / cli docstring

- `docs/extension_path_migration.md`: `migration-log` セクション +
  error/warning 一覧 + manifest 保存失敗時の挙動 + roadmap 表に v2.8
  を実装済として追加 / v2.9 を rollback-plan へ更新
- `cli.py` module docstring を v2.7.x → v2.8.x

### 互換性

- `apply_extension_copy_plan()` の戻り値 schema (`ExtensionCopy
  ApplyResult`) は v2.7 と同じ。`manifest_path=None` のケースが
  v2.8 で増えた点のみ要注意 (manifest 保存失敗時)
- MCP tool / DSL / extension pack 形式 / `.install_meta.json` /
  `default_extensions_dir()` 返り値、すべて不変

---

## v2.7.1 — Docs / Review patch (v2.7 表記整合 + 仕様明文化)

v2.7.0 レビュー反映。コード変更なし。

### Docs / CLI 文言

- `cli.py` argparse `description` を「dual-path extension discovery,
  migration planning, copy-plan preview, **and controlled copy apply**
  (v2.7)」へ更新。v2.6 表記を解消。
- `cli.py` module docstring 末尾の「`~/.lab-executor/extensions/` への
  切替は v2.7+ で判断」を「v2.8+ 以降の future release で判断」に
  更新 (v2.7 では切替していないことを明示)。
- `ExtensionMigrationAction` docstring: 「controlled apply は v2.7+
  で検討」表現を v2.7 実装済の事実に合わせて書き直し。本 dataclass
  は recommend 用途に閉じ、実 copy は `ExtensionCopyPlan` /
  `apply_extension_copy_plan()` 経由のみという責務分離を明示。
- `ExtensionCopyCandidate` docstring: 「将来 v2.7+ で apply される予定」
  を v2.7 実装済へ更新。v2.7 で `--copy-plan --apply` を併用すれば
  実 copy 対象になる、と書き直し。
- `ExtensionCopyApplyResult.manifest_path` docstring を実装に合わせ
  「v2.7 では ok / blocked / partial_failure すべてで保存するため
  原則として非 None」に修正。manifest 保存自体が失敗したケースだけ
  None になりうる、と注記。

### `target_exists` の検出タイミング別 status 明文化

`docs/extension_path_migration.md` に表で明示:

| 検出タイミング | status | 動作 |
|------|------|------|
| pre-apply (copy_plan 段階で target が既存) | `blocked` | candidate=0、manifest 保存 |
| during apply (copy 直前の再確認で target が出現) | `partial_failure` | skipped に記録、以降を fail-fast 停止 |

いずれも **overwrite はしない**。後者は他プロセスの plan 表示後
race ケース。

### manifest 保存失敗時の方針 (v2.8+ 実装予定)

docs に **案 A** を予約として明記:

> manifest 保存に失敗した場合、実 copy の成否にかかわらず全体を
> `partial_failure` 扱いに格上げし、`failed[]` に `manifest_write_
> failed` を記録。manifest なしの copy 成功は audit 上「成功」と
> みなさない。

### Internal

- version 2.7.0 → 2.7.1 (`__init__.py` / `pyproject.toml`)
- コア logic (apply / plan / discovery) は無変更、tests 119 件 pass

---

## v2.7.0 — Controlled Extension Copy Apply

合言葉: **「v2.6 で copy 候補、v2.7 で実 copy。ただし source は触らず
target は上書きしない」**

v2.6 で出せた copy candidate を、**厳格な事前条件下でのみ実行**する
段階。delete / overwrite / move は v2.7 でも一切しない。

### 新規 API

```python
@dataclass(frozen=True)
class ExtensionCopyApplyResult:
    status: str   # "ok" / "blocked" / "partial_failure"
    copied: list[dict]
    failed: list[dict]
    skipped: list[dict]
    manifest_path: Path | None
    delete_performed: bool = False        # v2.7 では常に False
    overwrite_performed: bool = False     # v2.7 では常に False
    blocked_reasons: list[dict] = []

def apply_extension_copy_plan(
    *, paths=None, log_dir=None,
) -> ExtensionCopyApplyResult:
    ...
```

`ExtensionCopyApplyError` (構造化 error class) も追加。

### 厳格な事前条件 (一つでも欠ければ blocked)

- `copy_plan.status == "ready"`
- `copy_plan.candidates` が 1 件以上
- `copy_plan.blocked_reasons` が空 (v2.6.1 で予約した条件を実施)
- duplicate / invalid_metadata / missing_yaml がない
- 実行直前に **migration plan を再計算** し、UI 表示後の filesystem
  変化があれば blocked に倒す

### 安全方針 (実装で固定)

- source は **削除しない** (`delete_performed=False`)
- target は **上書きしない**。既存なら skipped + 全 candidate を停止
- candidate ごとに `target.tmp-<stamp>/` に copy → atomic-ish rename
- partial failure は **fail-fast** (途中失敗で停止、成功済みは残す、
  `status="partial_failure"`)
- manifest を `~/.lab-executor/migration_logs/extension-copy-<stamp>
  .json` に **必ず**保存 (blocked / partial_failure 時も保存)

### 新規 CLI flag: `--apply`

```bash
lab-executor extension migration-plan --copy-plan --apply
lab-executor extension migration-plan --copy-plan --apply --json
```

- `--apply` は **`--copy-plan` と併用必須**。単独使用は exit 2
- Exit code: ok=0、blocked/partial_failure/failed=1
- Human-readable に COPIED / FAILED / SKIPPED / BLOCKED + manifest
  path + `delete_performed=False` / `overwrite_performed=False` を
  明示出力

### v2.7 で **やらないこと**

- source delete / legacy path 自動 cleanup
- target overwrite / `--force` / `--overwrite`
- install default 変更
- active_read_paths の優先順位変更
- duplicate 自動解決
- 自動 rollback (manifest を残すのみで人手復旧前提)
- extension pack / `.install_meta.json` schema 変更
- MCP tool 追加 / DSL schema 変更

### Tests (119 件 pass)

`tests/test_v270_copy_apply.py`: 16 件

- `apply_copies_legacy_only_to_new_path`
- `apply_does_not_delete_source` (snapshot 比較で固定)
- `apply_does_not_overwrite_target` (preexisting target は不変)
- `apply_fails_when_duplicate_exists` / `_when_invalid_metadata` /
  `_when_target_exists` を含む blocked 系
- `apply_writes_manifest` / `_even_when_blocked`
- `apply_recomputes_plan_before_copy` (直前再計算 contract)
- `apply_no_overwrite_performed_flag`
- CLI: `--apply requires --copy-plan` (exit 2) / `--apply ok` /
  `--apply blocked returns 1`
- Boundary: PyVISA / `visa_mcp` 非依存 subprocess gate
- 回帰: install_default 不変 / Stable 43 + Experimental 7 = 50 不変

### docs / cli docstring

- `docs/extension_path_migration.md`: `--apply` セクション + 事前
  条件 + 安全保証 + manifest schema + やらないこと一覧、ロードマップ
  表に v2.7 を実装済として追加
- `cli.py` module docstring を v2.6.x → v2.7.x

### 互換性

- `plan_extension_migration()` (引数なし) は v2.5 完全互換、
  `copy_plan=True` は v2.6 互換
- MCP tool / DSL / extension pack 形式 / `.install_meta.json` /
  `default_extensions_dir()` 返り値、すべて不変

---

## v2.6.1 — Docs / Review patch (target_exists semantics 明文化)

v2.6.0 レビュー反映 patch。コード変更は最小限。

### Docs / CLI 文言

- `cli.py` argparse `description` を「dual-path extension discovery,
  migration planning, and copy-plan preview (v2.6)」へ更新。v2.5
  表記を解消。
- `cli.py` module docstring 末尾の「`~/.lab-executor/extensions/`
  への切替は v2.5+ で判断」を「v2.7+ で判断」に更新 (v2.6 時点での
  ロードマップ整合)。
- `ExtensionMigrationAction` docstring を v2.6 現状に書き直し:
  「v2.5+ では action は提案のみ。v2.6 で `--copy-plan` を導入したが
  `apply_available` は引き続き常に False。controlled apply は v2.7+」
  という表現に統一。

### `target_exists` semantics 明文化

`docs/extension_path_migration.md` に v2.6.0 で実装した
**partial-skipped 挙動**を明示:

| 状況 | `copy_plan.status` | `blocked_reasons[]` |
|------|---------------|-----|
| 全 legacy_only に target_exists | `blocked` | 全件を target_exists で列挙 |
| 一部のみ target_exists、他は copy 可 | `ready` | skipped 分のみ残す |

`blocked_reasons` は **「status=blocked の理由」とは限らず**、
「candidate にできなかった件の理由」を列挙する schema。`status=
ready` でも `blocked_reasons` に skipped 詳細が入りうる、という
読み方を明文化。

加えて、v2.7 で `--apply` を入れる時の事前条件として
**「`blocked_reasons` が空であること」を必須にする方針**を docs に
予約 (skipped を黙って無視しない、案 B の延長)。

### Internal

- version 2.6.0 → 2.6.1 (`__init__.py` / `pyproject.toml`)
- コア logic (plan_extension_migration / copy plan / CLI ロジック)
  は不変、tests 103 件 pass

---

## v2.6.0 — Extension Migration Copy Plan

合言葉: **「v2.5 で計画、v2.6 で copy 候補。まだ実行しない」**

v2.5 の migration plan を一段具体化し、「legacy にしかない pack を
new path に copy するなら何が対象か」を機械可読に出す段階。実 copy
/ move / delete は **v2.6 でも一切しない** (`--apply` は v2.7+ で
慎重に検討)。

### 新規 dataclass: `ExtensionCopyCandidate` / `ExtensionCopyPlan`

```python
@dataclass(frozen=True)
class ExtensionCopyCandidate:
    extension_id: str
    source: Path              # legacy 側 source
    target: Path              # new 側 target (まだ存在しない)
    reason: str
    safe_to_copy: bool = True
    overwrite_required: bool = False   # v2.6 では常に False

@dataclass
class ExtensionCopyPlan:
    status: str               # "ready" / "empty" / "blocked"
    candidates: list[ExtensionCopyCandidate]
    blocked_reasons: list[dict]
    apply_available: bool = False      # v2.6 では常に False
```

### `plan_extension_migration(copy_plan=True)`

既存 API を拡張 (default は False で v2.5 と同挙動):

- `copy_plan=False` (default): `ExtensionMigrationPlan.copy_plan = None`、
  schema_version=`v2.5`
- `copy_plan=True`: `ExtensionMigrationPlan.copy_plan = ExtensionCopyPlan
  (...)`、schema_version=`v2.6`、`summary.copy_candidates` /
  `summary.copy_blocked` を追加

### `copy_plan.status` 判定

| Status | 条件 |
|--------|------|
| `blocked` | duplicate_extension_id あり、または invalid_extension_metadata あり、または **全 legacy_only に target_exists** |
| `ready`   | candidate が 1 件以上ある (一部 skipped でも可、skipped は blocked_reasons に列挙) |
| `empty`   | legacy_only がなく candidate もない (cleanup 不要) |

### Block 条件 (実 copy 前に必ず止める)

- `duplicate_extension_id`: 案 B により、まず duplicate を解消する
  必要がある
- `invalid_extension_metadata`: `extension.yaml` parse 失敗 / `extension
  _id` 欠落
- `target_exists`: `new_path/<dir_name>` が既に存在する (overwrite は
  v2.6 では行わない)

### 新規 CLI flag: `extension migration-plan --copy-plan`

```bash
lab-executor extension migration-plan --copy-plan
lab-executor extension migration-plan --copy-plan --json
lab-executor extension migration-plan --copy-plan --strict
```

実ファイルは一切変更しない。Human-readable 出力に copy_plan セクション
(candidates / blocked / skipped + "no files were changed" 表示) を
追加。

### v2.6 で **やらないこと**

- `--apply` / 実 copy / 実 move / 実 delete
- target 自動作成 / overwrite
- install default 変更
- active_read_paths の優先順位変更
- extension pack / `.install_meta.json` schema 変更
- MCP tool 追加 / DSL schema 変更

### Tests (103 件 pass)

新規 `tests/test_v260_copy_plan.py`: 14 件

- `copy_plan_legacy_only_candidates` / `new_only_no_candidates`
- `copy_plan_duplicate_blocked` / `invalid_metadata_blocked` /
  `target_exists_skipped_or_blocked`
- **`copy_plan_no_file_changes`** (v2.6 の核): plan 前後で legacy /
  new directory tree が変わらないことを snapshot 比較で固定
- `copy_plan_apply_available_false`
- `copy_plan_omitted_when_flag_false` (default 互換)
- CLI: `--copy-plan` help / JSON 出力 / duplicate blocked
- Boundary: PyVISA / `visa_mcp` 非依存 subprocess gate
- 回帰: install_default 不変 / Stable 43 + Experimental 7 = 50 不変

### docs / cli docstring

- `docs/extension_path_migration.md`: `--copy-plan` セクション +
  blocked JSON 例 + ロードマップ表に Status 列 (実装済 / 検討中) 追加
- `cli.py` module docstring を v2.5.x → v2.6.x へ更新

### 互換性

- 既存 `plan_extension_migration()` (キーワードなし呼出) は v2.5 と
  完全同一の挙動 (`copy_plan=None`, schema_version=`v2.5`)
- MCP tool / DSL / extension pack 形式 / `.install_meta.json` /
  `default_extensions_dir()` 返り値、すべて不変

---

## v2.5.1 — Docs / Review patch + summary breakdown

v2.5.0 レビュー反映 patch。

### Docs / CLI 文言

- `cli.py` docstring を v2.4.x → v2.5.x へ更新。`migration-plan` /
  `resolve_extension_by_id()` を全体説明と Exit code policy section に
  追記
- argparse `description` を「dual-path extension discovery and
  migration planning (v2.5)」に更新
- `ExtensionMigrationAction` docstring を明確化。「`apply_available
  =False` は v2.5 では常に True にならない」という曖昧な表現を
  「v2.5 では `apply_available` は常に False。本 release は plan の
  みを出し、copy / move / delete は実行しない」に書き直し
- README から `docs/extension_path_migration.md` へのリンクを追加

### Summary breakdown (v2.5.1)

`ExtensionMigrationPlan.summary` に内訳 field を追加 (既存 field
は不変):

- `invalid_metadata`: `extension.yaml` parse 失敗 / `extension_id`
  欠落 (severity=error の起点)
- `missing_extension_yaml`: pack dir はあるが `extension.yaml` が
  ない (severity=warning の起点)

これで CI / 人間レビューが「error 系の invalid か、warning 系の
missing か」を summary 1 段で判別できる。`invalid` (合算) は
互換のため残す。

### docs/extension_path_migration.md 強化

- catalog/check と migration-plan で duplicate の severity が違う
  理由を表で明示 (catalog/check は warning、migration-plan は error)
- summary breakdown の 2 新 field を説明
- README からの導線確認

### Internal

- version 2.5.0 → 2.5.1 (`__init__.py` / `pyproject.toml`)
- コア logic (plan_extension_migration / resolve_extension_by_id /
  CLI ロジック) は不変、tests 88 件 pass

---

## v2.5.0 — Extension Migration Plan + Conflict Resolution Guidance

合言葉: **「v2.4 で検出、v2.5 で計画。まだ動かさない」**

v2.4 で dual-path read + duplicate 検出ができるようになった次の段階。
v2.5.0 では、検出結果に対する **plan のみ**を出し、ファイルは一切
変更しない。`--apply` / 自動 copy / 自動 move / 自動 delete は
v2.5 では実装しない (v2.6+ で慎重に検討)。

### 新規 module: `lab_executor.extension_migration`

```
ExtensionMigrationAction (frozen dataclass)
  action / extension_id / severity / locations / recommendation
  / apply_available  (v2.5 では常に False)

ExtensionMigrationPlan
  status / summary / actions / paths

plan_extension_migration(paths=None) -> ExtensionMigrationPlan
```

`summary` フィールド:

- `legacy_only`: `~/.visa-mcp/extensions/` にのみ存在する pack 数
- `new_only`: `~/.lab-executor/extensions/` にのみ存在する pack 数
- `duplicates`: 両 path にある `extension_id` の数
- `invalid`: metadata 不正 / YAML 不在の数
- `migration_required`: 実態ベースで判定
  (`legacy_only > 0` OR `duplicates > 0` OR `invalid > 0`)

`status`:

- `error`: duplicates あり、または invalid metadata あり
- `warning`: legacy_only あり、または missing extension.yaml あり
- `ok`: 上記いずれもなし (new_only のみは ok 扱い)

### 新規 API: `resolve_extension_by_id()`

`lab_executor.extension_discovery` に追加:

```python
def resolve_extension_by_id(
    extension_id: str,
    *,
    paths: ExtensionPaths | None = None,
) -> InstalledExtension:
    """
    - 見つからない -> ExtensionResolveError("extension_not_found")
    - 1 件だけ      -> InstalledExtension を返す
    - 複数 (duplicate) -> ExtensionResolveError(
                          "duplicate_extension_id")
    """
```

duplicate を **黙って解決しない** ことを API レイヤで強制する。
構造化 error class `ExtensionResolveError` (error_class /
extension_id / locations / message を保持) を新規追加。

### 新規 CLI: `lab-executor extension migration-plan`

```bash
lab-executor extension migration-plan
lab-executor extension migration-plan --json
lab-executor extension migration-plan --strict
```

**実ファイルは変更しない**。出力するのは現状 path 状態と推奨 action
のみ。

Exit code:

| status | default | --strict |
|---|---|---|
| ok | 0 | 0 |
| warning | 0 | 1 |
| error | 1 | 1 |

### 新規 docs: `docs/extension_path_migration.md`

- v2.4 以降の dual-read 構成
- write_default が legacy のままである理由
- duplicate を黙って優先しない方針 (案 B) の明文化
- duplicate を手作業で解消する手順
- `migration_required` の判定ロジック
- v2.5 で **やらないこと** の list
- v2.6 (copy-plan) / v2.7 (controlled apply) / v2.8 (default 切替)
  のロードマップ

### v2.5 で **やらない**こと

- `--apply` 実装
- 自動 copy / move / delete
- install default の `~/.lab-executor/extensions/` への変更
- duplicate 時の自動採用
- extension pack 形式 / `.install_meta.json` schema の変更
- MCP tool 追加 / DSL schema 変更

### Tests (88 件 pass)

`tests/test_v250_migration_plan.py`: 14 件

- plan: no_extensions / legacy_only / new_only / duplicate /
  invalid_metadata
- plan: ファイル変更なし回帰
- plan: schema_version=v2.5 / フィールド完全性
- `resolve_extension_by_id`: ok / not_found / duplicate
- CLI: help / --strict 挙動 (error→exit 1, warning→exit 0/1)
- Boundary: PyVISA / `visa_mcp` 非依存 subprocess gate
- 回帰: install_default 不変 / Stable 43 + Experimental 7 = 50 不変

### 互換性

MCP tool / DSL / extension pack 形式 / `.install_meta.json` / 既存
の `default_extensions_dir()` 返り値、すべて v2.4 から不変。

---

## v2.4.1 — Docs / Review patch + Release verification manifest

v2.4.0 レビュー反映 patch。コード変更は最小限 (docstring / help 文言
の更新)、加えて **raw 表示問題の再発防止策**として release-time
verification manifest を導入する。

### Docs / CLI 文言

- `src/lab_executor/cli.py` module docstring を v2.2.x → v2.4.x へ
  更新。v2.3 / v2.4 で追加されたサブコマンド (extension install /
  check / catalog / paths、dual-path discovery、duplicate conflict
  detection) を反映。
- `lab-executor extension paths --help` を「v2.3: planning only」→
  「v2.4: dual-read, legacy write default」へ修正。
- argparse `description` を v2.4 へ更新。
- README に **v2.4 path behavior 表**を追加 (read / write / duplicate
  / auto-precedence / policy id)。
- `discover_installed_extensions()` docstring に warning ブロックを
  追加: duplicate がある場合 `extensions[]` に入っている record は
  **display compatibility 目的**であり「選択された extension」では
  ない、と明示。downstream `extension_id` 解決は `duplicates` を
  チェックし `duplicate_extension_id` error を返すべき
  (v2.5+ で `resolve_extension_by_id()` 実装予定)。

### Raw 表示問題の根本対応 (再発防止)

複数の review で `raw.githubusercontent.com` 経由の file が "1 line
/ collapsed" と報告されてきたが、curl で実際の bytes を測ると毎回
LF=数十〜数百 / CR=0 で multi-line 確認できる。これは **viewer 側
artifact** であり repo 側ではない。

この事実を毎回 review で再証明させるのは非効率なので、release tag
時に **`RELEASE_VERIFICATION.md`** を自動生成して同梱する運用に変更:

- 新規 script: `scripts/release_verification.py`
  - critical files のリストを保持
  - **git canonical bytes** (`git show HEAD:<path>`) を読むことで、
    Windows の autocrlf 影響を排除して repo 真値で集計
  - `--check`: 全 critical file が `CR == 0` / `LF >= 10` / no BOM
    を満たすかを exit code で gate (CI 用)
  - 引数なし: markdown manifest (bytes / LF / CR / BOM の表) を
    stdout 出力
- 新規 file: `RELEASE_VERIFICATION.md` (root に commit)
  - reviewer が viewer の表示を疑ったとき、まず読むべき ground
    truth。`clone --branch <tag>` + `release_verification.py --check`
    で誰でも `OK` を確認できる
- README の line-ending note を更新し、`RELEASE_VERIFICATION.md`
  を canonical 参照先として明示

これにより、v2.4.1 以降は「viewer が 1 line と言っている」レビュー
コメントに対して、**毎回手作業で curl 検証を再実行する必要がなく
なる**。reviewer 側で `release_verification.py --check` を走らせる
だけで終わる。

### Internal

- バージョン 2.4.1 に bump (`__init__.py` / `pyproject.toml`)。
- コード本体 (extension_paths / extension_discovery / cli ロジック)
  は v2.4.0 から不変。tests 71 件は引き続き pass。

---

## v2.4.0 — Dual-path Extension Discovery + Duplicate Conflict Detection

合言葉: **「新 path を読み始める。ただし黙って優先しない」**

v2.3.0 で planning に留めた path 移行を、v2.4.0 で
**読み取りだけ dual-path 化** する。書き込み default は legacy
(`~/.visa-mcp/extensions/`) のまま。同じ `extension_id` が
new (`~/.lab-executor/extensions/`) と legacy 両方に存在する場合は
**自動採用せず、warning として報告**する (案 B:
`report_conflict_no_implicit_precedence`)。

### Source of truth: `ExtensionPaths` (v2.4 schema)

`lab_executor.extension_paths.get_extension_paths()` を拡張し、
読み・書き・表示を分離した:

```
read     : active_read_paths = [new_path, legacy_path]
write    : write_default       = legacy_path   (v2.4 では legacy のまま)
display  : current_default / future_default_candidate
policy   : duplicate_policy = "report_conflict_no_implicit_precedence"
```

新 fields: `legacy_path` / `new_path` / `write_default` /
`duplicate_policy`。`to_dict()` の `schema_version` は `"v2.4"`。

### 新規 module: `lab_executor.extension_discovery`

`catalog` / `check` / 将来の `migration-plan` が共有する dual-path
scan + duplicate 検出ロジックを 1 箇所に集約:

- `discover_installed_extensions(paths=None) -> ExtensionDiscoveryResult`
- `InstalledExtension` (frozen dataclass): `extension_id` / `path`
  / `source_path` / `metadata` / `install_meta`
- `ExtensionDiscoveryResult`: `extensions` / `duplicates` /
  `warnings` / `errors` / `duplicate_policy`

duplicate 判定は **ディレクトリ名ではなく `extension.yaml` の
`extension_id`** ベース。YAML 読み込み失敗は
`invalid_extension_metadata` / `missing_extension_yaml` として
errors / warnings に分離して報告する。

### CLI 挙動 (v2.4)

- **`lab-executor extension paths`**: `legacy_path` / `new_path`
  / `write_default` / `active_read_paths` / `duplicate_policy` を
  表示。
- **`lab-executor extension catalog`**: dual-path discovery 経由で
  install 済 pack を列挙。duplicate がある場合 `status=warning`
  + `duplicates` block 出力。`--strict` で warning → exit 1。
- **`lab-executor extension check`**: dual-path discovery + 個別
  integrity check の合算。`summary.duplicate_extension_ids` を
  返す。default では warning でも exit 0、`--strict` で exit 1。

### `lab-executor extension install` の挙動

**default 書き込み先は引き続き `~/.visa-mcp/extensions/`** (v2.4 で
変更しない)。v2.5+ で切替判断する。

### v2.4 で **やらないこと**

- install default を `~/.lab-executor/extensions/` に変更
- duplicate 時に自動で片方を優先 / 削除 / 移動
- migration 自動実行
- extension pack 形式 / `.install_meta.json` schema 変更
- MCP tool 追加 / DSL schema 変更

### Tests

- `tests/test_v240_extension_dual_path.py`: 18 件
  - `ExtensionPaths` v2.4 schema (active_read_paths dual /
    write_default legacy / duplicate_policy)
  - `discover_installed_extensions` (legacy 単独 / new 単独 /
    duplicate 検出 / missing_extension_yaml /
    invalid_extension_metadata)
  - CLI `extension paths/catalog/check` (`--strict` 含む)
  - `install_default` 不変回帰
  - PyVISA / `visa_mcp` 非依存 subprocess 検査
  - tool surface 不変 (Stable 43 + Experimental 7 = 50)
- v2.3 既存 tests を v2.4 schema 受容に更新
  (`schema_version in {"v2.3","v2.4"}`)

### 互換性

- MCP tool 名 / 引数 / response、DSL `dsl_version=0.8`、
  extension pack 形式 (`.visa-mcp-ext.zip`)、`.install_meta.json`
  schema はすべて不変。
- `default_extensions_dir()` の返り値 (`~/.visa-mcp/extensions/`)
  も不変。v2.4 では `write_default` と完全一致する。

---

## v2.3.1 — Docs / Review patch

v2.3.0 レビュー反映 patch。コード変更なし、docs と CHANGELOG の補強のみ。

### Docs

- **README "Line-ending / raw display note"**: viewer 側で
  `raw.githubusercontent.com` が "1 line / collapsed" と誤表示される
  ケースについて、誰でも検証できる `curl | python` 1-liner を例示。
  リポジトリは LF 単独 (CR=0) で保存されており、CI test
  `test_critical_files_are_multiline_and_lf_only` で gate されている
  ことを明記。
- **README "CLI status"**: v2.1 範囲の記述を v2.3 範囲へ更新。v2.2 /
  v2.3 で追加された CLI (`extension init/install/check/catalog/paths`
  / `instrument scaffold/promote-check/review-report` /
  `diagnose tool-surface`) を反映。
- **Exit code policy table**: v2.3 subcommand
  (`extension install/check/catalog/paths`) の row を追加。
- **`--skip-verify` 警告強化**: 「test 用途のみ。信頼できない zip に
  対しては絶対に使わない」を明示。
- **`--dry-run` semantics 明確化**: v2.3 dry-run は package verify
  のみで、install 済 extension_id の重複検査は行わないことを明記
  (v2.4+ で検討)。
- **`extension_paths` を v2.4 source of truth 化する旨**を docs TODO
  として記載 (`default_extensions_dir` → `get_extension_paths()
  .current_default` 段階移行計画)。

### Internal

- バージョンを 2.3.1 に bump (`__init__.py` / `pyproject.toml`)。
- コードは無変更、tests / CI 既存 gate は全て pass。

---

## v2.3.0 — Extension Lifecycle CLI + Path Migration Planning

合言葉: **「v2.2 で作る CLI が揃ったので、v2.3 で install して使う
CLI を揃える。path 移行は実装せず planning に留める」**

v2.2.x で `extension init / instrument scaffold / doctor / package /
verify-package` が揃った。v2.3.0 では **install → check → catalog**
までを `lab-executor` 側 CLI で完結できるようにし、`~/.lab-executor/
extensions/` への migration は **planning のみ** (path resolver +
`extension paths` CLI) で実装は v2.4+。並走して `SessionFacade`
Protocol 化と `JobManager` TYPE_CHECKING cleanup を実施。

### 新規 CLI subcommands (P0)

- **`lab-executor extension install <zip>`**: definition pack を
  `~/.visa-mcp/extensions/` に install (`--dry-run` で verify のみ、
  `--force` で上書き、`--skip-verify` は test 用)
- **`lab-executor extension check`**: install 済 extension の整合性
  検査 (checksum / manifest / metadata)。`--extension-id <id>` で
  対象を絞れる。`--strict` で warning → exit 1 (default は exit 0)
- **`lab-executor extension catalog`**: install 済 extension 一覧
  (extension_id / version / support_level)
- **`lab-executor extension paths`**: install path resolver の現状
  を表示 (v2.3 では planning only、default 動作は v2.2 から不変)

### Path migration planning

`lab_executor.extension_paths.get_extension_paths()` 公開 API
追加。`ExtensionPaths` dataclass で:

- `current_default`: 現在 install 先 (`~/.visa-mcp/extensions/`)
- `future_default_candidate`: 切替候補 (`~/.lab-executor/extensions/`)
- `active_read_paths`: catalog / check が読む path 一覧 (v2.3 は
  `current_default` 単独、v2.4 で dual-read 検討)
- `migration_required`: v2.3 では常に `False`

**v2.3 では default path 変更を行わない**。v2.4 で dual-read 設計、
v2.5+ で default 切替判断、というロードマップを `paths` CLI 出力で
明示する。

### Internal cleanup (P1)

- **`lab_executor.session.SessionFacade`** Protocol 新規追加
  (`runtime_checkable`):
  - `get_session(resource) -> Any` の最小 surface
  - `server._SessionFacade` / `visa-mcp` 側 `SessionManager` 双方が
    満たすことで、tool 層から見た session lookup の contract を明示
- **`src/lab_executor/job/manager.py` TYPE_CHECKING cleanup**:
  v2.2 まで残っていた `from visa_mcp.session_manager import
  SessionManager` / `from visa_mcp.visa_manager import VisaManager`
  を、lab-executor 側 Protocol へ置換 (`InstrumentBackend as
  VisaManager` / `SessionFacade as SessionManager` legacy alias)。
  これで `src/lab_executor/` 配下から `visa_mcp` 参照が完全に消えた
  (TYPE_CHECKING 含む)

### tests (`tests/test_v230_extension_lifecycle.py` 新規 12 件)

- `test_extension_paths_module_importable` / `..._default_legacy_path`
  / `..._to_dict`
- `test_cli_extension_paths_help` / `..._json`
- `test_cli_extension_install_help`
- `test_cli_extension_check_help`
- `test_cli_extension_catalog_help`
- `test_session_facade_protocol_importable`
- `test_session_facade_runtime_checkable` (内部 `_SessionFacade` が
  Protocol を満たす確認)
- `test_job_manager_type_checking_no_visa_mcp_reference`
- `test_no_pyvisa_for_extension_paths_subprocess`
- `test_mcp_tool_surface_unchanged` (43 + 7 = 50 不変)

合計 **53 件 pass** (v2.0 + v2.1 + v2.2 + v2.3)

### 互換性

- API / package 構造 / MCP tool / DSL / extension pack 形式: 不変
- **install path default**: `~/.visa-mcp/extensions/` (v2.2 から不変)
- `.install_meta.json` schema: 不変
- `SessionFacade` Protocol は新規追加のみ (既存 `_SessionFacade` /
  `SessionManager` の挙動を変えない)
- `JobManager` TYPE_CHECKING の rename は `visa_mcp` → 同名 alias
  なので呼び出し側コードは無修正

### v2.3.0 でやらないこと

- `~/.lab-executor/extensions/` への default 切替
- dual-read 実装 (v2.4 候補)
- remote registry / signature / trust store
- backend plugin system / replay backend
- MCP tool 追加 / DSL schema 変更

### v2.4+ 候補

- `~/.lab-executor/extensions/` dual-read support
- duplicate extension id の優先順位ルール
- migration dry-run / migration command
- catalog filtering (`--tag` / `--support-level`)
- Replay backend 設計着手

## v2.2.1 — v2.2.0 レビュー応答 (docstring 更新 / --id help / diagnose --strict / README exit code)

合言葉: **「v2.2.0 直後の docs / exit code policy 仕上げ」**

v2.2.0 external review (P1/P2) 反映の small patch。public API /
dependency / shim 動作すべて不変。

### 変更点

- **P1** (`src/lab_executor/cli.py` docstring):
  - 冒頭を「v2.1.0」→「v2.2.x」へ更新
  - v2.1.0 / v2.2.0 のサブコマンドを段階的に列挙
  - **Exit code policy** を docstring 内に明文化
    (0 / 1 / 2 の意味、`diagnose tool-surface` の strict mode 説明)
- **P1** (`extension init --id` help):
  - "reverse-DNS extension id (default: local.<pack_name>)" → より
    具体的な "default: 'local.<pack_name>', e.g. 'local.my_pack'"
    に変更
- **P1** (`diagnose tool-surface --strict` 追加):
  - default では warning でも exit 0 (手元診断向け、warning は表示
    のみ)
  - `--strict` 指定時のみ warning → exit 1 (CI gate 用途)
  - JSON 出力に `strict_mode` field 追加
- **P1** (`README.md` exit code table 拡張):
  - `extension init` / `instrument scaffold` / `instrument
    review-report` / `diagnose tool-surface` の exit code を追記
  - `diagnose tool-surface` の warning + `--strict` 無し → exit 0 の
    挙動を明示

### tests

- 既存 v2 smoke test 39 件 すべて pass (v2.0 + v2.1 + v2.2)
- diagnose `--strict` の追加は default behavior の緩和のみで、
  既存 test (`test_cli_diagnose_tool_surface_json`) は exit code
  0 か 1 を許容しているため pass 維持

### 互換性

- API / package 構造 / MCP tool / DSL / extension pack: すべて不変
- `diagnose tool-surface` の **default exit code が変わる**
  (warning で exit 1 → exit 0)。CI で fail させたい場合は `--strict`
  を明示すること

### 注意点 (v2.3+ の宿題)

- `src/lab_executor/job/manager.py` の `TYPE_CHECKING` 内に
  `visa_mcp.session_manager` / `visa_mcp.visa_manager` 参照が残存
  (runtime import ではないが、v2.3 で lab-executor 側 Protocol へ
  置換予定)
- `SessionFacade` を Protocol へ昇格 (v2.3 候補)
- `lab-executor extension install / check / catalog` + path migration
  は v2.3 で着手

## v2.2.0 — CLI Authoring Workflow + Backend Naming Cleanup

合言葉: **「v2.1 で server を起動できるようになった runtime を、
definition pack / instrument 定義を CLI で作れる段階まで育てる」**

v2.1.x で `lab-executor serve --backend mock` が動くようになった。
v2.2.0 では CLI authoring workflow を拡張し、runtime 内部の
`visa=` 命名を `backend=` へ移行する。public MCP tool / DSL /
extension pack 形式すべて不変。

### 新規 CLI subcommands (v1.x `visa-mcp` から port)

- **`lab-executor extension init <pack_name>`**: definition pack を
  scaffold (template: minimal / mock_basic / instrument_pack)
- **`lab-executor instrument scaffold <category>`**: instrument YAML
  を category 別 template から生成 (power_supply / dmm /
  temperature_meter / generic_scpi、`support_level: draft` 固定)
- **`lab-executor instrument review-report <path>`**: instrument
  YAML から markdown 形式 PR review を生成 (strict validate +
  promote-check 集約)
- **`lab-executor diagnose tool-surface`**: declared (43+7=50) vs
  registered MCP tool 数の差分を JSON / text で出力 (v2.1.1 で
  追加した `diagnose_tool_surface(server)` の CLI 化)

これで lab-executor 単独で「pack 作成 → instrument scaffold →
doctor → package → verify-package」の authoring loop が完結する。

### Runtime 内部命名整理

**`JobManager(backend=...)` keyword 追加** (v2.2.0 推奨):

```python
# v2.2.0+ 推奨
JobManager(backend=mock_backend, session_mgr=..., store=...)

# 旧 (v2.1 まで) — v2.2.0 で DeprecationWarning + v3.x で削除候補
JobManager(visa=mock_backend, session_mgr=..., store=...)
```

`visa=` と `backend=` 同時指定は `TypeError`。`server.create_server()`
は内部で `backend=` 経由に切替済 (DeprecationWarning を triggered
しない)。

### Templates パッケージ復活

`src/lab_executor/templates/instruments/` (dmm / power_supply /
temperature_meter / generic_scpi の YAML テンプレ) を v2.2.0 で
正式に含めた (v2.0 split 時の copy 漏れを修正)。
`instrument_authoring._load_template()` は
`lab_executor.templates.instruments.*` を優先、fallback で
`visa_mcp.templates.instruments.*` も試す。

### tests (`tests/test_v220_cli_authoring.py` 新規 11 件)

- `test_cli_extension_init_help` / `..._generates_pack`
- `test_cli_instrument_scaffold_help` / `..._generates_yaml`
- `test_cli_instrument_review_report_help`
- `test_cli_diagnose_tool_surface_help` / `..._json`
- `test_job_manager_accepts_backend_keyword`
- `test_job_manager_visa_keyword_deprecated`
- `test_job_manager_rejects_both_keywords`
- `test_create_server_uses_backend_keyword_path`
  (DeprecationWarning が出ないこと)
- `test_authoring_cli_no_pyvisa_subprocess`
  (`instrument scaffold` が PyVISA / visa_mcp なしで動く)

合計 **39 件 pass** (v2.0 + v2.1 + v2.2)

### 互換性

- public API / MCP tool / DSL `dsl_version=0.8` / extension pack
  形式すべて不変
- `JobManager(visa=...)` は **動作するが DeprecationWarning** (v3.x
  で削除候補)
- `serve --backend mock` の挙動: 不変

### v2.2.0 でやらないこと

- backend plugin system
- REST / replay backend 本実装
- `lab-executor extension install / catalog / check` (v2.3 候補)
- `~/.lab-executor/extensions/` への default 切替 (v2.3+ 候補)
- MCP tool 追加
- DSL schema 変更

### v2.3+ 候補

- `lab-executor extension install / check / catalog`
- `~/.lab-executor/extensions/` への dual-read 設計 + migration
  dry-run
- `SessionFacade` を Protocol に昇格 (review P1)
- Replay backend 設計着手

## v2.1.1 — v2.1.0 レビュー応答 (README serve table / exit code policy / diagnose_tool_surface)

合言葉: **「v2.1.0 直後の docs / diagnostic 仕上げ」**

v2.1.0 external review (P1) 反映の small patch。public API / dependency
/ MCP tool 数 declaration すべて不変。

### 変更点

- **P1** (`README.md`):
  - **serve 使い分け表** を追加 (`lab-executor serve --backend mock`
    vs `visa-mcp serve` の用途 / PyVISA 依存 / backend 種別を一覧化)
  - **Quick examples** section 追加 (`--dry-run` / `validate
    extension` / `extension doctor` / `package` + `verify-package`
    の典型 4 ケース)
  - **Exit code policy** を表形式で明文化:
    | Subcommand | exit 0 | 1 | 2 |
    `doctor` は warning でも exit 1 (CI gate として強い設計) を明記
- **P1** (`src/lab_executor/server.py`):
  - `diagnose_tool_surface(server)` 公開 helper 追加。`stability`
    declaration (43 + 7 = 50) と実 registry の差分を構造化辞書で返す
    (`missing_from_registry` / `extra_in_registry` 等)。v2.2+ で AI
    エージェント向けに「declaration にあるのに registry に無い tool」
    を可視化する診断 CLI の土台。
- **P1** (`README.md` notes):
  - runtime 内部の `JobManager(visa=...)` 引数名は v2.1 で互換維持
    していること、v2.2+ で `backend=` への rename を検討する旨を明記

### tests

- `test_diagnose_tool_surface` 追加 → **26 件 pass**

### 互換性

- API / package 構造: 不変
- MCP tool / DSL / extension pack: 不変
- 既存 `list_registered_tools()` API: 不変 (`diagnose_tool_surface()`
  を追加のみ)

## v2.1.0 — Mock Runtime Server / CLI Activation

合言葉: **「v2.0 で分離した runtime を、単独で起動できる形に近づける」**

v2.0.x まで placeholder だった `lab-executor serve` を、v2.1.0 で
**MockBackend 経由で起動可能**にする backend-independent MCP server
release。新しい MCP tool / DSL 変更 / extension pack 形式変更は無し。

### 新機能

- **`lab_executor.server.create_server(backend=None, *, name=...)`** 公開 API
  - `InstrumentBackend` を inject して MCP server を構成
  - 引数省略時は `MockBackend` を default 使用
  - `list_registered_tools(server)` helper も追加
- **`lab-executor serve --backend mock`** (CLI)
  - MockBackend で MCP server 起動 (PyVISA / visa-mcp 非依存)
  - `--dry-run` で server を compose して tool 一覧を出すだけ
  - 引数なしは exit 2 + `visa-mcp serve` への誘導 (実機 backend は
    visa-mcp 側で継続)
- **`lab-executor validate extension <path>`** port
- **`lab-executor extension {doctor,package,verify-package}`** port

### tools 登録

`tools/audit/commands/dsl/export/groups/info/jobs/monitor/observation/
pdf_extractor/recipes/waits` の `register_tools(mcp, ...)` を順に呼び、
v1.0 凍結の MCP tool surface を expose する。

- 内部 facade: `_SessionFacade` (SessionManager 互換最小実装)
- JobManager は MockBackend を `visa:` として受ける (duck-typed)
- 実 registry に登録される tool 数は >= 30 (実装で変動するが core
  tool はすべて含まれる)
- `stability.STABLE_TOOLS` / `EXPERIMENTAL_TOOLS` の declaration は
  **43 + 7 = 50** で不変

### Backend independence

- `lab_executor.server` module 自体は PyVISA / visa_mcp に依存しない
- `create_server()` 呼び出しも、`visa_mcp` を import 経路から block
  した状態で動作することを subprocess test で確認
  (`test_no_pyvisa_when_visa_mcp_blocked_subprocess`)

### tests (`tests/test_v210_server.py` 新規 14 件)

- `test_create_server_with_default_mock_backend`
- `test_create_server_with_explicit_mock_backend`
- `test_mock_server_tool_count_is_reasonable`
- `test_stability_declarations_unchanged` (43 + 7 = 50)
- `test_server_module_imports_without_pyvisa`
- `test_server_creates_without_visa_mcp_installed`
- `test_no_pyvisa_when_visa_mcp_blocked_subprocess`
- `test_cli_serve_requires_backend` (引数なし → exit 2)
- `test_cli_serve_backend_mock_dry_run`
- `test_cli_serve_help`
- `test_cli_validate_extension_help`
- `test_cli_extension_help`
- `test_cli_extension_doctor_help`
- `test_v2_1_version`
- `test_no_top_level_visa_mcp_import_added`

合計 25 件 pass (v2.0 smoke 含む)。

### CLI message 言語

`serve` placeholder で得た知見を踏襲し、CLI argparse の help /
description / stderr message は **ASCII-only** に統一。subprocess test
が Windows cp932 環境でも安全に動く。

### 互換性

- API / package 構造: 不変
- MCP tool 数 declaration: 43 + 7 = 50 (v1.0 から不変)
- DSL `dsl_version=0.8`: 完全互換
- extension pack 形式: 完全互換
- `~/.visa-mcp/extensions/` install path: 継続使用

### v2.1.0 でやらないこと

- backend plugin system
- REST / replay backend 実装
- remote registry
- package signing
- install path default 変更 (v2.2+ で検討)
- MCP tool 追加
- DSL schema 変更

### v2.2+ 候補

- `lab-executor extension init / install / catalog`
- `lab-executor instrument scaffold / review-report`
- `~/.lab-executor/extensions/` への並走移行計画
- 他 backend (REST / replay / plugin) の `--backend` choice
- visa-mcp shim 利用状況を見た Deprecation スケジュール調整

## v2.0.2 — CI hotfix (TYPE_CHECKING import / ASCII CLI / smoke test scope)

合言葉: **「v2.0.1 で CI 全 job 通すための hotfix」**

v2.0.1 push 後の GitHub Actions failure 解析と修正。public API / MCP
tool / DSL / extension pack すべて不変。

### 失敗原因

`run 26430762700` で `test` / `pyvisa-not-installed` job が fail:

1. **P0 (`src/lab_executor/tools/commands.py`)**: bootstrap script の
   patch 関数が `if TYPE_CHECKING:` を挿入する際、当該 file に
   `from typing import` が存在しなかったため `TYPE_CHECKING` が
   undefined になっていた。collection 段階で `NameError` 発生 →
   pytest 全停止
2. **P0 (`tests/test_v200_split.py`)**: `lab-executor serve` の
   stderr メッセージが日本語で、Windows subprocess decode 時に
   cp932 → utf-8 mismatch で `UnicodeDecodeError` 発生
3. **P1 (`.github/workflows/ci.yml` test job)**: pytest 全件 (152
   inherited visa-mcp tests + 1 smoke) を実行していたが、inherited
   tests は v2.0 split に未適応 → 大量 fail

### 修正

- **P0-1** (`src/lab_executor/tools/commands.py`): `from typing import
  TYPE_CHECKING` を追加
- **P0-1** bootstrap script (`visa-mcp` repo): `from typing import`
  が存在しない場合は `from __future__ import annotations` 直後に
  新規 import 行を挿入するよう改良 (再 bootstrap 時の regression
  防止)
- **P0-2** (`src/lab_executor/cli.py`): `serve` placeholder の stderr
  メッセージを **ASCII-only** に変更 (Windows cp932 / Linux UTF-8 /
  CI locale を問わず subprocess で安全に decode できる)
- **P0-2** (`tests/test_v200_split.py`): subprocess.run に
  `encoding="utf-8"` 明示
- **P1** (`.github/workflows/ci.yml`): `test` job の pytest を
  `tests/test_v200_split.py` のみに限定 (inherited visa-mcp tests
  152 件は v2.1 で curated subset へ拡張予定)

### 検証

```
PYTHONPATH=src python -m pytest tests/test_v200_split.py -q
→ 10 passed
```

### 互換性

- API / package 構造: 不変
- MCP tool 数 / DSL / extension pack: 不変
- CLI 動作: stderr メッセージのみ英語化、exit code / 動作不変

## v2.0.1 — v2.0.0 レビュー応答 (README 導線 / line-ending note / MockBackend docstring)

合言葉: **「正式版直後の peripheral 仕上げ」**

v2.0.0 external review (P0/P1/P2) を反映した small patch。
public API / dependency / extension pack / MCP tool / DSL すべて不変。

### 変更点

- **P0**: `README.md` に **line-ending note** を追加。GitHub raw view
  の一部 viewer が file を「1 line」と mis-report する件を、
  `.gitattributes` + CI gate (TOML/YAML parse / compileall /
  multiline guard) で対処していることを明文化
- **P1**: `README.md` 冒頭に **migration guide への強い導線**を追加
  (v1.x ユーザーは v2_migration.md を先に読むよう誘導)
- **P1**: README に **GitHub Actions CI badge** を追加
- **P2**: `src/lab_executor/backends/mock_backend.py` の class
  docstring から v1.11 表記を削除、v2.0 lab-executor-mcp 同梱 backend
  として書き直し (`MockVisaManager` は legacy internal name と整理)

### 互換性

- API / package 構造: 不変
- MCP tool 数 (Stable 43 + Experimental 7 = 50): 不変
- DSL `dsl_version=0.8` / extension pack 形式: 不変
- wheel build / install path / dependency: 不変

### 次の作業

`visa-mcp v2.0.0-rc1` (shim package 化 + `lab-executor-mcp >= 2.0`
依存追加) に並走着手予定。

## v2.0.0 — First stable release (split from visa-mcp v1.11.1)

**`lab-executor-mcp` の最初の安定版 release。** `visa-mcp` v1.x が
1 リポジトリで持っていた「PyVISA backend」と「実験実行 runtime」を
v2.0 で 2 リポジトリに分離した、その runtime 側。

### 位置づけ

```
v1.x までの visa-mcp:
  PyVISA backend + runtime + DSL + extension ecosystem (一体)

v2.0:
  lab-executor-mcp ← backend-independent runtime (これ)
  visa-mcp         ← PyVISA backend + 旧 import shim (v2.0.0-rc1
                     で並走着手予定)
```

依存方向: `visa-mcp → lab-executor-mcp` (許可) /
`lab-executor-mcp → visa-mcp` (禁止)。

### lab-executor-mcp に含まれるもの

- DSL (`ExperimentPlan`, `dsl_version=0.8`) + validator + dry-run
- Job manager / state machine / scheduler / barrier
- Group / Map executor
- Observation API (`timeline` / `live_view` / `summary`)
- Benchmark runner + repair tasks
- Definition pack ecosystem (extension `init/install/check/package/
  catalog/authoring`)
- Instrument authoring (`scaffold` / `promote-check` / `review-report`)
- Export / bundle (deterministic reproducibility)
- Audit / locks / SQLite (user_version=3)
- `InstrumentBackend` Protocol + `MockBackend`
- MCP tool: **Stable 43 + Experimental 7 = 50** (v1.0 から不変)
- minimal `lab-executor` CLI (`--version` / `--help` /
  `validate instrument`)

### 含まれないもの (visa-mcp 側に残る)

- `PyVisaBackend` (PyVISA 透過 adapter)
- `VisaManager` / `bus_manager` / `session_manager`
- Raw VISA tools (env-gated `send_command` / `query_instrument`)
- `tools/discovery.py` (PyVISA resource 列挙)

### 互換性

- MCP tool 名 / 引数 / response: **完全互換** (v1.0 凍結のまま)
- DSL `dsl_version=0.8`: **完全互換**
- extension pack `.visa-mcp-ext.zip` 形式: **完全互換**
- `.install_meta.json` schema: **完全互換**
- `~/.visa-mcp/extensions/` install path: **継続使用**
  (v2.1 以降で `~/.lab-executor/extensions/` 並走検討)

### CLI status (v2.0)

`lab-executor` CLI は v2.0 では **minimal**:

- `lab-executor --version` / `--help`: ✓
- `lab-executor validate instrument <path>`: ✓
- `lab-executor serve`: **placeholder** (exit code 2 で v2.1 案内)
- v1.x `visa-mcp` CLI 完全互換: **v2.1 以降で段階 port 予定**

実機 MCP server 起動は **`visa-mcp serve`** (v2.0.0-rc1 で並走 release
予定の visa-mcp shim 経由) を継続利用してください。利用者は v2.0 時点
で CLI を切り替える必要はありません。

### 依存関係

- PyVISA: **不要** (`pip install lab-executor-mcp` のみで動く)
- 実機 backend が必要なら `pip install visa-mcp` を追加 install
  (`lab-executor-mcp >= 2.0` を自動 pull)

### 検証済み項目

ローカル (Windows + Python 3.14):

```
python -m tomllib pyproject.toml          → OK
python -c "import yaml; yaml.safe_load(ci.yml)"  → OK
python -m compileall src tests             → OK
pytest tests/test_v200_split.py            → 10 passed
lab-executor --version                     → 2.0.0
lab-executor --help                        → OK
lab-executor serve (placeholder)           → exit 2, stderr "v2.1"
import lab_executor without visa-mcp       → OK (sys.meta_path block 検証)
import lab_executor without pyvisa         → OK
no top-level "from visa_mcp.*" import      → 0 件 (AST 検査)
Stable 43 + Experimental 7 = 50            → 不変
multiline / LF only guard                  → 11 file pass
```

GitHub Actions (rc4 で確立した 3 job 構成): `test` /
`pyvisa-not-installed` / `build` の green を v2.0.0 tag でも CI で
継続検証する。

### 既存ユーザー向け

```bash
# 実機を使う既存ユーザー (v1.x から)
pip install --upgrade visa-mcp
# → 自動的に lab-executor-mcp >= 2.0 も install される
#   (visa-mcp v2.0.0-rc1 release 後)

# 実機不要 (benchmark / dry-run / validate のみ)
pip install lab-executor-mcp

# 推奨 import (v2.0+)
from lab_executor.extension import ExtensionManifest
from lab_executor.dsl import validate_experiment_plan
# 旧 import (v2.0 で DeprecationWarning 付きで動作、v2.2+ で削除候補)
from visa_mcp.extension import ExtensionManifest  # DeprecationWarning
```

### 次の作業

- **`visa-mcp v2.0.0-rc1`** (並走着手): shim package 化 +
  `lab-executor-mcp >= 2.0` 依存追加 + `PyVisaBackend` + raw VISA
  tools + `visa-mcp serve` 互換維持
- **`lab-executor-mcp v2.1`**: `lab-executor serve` 実装 + CLI
  完全 port + install path 移行計画

### 履歴

履歴は visa-mcp v1.11.1 から bootstrap (新規 repo として開始)。
v1.x までの開発履歴は `TECTOS-JP/visa-mcp` を参照。

Source of truth (visa-mcp repo):
- `docs/separation/module_ownership.yaml`
- `docs/separation/split_manifest.yaml`
- `scripts/bootstrap_lab_executor.py`

## v2.0.0-rc4 — rc3 レビュー応答 (multiline guard / CLI 文言整合 / serve placeholder test)

合言葉: **「正式 v2.0.0 直前の最後の peripheral 整備」**

rc3 external review (P1) を反映した patch。public API / dependency /
extension pack 形式すべて不変。

### 変更点

- **P0** (`tests/test_v200_split.py`):
  - `test_critical_files_are_multiline_and_lf_only` 追加。
    `pyproject.toml` / `ci.yml` / README / docs / 主要 .py が
    10 行以上 + CR 文字 0 件であることを CI で固定 (`.gitattributes`
    の効果を gate 化)
  - `test_lab_executor_serve_is_placeholder` 追加。
    `lab-executor serve` が exit code 2 + stderr に `v2.1` を含むこと
    を固定 (未実装なのに success と誤解されないようにする)
  - `test_lab_executor_cli_version` 追加
- **P1** (`docs/v2_migration.md`):
  - CLI section を README と整合。「v2.0 では Python API は
    `lab_executor` 推奨、CLI は visa-mcp shim 経由を推奨、`lab-executor`
    CLI 完全 port は v2.1+」と明示
  - `lab-executor serve` は placeholder と明示
  - 既存利用者は v2.0 で CLI を切り替える必要が無いことを明文化

### tests

- smoke test 7 件 → **10 件** に増加 (全て pass)
- TOML / YAML parse: OK
- compileall src tests: OK

### 互換性

- MCP tool / DSL schema / extension pack: 不変
- public API / package 構造: 不変

### 正式 v2.0.0 への残タスク

このレビューサイクル後、以下を経て `v2.0.0` 正式 release へ進む:

1. v2.0.0-rc4 GitHub Actions 全 job green 確認
2. tag clone + wheel install + pytest の検証結果を v2.0.0 release note
   に明記
3. 並走: `visa-mcp v2.0.0-rc1` (shim package 化、
   `lab-executor-mcp >= 2.0` 依存追加) 着手

## v2.0.0-rc3 — rc2 レビュー応答 (`.gitattributes` LF / README CLI status / mock_backend docstring)

合言葉: **「Windows CRLF artifact を消し、レビュアーの raw viewer の
mis-report を二度と起こさせない」**

rc2 external review の P0/P1 を反映した patch。public API / dependency /
extension pack 形式すべて不変。

### 調査結果 (rc2 P0: raw 改行問題)

レビュアーは `pyproject.toml` 等を「1 行」と report していたが、
**実体は LF / multi-line で正常**:

| File | git blob LF count |
|------|------------------:|
| `pyproject.toml` | 46 |
| `.github/workflows/ci.yml` | 79 |
| `README.md` | 42 |
| `docs/v2_migration.md` | 129 |
| `src/lab_executor/backends/base.py` | 64 |
| `src/lab_executor/backends/mock_backend.py` | 77 |
| `tests/test_v200_split.py` | 73 |

検証方法:

```bash
git clone --depth 1 --branch v2.0.0-rc2 \
    https://github.com/TECTOS-JP/lab-executor-mcp.git rc2-check
cd rc2-check
python -c "import tomllib; tomllib.loads(open('pyproject.toml').read())"
# → OK
python -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml'))"
# → OK
python -m compileall src tests
# → OK
```

原因: Windows checkout (autocrlf=true) によって working tree の
file が CRLF 化される。レビュアーの viewer はおそらく **`\n` not
preceded by `\r`** を line count とみなしており、純 CRLF file を
「1 line」と mis-report していたと推測される。

### rc3 で打った対策

- **`.gitattributes` 追加**: `* text=auto eol=lf` + 主要拡張子
  (`*.py` / `*.toml` / `*.yaml` / `*.yml` / `*.json` / `*.md` 等) に
  `eol=lf` を強制。これで Windows checkout でも file は LF となり、
  raw viewer の counter 仕様に関わらず正しく line を見せる
- **P1**: `README.md` に **CLI status in v2.0** section を追加
  (lab-executor CLI の minimal 範囲 + serve は placeholder + v2.1+
  で段階 port 予定を明記)
- **P1**: `src/lab_executor/backends/mock_backend.py` docstring を
  **v2.0 lab-executor-mcp 同梱 backend** として書き直し
  (v1.11 内部準備の文脈を削除、`MockVisaManager` は legacy internal
  と整理)

### tests

- 全 smoke test 7 件 (rc1 から不変): pass
- `compileall src tests`: OK
- `lab-executor --version` / `--help`: OK
- TOML / YAML parse: OK

### 互換性

- MCP tool / DSL schema / extension pack: 不変
- API / CLI / pyproject 構造: 不変
- `.gitattributes` 追加のみ (動作影響ゼロ、行末文字の固定のみ)

### 次フェーズ

`v2.0.0-rc3` レビュー後に `v2.0.0` 正式 release。並走して
`visa-mcp v2.0.0-rc1` (shim 化 + `lab-executor-mcp >= 2.0` 依存追加)
着手予定。

## v2.0.0-rc2 — rc1 レビュー応答

合言葉: **「rc1 で抜けていた CLI entry point と CI gate を埋める」**

`lab-executor-mcp` v2.0.0-rc1 の external review (P0/P1) を反映した
patch。public API / dependency / extension pack 形式すべて不変。

### 主な変更

- **P0**: `src/lab_executor/cli.py` 新規追加 (rc1 で `pyproject.toml`
  `[project.scripts] lab-executor = "lab_executor.cli:main"` が
  module 不在で壊れていた)。`lab-executor --version` / `--help` /
  `validate instrument <path>` / `serve` (placeholder) を提供
- **P0**: `.github/workflows/ci.yml` を 3 job 構成に拡張:
  - `test`: compileall + import smoke + MockBackend smoke +
    `lab-executor --help` smoke + AST 検査 (top-level `visa_mcp.*`
    import 0 件) + pytest
  - `pyvisa-not-installed`: pyvisa uninstall 後の import / smoke
  - `build`: wheel build + wheel install + import 確認
- **P1**: `src/lab_executor/backends/base.py` docstring を **v2.0
  公開境界向け** に書き直し (v1.1 spike / v1.11 内部準備の文脈を削除)
- bootstrap script (`visa-mcp` repo の
  `scripts/bootstrap_lab_executor.py`) に `cli.py` 生成を追加し、
  再 bootstrap で同等の rc2 ツリーを再現できるようにした

### 検証

ローカル (Windows + Python 3.14) で以下が通る:

```
python -m tomllib pyproject.toml   # OK
python -m compileall src tests     # OK
python -c "import lab_executor"    # OK (Stable 43 + Exp 7 = 50)
lab-executor --version             # 2.0.0-rc2
lab-executor --help                # OK
pytest tests/test_v200_split.py    # 7 passed
```

`visa-mcp` を import 経路から block して再検証 → `lab-executor` は
`pyvisa` を sys.modules に load しないことを確認済。

### 互換性

- MCP tool 数 (Stable 43 + Experimental 7 = 50): 不変
- DSL `dsl_version=0.8`: 完全互換
- extension pack 形式 / `.install_meta.json`: 完全互換
- `~/.visa-mcp/extensions/` install path: 継続使用

## v2.0.0-rc1 — Initial split candidate from visa-mcp v1.11.1

lab-executor-mcp の最初の release candidate。`visa-mcp` v1.11.1 から
runtime / DSL / ecosystem layer を切り出した。

### 含まれるもの

- DSL (`ExperimentPlan`, `dsl_version=0.8`) + validator + dry-run
- Job manager / state machine / scheduler / barrier
- Group / Map executor
- Observation API
- Benchmark runner + repair tasks + 5 fixture tasks
- Definition pack ecosystem (extension init/install/check/package/catalog/authoring)
- Instrument authoring (scaffold/promote-check/review-report)
- Export / bundle (deterministic reproducibility)
- Audit / locks / SQLite (user_version=3)
- `InstrumentBackend` Protocol + `MockBackend`
- MCP tool: Stable 43 + Experimental 7 = 50 (v1.0 から不変)

### 含まれないもの (visa-mcp 側に残る)

- `PyVisaBackend` (PyVISA 透過 adapter)
- `VisaManager` / `bus_manager` / `session_manager`
- Raw VISA tools (`send_command` / `query_instrument`, env-gated)
- `tools/discovery.py` (PyVISA resource 列挙)

### 互換性

- DSL `dsl_version=0.8` 完全互換
- extension pack 形式 (`.visa-mcp-ext.zip`) 完全互換
- `.install_meta.json` schema 完全互換
- `~/.visa-mcp/extensions/` install path 継続使用 (v2.x で再評価)
- MCP tool 名 / 引数 / response: v1.0 凍結のまま

### 依存関係

- PyVISA: **不要** (`pip install lab-executor-mcp` で動く)
- 実機 backend が必要なら `pip install visa-mcp` を追加 install

### 履歴

履歴は visa-mcp v1.11.1 から **切り出して新規 repo として開始**
(git filter-repo による history rewrite は行わない)。
visa-mcp の git log は引き続き `TECTOS-JP/visa-mcp` を参照。

### Source of truth

- `docs/separation/module_ownership.yaml` (visa-mcp v1.11.1)
- `docs/separation/split_manifest.yaml` (visa-mcp v1.11.1)
- bootstrap script: `scripts/bootstrap_lab_executor.py` (visa-mcp)
