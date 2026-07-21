"""
v1.8: Instrument Definition Authoring

合言葉: 「**instrument YAML を作りやすくする**」

v1.7 で **空のディレクトリから pack の雛形** が作れるようになった。
v1.8 では pack の中身である **instrument YAML を category 別 template
から scaffold** できるようにし、さらに pack に登録する
`add-instrument` まで提供する。

提供 API:

- `scaffold_instrument_definition(category, *, output, manufacturer,
  model, force)` → `ScaffoldResult`
- `add_instrument_to_pack(pack_dir, *, instrument_id, category, ...,
  dry_run, force)` → `AddInstrumentResult`

category templates (v1.8):
- `power_supply`: identify / set_voltage / query_voltage / set_output /
  query_output + verify + safety + safe_shutdown + state_query
- `dmm`: identify / measure_voltage / measure_current + polling_safe
- `temperature_meter`: identify / measure_temperature + polling_safe
- `generic_scpi`: identify + reset + minimal skeleton

すべて **support_level: draft** で生成。
**manual_ref** に TODO placeholder を必ず入れる。
生成 YAML は `validate instrument` を error 0 で通る。

MCP tool 追加ゼロ。CLI 経由のみ。
"""
from __future__ import annotations
import logging
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path
from typing import Any

import yaml

from lab_executor.registry import validate_instrument_file

logger = logging.getLogger(__name__)


CATEGORIES = ("power_supply", "dmm", "temperature_meter", "generic_scpi")

# v1.9: support_level 昇格判定で参照する order
_SUPPORT_LEVEL_ORDER = ["draft", "experimental", "tested", "verified"]


@dataclass
class PromoteCheckResult:
    """v1.9: instrument promote-check minimal 結果"""
    status: str = "ok"  # ok / warning / error
    file: str = ""
    current_support_level: str = ""
    target_support_level: str = ""
    eligible: bool = False
    blocking_issues: list[dict[str, Any]] = field(default_factory=list)
    recommended_actions: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "file": self.file,
            "current_support_level": self.current_support_level,
            "target_support_level": self.target_support_level,
            "eligible": self.eligible,
            "blocking_issues": list(self.blocking_issues),
            "recommended_actions": list(self.recommended_actions),
        }


def promote_check_instrument(
    path: str | Path,
    *,
    target: str = "tested",
) -> PromoteCheckResult:
    """v1.9: instrument YAML が `target` support_level に昇格してよいか
    診断する **minimal** helper。内部的には
    `validate_instrument_file(strict=True)` を再利用する。

    target=`tested`:
      - strict validate を通る (manual_ref TODO 無し / verify / safe_shutdown
        / safety.ratings 揃い)
    target=`verified`:
      - target=tested を満たし、かつ `metadata.validation_evidence` が
        非空
    """
    from lab_executor.registry import (
        validate_instrument_file, SUPPORT_LEVELS,
    )
    res = PromoteCheckResult(file=str(path), target_support_level=target)

    if target not in SUPPORT_LEVELS:
        res.status = "error"
        res.blocking_issues.append({
            "issue": "invalid_target",
            "message": (
                f"target={target!r} は {list(SUPPORT_LEVELS)} のいずれか"
            ),
        })
        return res

    p = Path(path).expanduser()
    if not p.exists():
        res.status = "error"
        res.blocking_issues.append({
            "issue": "not_found",
            "message": f"file not found: {p}",
        })
        return res

    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception as e:
        res.status = "error"
        res.blocking_issues.append({
            "issue": "schema_invalid",
            "message": str(e),
        })
        return res

    md = data.get("metadata") or {}
    current = md.get("support_level", "draft")
    res.current_support_level = current

    # 下方移動 (verified → tested 等) は明確に許可
    if (current in _SUPPORT_LEVEL_ORDER and target in _SUPPORT_LEVEL_ORDER
            and _SUPPORT_LEVEL_ORDER.index(target)
            <= _SUPPORT_LEVEL_ORDER.index(current)):
        res.eligible = True
        return res

    # strict validate
    val = validate_instrument_file(p, strict=True)
    blocking = []
    for e in val.errors:
        blocking.append({
            "issue": e.get("error_class", "strict_error"),
            "message": e.get("message", ""),
            "field_path": e.get("field_path", ""),
            "details": e.get("details") or {},
        })

    # target=verified の追加条件
    if target == "verified":
        ev = md.get("validation_evidence") or {}
        if not ev:
            blocking.append({
                "issue": "instrument_verified_missing_evidence",
                "message": (
                    "target=verified には metadata.validation_evidence "
                    "(非空) が必要"
                ),
                "field_path": "metadata.validation_evidence",
            })

    res.blocking_issues = blocking
    res.eligible = not blocking

    # recommended_actions (strict_validate と同一情報の簡易要約)
    for b in blocking:
        cls = b["issue"]
        if cls == "instrument_manual_ref_todo":
            res.recommended_actions.append({
                "action": "fill_manual_ref",
                "reason": "Replace TODO with actual manual / URL reference",
            })
        elif cls == "instrument_missing_safe_shutdown":
            res.recommended_actions.append({
                "action": "add_safe_shutdown",
                "reason": "Output-capable instrument requires safe_shutdown",
            })
        elif cls == "instrument_missing_safety_ratings":
            res.recommended_actions.append({
                "action": "add_safety_ratings",
                "reason": "Define voltage / current ratings",
            })
        elif cls == "instrument_missing_verify":
            res.recommended_actions.append({
                "action": "add_verify",
                "reason": (
                    f"Add verify (readback) to "
                    f"{b.get('details', {}).get('command', 'command')}"
                ),
            })
        elif cls == "instrument_verified_missing_evidence":
            res.recommended_actions.append({
                "action": "add_validation_evidence",
                "reason": (
                    "Fill metadata.validation_evidence (tested_by / "
                    "tested_at / interface / firmware / tested_items)"
                ),
            })

    if blocking:
        res.status = "warning"  # error ではなく blocking を warning 扱い
    return res


