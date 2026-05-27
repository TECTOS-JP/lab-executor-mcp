# Extension Path Migration Guide (v2.5)

## Background

lab-executor-mcp v2.0 で `visa-mcp` から分離した時点では、
definition pack の install path は v1.x 互換のため
`~/.visa-mcp/extensions/` を継続使用していた。

v2.4.0 で **dual-path read** を導入し、新 path
`~/.lab-executor/extensions/` も読み取り対象になった。書き込み
default は引き続き `~/.visa-mcp/extensions/` (legacy)。

v2.5.0 では、その path 状態に対する **計画出力 (plan)** を追加する。
ファイルの copy / move / delete は **まだ実行しない**。

## Path roles (v2.5)

| Role | Path |
|------|------|
| Legacy install path | `~/.visa-mcp/extensions/` |
| New install path | `~/.lab-executor/extensions/` |
| Read (catalog / check) | both (new first, then legacy) |
| Write default (install) | legacy (`~/.visa-mcp/extensions/`) |

## Duplicate policy (案 B)

```text
report_conflict_no_implicit_precedence
```

The system **does not silently prefer one location** when duplicate
extension IDs exist. You must resolve duplicates explicitly.

- `extension catalog` / `check` で duplicate を **warning** として報告
- `--strict` で exit 1
- `resolve_extension_by_id(id)` API は duplicate 時に
  `duplicate_extension_id` error を返す (v2.5+)

### catalog/check と migration-plan で severity が違う理由

| CLI | duplicate の扱い | 理由 |
|-----|--------------|------|
| `extension catalog` / `check` | **warning** (`--strict` で exit 1) | 「現在の状態」を報告するコマンド。duplicate でも閲覧は可能 |
| `extension migration-plan` | **error** (default で exit 1) | 移行を妨げる衝突。**解消しない限り migration には進めない** |

つまり、duplicate は「動作させるだけなら見逃せるが、移行する前に
必ず解消すべき」というポリシー。`catalog` は CI を止めず、
`migration-plan` は CI を止める。

## `lab-executor extension migration-plan`

v2.5 で追加された **plan-only** CLI:

```bash
lab-executor extension migration-plan
lab-executor extension migration-plan --json
lab-executor extension migration-plan --strict
```

何が出力されるか:

- `summary.legacy_only`: legacy path にのみ存在する pack 数
- `summary.new_only`: new path にのみ存在する pack 数
- `summary.duplicates`: 両 path に存在する `extension_id` 数
- `summary.invalid`: 上記 2 つの合算 (legacy 互換)
- `summary.invalid_metadata`: `extension.yaml` parse 失敗 / `extension_id`
  欠落など (severity=error の起点、v2.5.1+)
- `summary.missing_extension_yaml`: `extension.yaml` 自体が無い pack
  ディレクトリ (severity=warning の起点、v2.5.1+)
- `summary.migration_required`: True なら何らかの対応が必要
- `actions[]`: 推奨 action と severity (info / warning / error)

**v2.5 では `--apply` は存在しない**。実ファイルは一切変更しない。

## `lab-executor extension migration-plan --copy-plan` (v2.6)

```bash
lab-executor extension migration-plan --copy-plan
lab-executor extension migration-plan --copy-plan --json
```

v2.6 で追加された **copy candidate 出力**。

- `legacy_only` extension に対し「将来 copy するなら source → target」
  を `copy_plan.candidates[]` に列挙する
- **実 copy は一切しない** (`apply_available=False`、v2.6 固定)
- 以下のいずれかがあれば `copy_plan.status="blocked"` で candidate を
  出さない:
  - `duplicate_extension_id` (案 B により先に解消が必要)
  - `invalid_extension_metadata`
  - target (`new_path/<name>`) が既に存在する (`target_exists`)

`summary` に v2.6 で追加された field:

- `copy_candidates`: 出力された candidate 数
- `copy_blocked`: copy_plan が blocked 状態か (bool)

### `target_exists` の扱い (v2.6.1 明文化)

`new_path/<dir_name>` が既に存在する candidate は **copy できない**
ものとして処理する。挙動は他 candidate の有無で分岐:

| 状況 | `copy_plan.status` | `blocked_reasons[]` |
|------|---------------|-----|
| 全 legacy_only が `target_exists` → candidate が 0 件 | `blocked` | 全件を `target_exists` で列挙 |
| 一部だけ `target_exists`、他は copy 可能 → candidate >=1 | **`ready`** | skipped 分を `target_exists` で残す |

つまり `blocked_reasons` は status=blocked の理由とは限らず、
**「candidate にできなかった件の理由」**を列挙する schema。`status=
ready` でも `blocked_reasons` に skipped 詳細が入りうる。

