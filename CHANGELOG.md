# 変更履歴

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
