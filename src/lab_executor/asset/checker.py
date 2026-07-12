"""実験資産 zip の検査 (check_asset, v2.25.0)

asset zip を読み、独立可用性レベル L0〜L5 を機械判定する。
I/O + levels.py の純関数呼び出しを担う (判定ロジック自体は levels.py)。
"""
from __future__ import annotations

import hashlib
import json
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from lab_executor.asset import levels as L
from lab_executor.asset.capability import match_capabilities
from lab_executor.asset.manifest import AssetManifest


@dataclass
class CheckReport:
    asset_id: str = ""
    schema_ok: bool = False
    checksums_ok: bool = False
    level_declared: int | None = None
    level_verified: int = -1
    levels: dict[str, dict] = field(default_factory=dict)
    warnings: list[dict] = field(default_factory=list)
    # v2.32.0 (SP-6): py / dll ステップを含む資産の表示 (spec §6.1)。
    # {"python": bool, "dll": bool} または None (コードなし)。
    contains_code: dict[str, bool] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "schema_ok": self.schema_ok,
            "checksums_ok": self.checksums_ok,
            "level_declared": self.level_declared,
            "level_verified": self.level_verified,
            "levels": self.levels,
            "warnings": self.warnings,
            "contains_code": self.contains_code,
        }

    @property
    def integrity_broken(self) -> bool:
        """スキーマ or checksum 破損 (CLI exit 1 判定用)。"""
        return not self.schema_ok or not self.checksums_ok


def _read_names(zf: zipfile.ZipFile) -> set[str]:
    return set(zf.namelist())


def _count_result_rows(zf: zipfile.ZipFile, names: set[str]) -> int:
    n = 0
    if "bundle/results.jsonl" in names:
        try:
            txt = zf.read("bundle/results.jsonl").decode("utf-8")
            n = sum(1 for line in txt.splitlines() if line.strip())
        except Exception:
            n = 0
    if n == 0 and "bundle/results.csv" in names:
        try:
            txt = zf.read("bundle/results.csv").decode("utf-8")
            data_lines = [x for x in txt.splitlines() if x.strip()]
            n = max(0, len(data_lines) - 1)  # header を除く
        except Exception:
            n = 0
    return n


def _raw_value_paired(zf: zipfile.ZipFile, names: set[str]) -> bool:
    """results に raw_response と value_numeric の両方が保持されているか。

    bundle の results 行 (標準 columns) では value 列に raw / 数値のいずれかが
    入る。少なくとも 1 系列で value が数値、かつ別行 (または同一) に raw 文字列が
    保持されていることを緩く確認する。ここでは results.jsonl 内に数値 value を
    持つ行と、job_steps 由来で raw_response を保持する行が存在するかを見る。
    """
    if "bundle/results.jsonl" not in names:
        return False
    try:
        txt = zf.read("bundle/results.jsonl").decode("utf-8")
    except Exception:
        return False
    has_numeric = False
    has_raw = False
    for line in txt.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        v = obj.get("value")
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            has_numeric = True
        elif isinstance(v, str) and v:
            # raw 文字列 (数値文字列含む) を raw 応答として扱う
            has_raw = True
    # value_numeric measurement が emit された行も numeric とみなす
    return has_numeric and (has_raw or has_numeric)


