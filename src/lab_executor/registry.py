"""
v0.9.2: instrument 定義 registry + lint + validation helpers (experimental)

`registry/INDEX.yaml` を読み、機器定義 YAML を schema validation + lint で
品質判定する。CLI (`visa-mcp validate ...`) のバックエンド。
"""
from __future__ import annotations
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from lab_executor.models.instrument_def import InstrumentDefinition

logger = logging.getLogger(__name__)


SUPPORT_LEVELS = ("verified", "tested", "experimental", "draft")

# v1.9: 出力系 instrument category (safe_shutdown 必須 / state 変更系
# command の verify 必須)
OUTPUT_CAPABLE_CATEGORIES = (
    "power_supply",
    "smu",
    "function_generator",
    "electronic_load",
    "temperature_controller",
    "heater",
    "actuator",
)

# v1.9: category alias (CLI / metadata 表記揺れの正規化)
CATEGORY_ALIASES = {
    "multimeter": "dmm",
    "digital_multimeter": "dmm",
    "psu": "power_supply",
    "function_gen": "function_generator",
    "fg": "function_generator",
    "eload": "electronic_load",
    "tc": "temperature_controller",
}


def normalize_category(category: str) -> str:
    """v1.9: category alias を正規化"""
    if not category:
        return ""
    return CATEGORY_ALIASES.get(category.lower(), category.lower())


# v1.9: state 値を変更する set 系 command の prefix (strict で verify 必須)
_STATE_CHANGING_PREFIXES = (
    "set_voltage", "set_current", "set_output",
    "set_temperature", "set_frequency", "set_amplitude",
    "set_range", "set_mode", "set_setpoint",
    "set_pressure", "set_flow", "set_speed",
)

# v1.9: TODO placeholder 検出
_MANUAL_REF_TODO_PATTERNS = (
    "todo", "tbd", "fixme",
    "url or document",  # scaffold が入れる placeholder の特徴文字列
)


def _manual_ref_contains_todo(value: Any) -> bool:
    """v1.9: manual_ref に TODO 系 placeholder が残っているか判定"""
    import json as _json
    if value is None:
        return False
    text = value if isinstance(value, str) else _json.dumps(
        value, ensure_ascii=False)
    text_lower = text.lower()
    return any(p in text_lower for p in _MANUAL_REF_TODO_PATTERNS)


def _is_state_changing_command(name: str) -> bool:
    n = name.lower()
    if any(n.startswith(p) for p in _STATE_CHANGING_PREFIXES):
        return True
    # 一般的な set_* (display 等の見た目変更は除外したいので
    # _STATE_CHANGING_PREFIXES でフィルタ済み)
    return False


# ============================================================
# Registry index
# ============================================================


@dataclass
class RegistryEntry:
    id: str
    vendor: str
    model: str
    category: str
    support_level: str
    path: str


@dataclass
class RegistryIndex:
    instruments: list[RegistryEntry] = field(default_factory=list)
    root: Path | None = None


def load_registry_index(index_path: str | Path) -> RegistryIndex:
    p = Path(index_path)
    if not p.exists():
        raise FileNotFoundError(p)
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    entries: list[RegistryEntry] = []
    for item in raw.get("instruments") or []:
        entries.append(RegistryEntry(
            id=item.get("id", ""),
            vendor=item.get("vendor", ""),
            model=item.get("model", ""),
            category=item.get("category", ""),
            support_level=item.get("support_level", "draft"),
            path=item.get("path", ""),
        ))
    return RegistryIndex(instruments=entries, root=p.parent)


# ============================================================
# Validation + lint
# ============================================================


@dataclass
class ValidationReport:
    """schema validation + lint の結果"""
    file: str
    schema: str = ""
    status: str = "ok"          # "ok" / "warning" / "error"
    errors: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "file": self.file,
            "schema": self.schema,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }


