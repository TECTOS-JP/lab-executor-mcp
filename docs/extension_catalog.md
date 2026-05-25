# Extension Catalog / Discovery (v1.6, experimental)

合言葉: **「package できる」 → 「どの pack を使うべきか判断できる」**

v1.5 までで pack の作成・配布・整合性確認は揃った。v1.6 では、
**install 前 / install 後の pack を選定・比較・採用判断する**ための
catalog metadata と CLI を整える。

> v1.6 では **MCP tool 追加ゼロ**、CLI のみ。
> remote registry / pull / signature にはまだ進まない。

## CLI

```bash
# installed pack を catalog 形式で一覧
visa-mcp extension catalog
visa-mcp extension catalog --installed --json

# 指定 directory 配下の .visa-mcp-ext.zip を catalog 形式で一覧
visa-mcp extension catalog --packages dist/
visa-mcp extension catalog --packages dist/ --json

# zip package を install せずに中身を読む (採用判断用)
visa-mcp extension inspect-package dist/foo.visa-mcp-ext.zip
visa-mcp extension inspect-package dist/foo.visa-mcp-ext.zip --json
```

## `list` / `inspect` / `catalog` / `inspect-package` の役割分担

| CLI | 対象 | 用途 |
|-----|------|------|
| `extension list` | installed lockfile | install 状態の最小一覧 |
| `extension inspect <id>` | installed pack 1 件 | 軽量詳細 + integrity 概要 |
| `extension check <id>` | installed pack 1 件 | **完全な sha256 drift 検査** |
| `extension catalog` | installed / dist/ 全件 | **選定 / 比較**用 metadata 一覧 |
| `extension inspect-package <zip>` | zip 1 件 | **install せず**に中身を読む |
| `extension verify-package <zip>` | zip 1 件 | **完全整合性検査** (checksum / zip slip 等) |

## extension.yaml の `catalog` field (v1.6 新規、任意)

```yaml
extension_id: tectos.mock.basic
name: Basic Mock Definition Pack
version: 0.1.0
type: definition_pack
stability:
  support_level: tested
  executable_code: false
contents:
  instruments: [instruments/mock_psu.yaml]

# v1.6 新規 (すべて任意)
catalog:
  summary: "Mock PSU/DMM/temperature meter pack for visa-mcp benchmarks."
  description: >
    Provides mock instrument definitions and benchmark tasks for local
    testing. No real hardware I/O.
  authors:
    - { name: "TECTOS" }
  license: "MIT"
  homepage: "https://github.com/TECTOS-JP/visa-mcp"
  tags:
    - mock
    - benchmark
    - power-supply
  categories:
    - instrument-definitions
    - benchmarks
  target_users:
    - developers
    - benchmark-authors
  safety_notes:
    - "No real hardware I/O. Mock/testing use only."
```

| Field | 型 | 用途 |
|-------|----|------|
| `summary` | str | 1 行紹介。CLI / catalog で必ず表示。strict で空 → error |
| `description` | str | 詳細紹介 (複数行可) |
| `authors` | list[dict] | `[{name, email?, url?}, ...]` |
| `license` | str | SPDX 識別子推奨 (`MIT` / `Apache-2.0` / ...)。strict で空 → error |
| `homepage` | str | URL |
| `tags` | list[str] | 検索 / 分類用キーワード |
| `categories` | list[str] | `instrument-definitions` / `benchmarks` / `templates` 等 |
| `target_users` | list[str] | `developers` / `researchers` / `qa-engineers` 等 |
| `safety_notes` | list[str] | 安全上の注意 (実機操作 / 高電圧 / 化学薬品 等) |

v1.6 では `catalog` field を **必須化しない** (後方互換)。
ただし `extension package --strict` で `summary` / `license` が空のとき
は error。

## support_level_summary

pack 内 instrument の `metadata.support_level` を集計した値。
`catalog` / `inspect-package` 出力に含まれる。

```json
{
  "support_level_summary": {
    "verified": 1,
    "tested": 2,
    "experimental": 0,
    "draft": 0
  }
}
```

外部 contributor pack の品質を 1 目で見るための指標。

### top-level `author` vs `catalog.authors` (v1.6.1 docs 追記)

| Field | 位置 | 型 | 役割 |
|-------|------|----|------|
| `author` | root | str | legacy / 単純 metadata。1 人想定 |
| `catalog.authors` | `catalog.` | list[dict] | discovery / 表示用。複数 author や `{name, email, url}` 等の構造化情報向け |

v1.6 以降、新規 pack では **`catalog.authors` を推奨**。root `author`
は後方互換のため残し、両方が指定された場合は **`catalog.authors` を優先
表示**する (`extension catalog` 出力上の選定指標)。

## quality_signals (score ではなく signals)

`catalog` / `inspect-package` は **数値 score を返さない**。代わりに
**boolean / count の構造化シグナル**を返す。

```json
{
  "quality_signals": {
    "has_readme": true,
    "has_catalog_summary": true,
    "has_catalog_license": true,
    "has_validation_evidence": true,
    "verified_instruments": 1,
    "tested_instruments": 2,
    "experimental_instruments": 0,
    "draft_instruments": 0,
    "package_verified": null,
    "package_verification_status": "not_checked",
    "strict_validation_passed": null,
    "strict_validation_status": "not_checked"
  }
}
```

#### `package_verified` / `*_status` の意味 (v1.6.1)

`null` を見たときの意味を明示するため、文字列 status を併記している:

| `package_verified` | `package_verification_status` |
|--------------------|-------------------------------|
| `true` | `"verified"` |
| `false` | `"failed"` |
| `null` | `"not_checked"` (inspect/catalog では未検査) |

完全な検査は `extension verify-package` で別途実行する。`inspect-package`
/ `catalog` は **軽量読み取り**であり、checksum 検証はしない。
同様に `strict_validation_passed` も `strict_validation_status` を併記。

### なぜ score 化しないか

- 評価基準がまだ未成熟
- 実機確認の証拠が多様 (firmware / interface / 操作項目で意味が変わる)
- **AI エージェントが数値を過信しやすい**
- 単一 score は「なぜそうなったか」を隠す

signals は **各次元独立**に出すことで、AI エージェントも人間も「何が
足りないか」を直接読み取れる。

## `installed_from` (v1.6 新規、`.install_meta.json`)

install 時に install 元を構造化記録。`extension catalog --installed`
で source.installed_from として表示される。

### directory install (extension.yaml 直接)

```json
{
  "installed_from": {
    "kind": "directory",
    "source_path": "/path/to/extension.yaml"
  }
}
```

### package install (.visa-mcp-ext.zip)

```json
{
  "installed_from": {
    "kind": "package",
    "package_path": "/path/to/foo.visa-mcp-ext.zip",
    "package_sha256": "...",
    "package_format_version": "1.0"
  }
}
```

後の audit / bundle / replay で「この pack はどの配布物から入れたか」
を辿る根拠になる。

## v1.6 で対応しない (v1.7+ 候補)

- remote registry / pull CLI
- `visa-mcp extension install https://...`
- signature / trust store
- automatic update
- quality **score** 化 (signals に留める)
- Python plugin / backend plugin

## 関連 docs

- [`extension_packaging.md`](extension_packaging.md)
- [`extension_install.md`](extension_install.md)
- [`extension_integrity.md`](extension_integrity.md)
- [`extension_registry_overlay.md`](extension_registry_overlay.md)
- [`definition_packs.md`](definition_packs.md)
- [`error_taxonomy.md`](error_taxonomy.md)