# ============================================================
# v1.10: instrument review-report (minimal)
# ============================================================


def review_report_instrument(
    path: str | Path,
) -> dict[str, Any]:
    """v1.10: instrument YAML 1 件 → markdown 形式 review report。

    PR review / 昇格判定の参考に使う **読み物**であり、JSON データを
    返さない (内部で `validate_instrument_file(strict=True)` +
    `promote_check_instrument(target=tested)` /
    `promote_check_instrument(target=verified)` を集約する)。

    Returns: {"status": "ok"/"warning"/"error", "markdown": str,
              "file": str}
    """
    from lab_executor.registry import validate_instrument_file
    p = Path(path).expanduser()
    out_status = "ok"
    lines: list[str] = []

    if not p.exists():
        return {
            "status": "error",
            "markdown": f"# Instrument review report\n\n**ERROR**: file not found: {p}\n",
            "file": str(p),
        }

    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception as e:
        return {
            "status": "error",
            "markdown": f"# Instrument review report\n\n**ERROR**: parse failed: {e}\n",
            "file": str(p),
        }

    md = data.get("metadata") or {}
    current = md.get("support_level", "draft")

    lines.append(f"# Instrument review report - `{p.name}`")
    lines.append("")
    lines.append(
        f"- **manufacturer / model**: "
        f"{md.get('manufacturer', '?')} / {md.get('model', '?')}")
    lines.append(f"- **category**: {md.get('category', '?')}")
    lines.append(f"- **current support_level**: `{current}`")
    lines.append(f"- **manual_ref**: {md.get('manual_ref', '(empty)')}")
    lines.append("")

    # strict validate
    val = validate_instrument_file(p, strict=True)
    if val.errors:
        out_status = "warning"
        lines.append("## Strict validation\n")
        lines.append(f"[FAIL] **{len(val.errors)} strict issue(s)**\n")
        for e in val.errors:
            lines.append(
                f"- `{e.get('error_class')}`: {e.get('message')}")
        lines.append("")
    else:
        lines.append("## Strict validation\n")
        lines.append("[OK] all strict checks pass\n")

    # promote-check (tested)
    tested = promote_check_instrument(p, target="tested")
    lines.append("## Promote → `tested`\n")
    lines.append(
        f"- **eligible**: {tested.eligible}")
    if tested.blocking_issues:
        out_status = out_status if out_status == "error" else "warning"
        lines.append(
            f"- blocking: {len(tested.blocking_issues)} issue(s)")
        for b in tested.blocking_issues:
            lines.append(f"  - `{b['issue']}`: {b['message']}")
    if tested.recommended_actions:
        lines.append("- recommended:")
        for a in tested.recommended_actions:
            lines.append(f"  - **{a['action']}**: {a['reason']}")
    lines.append("")

    # promote-check (verified)
    verified = promote_check_instrument(p, target="verified")
    lines.append("## Promote → `verified`\n")
    lines.append(f"- **eligible**: {verified.eligible}")
    if verified.blocking_issues:
        lines.append(
            f"- blocking: {len(verified.blocking_issues)} issue(s)")
        for b in verified.blocking_issues:
            lines.append(f"  - `{b['issue']}`: {b['message']}")
    if verified.recommended_actions:
        lines.append("- recommended:")
        for a in verified.recommended_actions:
            lines.append(f"  - **{a['action']}**: {a['reason']}")
    lines.append("")

    # support_level 昇格判定要約
    lines.append("## Summary\n")
    if tested.eligible and verified.eligible:
        lines.append("[OK] Ready for **verified** promotion.")
    elif tested.eligible:
        lines.append("[WARN] Ready for **tested** promotion. "
                      "`verified` needs additional evidence.")
    else:
        lines.append("[NO] Not ready for promotion. "
                      "Address blocking issues above.")
    lines.append("")
    lines.append(
        "_(Generated by `visa-mcp instrument review-report`, v1.10)_")

    return {
        "status": out_status,
        "markdown": "\n".join(lines),
        "file": str(p),
    }


