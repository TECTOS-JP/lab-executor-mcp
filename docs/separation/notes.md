# Separation Notes (v1.9 → v1.10 更新, draft for v2.0 split)

## v1.10 で追加した成果物

- `docs/separation/module_ownership.yaml` (機械可読 manifest, 73
  module 全分類、unclassified=0)
- `docs/separation/split_manifest.yaml` (v2.0 で `git filter-repo`
  する path の網羅リスト + rc1_gates)
- `docs/separation/dependency_graph.md` (自動生成、
  `python -m visa_mcp.dev.ownership_check --graph-md ...` で再生成)
- `src/visa_mcp/dev/ownership_check.py` (CI gate: 未分類 module 検出
  + 新規 lab→visa top-level violation 検出 + `KNOWN_V111_TO_RESOLVE`
  registry。v1.11 で InstrumentBackend Protocol 経由化で解消する 10
  件の既知 violation を tracking)
- `visa-mcp instrument review-report` CLI (PR 用 markdown report、
  strict validate + promote-check を集約)

v1.11 では `KNOWN_V111_TO_RESOLVE` を 0 件まで削減することが gate。

## v1.10.1 patch (raw 改行 + statistics 自動検証 + 補強)

- repo format guard (`tests/test_repo_format_guard.py`) の sweep 対象に
  `docs/**/*.yaml` / `docs/**/*.yml` を追加 (今回 separation manifest
  が format guard の対象外だった件への P0 修正)
- `tests/test_v110_separation_audit.py` に以下を追加:
  - `test_dependency_graph_md_committed_multiline`:
    `docs/separation/dependency_graph.md` が 20 行以上 + 必須 section
    を含むこと
  - `test_module_ownership_statistics_match`: statistics block と
    実 owner count が一致 (P1-4)
  - `test_module_ownership_yaml_not_collapsed`: separation YAML 2 件が
    30 行以上 + CR 無し
- `test_version_is_1_10_x`: patch release で fail させないよう
  `1.10.*` 許容
- `split_manifest_paths_exist` のしきい値方針を inline コメント化
  (v1.10 70% / v1.11 100% / rc1 split_files 解消)

## v1.11.0 実績 (達成)

- **`KNOWN_V111_TO_RESOLVE = 0`** 達成 (TYPE_CHECKING + 関数内 lazy
  import への移行で 10 件すべて解消)
- `src/visa_mcp/backends/`: `InstrumentBackend` Protocol +
  `PyVisaBackend` (visa-mcp owner) + `MockBackend` (lab-executor owner)
  を実体化
- `src/visa_mcp/dev/split_rehearsal.py`: tmp directory に
  `lab_executor_candidate/` を生成 + import rewrite dry-run
- `docs/raw_visa.md`: visa-mcp v2.0 用 draft 公開
- 全 test: 1533 passing (Stable 43 / Experimental 7 / 計 50 不変)

## v1.11 当初目標 (旧 preview, 参照用)

**`KNOWN_V111_TO_RESOLVE` を 0 件にする** ことが v1.11 完了条件。残った
ままだと v2.0 で lab-executor 側が visa-mcp に top-level 依存したまま
分離される。

v1.11 P0:

```
1. InstrumentBackend Protocol 実体化
2. PyVisaBackend / MockBackend adapter
3. session_manager direct import → InstrumentBackend 経由
4. visa_manager direct import → PyVisaBackend 経由
5. testing/mock_instruments の lazy exception を共通 error へ置換
6. KNOWN_V111_TO_RESOLVE = empty set
7. step_executor.py / tools/commands.py / registry.py / server.py /
   cli.py の split 準備
8. src/lab_executor_candidate/ split rehearsal
```

## visa-mcp 側 docs 戦略 (v1.11 / rc1 で確定する TODO)

`split_manifest.yaml` で `docs/raw_visa.md` を v2.0 新規作成予定と
書いたが、**v1.11 か rc1 で drafts を作っておく**と分離後の visa-mcp
が空洞に見えない。想定構成:

```
lab-executor-mcp docs:
- DSL / Job / Observation / Benchmark / Definition pack /
  Instrument authoring / extension ecosystem / v2 migration guide

visa-mcp docs:
- PyVISA backend setup
- list_resources / raw VISA / environment flags
  (VISA_MCP_ALLOW_RAW など)
- migration shim (visa_mcp → lab_executor)
- how to use visa-mcp as backend provider for lab-executor-mcp
```

## instrument_authoring.py 分割の v1.11 検討

`review_report_instrument()` 追加で `instrument_authoring.py` の責務
が増えたため、v1.11 split rehearsal の段階で:

```
lab_executor/instrument_authoring/
  scaffold.py
  add_to_extension.py
  promote.py
  review.py
```

への分割を検討する (v1.10.1 では不要)。



合言葉: **「v2.0 でリポジトリを分けるための、v1.9 から積む下準備
メモ」**

ロードマップ v6 の Phase A (v1.9〜v1.11) で固める情報を、まず軽い
形でここに集約する。v1.10 で `module_ownership.yaml` と
`split_manifest.yaml` の機械可読版に昇格する予定。

## 分離の最終ゴール (v2.0)

- `lab-executor-mcp` (新規 repo): runtime + DSL + Job + Observation
  + Benchmark + Export + Audit + **definition pack ecosystem 全部** +
  instrument 定義 schema / response_format parser
- `visa-mcp` (現 repo): PyVISA backend 実装 (`VisaManager` /
  `PyVisaBackend` adapter) + raw VISA + PDF extractor 残置検討

詳細はロードマップ v6 「v2.0 分離計画」section 参照。

## v1.9 boundary smoke tests の限界 (v1.9.1 追記 P1-5)