v2.7 で `--apply` を入れるときは、`blocked_reasons` が空である
ことを **追加の事前条件**として要求する予定 (現在 candidate になって
いるものだけを apply 対象とし、skipped を黙って無視しない)。

## `lab-executor extension migration-plan --copy-plan --apply` (v2.7)

```bash
lab-executor extension migration-plan --copy-plan --apply
lab-executor extension migration-plan --copy-plan --apply --json
```

v2.7 で追加された **controlled copy apply**。legacy にしかない pack
を new path へ実コピーする。

### 厳格な事前条件 (一つでも欠ければ apply 不可)

- `copy_plan.status == "ready"`
- `copy_plan.candidates` が 1 件以上
- `copy_plan.blocked_reasons` が空 (`target_exists` skipped も含む)
- duplicate_extension_id / invalid_extension_metadata がない
- `--apply` 単独使用は不可 (`--copy-plan` と併用必須、単独は exit 2)

### 安全保証

- **source は削除しない** (`delete_performed=False` 固定)
- **target は上書きしない** (既存なら skipped + status=blocked)
- candidate ごとに `target.tmp-<stamp>/` に copy → atomic-ish rename
- 実行直前に migration plan を **再計算** (UI 表示後の filesystem
  変化をケア)
- partial failure 時は **fail-fast** (以降の candidate を実行せず、
  成功済みは残す、`status="partial_failure"`)
- manifest を `~/.lab-executor/migration_logs/extension-copy-<stamp>
  .json` に必ず保存 (blocked 時も保存)

### manifest schema

```json
{
  "schema_version": "v2.7",
  "operation": "extension_copy_apply",
  "created_at": "2026-05-26T...",
  "source_default": "~/.visa-mcp/extensions",
  "target_default": "~/.lab-executor/extensions",
  "status": "ok",
  "copied": [
    {"extension_id": "...", "source": "...", "target": "...",
     "file_count": 12, "bytes": 34567}
  ],
  "failed": [],
  "skipped": [],
  "blocked_reasons": [],
  "delete_performed": false,
  "overwrite_performed": false
}
```

## v2.12: Controlled Cleanup Apply (trash 移動)

```bash
# 1. preflight で confirmation token を取得
lab-executor extension migration-log cleanup-plan --latest --preflight --json

# 2. token を --confirm に渡して apply
lab-executor extension migration-log cleanup-plan --latest --apply \
  --confirm cleanup:2:extension-copy-20260527-103012
```

### 安全保証 (実装で固定)

- legacy source は **完全削除しない**。`~/.lab-executor/migration_
  trash/<manifest_stem>/` へ移動するだけ (`permanent_delete_performed
  =False` 固定)
- **target (new path) は変更しない**
- target に何もしないため `overwrite_performed=False` 固定
- apply 直前に plan + preflight を **再計算**。UI 表示後の filesystem
  変化があれば blocked
- `--confirm` 必須。token は `cleanup:<count>:<manifest_stem>`
- token 不一致 → blocked、`--confirm` 無し → exit 2
- trash target 既存 → blocked (上書きしない)
- cross-device error (EXDEV) → failed (copy+delete fallback なし)
- partial failure は **fail-fast**
- cleanup manifest を `~/.lab-executor/migration_logs/extension-
  cleanup-<stamp>.json` に必ず保存 (blocked 時も保存)

### Cleanup manifest schema (v2.12)

```json
{
  "schema_version": "v2.12",
  "operation": "extension_cleanup_apply",
  "source_manifest": ".../extension-copy-...json",
  "confirmation_token": "cleanup:2:extension-copy-...",
  "trash_root": "~/.lab-executor/migration_trash/...",
  "status": "ok",
  "moved_to_trash": [...],
  "failed": [],
  "skipped": [],
  "blocked_reasons": [],
  "delete_performed": false,
  "permanent_delete_performed": false,
  "overwrite_performed": false,
  "trash_move_performed": true
}
```

`migration-log list / inspect` は v2.12 で cleanup manifest も対象に
含まれる (`verify` は当面 copy manifest のみ)。

### rollback-plan への影響

cleanup apply 後は legacy source が trash へ移動しているため、同じ
copy manifest から `rollback-plan` を見ると legacy source missing
扱いで blocked になる (戻す先が無い)。trash 内容から手作業で復元
する必要がある。

### rollback apply は **v2.12 でも未実装**

`rollback-plan --apply` は exit 2。v2.14+ で慎重に検討する。

## v2.11: Apply Preflight (削除前提条件評価)

```bash
lab-executor extension migration-log cleanup-plan  --latest --preflight
lab-executor extension migration-log rollback-plan --latest --preflight
```

実 ファイルは変更しない。**実 apply は v2.12+ で慎重に検討**。
preflight は次を評価する:

