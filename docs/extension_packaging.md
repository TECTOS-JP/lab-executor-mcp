# Extension Packaging (v1.5, experimental)

合言葉: **「作れる / install できる / 整合できる」→「配布可能な成果物としてまとめられる」**

v1.4 までで「local pack の検証 / install / integrity」が揃った。
v1.5 では、外部 contributor が作った definition pack を **配布可能な
zip パッケージ**にまとめ、受け取り側で **再検証**できるようにする。

> v1.5 は **packaging まで**。zip からの install / remote install /
> 署名 / trust store には進まない (v1.6+ 候補)。

## CLI

```bash
# pack 化 (default: <pack_dir>/dist/<extension_id>-<version>.visa-mcp-ext.zip)
visa-mcp extension package path/to/extension.yaml
visa-mcp extension package path/to/extension.yaml --output dist/
visa-mcp extension package path/to/extension.yaml --strict
visa-mcp extension package path/to/extension.yaml --json

# package 整合検証
visa-mcp extension verify-package dist/tectos.mock.basic-0.1.0.visa-mcp-ext.zip
visa-mcp extension verify-package dist/xxx.zip --json
```

## Package 形式

```
<extension_id>-<version>.visa-mcp-ext.zip
├── extension.yaml
├── package_manifest.json        ← v1.5 必須
├── checksums.sha256             ← v1.5 必須
├── README.md                    (任意 / --strict で error 候補)
├── instruments/
├── benchmarks/
├── templates/
├── registry_entries/
└── mock_scenarios/
```

### package_manifest.json

```json
{
  "package_format": "visa-mcp-extension-package",
  "package_format_version": "1.0",
  "extension_id": "tectos.mock.basic",
  "extension_version": "0.1.0",
  "created_at": "2026-05-23T12:00:00+00:00",
  "created_by": "visa-mcp 1.5.0",
  "executable_code": false,
  "file_count": 8,
  "files": [
    {"path": "extension.yaml", "sha256": "..."},
    {"path": "instruments/mock_psu.yaml", "sha256": "..."}
  ],
  "checksums_file": "checksums.sha256",
  "checksums_sha256": "..."
}
```

#### Field 仕様

| Field | 型 | 説明 |
|-------|----|------|
| `package_format` | str | 固定値 `"visa-mcp-extension-package"`。verify-package は値域チェック |
| `package_format_version` | str | SemVer 表記の package 形式バージョン。v1.5 では `"1.0"`。将来 field 追加時に minor up |
| `extension_id` | str | extension.yaml の `extension_id` (reverse-DNS 推奨) |
| `extension_version` | str | extension.yaml の `version` (SemVer) |
| `created_at` | str | ISO8601 UTC、`+00:00` 付き |
| `created_by` | str | `"visa-mcp <semver>"` 形式 (生成 CLI バージョン) |
| `executable_code` | bool | **常に `false`**。`true` は verify-package で error。v1.x で Python plugin を許可しないポリシーが続く |
| `file_count` | int | `files[]` の要素数 |
| `files` | list | 各 file の `{path, sha256}` (path は zip 内 rel、sorted) |
| `files[*].path` | str | zip 内 相対 path (POSIX `/` 区切り) |
| `files[*].sha256` | str | sha256 hex digest (64 char) |
| `checksums_file` | str | 固定値 `"checksums.sha256"` |
| `checksums_sha256` | str | `checksums.sha256` 自身の sha256 (信頼の連鎖) |

#### 後方互換ポリシー

- `package_format_version` の **minor up** = field 追加のみ (既存
  field の型 / 意味は変えない)。verify-package は未知の field を無視。
- **major up** = breaking change。verify-package は `package_format_invalid`
  に格上げする可能性あり。
- v1.5 内では `1.0` を固定。

### checksums.sha256

各行 `<sha256>  <relative-path>` 形式 (`sha256sum` と互換)。

```
9f86d081...  extension.yaml
b94d27b9...  instruments/mock_psu.yaml
```

## package 時の検査

1. `validate_extension_file()` を通す (`--strict` 指定時は strict)
2. pack directory 内の file を再帰収集 (除外ルール適用)
3. `..` や絶対 path を含む rel は拒否 (`package_path_unsafe`)
4. file 数 0 なら拒否 (`empty_package`)
5. `checksums.sha256` を生成
6. `package_manifest.json` を生成
7. deterministic な順序で zip 化 (sorted by rel path)
8. zip 全体の sha256 を計算して返却

### 除外ルール (staging copy と同じ)

| 種別 | 例 |
|------|-----|
| directory | `.git/` `__pycache__/` `.mypy_cache/` `.pytest_cache/` `.idea/` `.vscode/` `node_modules/` |
| file 名 | `.DS_Store` `Thumbs.db` |
| 拡張子 | `*.pyc` `*.pyo` `*.tmp` `*.swp` |

