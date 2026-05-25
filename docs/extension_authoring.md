# Extension Authoring (v1.7, experimental)

合言葉: **「良い definition pack を作りやすくする」**

v1.2〜v1.6 で「定義 → install → check → package → catalog / install」
までは揃った。v1.7 では入口側、つまり **空の directory から安全な
pack を作り、整え、配布可能な品質に持っていく** までを CLI で支援する。

> v1.7 でも MCP tool 追加ゼロ。**authoring CLI のみ**。remote registry /
> Python plugin / signature 等には進まない。

## 1 分でできる: scaffold → doctor → package

```bash
# 1. scaffold
visa-mcp extension init my_pack --template instrument_pack \
    --id com.example.my_pack --author "Acme"

# 2. 何を直せば良いかを doctor で確認
visa-mcp extension doctor my_pack/extension.yaml
visa-mcp extension doctor my_pack/extension.yaml --strict

# 3. zip を作らず内容を preview
visa-mcp extension package my_pack/extension.yaml --dry-run

# 4. strict で zip 化 (publishing 前)
visa-mcp extension package my_pack/extension.yaml --strict

# 5. 配布前 / receive 後の整合性検査
visa-mcp extension verify-package dist/com.example.my_pack-0.1.0.visa-mcp-ext.zip

# 6. local install して動作確認
visa-mcp extension install dist/com.example.my_pack-0.1.0.visa-mcp-ext.zip
visa-mcp extension check com.example.my_pack
```

## `extension init`

```bash
visa-mcp extension init <pack_name>
    [--target-dir <dir>]
    [--id <reverse-DNS extension_id>]
    [--template minimal|mock_basic|instrument_pack]
    [--author "<name>"]
    [--force]
    [--json]
```

| Template | 生成内容 |
|----------|---------|
| `minimal` | `extension.yaml` + `README.md` のみ |
| `mock_basic` | minimal + `instruments/README.md` + `benchmarks/README.md` |
| `instrument_pack` | minimal + `instruments/example_instrument.yaml` + `registry_entries/INDEX.yaml` |

### 生成 YAML の形式保証 (v1.7.1)

生成された `extension.yaml` は **human-readable な multi-line YAML**
として書き出され、`yaml.safe_load()` で正しく round-trip できる。
test (`tests/test_v171_review.py`) で各 template について保証している。

### `--force` の挙動 (v1.7.1 docs 化)

`--force` は **scaffold が生成する file のみ上書き** する。既存の手書き
file (例: pack 内に手で追加した `instruments/myinst.yaml`、
独自 `docs/` 等) は **削除されず残る**。

```
visa-mcp extension init my_pack --force
[OK] init local.my-pack -> ./my_pack
  WARN  extension_init_force_retains_files: --force は既存 file
        (3 件) を残し、template が生成する file のみ上書きする
  + extension.yaml
  + README.md
```

完全に作り直したい場合は、手動で directory を削除してから init する:

```bash
rm -rf my_pack
visa-mcp extension init my_pack --template minimal
```

これは「scaffold で誤って成果物を消す」事故を防ぐためのポリシー。

`extension.yaml` には **catalog metadata の雛形** (summary / license /
authors / safety_notes 等) と `stability.support_level: draft` /
`executable_code: false` が入る。生成直後でも
`visa-mcp validate extension` が通る。

`--id` 省略時は `local.<pack_name>` に正規化 (小文字、`_` を `-` に置換)。

### 例

```bash
$ visa-mcp extension init scope_pack --template instrument_pack \
    --author "TECTOS"
[OK] init local.scope-pack -> ./scope_pack
  + extension.yaml
  + README.md
  + instruments/README.md
  + instruments/example_instrument.yaml
  + registry_entries/INDEX.yaml
  next: visa-mcp extension doctor scope_pack/extension.yaml
```

## `extension doctor`

```bash
visa-mcp extension doctor <extension.yaml> [--strict] [--json]
```

`validate` / `validate --strict` / `package --dry-run` / catalog / README
/ license / verified evidence を **まとめて 1 ステップで確認**し、
構造化された **`recommended_actions`** を返す。

### 出力 (JSON)

