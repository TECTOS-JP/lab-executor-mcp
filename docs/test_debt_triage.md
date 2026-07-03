# Test Debt Triage — フェーズ1 (v2.0 リポジトリ分割の移行負債整理)

## 背景

`python -m pytest` で **232 件が失敗**していた (1642 passed)。原因はすべて
v2.0 のリポジトリ分割 (visa-mcp → lab-executor-mcp + visa-mcp シム) に伴う
移行負債。ランタイムコード (`src/lab_executor/`) は一切変更せず、テスト /
docs / README / CI workflow / 設定側のみを修正して負債を解消した。

- 対象 HEAD: `v2.20.0`
- 最終結果: **0 failed / 1799 passed / 36 skipped**

## カテゴリ定義と対処方針

| カテゴリ | 内容 | 対処 |
|---|---|---|
| A | 旧パス参照 (`src/visa_mcp/...`)。資産は `src/lab_executor/...` に移動済み | テストのパスを新パスに修正して復活 |
| B | 分割前の歴史ガード (v0.x/v1.x 版数・旧 CHANGELOG など、本 repo では二度と成立しない) | テスト関数を削除 |
| C | 実機必須テスト (PMX 電源 / 横河計測器) | `LAB_EXECUTOR_HW_TESTS=1` が無ければ module 単位で skip |
| D | 現行ドキュメント / CI のガード | 検査が今も妥当なら docs/CI を直して生かす。妥当でなければ B 扱いで削除 |

## カテゴリ別 件数集計 (失敗 232 件の内訳)

| カテゴリ | 件数 | 主な対処 |
|---|---:|---|
| A: 旧パス参照 (LF/multiline/template/rollback/docstring 等) | 165 | パス・import 修正で復活 |
| B: 歴史ガード (version 22 + changelog 14 + docstring 1 + README table 2) | 39 | 関数削除 |
| C: 実機必須 | 10 | module skip 化 (実際は同ファイル 28 件すべて skip) |
| D: 現行 docs/CI ガード (CONTRIBUTING / README / CI workflow) | 16 | docs / ci.yml / README を修正して生かす |
| 要人間判断 (分割プロセス監査; 削除に自信が持てず skip 化) | 8 | skip + 本書に記録 |
| フレーク (タイミング依存、単体では pass) | 1 | 対処不要 |

> 注: 失敗リスト先頭行 `test_reject_if_busy_returns_failed` は "FAILED" 接頭辞の
> 無いヘッダ断片。単体・スイート内いずれでも pass するタイミング依存フレークで、
> 変更不要と判断した。

## 機械的変更 (全ファイル横断)

- **カテゴリA パス修正**: 22 個の review/version テストファイルで
  文字列 `src/visa_mcp/` を `src/lab_executor/` に一括置換
  (LF/multiline パラメトライズ list、template パス、segment 形式
  `"src" / "visa_mcp"` を含む)。移動先ファイルは全件実在を確認済み。
- `pyproject.toml`: `hardware` マーカーを登録 (`[tool.pytest.ini_options].markers`)。

## ファイル別 対処表

凡例: 対処 = 「パス修正(A)」「削除(B)」「docs/CI修正(D)」「skip化(C/要人間判断)」