pack directory 内の `package_manifest.json` / `checksums.sha256` は
**package 生成側で常に上書き**されるため、元 pack に置かれていても無視
される (誤って手書きしないこと)。

## verify-package の検査

1. zip として読める (`package_invalid_zip`)
2. すべての member が **zip slip safe** (`package_zip_slip`)
   - 絶対 path / drive letter / `..` 含みを拒否
3. `extension.yaml` / `package_manifest.json` / `checksums.sha256` 必須
4. `package_manifest.json` parse + `package_format` 値域
5. `executable_code: true` を error (`package_executable_code_true`)
6. zip 内 file の sha256 と `checksums.sha256` 行を照合
   (`package_checksum_mismatch`)
7. `package_manifest.files[*].sha256` と実 file を照合
   (`package_manifest_sha_mismatch`)
8. tmp 展開後 `validate_extension_file()` を再実行

## Normal vs Strict mode

`package` / `verify-package` / `validate extension` の挙動を 1 表で。

| 項目 | normal (default) | `--strict` |
|------|------------------|------------|
| 想定用途 | ローカル開発 / 動作確認 | **CI / registry 掲載 / release 前検査** |
| `empty_contents` | warning | **error** (`strict_empty_contents`) |
| `registry_entries_format` | warning | **error** (`strict_registry_entries_format`) |
| 参照 instrument `support_level=draft` | warning | **error** (`strict_support_level_draft`) |
| `support_level=verified` で `validation_evidence` 空 | (許容) | **error** (`strict_verified_requires_evidence`) |
| `registry_entries[*]` の必須 field 欠落 | warning | **error** (`strict_registry_entry_missing_<field>`) |
| `registry_entries[*].path` が pack 外 | (load_overlay で error) | **error** (`strict_registry_entry_path_outside_pack`) |
| `registry_entries[*].support_level` と instrument metadata の不一致 | warning | **error** (`strict_registry_entry_support_level_mismatch`) |
| pack に `README.md` 無し | warning (`missing_pack_readme`) | **error** (`strict_missing_pack_readme`) |
| `extension_extra_file` (install 後) | warning | **error** (`extension check --strict`) |

> 経験則: 開発中は normal でこまめに検証し、PR / tag 前に `--strict`
> を 1 度通す。CI でも `--strict` を fail gate にするのが推奨。

## strict mode

`package --strict` で:

- `support_level=verified` で `validation_evidence` 空 → error
  (`strict_verified_requires_evidence`)
- `registry_entries[*]` の必須 field / pack 内 path / support_level 一致
  (v1.4.1 の strict_registry_entry_* 系)
- pack に `README.md` が無い → error
  (`strict_missing_pack_readme`)

通常 (`--strict` 無し) では README 無しは warning
(`missing_pack_readme`) のみ。

## 出力例 (JSON)

```json
{
  "package": {
    "status": "ok",
    "extension_id": "tectos.mock.basic",
    "version": "0.1.0",
    "package_path": "dist/tectos.mock.basic-0.1.0.visa-mcp-ext.zip",
    "package_sha256": "...",
    "file_count": 8,
    "errors": [],
    "warnings": [],
    "manifest": { ... }
  }
}
```

## v1.5 で対応しない (v1.6+ 候補)

- ~~**zip からの install**~~ ── **v1.6 で対応済み** (下記参照)
- remote URL / git からの install
- registry pull CLI
- signature / trust store / 公開鍵検証
- automatic update
- Python plugin / entry_points discovery

順序:

```
v1.5: package 作成 / 検証
v1.6: local zip install (本リリース)     ← v1.6 で完了
v1.7+: remote registry / signature (慎重に判断)
```

## v1.6: zip からの install

v1.6 では `visa-mcp extension install` が `.zip` (主に
`.visa-mcp-ext.zip`) を受け付ける。詳細は
[`extension_install.md`](extension_install.md#v16-zip-からの-install)
参照。

```bash
visa-mcp extension package ./mypack/extension.yaml --output dist/
visa-mcp extension install dist/tectos.mock.basic-0.1.0.visa-mcp-ext.zip
```

zip install は内部で `verify_extension_package()` を必ず通すため、
checksum mismatch / zip slip / `executable_code=true` は install 段階
で弾かれる。

## 関連 docs

- [`extension_publishing_checklist.md`](extension_publishing_checklist.md) ── 配布前チェックリスト
- [`extension_integrity.md`](extension_integrity.md)
- [`extension_install.md`](extension_install.md)
- [`extension_registry_overlay.md`](extension_registry_overlay.md)
- [`error_taxonomy.md`](error_taxonomy.md)