| Check | 内容 |
|-------|------|
| `has_candidates` | candidate >=1 |
| `plan_blocked_reasons_empty` | plan に blocked が無い |
| target / legacy source 存在 | preflight 時点で再確認 |
| target ≠ legacy source | 同一 path だと事故防止のため block |

`apply_supported` / `apply_available` は v2.11 では **常に False**。
preflight が `eligible=true` でも実 apply はできない。

### Confirmation token (v2.12+ で `--confirm` で要求)

```
cleanup:<candidate_count>:<manifest_stem>
rollback:<candidate_count>:<manifest_stem>
```

v2.12+ で `cleanup-plan --apply --confirm cleanup:2:extension-copy-...`
のように使う想定。v2.11 では preflight 出力に表示のみ。

### Trash strategy (v2.12+ 方針)

cleanup / rollback の実 apply 時は **完全削除せず trash 移動**を予定:

```
~/.lab-executor/migration_trash/<manifest_stem>/
```

preflight 出力に `future_trash_root` field で明示。

## v2.10: status semantics & `--latest`

### `--latest` flag

`inspect` / `verify` / `rollback-plan` / `cleanup-plan` で `--latest`
を指定すると、`operation == extension_copy_apply` の最新 manifest を
自動選択する。明示 path との併用は usage error (exit 2)。

### Plan-only warning は status を変えない (案 A)

v2.10 で **plan-only warning と real problem を分離**:

- 実 problem (blocked / verify error / manifest 改ざん) があれば
  `status="warning"` または `"error"`
- 実 problem が無く plan-only warning だけなら `status="ok"`
- `--strict` は real problem だけで exit 1 化 (plan-only では落ちない)

これにより CI で `--strict` を使っても plan-only 状態で false fail
しない。

## rollback / cleanup の状態別 ふるまい

| 状態 | rollback-plan | cleanup-plan |
|------|---------------|--------------|
| target あり / legacy source あり | rollback candidate | cleanup candidate になり得る |
| target なし / legacy source あり | **already_absent** | cleanup 対象外 |
| target あり / legacy source なし | blocked | legacy_source_missing リスト |
| target verify error | rollback candidate にしない | blocked |
| manifest 異常 | blocked | blocked |

## Command matrix (which one to use when)

| コマンド | 目的 | 実ファイル変更 |
|----------|------|---|
| `migration-log verify <manifest>` | copy 結果が健全か確認 | なし |
| `migration-log rollback-plan <manifest>` | 戻すなら何を target 削除候補にするか表示 | なし |
| `migration-log cleanup-plan <manifest>` | legacy source 整理候補を表示 (target が verify ok 前提) | なし |
| `migration-log rollback --apply` | **未実装** (v2.10+ 候補) | — |
| `migration-log cleanup --apply` | **未実装** (v2.10+ 候補) | — |

`rollback-plan` と `cleanup-plan` は **方向が逆**:

- `rollback-plan` = migration を **取り消す**方向 → target 側を削除
  候補、legacy source が必要
- `cleanup-plan` = migration を **進める**方向 → legacy source を
  削除候補、target が verify ok 必要

混同すると危険なので、目的と削除対象が逆になることを意識すること。

## `lab-executor extension migration-log` (v2.8)

```bash
lab-executor extension migration-log list
lab-executor extension migration-log inspect <manifest>
lab-executor extension migration-log verify <manifest>
```

v2.7 で生成した apply manifest を CLI で読み返す段階。実 ファイル
変更は一切しない (rollback / delete / overwrite は未実装)。

- **`list`**: `~/.lab-executor/migration_logs/extension-copy-*.json`
  を timestamp 降順で列挙。各 entry に
  `created_at` / `status` / `copied_count` / `failed_count` /
  `skipped_count` / `manifest_path`
- **`inspect <manifest>`**: 1 件の manifest を parse + schema 検査して
  表示。`delete_performed=false` / `overwrite_performed=false` を
  目立たせる
- **`verify <manifest>`**: copied[] の target が現在も存在し、
  `extension.yaml` が読め、`extension_id` が manifest と一致するかを
  確認

### verify が検出する error / warning

| 種別 | error_class / warning_class |
|------|------|
| error | `target_missing` / `target_extension_yaml_missing` / `target_extension_yaml_unreadable` / `extension_id_mismatch` / `delete_performed_unexpected` / `overwrite_performed_unexpected` / `manifest_schema_unsupported` / `manifest_not_found` |
| warning | `source_missing` (source は将来整理される可能性があるため warning) |

`source_missing` のみなら status=warning、それ以外が混じれば
status=error。`--strict` で warning も exit 1。

### manifest 保存失敗時 (v2.8 で実装)

v2.7.1 で予約した案 A を実装:

```
manifest 保存成功 → 通常どおり ok / partial_failure
manifest 保存失敗 → status=partial_failure に格上げ
                  + failed[] に manifest_write_failed
                  + manifest_path = None
```

