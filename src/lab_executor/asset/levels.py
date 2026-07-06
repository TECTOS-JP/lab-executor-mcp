"""独立可用性レベル L0〜L5 判定の純関数群 (v2.25.0)

すべて **I/O しない**。checker が zip から読み出した dict / bytes を渡す。
各関数は ``{"ok": bool, "details": [...], "missing": [...]}`` 形式を返す。

判定基準は docs/asset_v01_plan.md / docs/experiment_asset_schema_v0.md に従う。
"""
from __future__ import annotations

from typing import Any


LEVEL_IDS = ("L0", "L1", "L2", "L3", "L4", "L5")


def _mk(ok: bool, *, details: list | None = None,
        missing: list | None = None) -> dict[str, Any]:
    return {
        "ok": bool(ok),
        "details": details or [],
        "missing": missing or [],
    }


def judge_l0(*, results_row_count: int) -> dict[str, Any]:
    """L0 — 生データのみ。results.jsonl または results.csv に 1 行以上。"""
    missing: list[str] = []
    if results_row_count <= 0:
        missing.append("results (>=1 row)")
    return _mk(
        results_row_count > 0,
        details=[f"results_row_count={results_row_count}"],
        missing=missing,
    )


def judge_l1(*, job_record: dict[str, Any]) -> dict[str, Any]:
    """L1 — 測定条件付属。recipe / parameters / created_at / resource_name。"""
    missing: list[str] = []
    jr = job_record or {}
    if not jr.get("recipe"):
        missing.append("job_record.recipe")
    if not jr.get("parameters"):
        missing.append("job_record.parameters")
    if not jr.get("created_at"):
        missing.append("job_record.created_at")
    if not jr.get("resource_name"):
        missing.append("job_record.resource_name")
    return _mk(not missing, missing=missing)


def judge_l2(
    *,
    has_instrument_def: bool,
    has_timeline: bool,
    conditions: dict[str, Any] | None,
) -> dict[str, Any]:
    """L2 — 装置・校正・環境が付属。

    - instrument/ に定義 YAML が存在
    - timeline.jsonl が存在
    - conditions.calibration / environment のキーが存在
      ("not_recorded" も可。**キー欠落は不可**)
    """
    missing: list[str] = []
    if not has_instrument_def:
        missing.append("instrument definition")
    if not has_timeline:
        missing.append("bundle/timeline.jsonl")
    cond = conditions or {}
    if "calibration" not in cond:
        missing.append("conditions.calibration")
    if "environment" not in cond:
        missing.append("conditions.environment")
    return _mk(not missing, missing=missing)


def judge_l3(
    *,
    checksums_ok: bool,
    raw_value_paired: bool,
    has_analysis: bool,
    schema_ok: bool,
) -> dict[str, Any]:
    """L3 — 第三者が再解析可能。

    - checksums 全一致
    - raw_response ↔ value_numeric の対応が少なくとも 1 系列で保持
    - analysis/ に 1 ファイル以上
    - asset.yaml スキーマ検証 pass
    """
    missing: list[str] = []
    if not checksums_ok:
        missing.append("checksums all match")
    if not raw_value_paired:
        missing.append("raw_response <-> value_numeric pairing")
    if not has_analysis:
        missing.append("analysis/ (>=1 file)")
    if not schema_ok:
        missing.append("asset.yaml schema valid")
    return _mk(not missing, missing=missing)


def judge_l4(
    *,
    has_requires: bool,
    capability_match: dict[str, Any] | None,
) -> dict[str, Any]:
    """L4 — 代替装置で追試可能。

    - recipe に requires: が存在
    - match_capabilities(requires, 同梱 instrument 定義) が satisfied
    """
    missing: list[str] = []
    details: list[str] = []
    if not has_requires:
        missing.append("recipe.requires")
    cm = capability_match or {}
    if has_requires and not cm.get("satisfied", False):
        if cm.get("missing_commands"):
            missing.append(
                "capability.missing_commands="
                f"{cm['missing_commands']}")
        if cm.get("range_violations"):
            missing.append(
                "capability.range_violations="
                f"{len(cm['range_violations'])}")
        if not cm.get("missing_commands") and not cm.get("range_violations"):
            missing.append("capability not satisfied")
    if cm.get("range_unknown"):
        details.append(f"range_unknown={cm['range_unknown']}")
    return _mk(has_requires and cm.get("satisfied", False),
               details=details, missing=missing)


def judge_l5(
    *,
    hazards: dict[str, Any] | None,
    expected_results: list | None,
    dry_run: dict[str, Any] | None,
    instrument_strict_ok: bool,
) -> dict[str, Any]:
    """L5 — AI エージェントが安全確認後に再実行可能。

    - hazards 明示 (none_declared=true か、上限値記入)
    - expected_results が 1 件以上
    - dry_run.ok == true
    - 同梱 instrument 定義が strict 検証 pass
    """
    missing: list[str] = []
    hz = hazards or {}
    hazards_declared = bool(hz) and (
        hz.get("none_declared") is True
        or hz.get("voltage_max") is not None
        or hz.get("temperature_max") is not None
        or bool(hz.get("chemicals"))
    )
    if not hazards_declared:
        missing.append("hazards (none_declared=true or limits)")
    if not expected_results:
        missing.append("expected_results (>=1)")
    dr = dry_run or {}
    if dr.get("ok") is not True:
        missing.append("dry_run.ok == true")
    if not instrument_strict_ok:
        missing.append("instrument strict validation (0 errors)")
    return _mk(not missing, missing=missing)


def summarize_verified_level(levels: dict[str, dict]) -> int:
    """L0..L5 の判定結果 dict から、累積的に達成できた最高レベルを返す。

    レベルは累積的 (下位を全て満たさないと上位に上がれない)。
    L0 すら満たさなければ -1 を返す。
    """
    verified = -1
    for i, lid in enumerate(LEVEL_IDS):
        entry = levels.get(lid) or {}
        if entry.get("ok"):
            verified = i
        else:
            break
    return verified