| ファイル | 失敗数 | カテゴリ | 取った対処 | 備考 |
|---|---:|---|---|---|
| test_hardware_integration.py | 10 | C | module skip 化 | `pytestmark` に `skipif(LAB_EXECUTOR_HW_TESTS!=1)` を追加。ファイル内 28 件すべて skip |
| test_job_queue_interleave.py | 1 | (フレーク) | 対処なし | ヘッダ断片。タイミング依存で単体・スイート内とも pass |
| test_repo_format_guard.py | 1 | D | ci.yml 修正 + パス修正 | `lint` job を ci.yml に追加。SWEEP_PATTERNS / 例外 dict の `src/visa_mcp/`→`src/lab_executor/` |
| test_v0921_review.py | 4 | A | パス修正 | LF/multiline |
| test_v0931_review.py | 8 | A | パス修正 | LF/multiline |
| test_v101_review.py | 5 | A + B | パス修正 + 削除3 | 削除: `test_version_v1_0_1`, `test_readme_tool_count_matches_stability_module`, `test_readme_results_tools_not_marked_experimental` |
| test_v11.py | 5 | A + B | パス修正 + 削除1 | 削除: `test_version_is_v1_1_0` |
| test_v110_separation_audit.py | 2 | 要人間判断 | skip化2 | skip: `test_module_ownership_manifest_complete`, `test_split_manifest_paths_exist` |
| test_v111_review.py | 9 | A + B | パス修正 + 削除1 | 削除: `test_version_v1_1_1` |
| test_v111_separation_refactor.py | 7 | B + 要人間判断 | 削除1 + skip6 | 削除: `test_version_is_1_11_0`。skip: `test_split_rehearsal_generates_candidate`, `test_split_rehearsal_cli_runs`, `test_split_rehearsal_verify_candidate`, `test_raw_visa_doc_exists`, `test_v111_new_files_covered_by_format_guard`, `test_v111_new_files_are_multiline` |
| test_v121_review.py | 3 | A + B | パス修正 + 削除1 | 削除: `test_version_v1_2_1` |
| test_v12_extension.py | 3 | A + B | パス修正 + 削除1 | 削除: `test_version_v1_2_0` |
| test_v131_review.py | 9 | A + B | パス修正 + 削除3 | 削除: `test_changelog_has_v131_entry`, `test_cli_module_docstring_v13`, `test_version_v1_3_1`。`test_cli_module_docstring_v13` は runtime cli.py が v2.12 docstring のため v1.3 要求は成立せず (ランタイム変更不可) |
| test_v13_extension_install.py | 3 | A + B | パス修正 + 削除1 | 削除: `test_version_v1_3_0` |
| test_v141_review.py | 13 | A + B | パス修正 + 削除2 | 削除: `test_changelog_has_v141_entry`, `test_version_v1_4_1`。`test_instrument_def_comment_matches_strict_error_behavior` は models パス修正で復活 |
| test_v14_extension_integrity.py | 10 | A + B | パス修正 + 削除2 | 削除: `test_changelog_has_v140_entry`, `test_version_v1_4_0` |
| test_v151_review.py | 12 | A + B | パス修正 + 削除2 | 削除: `test_changelog_has_v151_entry`, `test_version_v1_5_1` |
| test_v15_extension_packaging.py | 6 | A + B | パス修正 + 削除2 | 削除: `test_changelog_has_v150_entry`, `test_version_v1_5_0` |
| test_v161_review.py | 12 | A + B | パス修正 + 削除2 | 削除: `test_changelog_has_v161_entry`, `test_version_v1_6_1` |
| test_v16_catalog.py | 7 | A + B | パス修正 + 削除1 | 削除: `test_changelog_has_v160_catalog_entry` |
| test_v16_zip_install.py | 8 | A + B | パス修正 + 削除2 | 削除: `test_changelog_has_v160_entry`, `test_version_v1_6_0` |
| test_v171_review.py | 17 | A + B + D | パス修正 + 削除2 + CONTRIBUTING復活 | 削除: `test_changelog_has_v171_entry`, `test_version_v1_7_1`。`test_contributing_has_data_handling_policy` は CONTRIBUTING.md 復活で pass |
| test_v17_authoring.py | 9 | A + B + D | パス修正 + 削除2 + CONTRIBUTING復活 | 削除: `test_changelog_has_v170_entry`, `test_version_v1_7_0`。`test_contributing_mentions_definition_pack_workflow` は復活で pass |
| test_v181_review.py | 30 | A + B + D | パス修正 + 削除2 + import修正 + CONTRIBUTING復活 | 削除: `test_changelog_has_v181_entry`, `test_version_v1_8_1`。rollback 4 件は monkeypatch 対象を `visa_mcp.extension`→`lab_executor.extension` に修正して復活 |
| test_v18_instrument_authoring.py | 9 | A + B + D | パス修正 + 削除2 + CONTRIBUTING復活 | 削除: `test_changelog_has_v180_entry`, `test_version_v1_8_0`。`test_contributing_mentions_instrument_workflow` は復活で pass |
| test_v191_review.py | 14 | A + B + D | パス修正 + 削除2 + ci.yml修正 | 削除: `test_changelog_has_v191_entry`, `test_version_v1_9_1`。CI 2 件 (`test_ci_workflow_has_pyvisa_not_installed_job`, `test_ci_workflow_has_lint_job_running_repo_guard`) は ci.yml 修正で復活 |
| test_v19_instrument_quality.py | 12 | A + B | パス修正 + 削除2 | 削除: `test_changelog_has_v190_entry`, `test_version_v1_9_0` |
| test_v1_stability.py | 4 | B + D | 削除2 + README修正 | 削除: `test_pyproject_version_is_v1`, `test_visa_mcp_package_version_is_v1`。README に `export_experiment_bundle` / `v1_stability_policy` 参照を追記して 2 件復活 |

