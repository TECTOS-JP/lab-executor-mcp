"""
v0.8.3: experiment template override helper

保存済み ExperimentPlan template を **限定された override** で実行するための
pure helper。LLM が template + 部分上書きで Plan を再生成しなくて済むようにする。

Override 仕様 (実装方針 #8):
  allowed top-level keys:
    - name              (新しい Job 名)
    - unit              (experiment_unit を差し替え)
    - bindings          (role → alias の dict, deep merge ではなく key 単位 override)
    - parameters        (plan.variables への shallow merge)
    - owner             (Job owner, plan に入らず Job metadata 側に伝播)

  disallowed (誤った再利用を防ぐ):
    - steps                 (構造を変えるなら template ではなく通常 Plan を使う)
    - dsl_version
    - safe_shutdown (steps 内なので自動的に拒否される)
    - parallel.branches の書き換え
"""
from __future__ import annotations
from copy import deepcopy
from typing import Any


ALLOWED_OVERRIDE_KEYS = ("name", "unit", "bindings", "parameters", "owner")
DISALLOWED_OVERRIDE_KEYS = (
    "steps", "dsl_version", "description", "variables",
)


class TemplateOverrideError(Exception):
    """override 内容が許可されていない場合に投げる"""

    def __init__(self, message: str, *, rejected_keys: list[str] | None = None):
        super().__init__(message)
        self.rejected_keys = rejected_keys or []


def apply_template_override(
    template_plan: dict[str, Any],
    override: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """template plan に override を適用し、(expanded_plan, applied_summary) を返す。

    Args:
        template_plan: 保存済み Plan JSON (dsl_version=0.8 等)
        override: { name?, unit?, bindings?, parameters?, owner? }

    Returns:
        (expanded_plan, applied_summary)
        - expanded_plan: ExperimentPlan 互換 dict (template の deep copy + override 適用)
        - applied_summary: { override_applied, override_keys, owner } のような記録用 dict

    Raises:
        TemplateOverrideError: override に許可外キーが含まれる場合
    """
    if not isinstance(template_plan, dict):
        raise TemplateOverrideError("template_plan は dict が必要です")

    override = override or {}
    if not isinstance(override, dict):
        raise TemplateOverrideError("override は dict が必要です")

    # 許可外キー検出
    rejected = [k for k in override if k not in ALLOWED_OVERRIDE_KEYS]
    if rejected:
        raise TemplateOverrideError(
            f"override に許可されていないキー: {rejected}。"
            f"許可: {list(ALLOWED_OVERRIDE_KEYS)}。"
            f"steps / dsl_version を変える場合は通常 Plan として "
            f"start_experiment_job を使ってください",
            rejected_keys=rejected,
        )

    # v0.8.3.1: template を deepcopy (steps / variables 共有を避けるため)。
    # template は保存済み再利用物。expanded を後段で加工しても元 template に
    # 副作用が及ばないことを保証する。
    expanded: dict[str, Any] = deepcopy(template_plan)
    applied_keys: list[str] = []

    if "name" in override and override["name"] is not None:
        expanded["name"] = override["name"]
        applied_keys.append("name")

    if "unit" in override and override["unit"] is not None:
        expanded["unit"] = override["unit"]
        applied_keys.append("unit")

    # bindings: template の bindings + override.bindings (override 優先)
    if "bindings" in override and override["bindings"] is not None:
        ov_b = override["bindings"]
        if not isinstance(ov_b, dict):
            raise TemplateOverrideError(
                "override.bindings は dict が必要です",
                rejected_keys=["bindings"],
            )
        merged = dict(template_plan.get("bindings") or {})
        for k, v in ov_b.items():
            merged[k] = v
            applied_keys.append(f"bindings.{k}")
        expanded["bindings"] = merged

    # parameters: plan.variables へ shallow merge
    if "parameters" in override and override["parameters"] is not None:
        ov_p = override["parameters"]
        if not isinstance(ov_p, dict):
            raise TemplateOverrideError(
                "override.parameters は dict が必要です",
                rejected_keys=["parameters"],
            )
        merged_v = dict(template_plan.get("variables") or {})
        for k, v in ov_p.items():
            merged_v[k] = v
            applied_keys.append(f"parameters.{k}")
        expanded["variables"] = merged_v

    owner_override = override.get("owner") if "owner" in override else None

    # disallowed キー検出も念のため (上で rejected に拾われるはず)
    bad = [k for k in expanded.keys() if k in DISALLOWED_OVERRIDE_KEYS
           and k in override]
    if bad:
        raise TemplateOverrideError(
            f"disallowed override keys detected: {bad}",
            rejected_keys=bad,
        )

    summary: dict[str, Any] = {
        "override_applied": bool(applied_keys),
        "override_keys": applied_keys,
    }
    if owner_override is not None:
        summary["owner"] = owner_override
    return expanded, summary