def _lint_deferred_ranges(defn: InstrumentDefinition, rep: "ValidationReport") -> None:
    """recipe の ${...} 実行時解決引数が範囲宣言を持つか検査 (SP-2 安全要件)。

    宣言 (ParameterDefinition.range または recipe.requires.ranges) が無い場合は
    error を追加する。文字列内埋め込み (${...} が arg 値全体を占めない) も
    未対応として error にする。

    v2.29.0 (SP-3): branch case / repeat body 内の command (raw dict) も
    再帰的に検査する (保存時に弾く安全要件はネストにも適用)。
    """
    from lab_executor.utils.seq_expression import SeqExpressionError, parse_deferred

    def _check_command(
        recipe_name: str, ranges_decl: dict,
        command: str, args: dict, path: str,
    ) -> None:
        for arg, value in (args or {}).items():
            try:
                expr = parse_deferred(value)
            except SeqExpressionError as e:
                rep.errors.append({
                    "error_class": "recipe_deferred_arg_unsupported",
                    "message": (
                        f"recipe '{recipe_name}' {path} {command}.{arg}: {e}"
                    ),
                    "field_path": f"recipes.{recipe_name}.{path}",
                })
                continue
            if expr is None:
                continue
            # 範囲宣言の存在を確認
            declared = f"{command}.{arg}" in ranges_decl
            if not declared:
                cmd_def = defn.commands.get(command)
                if cmd_def is not None:
                    for pdef in cmd_def.parameters:
                        if pdef.name == arg and pdef.range:
                            declared = True
                            break
            if not declared:
                rep.errors.append({
                    "error_class": "recipe_deferred_arg_missing_range",
                    "message": (
                        f"recipe '{recipe_name}' {path}: 実行時解決引数 "
                        f"'{command}.{arg}' に範囲宣言がありません。"
                        "ParameterDefinition.range または requires.ranges "
                        "が必須です (安全要件 §6)"
                    ),
                    "field_path": f"recipes.{recipe_name}.{path}",
                    "details": {"command": command, "arg": arg},
                })

    def _walk_raw_steps(
        recipe_name: str, ranges_decl: dict, steps: list, path: str,
    ) -> None:
        """branch/repeat 内の raw dict step を再帰検査する。"""
        for i, raw in enumerate(steps or []):
            if not isinstance(raw, dict):
                continue
            spath = f"{path}[{i}]"
            if raw.get("command"):
                _check_command(
                    recipe_name, ranges_decl,
                    raw["command"], raw.get("args") or {}, spath,
                )
            for case in (raw.get("branch") or []):
                if isinstance(case, dict):
                    _walk_raw_steps(
                        recipe_name, ranges_decl,
                        case.get("steps") or [], f"{spath}.branch.steps",
                    )
            rp = raw.get("repeat")
            if isinstance(rp, dict):
                _walk_raw_steps(
                    recipe_name, ranges_decl,
                    rp.get("steps") or [], f"{spath}.repeat.steps",
                )

    for recipe_name, recipe in (defn.recipes or {}).items():
        ranges_decl = (recipe.requires.ranges if recipe.requires else {}) or {}
        for i, rs in enumerate(recipe.steps):
            st = rs.step_type
            if st == "command":
                _check_command(
                    recipe_name, ranges_decl,
                    rs.command or "", rs.args or {}, f"steps[{i}]",
                )
            elif st == "branch":
                for case in (rs.branch or []):
                    if isinstance(case, dict):
                        _walk_raw_steps(
                            recipe_name, ranges_decl,
                            case.get("steps") or [],
                            f"steps[{i}].branch.steps",
                        )
            elif st == "repeat":
                _walk_raw_steps(
                    recipe_name, ranges_decl,
                    (rs.repeat or {}).get("steps") or [],
                    f"steps[{i}].repeat.steps",
                )


