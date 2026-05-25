# Extension Publishing Checklist (v1.5)

definition pack を **配布物として公開する前**に通すべきチェック一覧。
将来の remote registry / PR review でも同じ基準を使う。

## 1. Manifest

- [ ] `extension_id` が **reverse-DNS style** (例: `tectos.mock.basic`)
- [ ] `version` が **SemVer** (例: `0.1.0` / `1.2.3-rc1`)
- [ ] `type: definition_pack`
- [ ] `stability.executable_code: false`
- [ ] `stability.support_level` が `verified` / `tested` /
      `experimental` / `draft` のいずれか
- [ ] `visa_mcp_compatibility` が指定されている (例: `">=1.5,<2.0"`)
- [ ] `author` / `homepage` / `license` がある

## 2. Validation

- [ ] `visa-mcp validate extension <extension.yaml>` が通る (normal)
- [ ] `visa-mcp validate extension <extension.yaml> --strict` が通る
- [ ] 含まれる instrument YAML が
      `visa-mcp validate instrument` を全て通る

## 3. Instrument 定義の品質

- [ ] 各 instrument の `metadata.support_level` が draft より上
- [ ] `support_level=verified` の instrument は
      `metadata.validation_evidence` を持つ
  - `tested_by` / `tested_at` / `interface` / `firmware` / `tested_items`
- [ ] `write` 系 command に `verify` がある (read-back 確認)
- [ ] 出力系 instrument は `safe_shutdown` を持つ
- [ ] `state_query` がある (get_state / describe / wait_for_condition 用)

## 4. Registry entries (もし含めるなら)

- [ ] `id` / `path` / `vendor` / `model` / `category` / `support_level`
      がすべて埋まっている
- [ ] `path` が pack 内 instrument YAML を指している (pack 外 NG)
- [ ] registry の `support_level` と instrument 側 metadata が一致
- [ ] built-in registry / 他 pack と id 衝突しない
      (`visa-mcp extension validate-installed` で確認可能)

## 5. Package 生成

- [ ] `visa-mcp extension package <extension.yaml> --strict` が通る
- [ ] 生成物が
      `dist/<extension_id>-<version>.visa-mcp-ext.zip`
- [ ] `package_manifest.json` の `executable_code: false`
- [ ] `checksums.sha256` が全 file を含む

## 6. Package 検証

- [ ] `visa-mcp extension verify-package <zip>` が通る
- [ ] checksum mismatch が無い
- [ ] zip slip / 絶対 path 違反が無い

## 7. Documentation

- [ ] pack 内に **`README.md`** がある
  - 何の pack か
  - 含まれる機器定義一覧
  - support_level の根拠
  - 動作確認した interface / firmware
  - 既知の制約 / 注意事項
- [ ] CHANGELOG (任意だが推奨)

## 8. Install 互換性

- [ ] 他 instance で
      `visa-mcp extension install <pack>/extension.yaml` が通る
      (zip install は v1.6+)
- [ ] install 後 `visa-mcp extension check <id>` が `ok`
- [ ] install 後 `visa-mcp extension validate-installed` で
      overlay duplicate が出ない

## 9. License / contribution metadata

- [ ] `license` が manifest に記載
- [ ] LICENSE ファイル (任意だが registry 掲載時は必須を推奨)
- [ ] vendor manual / datasheet 由来情報の引用元を明記

## 10. CI / Release

- [ ] CI で `--strict` validation + `verify-package` を実行
- [ ] tag 付与前に full integrity / verify-package を完走

---

## チェック自動化スクリプト例

```bash
#!/usr/bin/env bash
set -euo pipefail
pack=$1
visa-mcp validate extension "$pack/extension.yaml" --strict --json
visa-mcp extension package "$pack/extension.yaml" --strict --json
zip="$pack/dist/$(basename "$pack")-*.visa-mcp-ext.zip"
visa-mcp extension verify-package $zip --json
```

## 関連 docs

- [`extension_packaging.md`](extension_packaging.md)
- [`extension_install.md`](extension_install.md)
- [`extension_integrity.md`](extension_integrity.md)
- [`registry_contribution.md`](registry_contribution.md)
- [`definition_packs.md`](definition_packs.md)