def check_asset(zip_path: str | Path) -> CheckReport:
    """asset zip を検査し、CheckReport を返す。"""
    rep = CheckReport()
    p = Path(zip_path)
    if not p.exists():
        rep.warnings.append({
            "warning_class": "not_found",
            "message": f"asset not found: {p}",
        })
        return rep
    try:
        zf = zipfile.ZipFile(p, "r")
    except (zipfile.BadZipFile, OSError) as e:
        rep.warnings.append({
            "warning_class": "invalid_asset_format",
            "message": f"invalid asset zip: {e}",
        })
        return rep

    try:
        names = _read_names(zf)

        # ---- asset.yaml スキーマ検証 ----
        manifest: AssetManifest | None = None
        manifest_raw: dict = {}
        if "asset.yaml" not in names:
            rep.warnings.append({
                "warning_class": "missing_manifest",
                "message": "asset.yaml が asset 内に存在しません",
            })
        else:
            try:
                manifest_raw = yaml.safe_load(
                    zf.read("asset.yaml").decode("utf-8")) or {}
                manifest = AssetManifest(**manifest_raw)
                rep.schema_ok = True
                rep.asset_id = manifest.asset_id
                rep.level_declared = manifest.level_declared
                # v2.32.0 (SP-6): contains_code をレポートに表示
                rep.contains_code = manifest.contains_code
            except Exception as e:
                rep.warnings.append({
                    "warning_class": "invalid_manifest",
                    "message": f"asset.yaml schema invalid: {e}",
                })

        # ---- checksums 検証 (contents 各エントリ) ----
        checksums_ok = True
        contents = (manifest.contents if manifest else [])
        if not contents:
            # スキーマ壊れている場合は checksum も未確認扱い
            checksums_ok = rep.schema_ok
        for entry in contents:
            if entry.path not in names:
                checksums_ok = False
                rep.warnings.append({
                    "warning_class": "checksum_missing_file",
                    "message": f"contents path が zip に無い: {entry.path}",
                })
                continue
            actual = hashlib.sha256(zf.read(entry.path)).hexdigest()
            if actual != entry.sha256:
                checksums_ok = False
                rep.warnings.append({
                    "warning_class": "checksum_mismatch",
                    "message": f"sha256 不一致: {entry.path}",
                })
        rep.checksums_ok = checksums_ok

        # ---- job_record ----
        job_record: dict = {}
        if "bundle/job_record.json" in names:
            try:
                job_record = json.loads(
                    zf.read("bundle/job_record.json").decode("utf-8"))
            except Exception:
                job_record = {}

        # ---- instrument 定義 (同梱) ----
        instr_names = [n for n in names
                       if n.startswith("instrument/")
                       and n.endswith((".yaml", ".yml"))]
        has_instrument_def = bool(instr_names)

        # ---- analysis ----
        has_analysis = any(
            n.startswith("analysis/") and not n.endswith("/")
            for n in names
        )

        # ---- results ----
        results_row_count = _count_result_rows(zf, names)
        raw_value_paired = _raw_value_paired(zf, names)

        # ---- conditions (キー欠落判定) ----
        conditions = manifest_raw.get("conditions") if manifest_raw else None

        # ---- recipe requires / capability (L4) ----
        recipe_requires = None
        capability_match = None
        if "recipe/recipe.yaml" in names:
            try:
                recipe_doc = yaml.safe_load(
                    zf.read("recipe/recipe.yaml").decode("utf-8")) or {}
                frag = (recipe_doc.get("fragment") or {})
                defn = frag.get("definition") or {}
                recipe_requires = defn.get("requires")
            except Exception:
                recipe_requires = None

        has_requires = recipe_requires is not None
        if has_requires:
            capability_match = _run_capability_match(
                recipe_requires, zf, instr_names)

        # ---- instrument strict 検証 (L5) ----
        instrument_strict_ok = _instrument_strict_ok(zf, instr_names)

        # ---- L5 fields ----
        hazards = manifest_raw.get("hazards") if manifest_raw else None
        expected_results = (
            manifest_raw.get("expected_results") if manifest_raw else None)
        dry_run = manifest_raw.get("dry_run") if manifest_raw else None

        # ---- 各レベル判定 ----
        has_timeline = "bundle/timeline.jsonl" in names
        lv: dict[str, dict] = {}
        lv["L0"] = L.judge_l0(results_row_count=results_row_count)
        lv["L1"] = L.judge_l1(job_record=job_record)
        lv["L2"] = L.judge_l2(
            has_instrument_def=has_instrument_def,
            has_timeline=has_timeline,
            conditions=conditions,
        )
        lv["L3"] = L.judge_l3(
            checksums_ok=checksums_ok,
            raw_value_paired=raw_value_paired,
            has_analysis=has_analysis,
            schema_ok=rep.schema_ok,
        )
        lv["L4"] = L.judge_l4(
            has_requires=has_requires,
            capability_match=capability_match,
        )
        lv["L5"] = L.judge_l5(
            hazards=hazards,
            expected_results=expected_results,
            dry_run=dry_run,
            instrument_strict_ok=instrument_strict_ok,
        )
        rep.levels = lv
        rep.level_verified = L.summarize_verified_level(lv)
    finally:
        zf.close()
    return rep


def _run_capability_match(
    recipe_requires: dict, zf: zipfile.ZipFile, instr_names: list[str],
) -> dict:
    """recipe requires を同梱装置定義と照合する (最初の定義を使う)。"""
    from lab_executor.models.instrument_def import (
        CapabilityRequirements,
        InstrumentDefinition,
    )
    try:
        req = CapabilityRequirements(**recipe_requires)
    except Exception:
        req = None
    defn = None
    for n in instr_names:
        try:
            data = yaml.safe_load(zf.read(n).decode("utf-8")) or {}
            defn = InstrumentDefinition(**data)
            break
        except Exception:
            continue
    return match_capabilities(req, defn)


def _instrument_strict_ok(
    zf: zipfile.ZipFile, instr_names: list[str],
) -> bool:
    """同梱 instrument 定義が strict 検証 pass するか (errors 0)。"""
    if not instr_names:
        return False
    from lab_executor.registry import validate_instrument_file

    for n in instr_names:
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".yaml", delete=False, encoding="utf-8",
            ) as tf:
                tf.write(zf.read(n).decode("utf-8"))
                tmp = tf.name
        except Exception:
            return False
        try:
            report = validate_instrument_file(tmp, strict=True)
            if report.errors:
                return False
        finally:
            try:
                Path(tmp).unlink()
            except Exception:
                pass
    return True
