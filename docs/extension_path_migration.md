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
| v2.7.0 | Controlled `--apply` with backup / rollback | 検討中 |
| v2.8.0 | migration rollback / verify copied packs | 検討中 |
| v2.9+   | install default を `~/.lab-executor/extensions/` へ切替判断 | 検討中 |

順序は **検出 → 計画 → copy-plan → apply → default 切替** で固定。
extension path はユーザー環境に直接影響するため、急がない。