def validate_instrument_file(
    path: str | Path,
    *,
    strict: bool = False,
) -> ValidationReport:
    """機器定義 YAML を Pydantic schema + lint で検証

    v1.9: `strict=True` で以下が error 化される (registry 掲載 / CI /
    release 前検査向け):

    - `metadata.manual_ref` に TODO / TBD / FIXME placeholder 残存
      → `instrument_manual_ref_todo`
    - 出力系 instrument (category が `OUTPUT_CAPABLE_CATEGORIES` に
      含まれる) で `safe_shutdown` 未定義
      → `instrument_missing_safe_shutdown`
    - 出力系 instrument で `safety.ratings` 未定義 / 空
      → `instrument_missing_safety_ratings`
    - state 変更系 set command (`set_voltage` / `set_current` /
      `set_output` / `set_temperature` 等) に `verify` 未定義
      → `instrument_missing_verify`
    - `support_level=verified` だが `metadata.validation_evidence` 空
      → `instrument_verified_missing_evidence`

    通常 (`strict=False`) では既存 warning のみ。
    """
    rep = ValidationReport(file=str(path), schema="instrument.schema.json")
    p = Path(path)
    if not p.exists():
        rep.status = "error"
        rep.errors.append({
            "error_class": "not_found",
            "message": f"file not found: {p}",
        })
        return rep
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        defn = InstrumentDefinition(**raw)
    except Exception as e:
        rep.status = "error"
        rep.errors.append({
            "error_class": "schema_invalid",
            "message": str(e),
        })
        return rep

    # ---- lint warnings ----
    md = defn.metadata
    if md.support_level not in SUPPORT_LEVELS:
        rep.warnings.append({
            "warning_class": "invalid_support_level",
            "message": (
                f"metadata.support_level={md.support_level!r} は "
                f"{list(SUPPORT_LEVELS)} のいずれか推奨"
            ),
            "field_path": "metadata.support_level",
        })
    if md.support_level == "draft":
        rep.warnings.append({
            "warning_class": "support_level_draft",
            "message": (
                "support_level=draft はAIエージェント Plan 生成時に注意推奨。"
                "tested / verified への昇格を検討してください"
            ),
            "field_path": "metadata.support_level",
        })
    if not md.manufacturer or not md.model:
        rep.warnings.append({
            "warning_class": "missing_metadata",
            "message": "metadata.manufacturer / model が必須",
            "field_path": "metadata",
        })

    # safe_shutdown
    has_shutdown = (defn.safe_shutdown is not None
                    and len(defn.safe_shutdown) > 0)
    has_write = any(
        getattr(c, "type", "") == "write" for c in defn.commands.values()
    )
    if has_write and not has_shutdown:
        rep.warnings.append({
            "warning_class": "missing_safe_shutdown",
            "message": (
                "出力系 command (write 型) があるが safe_shutdown が未定義。"
                "cancel / interrupted / resume 前の安全停止に必須"
            ),
            "field_path": "safe_shutdown",
        })

    # state_query
    if not defn.state_query:
        rep.warnings.append({
            "warning_class": "missing_state_query",
            "message": (
                "state_query が空。get_state / describe_instrument / "
                "wait_for_condition の AI 利用に必要"
            ),
            "field_path": "state_query",
        })

    # verify: set 系 write command に verify が無いものを警告
    for name, cmd in defn.commands.items():
        if getattr(cmd, "type", "") == "write" and name.startswith("set"):
            if cmd.verify is None:
                rep.warnings.append({
                    "warning_class": "missing_verify",
                    "message": (
                        f"command '{name}' (write/set 系) に verify が未定義。"
                        f"write 後 read-back の自動確認を推奨"
                    ),
                    "field_path": f"commands.{name}.verify",
                })

    # ============================================================
    # v2.28.0 (SP-2): recipe の ${...} 実行時解決引数に範囲宣言必須
    # ============================================================
    # 安全要件 (sequence_processing_spec §6): 実行時解決される引数は
    # 事前レビューで値が読めないため、範囲 (ParameterDefinition.range または
    # recipe.requires.ranges) で縛る以外に審査できない。宣言が無い場合は error
    # とし、保存・実行を弾く。
    _lint_deferred_ranges(defn, rep)

    # ============================================================
    # v1.9: strict mode 追加検査
    # ============================================================
    if strict:
        normalized_category = normalize_category(md.category or "")
        is_output_capable = normalized_category in OUTPUT_CAPABLE_CATEGORIES

        # 1. manual_ref TODO 残存
        if _manual_ref_contains_todo(md.manual_ref):
            rep.errors.append({
                "error_class": "instrument_manual_ref_todo",
                "message": (
                    f"(strict) metadata.manual_ref に TODO 系 placeholder "
                    f"が残存: {md.manual_ref!r}"
                ),
                "field_path": "metadata.manual_ref",
                "details": {"manual_ref": md.manual_ref},
            })

        # 2. 出力系 instrument の safe_shutdown 必須
        if is_output_capable and not has_shutdown:
            rep.errors.append({
                "error_class": "instrument_missing_safe_shutdown",
                "message": (
                    f"(strict) 出力系 instrument (category="
                    f"{normalized_category!r}) で safe_shutdown が未定義"
                ),
                "field_path": "safe_shutdown",
                "details": {
                    "category": normalized_category,
                    "category_raw": md.category,
                },
            })

        # 3. 出力系 instrument の safety.ratings 必須
        if is_output_capable:
            ratings = (defn.safety.ratings or {}) if defn.safety else {}
            if not ratings:
                rep.errors.append({
                    "error_class": "instrument_missing_safety_ratings",
                    "message": (
                        f"(strict) 出力系 instrument (category="
                        f"{normalized_category!r}) で safety.ratings が"
                        "未定義"
                    ),
                    "field_path": "safety.ratings",
                    "details": {"category": normalized_category},
                })

        # 4. state 変更 set 系 command の verify 必須
        # (display / beep / reset / clear 等の auxiliary write は除外)
        for name, cmd in defn.commands.items():
            if getattr(cmd, "type", "") != "write":
                continue
            if not _is_state_changing_command(name):
                continue
            if cmd.verify is None:
                # readback 候補を推測 (set_voltage → query_voltage)
                guess = None
                if name.startswith("set_"):
                    suffix = name[4:]
                    for cand in (f"query_{suffix}", f"measure_{suffix}"):
                        if cand in defn.commands:
                            guess = cand
                            break
                rep.errors.append({
                    "error_class": "instrument_missing_verify",
                    "message": (
                        f"(strict) state 変更 command '{name}' に verify "
                        "が未定義。read-back tolerance を設定するか、"
                        "device が readback 不可なら明示的に除外"
                    ),
                    "field_path": f"commands.{name}.verify",
                    "details": {
                        "command": name,
                        "suggested_readback_command": guess,
                    },
                })

        # 5. support_level=verified なのに validation_evidence 空
        if md.support_level == "verified":
            ev = md.validation_evidence or {}
            if not ev:
                rep.errors.append({
                    "error_class": "instrument_verified_missing_evidence",
                    "message": (
                        "(strict) support_level=verified だが "
                        "metadata.validation_evidence が空"
                    ),
                    "field_path": "metadata.validation_evidence",
                })

    if not rep.errors and rep.warnings:
        rep.status = "warning"
    elif not rep.errors and not rep.warnings:
        rep.status = "ok"
    else:
        rep.status = "error"
    return rep