## 削除したテスト関数 (カテゴリB) — 全列挙

### version ガード (22 件)
- test_v101_review.py: `test_version_v1_0_1`
- test_v11.py: `test_version_is_v1_1_0`
- test_v111_review.py: `test_version_v1_1_1`
- test_v111_separation_refactor.py: `test_version_is_1_11_0`
- test_v121_review.py: `test_version_v1_2_1`
- test_v12_extension.py: `test_version_v1_2_0`
- test_v131_review.py: `test_version_v1_3_1`
- test_v13_extension_install.py: `test_version_v1_3_0`
- test_v141_review.py: `test_version_v1_4_1`
- test_v14_extension_integrity.py: `test_version_v1_4_0`
- test_v151_review.py: `test_version_v1_5_1`
- test_v15_extension_packaging.py: `test_version_v1_5_0`
- test_v161_review.py: `test_version_v1_6_1`
- test_v16_zip_install.py: `test_version_v1_6_0`
- test_v171_review.py: `test_version_v1_7_1`
- test_v17_authoring.py: `test_version_v1_7_0`
- test_v181_review.py: `test_version_v1_8_1`
- test_v18_instrument_authoring.py: `test_version_v1_8_0`
- test_v191_review.py: `test_version_v1_9_1`
- test_v19_instrument_quality.py: `test_version_v1_9_0`
- test_v1_stability.py: `test_visa_mcp_package_version_is_v1`
- test_v1_stability.py: `test_pyproject_version_is_v1`

### CHANGELOG ガード (14 件) — 分割時に CHANGELOG.md がリセットされ、v0.x/v1.x エントリは二度と成立しない
- test_v131_review.py: `test_changelog_has_v131_entry`
- test_v141_review.py: `test_changelog_has_v141_entry`
- test_v14_extension_integrity.py: `test_changelog_has_v140_entry`
- test_v151_review.py: `test_changelog_has_v151_entry`
- test_v15_extension_packaging.py: `test_changelog_has_v150_entry`
- test_v161_review.py: `test_changelog_has_v161_entry`
- test_v16_catalog.py: `test_changelog_has_v160_catalog_entry`
- test_v16_zip_install.py: `test_changelog_has_v160_entry`
- test_v171_review.py: `test_changelog_has_v171_entry`
- test_v17_authoring.py: `test_changelog_has_v170_entry`
- test_v181_review.py: `test_changelog_has_v181_entry`
- test_v18_instrument_authoring.py: `test_changelog_has_v180_entry`
- test_v191_review.py: `test_changelog_has_v191_entry`
- test_v19_instrument_quality.py: `test_changelog_has_v190_entry`

### その他の歴史ガード (3 件)
- test_v131_review.py: `test_cli_module_docstring_v13` — cli.py docstring に `v1.3` 表記を要求。runtime cli.py は現在 v2.12 系 docstring。ランタイム変更不可のため成立せず削除。
- test_v101_review.py: `test_readme_tool_count_matches_stability_module` — README の "MCP ツール（N 個" カウント表記を要求。v2.0 で README が刷新されカウント表記が撤去された (ツール一覧の SoT は `docs/v1_stability_policy.md` へ移行、`test_all_stable_tools_appear_in_v1_stability_policy` で担保)。
- test_v101_review.py: `test_readme_results_tools_not_marked_experimental` — README の per-tool markdown table 行を要求。同上で v2.0 README には該当 table が存在しない。

## docs / CI / README 側の修正 (カテゴリD)

- **CONTRIBUTING.md を復活** (新規作成)。分割時に本 repo から消えていたが、隣接
  `visa-mcp/CONTRIBUTING.md` の内容が lab-executor でも妥当なため復活。CLI 名を
  `visa-mcp` → `lab-executor` に読み替え済み。以下 3 テストが復活:
  `test_contributing_has_data_handling_policy` (v171),
  `test_contributing_mentions_definition_pack_workflow` (v17_authoring),
  `test_contributing_mentions_instrument_workflow` (v18)。
  加えて各 v17/v18 系の `CONTRIBUTING.md` を対象にした LF/multiline パラメトライズも復活。