# ============================================================
# Templates (multi-line YAML strings)
# ============================================================


_DRAFT_HEADER = """\
# Generated by `visa-mcp instrument scaffold` (v1.8+).
#
# THIS IS A DRAFT DEFINITION.
# Do not use with real hardware until the following are reviewed:
#   - commands / SCPI strings (verify against manufacturer manual)
#   - safety limits (ratings / preconditions)
#   - safe_shutdown sequence
#   - verify (read-back) settings
#   - state_query mapping
#   - response_format unit / parsing
# See docs/instrument_authoring.md for the publishing checklist.
"""


# v2.2.0: template は外部 YAML file (importlib.resources で読み込み)
# 配置: src/lab_executor/templates/instruments/<category>.yaml
# (v2.0 で lab_visa_mcp -> lab_executor へ移送、bootstrap script の rewrite
# 漏れを修正)
def _load_template(category: str) -> str:
    try:
        return (
            files("lab_executor.templates.instruments")
            .joinpath(f"{category}.yaml")
            .read_text(encoding="utf-8")
        )
    except (FileNotFoundError, ModuleNotFoundError):
        # 互換 fallback: lab-executor-mcp に templates が無く、
        # visa-mcp が install 済みなら旧 path を試す
        return (
            files("lab_visa_mcp.templates.instruments")
            .joinpath(f"{category}.yaml")
            .read_text(encoding="utf-8")
        )




# ============================================================
# scaffold
# ============================================================


@dataclass
class ScaffoldResult:
    status: str = "error"  # ok / error
    output_path: str = ""
    category: str = ""
    errors: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "output_path": self.output_path,
            "category": self.category,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }


def scaffold_instrument_definition(
    category: str,
    *,
    output: str | Path,
    manufacturer: str = "TODO",
    model: str = "TODO",
    force: bool = False,
) -> ScaffoldResult:
    """category 別 template から instrument YAML を生成する。

    生成 YAML は **`support_level: draft`** 固定。`manual_ref` に必ず
    TODO placeholder が入る。`validate instrument` を error 0 で通る。
    """
    res = ScaffoldResult(category=category)
    if category not in CATEGORIES:
        res.errors.append({
            "error_class": "validation",
            "message": (
                f"unknown category: {category!r} "
                f"(choose from {list(CATEGORIES)})"
            ),
            "details": {
                "sub_class": "instrument_scaffold_unknown_category",
            },
        })
        return res

    out = Path(output).expanduser()
    if out.exists() and not force:
        res.errors.append({
            "error_class": "validation",
            "message": (
                f"output already exists: {out} (use --force to overwrite)"
            ),
            "details": {
                "sub_class": "instrument_scaffold_target_exists",
            },
        })
        return res

    # v1.8.1 P1-6: --force で既存 file を上書きする場合、誤って手編集
    # 内容を消さないよう **`.bak-<UTC ts>` backup** を作る
    if out.exists() and force:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        bak = out.with_suffix(out.suffix + f".bak-{ts}")
        try:
            shutil.copy2(out, bak)
            res.warnings.append({
                "warning_class": "instrument_scaffold_force_backup",
                "message": (
                    f"--force: existing file backed up to {bak.name} "
                    "(remove manually after review)"
                ),
                "details": {"backup_path": str(bak)},
            })
        except Exception as e:
            logger.warning(
                "scaffold --force backup failed: %s", e,
            )

    # v1.8.1 P1-3: template は外部 YAML file から読み込み (旧: 巨大文字列)
    # SCPI 引数の {voltage} 等のブレースを壊さないよう、`.format()`
    # ではなく特定 placeholder のみ str.replace で置換する
    template = _load_template(category)
    body = (
        template
        .replace("{manufacturer}", manufacturer)
        .replace("{model}", model)
    )
    text = _DRAFT_HEADER + body
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")

    # draft 注意 warning は常に出す (AI agent / contributor 双方向け)
    res.warnings.append({
        "warning_class": "instrument_definition_draft",
        "message": (
            "Generated instrument definition is draft. Review commands, "
            "safety, safe_shutdown, verify, state_query, and manual_ref "
            "before real hardware use."
        ),
    })
    res.output_path = str(out)
    res.status = "ok"
    return res