REQUIRED_INDEX_FIELDS = ("id", "vendor", "model", "category", "path")


def _validate_index_entries(
    index_path: Path, root: Path,
) -> ValidationReport:
    """v0.9.2.1: INDEX.yaml 自体の品質検証 (必須項目 / 重複 ID / path 存在)"""
    rep = ValidationReport(file=str(index_path),
                            schema="registry_index (v0.9.2.1)")
    try:
        raw = yaml.safe_load(index_path.read_text(encoding="utf-8")) or {}
    except Exception as e:
        rep.status = "error"
        rep.errors.append({
            "error_class": "schema_invalid",
            "message": f"INDEX.yaml parse failed: {e}",
        })
        return rep

    entries = raw.get("instruments") or []
    if not isinstance(entries, list):
        rep.status = "error"
        rep.errors.append({
            "error_class": "schema_invalid",
            "message": "INDEX.yaml.instruments は list が必要",
        })
        return rep

    seen_ids: set[str] = set()
    for i, item in enumerate(entries):
        if not isinstance(item, dict):
            rep.errors.append({
                "error_class": "schema_invalid",
                "message": f"entry[{i}] は dict が必要",
            })
            continue
        # 必須項目
        for fld in REQUIRED_INDEX_FIELDS:
            if not item.get(fld):
                rep.errors.append({
                    "error_class": "registry_entry_missing_field",
                    "message": (
                        f"entry[{i}] (id={item.get('id', '?')!r}) に "
                        f"'{fld}' が無い"
                    ),
                    "field_path": f"instruments[{i}].{fld}",
                })
        # 重複 id
        eid = item.get("id")
        if eid:
            if eid in seen_ids:
                rep.errors.append({
                    "error_class": "registry_duplicate_id",
                    "message": f"id={eid!r} が重複",
                    "field_path": f"instruments[{i}].id",
                })
            seen_ids.add(eid)
        # path 存在 + registry 配下
        p = item.get("path")
        if p:
            full = (root / p) if not Path(p).is_absolute() else Path(p)
            if not full.exists():
                rep.errors.append({
                    "error_class": "registry_entry_path_not_found",
                    "message": f"entry[{i}] path={p!r} が存在しません",
                    "field_path": f"instruments[{i}].path",
                })
            else:
                try:
                    full.resolve().relative_to(root.resolve())
                except ValueError:
                    rep.warnings.append({
                        "warning_class": "registry_path_outside_registry",
                        "message": (
                            f"entry[{i}] path={p!r} が registry/ 配下では"
                            f"ありません"
                        ),
                        "field_path": f"instruments[{i}].path",
                    })
        # support_level の語彙チェック (registry 側では error にする)
        sl = item.get("support_level")
        if sl is not None and sl not in SUPPORT_LEVELS:
            rep.errors.append({
                "error_class": "invalid_support_level",
                "message": (
                    f"entry[{i}] support_level={sl!r} は "
                    f"{list(SUPPORT_LEVELS)} のいずれかが必要"
                ),
                "field_path": f"instruments[{i}].support_level",
            })

    if rep.errors:
        rep.status = "error"
    elif rep.warnings:
        rep.status = "warning"
    return rep


