# lab-executor-mcp

[![CI](https://github.com/TECTOS-JP/lab-executor-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/TECTOS-JP/lab-executor-mcp/actions/workflows/ci.yml)

Backend-independent **experiment execution runtime** for AI agents.
Split from `visa-mcp` at v2.0.

> **⚠ Upgrading from visa-mcp v1.x?**
> **First read [`docs/v2_migration.md`](docs/v2_migration.md).**
> 実機 backend が必要な既存利用者は **`pip install --upgrade visa-mcp`** が
> 互換経路 (v2.0 では visa-mcp v2.0 release を待つ必要あり)。

> **Line-ending / raw display note** (v2.4.1 強化):
>
> Some third-party viewers and LLM-based fetchers occasionally
> report repository files as "1 line" / "collapsed" when reading
> `raw.githubusercontent.com`. This is a **viewer-side artifact**,
> not a repository defect. The repository stores all text as LF.
>
> **Ground truth lives in [`RELEASE_VERIFICATION.md`](RELEASE_VERIFICATION.md)**
> — a tag-time generated manifest listing each critical file's
> exact `bytes` / `LF` / `CR` / `BOM` counts. Any reviewer can run:
>
> ```bash
> git clone --branch v2.4.1 https://github.com/TECTOS-JP/lab-executor-mcp.git
> cd lab-executor-mcp
> python scripts/release_verification.py --check
> # expect: "OK" + exit 0  (CR=0, LF>=10, no BOM for every critical file)
> ```
>
> If your viewer disagrees with the manifest, your viewer is wrong.
> CI also enforces this via
> `tests/test_v200_split.py::test_critical_files_are_multiline_and_lf_only`.

## What it provides

- DSL (`ExperimentPlan`, `dsl_version=0.8`) + validator + dry-run
- Job manager / state machine / scheduler / barrier
- Observation API (`timeline` / `live_view` / `summary`)
- Benchmark runner (MockBackend, PyVISA 不要)
- Definition pack ecosystem (`extension init/install/check/package/...`)
- Instrument authoring (`instrument scaffold/promote-check/review-report`)
- Export / bundle (deterministic reproducibility)
- MCP tool surface: Stable 43 + Experimental 7 = 50 (v1.0 から不変)

## Install

```bash
pip install lab-executor-mcp
```

PyVISA は **必須ではない**。実機 backend が必要な場合は
`visa-mcp` を install すると自動的に `lab-executor-mcp` も入る。

```bash
pip install visa-mcp     # PyVISA backend + lab-executor-mcp runtime
```

## Extension path behavior (v2.4)

v2.4 introduces **dual-path read** for installed extension packs while
keeping the **write default unchanged** (legacy `~/.visa-mcp/extensions/`).
Duplicate `extension_id` across both paths is **not auto-resolved**:
it is reported as a warning (or error with `--strict`).

| Aspect | v2.4 behavior |
|---|---|
| Read (catalog / check) | `~/.lab-executor/extensions/` **and** `~/.visa-mcp/extensions/` |
| Write (install) default | `~/.visa-mcp/extensions/` (unchanged) |
| Duplicate `extension_id` | warning by default, error with `--strict` |
| Auto-precedence | **none** — duplicates are never silently resolved |
| Policy id | `duplicate_policy = report_conflict_no_implicit_precedence` |

The default install target will be re-evaluated in v2.5+. v2.4 is a
**reporting phase** — it sees both paths, but never picks one
implicitly. Inspect the resolver state with:

```bash
lab-executor extension paths --json
```

## How to start the server (v2.1+)

| 用途 | コマンド | PyVISA | backend |
|---|---|---|---|
| Mock / dry-run / benchmark / validation | `lab-executor serve --backend mock` | 不要 | `MockBackend` |
| 実機 (PyVISA 経由) | `visa-mcp serve` | 必要 | `PyVisaBackend` (visa-mcp 同梱) |

### Quick examples

```bash
# 1) Server を compose して tool 一覧を確認する (transport 起動なし)
lab-executor serve --backend mock --dry-run

# 2) 実 transport を起動 (Claude Code / MCP client から接続)
lab-executor serve --backend mock

# 3) Extension pack を検証
lab-executor validate extension my_pack/extension.yaml --strict
lab-executor extension doctor my_pack/

# 4) Package + verify
lab-executor extension package my_pack/ --output my_pack.visa-mcp-ext.zip
lab-executor extension verify-package my_pack.visa-mcp-ext.zip

# 5) Install + lifecycle (v2.3+)
lab-executor extension install my_pack.visa-mcp-ext.zip --dry-run
lab-executor extension install my_pack.visa-mcp-ext.zip
lab-executor extension catalog          # installed pack 一覧
lab-executor extension check            # checksum / manifest 検査
lab-executor extension paths            # install path resolver 状態
```

### Exit code policy (v2.1.1)

| Subcommand | exit 0 | exit 1 | exit 2 |
|---|---|---|---|
| `serve` | server 起動 / `--dry-run` 成功 | — | `--backend` 未指定 / 不正 |
| `validate {instrument,extension}` | errors == 0 | errors > 0 | usage error |
| `extension doctor` | `status == "ok"` | warnings / errors あり | usage error |
| `extension package` | package 成功 | 失敗 | usage error |
| `extension verify-package` | checksums OK | mismatch / 不正 | usage error |
| `extension init` | pack 生成成功 | 失敗 | usage error |
| `extension install` | install 成功 / `--dry-run` 成功 | verify 失敗 / 書き込み失敗 | usage error |
| `extension check` | OK (warning も exit 0) | error あり、または `--strict` で warning | usage error |
| `extension catalog` | 一覧出力 (空でも exit 0) | 内部 error | usage error |
| `extension paths` | 出力成功 | — | usage error |
| `instrument scaffold` | YAML 生成成功 | 失敗 | usage error |
| `instrument review-report` | markdown 出力成功 | file 不在等の error | usage error |
| `diagnose tool-surface` | `status == "ok"` または warning + `--strict` 無し | warning + `--strict` | usage error |

`doctor` は warning だけでも exit 1 になります (CI で fail させる
強い gate として設計)。warning を許容したい場合は `doctor` 出力を
`--json` で取り、`errors` のみを判定してください。

### Notes

- `lab-executor serve` は **`--backend mock` が必須** (引数なしは
  exit 2 で `visa-mcp serve` への案内)
- 他 backend (REST / replay / plugin) は v2.2+ で検討
- runtime 内部の `JobManager` は v2.1 時点で `visa=` 引数名を受け取る
  (v1.x からの互換維持)。v2.2+ で `backend=` への rename を検討予定

## CLI status in v2.3

v2.3.1 までに活性化された範囲:

**v2.1.0**:
- `lab-executor --version` / `--help`
- `lab-executor serve --backend mock` (MockBackend で MCP server 起動)
- `lab-executor serve --backend mock --dry-run`
- `lab-executor validate instrument <path>`
- `lab-executor validate extension <path>`
- `lab-executor extension doctor <pack_dir>`
- `lab-executor extension package <pack_dir>`
- `lab-executor extension verify-package <zip>`

**v2.2.0**:
- `lab-executor extension init <dir>` (pack scaffold 生成)
- `lab-executor instrument scaffold <category>`
- `lab-executor instrument promote-check <yaml>`
- `lab-executor instrument review-report <yaml>`
- `lab-executor diagnose tool-surface` (Stable 43 + Experimental 7 検査)

**v2.3.0**:
- `lab-executor extension install <zip>` (`--dry-run` / `--force` /
  `--skip-verify`)
- `lab-executor extension check` (`--extension-id` / `--strict`)
- `lab-executor extension catalog` (install 済 pack 一覧)
- `lab-executor extension paths` (path resolver 状態 / `--json`)

### `--skip-verify` の取り扱い (重要)

`extension install --skip-verify` は **test 用途専用**。信頼できない
配布物 (第三者 zip / 外部レジストリ pull) には **絶対に使わない**こと。
checksum 検証を skip するため、tampering 検出ができなくなる。

### `--dry-run` semantics (v2.3)

`extension install --dry-run` は v2.3 時点で **package verify のみ** を
実行する。install 済 `extension_id` の重複検査や install path への
書き込み権限確認は行わない (v2.4+ で検討)。

### v2.4+ で port 予定 / 検討中

- `extension uninstall` (`.install_meta.json` ベースの安全削除)
- `extension list-installed` (catalog のエイリアス整理)
- 他 backend (REST / replay / plugin) の `--backend` choice 追加
- `default_extensions_dir` の source of truth を `extension_paths
  .get_extension_paths().current_default` に統合 (現状は 2 系統存在)

実機 backend を起動する経路は引き続き **`visa-mcp serve`** (visa-mcp
v2.0+ を install)。

## v2.0 split

- v1.x までは `visa-mcp` 1 リポジトリで提供されていた
- v2.0 で **backend (visa-mcp) と runtime (lab-executor-mcp) を分離**
- MCP tool / DSL schema / extension pack 形式は完全互換
- 旧 `from visa_mcp.extension import ...` は v2.0 で
  DeprecationWarning 付きで動作

詳細: `docs/v2_migration.md`

## License

MIT