```json
{
  "status": "warning",
  "summary": {
    "errors": 0,
    "warnings": 3,
    "has_readme": true,
    "has_catalog_summary": true,
    "has_catalog_license": true,
    "has_safety_notes": true,
    "missing_validation_evidence_count": 1,
    "ready_to_package": true,
    "ready_for_registry_review": false
  },
  "recommended_actions": [
    {
      "action": "add_validation_evidence",
      "reason": "instruments/foo.yaml: support_level=verified but metadata.validation_evidence is empty"
    }
  ]
}
```

| `summary.*` | 意味 | gate |
|-------------|------|------|
| `ready_to_package` | error が無く `extension package` を成功させられる **最低条件** | local zip 化 |
| `ready_for_registry_review` | 上記 + strict 観点でも問題なし (README / catalog.summary / catalog.license / verified evidence 完備) | **publishing / PR / registry 掲載** |

`ready_to_package=true` でも `ready_for_registry_review=false` は
自然にあり得る (例: ローカル開発中の draft pack)。
**外部に出す前は必ず `ready_for_registry_review` を確認**する。

### 出力分類 (CLI human-readable, v1.7.1)

`doctor` の human-readable 出力は 3 グループに分けて表示する:

```
[WARN] doctor local.scope-pack  errors=0 warnings=2  ready_to_package=True  ready_for_registry_review=False
  Errors (block package):
    (none)
  Warnings (quality):
    WARN   [validate] missing_catalog_summary: ...
  Strict-only issues (must fix before registry / publishing):
    STRICT [strict_validate] strict_empty_contents: ...
  Recommended actions:
    fix?   add_catalog_summary: catalog.summary is shown in ...
```

- **Errors**: package 作成を妨げる問題
- **Warnings**: 品質改善推奨 (任意)
- **Strict-only issues**: registry review / publishing 前に必ず解消
  (`--strict` 付き doctor では本体 errors に格上げ)

`--strict` を付けると、`strict_*` warning を本体 errors に格上げ
する (CI fail gate 向け)。default は warning として表示。

## `extension package --dry-run`

```bash
visa-mcp extension package <extension.yaml> --dry-run [--strict] [--json]
```

zip を **作らず**、以下を返す:

- `package_name`: 生成される予定の zip 名
- `files_included`: 含まれる予定 file (sorted)
- `files_excluded`: 除外 file (.git / __pycache__ / *.pyc 等)
- `package_manifest_preview`: 先頭 10 件の `{path, sha256}` + 全体 `file_count`
- `checksums_preview_count`: checksum 行数

CI で「次に package したら何が入るか」を確認する用途。
本物の `package` と同じ validation を経由するため、
`extension validation_failed` 系 error はここでも出る。

## Authoring workflow (一本道)

```
visa-mcp extension init <name>
  ↓ (edit extension.yaml, add instruments/...)
visa-mcp extension doctor <ext.yaml>           # 何を直せばよいか
  ↓
visa-mcp validate extension <ext.yaml>         # schema 細部
visa-mcp validate instrument <inst.yaml>       # instrument 個別
  ↓
visa-mcp extension package <ext.yaml> --dry-run
  ↓
visa-mcp extension package <ext.yaml> --strict
  ↓
visa-mcp extension verify-package dist/<...>.zip
  ↓
visa-mcp extension install dist/<...>.zip
visa-mcp extension check <extension_id>
visa-mcp extension catalog --installed
```

v1.7 では `extension doctor` がこの workflow の中央 hub。

## v1.7 で対応しない (v1.8+ 候補)

- `extension add-instrument` / `extension add-template` などの
  詳細 scaffold (instrument 1 件追加が手動)
- remote registry / pull CLI
- author profile の永続化 (`~/.visa-mcp/profile.yaml` 等)
- AI-assisted authoring (LLM に PDF → YAML 起こさせる)
- Python plugin / backend plugin
- **scaffold template の外部ファイル化** (現在は
  `extension_authoring.py` 内に同梱)。v1.8+ で
  `src/visa_mcp/templates/extensions/<name>/` に分離し、
  `importlib.resources` で読み込む形 (Jinja2 依存は引き続き入れない)

## 関連 docs

- [`extension_publishing_checklist.md`](extension_publishing_checklist.md)
- [`extension_packaging.md`](extension_packaging.md)
- [`extension_install.md`](extension_install.md)
- [`extension_integrity.md`](extension_integrity.md)
- [`extension_catalog.md`](extension_catalog.md)
- [`definition_packs.md`](definition_packs.md)
- [`registry_contribution.md`](registry_contribution.md)