- **README.md に追記**: "What it provides" に `export_experiment_bundle` を明記し、
  `docs/v1_stability_policy.md` への参照リンクを追加。
  `test_readme_lists_export_experiment_bundle`, `test_readme_links_to_v1_stability_policy` が復活。
  (`export_experiment_bundle` は `lab_executor/stability.py` に実在する現行ツール)
- **.github/workflows/ci.yml を修正**:
  - `lint` job を追加し `pytest tests/test_repo_format_guard.py` を実行
    (repo-wide format guard を CI の SoT に)。
  - `pyvisa-not-installed` job に `python -m lab_executor.dev.dependency_report`
    step と `tests/test_separation_boundary.py` の実行を追加。
  - 復活したテスト: `test_ci_workflow_includes_pyvisa_not_installed_job` (repo_format_guard),
    `test_ci_workflow_has_pyvisa_not_installed_job` / `test_ci_workflow_has_lint_job_running_repo_guard` (v191)。

## skip 化 (C: 実機)

- **test_hardware_integration.py**: module 冒頭 `pytestmark` に
  `pytest.mark.skipif(os.environ.get("LAB_EXECUTOR_HW_TESTS") != "1", ...)` を追加。
  実機 (Kikusui PMX 電源 / Yokogawa 計測器) が必要で、環境変数
  `LAB_EXECUTOR_HW_TESTS=1` を設定した場合のみ実行される。ファイル内 28 件が skip。
  `hardware` マーカーは `pyproject.toml` に登録済み。

## 要人間判断として残した項目 (skip 化 8 件)

分割プロセス *自体* を監査するテスト群。分割が完了した本 repo では前提
(src/visa_mcp ツリー・分割前 planning artifact) が失われており、検査が
現状と整合しない。削除してよいか確信が持てないため、**安全側に倒して
skip 化**し、ここに記録する。将来の判断が必要。

### test_v110_separation_audit.py (2 件) — 分割準備の監査
- `test_module_ownership_manifest_complete`
  — `docs/separation/module_ownership.yaml` が分割前の `visa_mcp.*` module 76 件を
    宣言しており、本 repo では ghost module 扱いになる。
- `test_split_manifest_paths_exist`
  — `docs/separation/split_manifest.yaml` が `src/visa_mcp/*.py` を列挙 (実在 7/47)。

判断ポイント: planning doc (`docs/separation/*.yaml`) を `lab_executor.*` に全面
書き換えして監査を生かすか、分割完了に伴い歴史的監査として削除するか。

### test_v111_separation_refactor.py (6 件) — split rehearsal (移行リハーサル) の監査
- `test_split_rehearsal_generates_candidate`
- `test_split_rehearsal_cli_runs`
- `test_split_rehearsal_verify_candidate`
  — `lab_executor.dev.split_rehearsal.generate_candidate` は `src/visa_mcp` ツリーを
    コピー対象にするが本 repo に存在せず `copied_count=0` になる。
- `test_raw_visa_doc_exists`
  — `docs/raw_visa.md` を要求。当該 doc は Raw VISA backend の解説で visa-mcp 側の資産
    (隣接 `visa-mcp/docs/raw_visa.md` に存在)。lab-executor には無い。
- `test_v111_new_files_covered_by_format_guard`
- `test_v111_new_files_are_multiline`
  — `src/visa_mcp/backends/pyvisa_backend.py` を参照。pyvisa 依存の当該ファイルは
    lab-executor には意図的に存在しない (visa-mcp 所有)。

判断ポイント: 移行リハーサル tool (`split_rehearsal`) と関連テストは分割完了で
役目を終えている。テスト群および `src/lab_executor/dev/split_rehearsal.py` を
削除するか、visa-mcp 側へ移すか。

## 最終テスト結果

```
1799 passed, 36 skipped, 0 failed
```

- skipped 36 = 実機 28 (test_hardware_integration.py) + 要人間判断 8
  (test_v110_separation_audit.py 2 + test_v111_separation_refactor.py 6)
