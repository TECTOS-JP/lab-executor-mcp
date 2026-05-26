# 変更履歴

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