manifest なしの copy 成功は audit 上「成功」とみなさない。

### `target_exists` の検出タイミング別 status (v2.7.1 明文化)

`target_exists` は **検出されたタイミングで status が変わる**:

| 検出タイミング | 結果 status | manifest |
|------|-------|------|
| pre-apply (`copy_plan` 段階で既に target が存在) | `blocked` | 保存 (copied=空) |
| during apply (実 copy 直前の再確認で target が出現) | `partial_failure` | 保存 (skipped に記録) |

いずれのケースでも **overwrite は行わない**。後者は他プロセスが
plan 表示後に target を作ったレースケース。実装上は `copytree`
直前にもう一度 `target.exists()` を確認し、存在すれば skipped に
記録して以降の candidate を停止する (fail-fast)。

### manifest 保存失敗時の方針 (v2.8+ で実装予定)

現在は manifest 書き込み失敗時の挙動が未定義。v2.8 で次の **案 A**
を実装予定:

> manifest 保存に失敗した場合、実 copy の成否にかかわらず全体を
> `partial_failure` 扱いに格上げし、`failed[]` に `manifest_write_
> failed` を記録する。manifest なしの copy 成功は audit 上「成功」と
> みなさない方針。

### v2.7 で **やらないこと**

- source delete / legacy path 自動 cleanup
- target overwrite / `--force`
- install default 変更
- duplicate 自動解決
- 自動 rollback (manifest 保存のみで人手復旧前提)
- copy 後の verify (v2.8 で導入予定: file_count / bytes 一致、
  extension.yaml が読めること、`extension check` で通ること)

### blocked 例 (duplicate あり)

```json
{
  "status": "error",
  "copy_plan": {
    "status": "blocked",
    "candidates": [],
    "blocked_reasons": [
      {
        "reason_class": "duplicate_extension_id",
        "extension_id": "local.my_pack",
        "locations": [
          "~/.lab-executor/extensions/local.my_pack",
          "~/.visa-mcp/extensions/local.my_pack"
        ]
      }
    ],
    "apply_available": false
  }
}
```

## Duplicate を解消する手順 (手作業)

```bash
# 1. duplicate を確認
lab-executor extension migration-plan --json

# 2. どちらを残すか決める (通常は新しい方 / version が新しい方)
ls -la ~/.visa-mcp/extensions/local.my_pack
ls -la ~/.lab-executor/extensions/local.my_pack

# 3. 不要な方を rename or 削除
mv ~/.visa-mcp/extensions/local.my_pack \
   ~/.visa-mcp/extensions/.local.my_pack.bak

# 4. 再確認
lab-executor extension migration-plan
lab-executor extension check
```

## migration_required の判定 (v2.5)

```
migration_required = True if any of:
  - legacy_only >= 1   (legacy にのみある → 新 path への移行候補)
  - duplicates >= 1    (両 path にある → 解消が必要)
  - invalid >= 1       (壊れた pack がある → 修正が必要)

migration_required = False otherwise (new_only のみ等)
```

## やらないこと (v2.5)

- `--apply` flag
- 自動 copy / move / delete
- install default の `~/.lab-executor/extensions/` への切替
- duplicate 時の自動採用
- extension pack 形式 / `.install_meta.json` schema の変更

## 将来のロードマップ

| Version | 内容 | Status |
|---------|------|------|
| v2.4.0 | dual-path read + duplicate 検出 | 実装済 |
| v2.5.0 | migration-plan (現状分類) | 実装済 |
| v2.6.0 | migration-plan --copy-plan (copy 候補生成、実 copy なし) | 実装済 |
| v2.7.0 | Controlled `--apply` (実 copy、no delete / no overwrite) | 実装済 |
| v2.8.0 | migration-log list / inspect / verify (実 copy 後の追跡 / 検証) | 実装済 |
| v2.9.0 | rollback-plan / cleanup-plan (plan only、削除なし) | 実装済 |
| v2.10.0 | plan refinement (`--latest` / `already_absent` 分離 / status 整理 / verify 統合) | 実装済 |
| v2.11.0 | cleanup / rollback `--preflight` (apply 前提条件評価、削除なし) | 実装済 |
| v2.12.0 | controlled cleanup `--apply` (trash 移動、`--confirm` 必須、永久削除なし) | 実装済 |
| v2.13+ | cleanup manifest verify / trash inspection | 検討中 |
| v2.14+ | controlled rollback `--apply` | 検討中 |
| v2.9+   | install default を `~/.lab-executor/extensions/` へ切替判断 | 検討中 |

順序は **検出 → 計画 → copy-plan → apply → default 切替** で固定。
extension path はユーザー環境に直接影響するため、急がない。