def validate_registry(index_path: str | Path) -> list[ValidationReport]:
    """INDEX.yaml に列挙された全機器定義を検証 (INDEX 自身 + 各 entry)"""
    idx = load_registry_index(index_path)
    out: list[ValidationReport] = []
    root = idx.root or Path(index_path).parent
    # v0.9.2.1: INDEX 自体の lint (必須項目 / 重複 / path / support_level)
    index_rep = _validate_index_entries(Path(index_path), root)
    out.append(index_rep)
    for e in idx.instruments:
        defn_path = root / e.path if not Path(e.path).is_absolute() else Path(e.path)
        rep = validate_instrument_file(defn_path)
        rep.file = str(defn_path)
        # entry-level チェック
        if not defn_path.exists():
            # validate_instrument_file が既に error にする
            pass
        else:
            # 申告 support_level と YAML 内 support_level の一致
            try:
                raw = yaml.safe_load(defn_path.read_text(encoding="utf-8")) or {}
                yaml_sl = (raw.get("metadata") or {}).get("support_level")
                if yaml_sl and yaml_sl != e.support_level:
                    rep.warnings.append({
                        "warning_class": "registry_support_level_mismatch",
                        "message": (
                            f"INDEX.yaml の support_level={e.support_level!r} と "
                            f"機器定義 YAML の support_level={yaml_sl!r} が不一致"
                        ),
                        "field_path": "metadata.support_level",
                    })
                    if rep.status == "ok":
                        rep.status = "warning"
            except Exception:
                pass
        out.append(rep)
    return out


# ============================================================
# Plan / Benchmark task validation (CLI 用)
# ============================================================


def validate_plan_file(path: str | Path) -> ValidationReport:
    """DSL plan JSON / YAML をパースして ExperimentPlan として validate

    **重要 (v0.9.2.1 docs 明記)**: ここでの validation は **Pydantic schema 確認**
    のみ。system_config / instrument 定義の参照解決 / safety / resource は
    **行わない**。これらを含む完全 validation は MCP tool
    `validate_experiment_plan` (`validate_and_compile` 経由) を使ってください。
    """
    rep = ValidationReport(file=str(path), schema="dsl.schema.json")
    p = Path(path)
    if not p.exists():
        rep.status = "error"
        rep.errors.append({"error_class": "not_found",
                            "message": f"file not found: {p}"})
        return rep
    try:
        text = p.read_text(encoding="utf-8")
        if p.suffix in (".yaml", ".yml"):
            data = yaml.safe_load(text)
        else:
            import json
            data = json.loads(text)
    except Exception as e:
        rep.status = "error"
        rep.errors.append({"error_class": "schema_invalid",
                            "message": f"parse failed: {e}"})
        return rep
    try:
        from lab_executor.dsl.schema import ExperimentPlan
        ExperimentPlan(**data)
    except Exception as e:
        rep.status = "error"
        rep.errors.append({"error_class": "schema_invalid",
                            "message": str(e)})
    return rep


def validate_benchmark_task_file(path: str | Path) -> ValidationReport:
    rep = ValidationReport(file=str(path), schema="benchmark_task.schema.json")
    p = Path(path)
    if not p.exists():
        rep.status = "error"
        rep.errors.append({"error_class": "not_found",
                            "message": f"file not found: {p}"})
        return rep
    try:
        from lab_executor.testing.benchmark_task import load_benchmark_task
        load_benchmark_task(p)
    except Exception as e:
        rep.status = "error"
        rep.errors.append({"error_class": "schema_invalid",
                            "message": str(e)})
    return rep


def validate_system_config_file(path: str | Path) -> ValidationReport:
    rep = ValidationReport(file=str(path), schema="system_config.schema.json")
    p = Path(path)
    if not p.exists():
        rep.status = "error"
        rep.errors.append({"error_class": "not_found",
                            "message": f"file not found: {p}"})
        return rep
    try:
        from lab_executor.system_config import SystemConfig
        SystemConfig.from_yaml(p)
    except Exception as e:
        rep.status = "error"
        rep.errors.append({"error_class": "schema_invalid",
                            "message": str(e)})
    return rep
