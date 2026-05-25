"""
v1.0.1: Stable / Experimental ツール分類の **単一 source of truth**

docs / README / tests / release note のズレを防ぐため、ここに 1 箇所だけ
ツール名を列挙し、他はここから参照する。
"""
from __future__ import annotations
from typing import Final


# ============================================================
# Stable tools (v1.x 互換保証)
# ============================================================

STABLE_TOOLS: Final[dict[str, list[str]]] = {
    "Core / instrument": [
        "list_resources",
        "identify_instrument",
        "identify_all_instruments",
        "list_identified_instruments",
        "bind_definition",
        "list_available_definitions",
        "list_commands",
        "get_instrument_info",
        "list_safety_constraints",
        "validate_operation",
        "reload_definitions",
        "describe_instrument",
        "get_state",
        "get_last_measurement",
    ],
    "Recipe / Job": [
        "execute_named_command",
        "list_recipes",
        "execute_recipe",
        "start_recipe_job",
        "start_wait_job",
        "get_job_status",
        "get_job_result",
        "list_jobs",
        "cancel_job",
    ],
    "Group / Map": [
        "list_groups",
        "list_experiment_units",
        "start_group_query_job",
        "start_map_recipe_job",
    ],
    "DSL": [
        "validate_experiment_plan",
        "dry_run_plan",
        "start_experiment_job",
        "save_experiment_template",
        "list_experiment_templates",
        "get_experiment_template",
    ],
    "Observation": [
        "get_experiment_timeline",
        "get_job_live_view",
        "get_job_summary",
    ],
    "Monitor": [
        "start_monitor",
        "stop_monitor",
        "get_monitor_data",
        "prune_monitor_data",
    ],
    "Results / Export": [
        "get_experiment_results",
        "export_experiment_results",
    ],
    "Ingestion": [
        "extract_pdf_commands",
    ],
}


# ============================================================
# Experimental tools (v1.x 内で変更可)
# ============================================================

EXPERIMENTAL_TOOLS: Final[dict[str, list[str]]] = {
    "Template execution": [
        "start_experiment_job_from_template",
    ],
    "Resume": [
        "resume_job",
    ],
    "Audit / Locks": [
        "query_audit",
        "list_locks",
    ],
    "Bundle export": [
        "export_experiment_bundle",
    ],
    "Bundle inspection (v1.1)": [
        "validate_experiment_bundle",
        "inspect_experiment_bundle",
    ],
}


# ============================================================
# Raw (env-gated, off by default)
# ============================================================

RAW_TOOLS: Final[list[str]] = [
    "unsafe_send_command",
    "unsafe_query_instrument",
]


# ============================================================
# Helpers
# ============================================================


def flatten(tools_map: dict[str, list[str]]) -> list[str]:
    out: list[str] = []
    for v in tools_map.values():
        out.extend(v)
    return out


def stable_tool_names() -> list[str]:
    return flatten(STABLE_TOOLS)


def experimental_tool_names() -> list[str]:
    return flatten(EXPERIMENTAL_TOOLS)


def all_documented_tool_names() -> list[str]:
    """Stable + Experimental (raw は env-gated なので含めない)"""
    return stable_tool_names() + experimental_tool_names()


def stable_count() -> int:
    return len(stable_tool_names())


def experimental_count() -> int:
    return len(experimental_tool_names())


def total_documented_count() -> int:
    """README / release note の "N 個" 表記用 (raw 除外)"""
    return stable_count() + experimental_count()


def category_of(tool_name: str) -> tuple[str, str] | None:
    """tool_name の (status, category) を返す。raw は無視。
    Returns:
        ("stable", "Core / instrument") 等。未登録なら None。
    """
    for cat, names in STABLE_TOOLS.items():
        if tool_name in names:
            return ("stable", cat)
    for cat, names in EXPERIMENTAL_TOOLS.items():
        if tool_name in names:
            return ("experimental", cat)
    return None
