# lab-executor-mcp

[![CI](https://github.com/TECTOS-JP/lab-executor-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/TECTOS-JP/lab-executor-mcp/actions/workflows/ci.yml)

AI agent 向けの、backend に依存しない**実験実行 runtime**です。
v2.0 で `visa-mcp` から分離されました。

> **⚠ VISA backend を使う場合の名称変更 (v2.37.0)**
> VISA backend は **`lab-visa-mcp`** へ改名されました (import 名も
> `lab_visa_mcp`)。**旧配布名 `visa-mcp` は PyPI から削除済みで、
> `pip install visa-mcp` は失敗します。**
>
> ```bash
> pip install lab-visa-mcp     # 旧 visa-mcp
> ```
>
> v1.x からの移行は **[`docs/v2_migration.md`](docs/v2_migration.md)** を
> 参照してください。

> **改行コード / raw 表示に関する注意** (v2.5.1 強化):
>
> 一部のサードパーティ製 viewer や LLM ベースの fetcher は、
> `raw.githubusercontent.com` を読み取る際、リポジトリのファイルを
> 「1 line」/「collapsed」と報告することがあります。これは
> **viewer 側の表示上の問題**であり、リポジトリの不具合ではありません。
> リポジトリではすべてのテキストを LF で保存しています。
>
> **正確な情報は [`RELEASE_VERIFICATION.md`](RELEASE_VERIFICATION.md) にあります。**
> これは tag 作成時に生成される manifest で、各重要ファイルの正確な
> `bytes` / `LF` / `CR` / `BOM` 数を記載しています。誰でも次を実行して
> 確認できます。
>
> ```bash
> git clone --branch v2.5.1 https://github.com/TECTOS-JP/lab-executor-mcp.git
> cd lab-executor-mcp
> python scripts/release_verification.py --check
> # expect: "OK" + exit 0  (CR=0, LF>=10, no BOM for every critical file)
> ```
>
> viewer の表示が manifest と一致しない場合、誤っているのは viewer 側です。
> CI でも次のテストによってこれを強制しています。
> `tests/test_v200_split.py::test_critical_files_are_multiline_and_lf_only`.

## 提供するもの

- DSL (`ExperimentPlan`, `dsl_version=0.8`) + 検証 + dry-run
- Job 管理 / state machine / scheduler / barrier
- Observation API (`timeline` / `live_view` / `summary`)
- benchmark 実行 (MockBackend, PyVISA 不要)
- Definition pack ecosystem (`extension init/install/check/package/...`)
- Instrument 作成支援 (`instrument scaffold/promote-check/review-report`)
- export / bundle (決定論的な再現性) — `export_experiment_bundle`
- MCP tool surface: Stable 43 + Experimental 7 = 50 (v1.0 から不変)

MCP API の安定性ポリシー (Stable / Experimental tool の区分、deprecation 方針)
は [`docs/v1_stability_policy.md`](docs/v1_stability_policy.md) を参照。

## インストール

```bash
pip install lab-executor-mcp
```

PyVISA は **必須ではありません**。実機 backend が必要な場合は、使う通信方式の
backend をインストールすると `lab-executor-mcp` も自動的に入ります。

```bash
pip install lab-visa-mcp     # VISA (GPIB / USB / TCPIP / ASRL)
pip install lab-modbus-mcp   # Modbus RTU / TCP
pip install lab-ble-mcp      # BLE 環境センサー (読み取り専用)
pip install lab-nidaq-mcp    # NI-DAQmx
```

## Extension path の動作 (v2.4)

v2.4 では、インストール済み extension pack の**二重 path 読み取り**を導入する一方、
**既定の書き込み先は変更せず**、従来の `~/.visa-mcp/extensions/` を維持します。
両方の path に重複する `extension_id` があっても**自動解決しません**。
warning として報告し、`--strict` では error として報告します。

| 項目 | v2.4 の動作 |
|---|---|
| 読み取り (catalog / check) | `~/.lab-executor/extensions/` **と** `~/.visa-mcp/extensions/` |
| 既定の書き込み先 (install) | `~/.visa-mcp/extensions/` (変更なし) |
| `extension_id` の重複 | 既定では warning、`--strict` では error |
| 自動的な優先順位 | **なし** — 重複を暗黙に解決することはありません |
| policy id | `duplicate_policy = report_conflict_no_implicit_precedence` |

既定の install 先は v2.5+ で再評価します。v2.4 は**報告段階**であり、
両方の path を認識しますが、暗黙に一方を選ぶことはありません。
resolver の状態は次のコマンドで確認できます。

```bash
lab-executor extension paths --json
```

v2.5 では `lab-executor extension migration-plan` (plan のみで file は変更しない)
と `resolve_extension_by_id()` API (extension_id が重複すると
`ExtensionResolveError` を送出) を追加します。完全な移行 guide と roadmap
(v2.6 copy-plan → v2.7 controlled apply → v2.8 default switch) は
[`docs/extension_path_migration.md`](docs/extension_path_migration.md)
を参照してください。

## server の起動方法 (v2.1+)

| 用途 | コマンド | PyVISA | backend |
|---|---|---|---|
| Mock / dry-run / benchmark / validation | `lab-executor serve --backend mock` | 不要 | `MockBackend` |
| 実機 (PyVISA 経由) | `visa-mcp serve` | 必要 | `PyVisaBackend` (visa-mcp 同梱) |

### クイック例

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

### 終了コードの policy (v2.1.1)

| subcommand | exit 0 | exit 1 | exit 2 |
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

### 注意

- `lab-executor serve` は **`--backend mock` が必須** (引数なしは
  exit 2 で `visa-mcp serve` への案内)
- 他 backend (REST / replay / plugin) は v2.2+ で検討
- runtime 内部の `JobManager` は v2.1 時点で `visa=` 引数名を受け取る
  (v1.x からの互換維持)。v2.2+ で `backend=` への rename を検討予定

## v2.3 の CLI 対応状況

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

### `--dry-run` の動作 (v2.3)

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

## v2.0 での分離

- v1.x までは `visa-mcp` 1 リポジトリで提供されていた
- v2.0 で **backend (visa-mcp) と runtime (lab-executor-mcp) を分離**
- MCP tool / DSL schema / extension pack 形式は完全互換
- 旧 `from visa_mcp.extension import ...` は v2.0 で
  DeprecationWarning 付きで動作

詳細: `docs/v2_migration.md`

## ライセンス

MIT