# ============================================================
# extension add-instrument
# ============================================================


@dataclass
class AddInstrumentResult:
    status: str = "error"  # ok / error
    dry_run: bool = False
    instrument_file: str = ""
    extension_contents_updated: bool = False
    registry_entry_added: bool = False
    changes_preview: dict[str, Any] = field(default_factory=dict)
    errors: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "dry_run": self.dry_run,
            "instrument_file": self.instrument_file,
            "extension_contents_updated": self.extension_contents_updated,
            "registry_entry_added": self.registry_entry_added,
            "changes_preview": dict(self.changes_preview),
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }


# registry_entries 用 id 制限 (overlay registry 側と整合)
_INSTRUMENT_ID_RE = r"^[a-z0-9]+(?:[_\-][a-z0-9]+)*$"


def add_instrument_to_pack(
    pack_dir: str | Path,
    *,
    instrument_id: str,
    category: str,
    manufacturer: str = "TODO",
    model: str = "TODO",
    dry_run: bool = False,
    force: bool = False,
) -> AddInstrumentResult:
    """既存 pack に instrument YAML を生成し、`extension.yaml`
    `contents.instruments` と `registry_entries/INDEX.yaml` に entry
    を追加する。

    安全策:
    - 既存 instrument file がある場合は **--force 必須**
    - registry id 重複は **--force でも拒否**
    - 変更前後で `validate_extension_file` を実行し、失敗時は変更を
      ロールバック
    - `--dry-run` で変更予定だけ JSON で返す
    """
    res = AddInstrumentResult(dry_run=dry_run)
    pdir = Path(pack_dir).expanduser()
    if not pdir.exists() or not pdir.is_dir():
        res.errors.append({
            "error_class": "not_found",
            "message": f"pack directory not found: {pdir}",
        })
        return res
    ext_yaml = pdir / "extension.yaml"
    if not ext_yaml.exists():
        res.errors.append({
            "error_class": "not_found",
            "message": f"extension.yaml not found in pack: {pdir}",
        })
        return res

    import re
    if not re.match(_INSTRUMENT_ID_RE, instrument_id):
        res.errors.append({
            "error_class": "validation",
            "message": (
                f"instrument_id={instrument_id!r} は小文字英数 + '_' / "
                "'-' のみ"
            ),
            "details": {
                "sub_class": "add_instrument_invalid_id",
            },
        })
        return res
    if category not in CATEGORIES:
        res.errors.append({
            "error_class": "validation",
            "message": (
                f"unknown category: {category!r} (choose from "
                f"{list(CATEGORIES)})"
            ),
            "details": {
                "sub_class": "instrument_scaffold_unknown_category",
            },
        })
        return res

    # まず extension.yaml が valid であることを事前確認
    from lab_executor.extension import validate_extension_file
    pre_rep = validate_extension_file(ext_yaml)
    if pre_rep.errors:
        res.errors.append({
            "error_class": "validation",
            "message": (
                "pack validate failed before update; fix extension.yaml "
                "first"
            ),
            "details": {
                "sub_class": "add_instrument_pack_invalid",
                "sub_errors": pre_rep.errors,
            },
        })
        return res

    instr_dir = pdir / "instruments"
    instr_file = instr_dir / f"{instrument_id}.yaml"

    # 既存 file チェック
    if instr_file.exists() and not force:
        res.errors.append({
            "error_class": "validation",
            "message": (
                f"instrument file already exists: {instr_file} "
                "(use --force to overwrite)"
            ),
            "details": {
                "sub_class": "add_instrument_target_exists",
                "path": str(instr_file),
            },
        })
        return res

    # registry index
    reg_index_path = pdir / "registry_entries" / "INDEX.yaml"
    reg_data: dict[str, Any] = {"instruments": []}
    if reg_index_path.exists():
        try:
            loaded = yaml.safe_load(
                reg_index_path.read_text(encoding="utf-8")) or {}
            if isinstance(loaded, dict):
                reg_data = loaded
                reg_data.setdefault("instruments", [])
        except Exception as e:
            res.errors.append({
                "error_class": "schema_invalid",
                "message": f"registry_entries/INDEX.yaml parse failed: {e}",
            })
            return res
    existing_ids = {
        e.get("id") for e in (reg_data.get("instruments") or [])
        if isinstance(e, dict)
    }
    if instrument_id in existing_ids:
        # --force でも拒否
        res.errors.append({
            "error_class": "validation",
            "message": (
                f"registry id {instrument_id!r} already in "
                "registry_entries/INDEX.yaml"
            ),
            "details": {
                "sub_class": "add_instrument_duplicate_registry_id",
                "id": instrument_id,
            },
        })
        return res

    # extension.yaml 読み込み + 変更 preview
    ext_text = ext_yaml.read_text(encoding="utf-8")
    ext_data = yaml.safe_load(ext_text) or {}
    contents = ext_data.setdefault("contents", {})
    instruments_list = contents.setdefault("instruments", []) or []
    rel_path = f"instruments/{instrument_id}.yaml"
    already_in_contents = rel_path in instruments_list
    reg_entries_list = contents.setdefault("registry_entries", []) or []
    reg_index_rel = "registry_entries/INDEX.yaml"
    needs_reg_ref = reg_index_rel not in reg_entries_list

    new_reg_entry = {
        "id": instrument_id,
        "vendor": manufacturer,
        "model": model,
        "category": category,
        "support_level": "draft",
        "path": rel_path,
    }

    res.changes_preview = {
        "instrument_file": str(instr_file),
        "extension_contents_add_instrument":
            None if already_in_contents else rel_path,
        "extension_contents_add_registry_entries":
            None if not needs_reg_ref else reg_index_rel,
        "registry_index_add_entry": new_reg_entry,
    }

    if dry_run:
        res.warnings.append({
            "warning_class": "instrument_definition_draft",
            "message": (
                "Generated instrument definition will be draft. Review "
                "manual_ref / safety / safe_shutdown / verify before "
                "real hardware use."
            ),
        })
        res.status = "ok"
        return res

    # --- 実更新 (rollback 用に元 state を保持) ---
    backup_ext_text = ext_text
    backup_reg_text: str | None = (
        reg_index_path.read_text(encoding="utf-8")
        if reg_index_path.exists() else None
    )
    instr_existed_before = instr_file.exists()
    backup_instr_text: str | None = (
        instr_file.read_text(encoding="utf-8")
        if instr_existed_before else None
    )

    try:
        # 1. instrument file
        scaffold = scaffold_instrument_definition(
            category, output=instr_file, manufacturer=manufacturer,
            model=model, force=force,
        )
        if scaffold.status != "ok":
            res.errors.extend(scaffold.errors)
            return res
        res.instrument_file = str(instr_file)
        # scaffold の draft warning を継承
        res.warnings.extend(scaffold.warnings)

        # 2. extension.yaml 更新
        if not already_in_contents:
            instruments_list.append(rel_path)
            contents["instruments"] = sorted(set(instruments_list))
        if needs_reg_ref:
            reg_entries_list.append(reg_index_rel)
            contents["registry_entries"] = sorted(set(reg_entries_list))
        ext_yaml.write_text(
            yaml.safe_dump(ext_data, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        res.extension_contents_updated = True

        # 3. registry_entries/INDEX.yaml 更新
        reg_index_path.parent.mkdir(parents=True, exist_ok=True)
        # reg_data["instruments"] が [] (falsy) でも壊さないように
        # setdefault で確保してから append
        if not isinstance(reg_data.get("instruments"), list):
            reg_data["instruments"] = []
        reg_data["instruments"].append(new_reg_entry)
        reg_index_path.write_text(
            yaml.safe_dump(reg_data, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        res.registry_entry_added = True

        # 4. 更新後の validate
        post_rep = validate_extension_file(ext_yaml)
        if post_rep.errors:
            raise RuntimeError(
                f"post-update validate failed: {post_rep.errors}"
            )

        res.status = "ok"
        return res
    except Exception as e:
        # rollback
        try:
            ext_yaml.write_text(backup_ext_text, encoding="utf-8")
            if backup_reg_text is not None:
                reg_index_path.write_text(backup_reg_text, encoding="utf-8")
            elif reg_index_path.exists():
                reg_index_path.unlink()
            if backup_instr_text is not None:
                instr_file.write_text(backup_instr_text, encoding="utf-8")
            elif instr_file.exists() and not instr_existed_before:
                instr_file.unlink()
        except Exception as rb:
            logger.error("add_instrument rollback failed: %s", rb)
        res.errors.append({
            "error_class": "internal",
            "message": f"add-instrument aborted and rolled back: {e}",
            "details": {"sub_class": "add_instrument_rolled_back"},
        })
        return res
