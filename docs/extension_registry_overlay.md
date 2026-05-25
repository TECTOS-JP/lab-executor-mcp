# Extension Registry Overlay (v1.3, experimental)

`visa-mcp` の effective registry は、**built-in registry + installed
definition packs の `registry_entries`** の合成として扱う。これを
**overlay registry** と呼ぶ。

```text
effective registry  =  built-in registry  +  installed pack 1 の registry_entries
                                          +  installed pack 2 の registry_entries
                                          ...
```

## Source の区別

各 overlay entry は **どこ由来か**を `source` に持つ:

| `source.kind` | 意味 |
|---------------|------|
| `builtin` | `<repo>/registry/INDEX.yaml` の entry |
| `extension` | installed pack の `contents.registry_entries[*]` |

`extension` 由来 entry には `extension_id` / `extension_version` を併記。
将来 `describe_instrument` 等で「この機器定義はどの pack 由来か」を AI
エージェントに示すために使う (v1.4+ 候補)。

## 衝突ルール (v1.3)

**重要**: AI エージェント向けに、暗黙の override は危険なので避ける。

| 衝突 | v1.3 挙動 |
|------|----------|
| built-in id と extension id の衝突 | **error** (`overlay_registry_duplicate_id`) |
| extension 同士の id 衝突 | **error** (同上) |
| 明示的 override | **v1.3 では未対応** (v1.4+ 候補) |

衝突を検出するには:

```bash
visa-mcp extension validate-installed --json
```

返却 (一部):

```json
{
  "overlay_registry": {
    "status": "error",
    "entries": [...],
    "errors": [
      {
        "error_class": "validation",
        "message": "overlay registry に duplicate id='mock_psu': ...",
        "details": {
          "sub_class": "overlay_registry_duplicate_id",
          "id": "mock_psu",
          "sources": [
            {"kind": "builtin"},
            {"kind": "extension", "extension_id": "tectos.mock.basic",
             "extension_version": "0.1.0"}
          ]
        }
      }
    ],
    "builtin_count": N,
    "extension_count": M
  }
}
```

## 解決方法

衝突が出た場合:

1. **builtin と衝突する場合**: extension 側で `registry_entries` の
   `id` を変更 (例: `mock_psu` → `tectos_mock_psu`) して repack / 再 install
2. **extension 同士の衝突**: どちらかを uninstall または id 変更

## API 利用 (Python)

```python
from visa_mcp.extension_install import load_overlay_registry

rep = load_overlay_registry(builtin_index_path="registry/INDEX.yaml")
print(f"status: {rep.status}")
print(f"builtin: {sum(1 for e in rep.entries if e.source.get('kind')=='builtin')}")
print(f"extension: {sum(1 for e in rep.entries if e.source.get('kind')=='extension')}")
for e in rep.errors:
    print(e)
```

## v1.4+ 候補

- 明示的 `override` (extension が builtin / 他 extension を上書きする宣言)
- registry pull CLI (remote)
- source metadata を `describe_instrument` の MCP response に追加
- per-pack の disable / priority 設定

## 関連 docs

- [`extension_install.md`](extension_install.md)
- [`extension_policy.md`](extension_policy.md)
- [`registry.md`](registry.md) — built-in registry 仕様