v1.9 で追加した `tests/test_separation_boundary.py` と
`python -m visa_mcp.dev.dependency_report` は、**module top-level の
import のみ**を検出する。

```
v1.9 boundary smoke tests detect import-time coupling only.
Function-level lazy imports are allowed temporarily.
v1.10 / v1.11 will reduce or document remaining lazy backend imports.
```

具体的に **v1.9 では検出しない** 依存:

- 関数 / メソッド / class 内の `from visa_mcp.visa_manager import ...`
  (例: `testing/mock_instruments.py` が VISA timeout error 互換を投げる
  ための遅延 import)
- `if TYPE_CHECKING:` ブロック内の type hint
- string ベースの `getattr(__import__("visa_mcp"), "visa_manager")` 等
  の動的 import (現状 0 件)

v1.10 / v1.11 で `module_ownership.yaml` を導入し、lazy import も
明示的に記録する予定。それまでは「これ以上 top-level 依存を追加しない」
ことが gate の意図。

## v1.9 で固定した境界 (boundary CI)

### Runtime 候補 module (PyVISA / visa_manager 非依存)

```
visa_mcp.dsl
visa_mcp.extension
visa_mcp.extension_packaging
visa_mcp.extension_install
visa_mcp.extension_catalog
visa_mcp.extension_authoring
visa_mcp.extension_integrity
visa_mcp.instrument_authoring
visa_mcp.observation
visa_mcp.testing
```

これらの **module top-level** で:
- `import pyvisa` / `from pyvisa import ...` → **禁止**
- `from visa_mcp.visa_manager import ...` / `import visa_mcp.visa_manager`
  → **禁止**

関数 / メソッド内の lazy import は許容 (例: `testing/mock_instruments.py`
は VISA timeout 互換 error を投げるために遅延 import している)。

CI で確認: `tests/test_separation_boundary.py` +
`python -m visa_mcp.dev.dependency_report` + GitHub Actions の
`pyvisa-not-installed` job。

### Backend layer (現状)

```
visa_mcp.visa_manager       ← PyVISA 透過 (将来 PyVisaBackend adapter)
visa_mcp.tools.discovery    ← PyVISA resource 列挙
visa_mcp.tools.pdf_extractor ← v6 では再分類検討 (authoring 寄り)
```

## v1.10 で詰める項目

- `docs/separation/module_ownership.yaml` を生成
- import dependency graph (`docs/separation/dependency_graph.md`)
- `split_manifest.yaml` (v2.0 で `git filter-repo` する path のリスト)
- `step_executor.py` / `tools/commands.py` の layer 内分割案
  (YAML 解釈側 vs PyVISA 透過側)

## v1.11 で詰める項目

- import violation を 0 件に
- `InstrumentBackend` Protocol 実体化 (`PyVisaBackend` /
  `MockBackend`)
- `src/lab_executor_candidate/` 仮 namespace で split rehearsal

## pyvisa CI 戦略 (v1.9.1 追記 P1)

```
v1.9: pyvisa is removed in CI after install to detect import-time
      coupling. `pip install -e .` may still pull pyvisa via extras;
      the `pyvisa-not-installed` job explicitly uninstalls it.
v2.0: lab-executor-mcp must NOT depend on pyvisa at install time.
      `pip install lab-executor-mcp` should succeed without pyvisa.
      visa-mcp keeps pyvisa as a required dependency.
```

## v1.10 向け registry.py 分割候補 TODO (P1-7)

`src/visa_mcp/registry.py` は v1.9 時点で以下の責務を 1 file に持って
いる:

- `RegistryIndex` / `load_registry_index`
- `validate_instrument_file` (+ strict 検査)
- `validate_plan_file` / `validate_system_config_file` /
  `validate_benchmark_task_file`
- `_validate_index_entries` (registry index lint)
- `OUTPUT_CAPABLE_CATEGORIES` / `CATEGORY_ALIASES` /
  `normalize_category`

v1.10 の `module_ownership.yaml` では **`registry.py: split`** として
扱い、v1.11 で以下のような分割を検討する:

```
registry/
  index.py                # RegistryIndex / load_registry_index
  instrument_validation.py  # validate_instrument_file (lint)
  strict_checks.py        # _is_state_changing_command 等 v1.9 strict
  plan_validation.py      # validate_plan_file
  benchmark_validation.py # validate_benchmark_task_file
  category_policy.py      # OUTPUT_CAPABLE_CATEGORIES / aliases
```

ただし v1.10 で必須ではない (まずは ownership manifest で印を付ける
だけで OK)。

## v2.0 への TODO メモ (v1.9 時点で書き留めておく)

- **PDF extractor**: 重い PDF 依存 (pypdf 等) は **optional extra** に
  する。`pip install lab-executor-mcp` ではなく
  `pip install lab-executor-mcp[pdf]` のような extras 設計を検討
- **tool registration boundary**:
  - `lab-executor-mcp`: MCP tool 定義 / schema / response envelope を所有
  - `visa-mcp`: `PyVisaBackend` を注入して従来互換 MCP server を起動
- **install path 段階移行**:
  - v2.0: `~/.visa-mcp/extensions/` を互換のため継続
  - v2.1: `~/.lab-executor/extensions/` 並走 / migration 案
  - v2.2: default 切替判断
- **`~/.visa-mcp/` 配下の audit / job DB** も同様の段階移行が必要
  (Phase C で別途検討)

## 参考

- ロードマップ v6: `~/.claude/plans/gpib-mcp-pure-ullman.md`
- `docs/naming_and_repository_strategy.md`
- `docs/backend_abstraction.md`
- `docs/v1_stability_policy.md`
