"""実験資産 zip の生成 (build_asset, v2.25.0)

完了 Job から、export bundle を内包する上位パッケージ (asset zip) を生成する。
bundle 生成コアは ``lab_executor.tools.export.build_bundle_files`` を再利用する
(MCP ツールは経由しない)。
"""
from __future__ import annotations

import hashlib
import io
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

import json as _json

from lab_executor.asset import levels as _L
from lab_executor.asset.capability import match_capabilities
from lab_executor.asset.manifest import (
    ASSET_VERSION,
    AssetManifest,
    ContentEntry,
)


# zip 内相対 path -> kind の分類
def _classify_kind(path: str) -> str:
    if path.startswith("bundle/results"):
        return "results"
    if path.startswith("bundle/"):
        if path.endswith("job_record.json"):
            return "run_metadata"
        if path.endswith(("timeline.jsonl", "audit.jsonl")):
            return "log"
        return "run_metadata"
    if path.startswith("instrument/"):
        return "instrument"
    if path.startswith("recipe/"):
        return "recipe"
    if path.startswith("analysis/"):
        return "analysis"
    return "other"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _find_recipe_and_instruments(
    instruments_dir: Path, recipe_name: str, resource_name: str,
) -> tuple[dict | None, list[tuple[str, str]]]:
    """instruments_dir 配下の定義 YAML から、recipe 断片と装置定義を探す。

    Returns:
        (recipe_fragment, [(slug, yaml_text), ...])
        recipe_fragment: 見つかったレシピ定義 dict (requires 込み) or None
        装置定義リスト: recipe を持つ / resource に対応する YAML の (名前, 中身)
    """
    recipe_fragment: dict | None = None
    instruments: list[tuple[str, str]] = []
    if not instruments_dir or not instruments_dir.exists():
        return None, []

    for p in sorted(instruments_dir.glob("*.yaml")) + sorted(
        instruments_dir.glob("*.yml")
    ):
        try:
            text = p.read_text(encoding="utf-8")
            data = yaml.safe_load(text) or {}
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        recipes = data.get("recipes") or {}
        has_recipe = recipe_name and recipe_name in recipes
        if has_recipe and recipe_fragment is None:
            recipe_fragment = {
                "name": recipe_name,
                "definition": recipes[recipe_name],
                "source_instrument": p.stem,
            }
        # この定義を同梱する条件: recipe を持つ定義、または resource 名の一致
        if has_recipe:
            instruments.append((p.stem, text))
    # recipe を含む定義が 1 つも無ければ、resource に対応しそうな定義は
    # 特定できないので instruments は空のまま返す (レベルが下がるだけ)。
    return recipe_fragment, instruments


def _detect_contains_code(recipe_fragment: dict | None) -> dict[str, bool] | None:
    """v2.32.0 (SP-6): recipe 断片に py / dll ステップが含まれるか検出する。

    branch case / repeat body のネスト steps も再帰的に走査する。
    含まれなければ None (asset.yaml にキーを載せない — 既存資産と同形)。
    表示義務 (spec §6.1): 受け手はコードを検分の上、自己のポリシーで
    実行可否を判断する。
    """
    found = {"python": False, "dll": False}

    def _walk(steps) -> None:
        for s in steps or []:
            if not isinstance(s, dict):
                continue
            # model_dump() された RecipeStep は py/dll/branch 等を全て
            # キーに持つ (値 None) ため、キー存在でなく非 None で判定する
            if s.get("py") is not None:
                found["python"] = True
            if s.get("dll") is not None:
                found["dll"] = True
            for case in (s.get("branch") or []):
                if isinstance(case, dict):
                    _walk(case.get("steps"))
            rp = s.get("repeat")
            if isinstance(rp, dict):
                _walk(rp.get("steps"))

    definition = (recipe_fragment or {}).get("definition") or {}
    if isinstance(definition, dict):
        _walk(definition.get("steps"))
    if found["python"] or found["dll"]:
        return found
    return None


_META_KEYS = ("conditions", "hazards", "expected_results", "sample")


def _run_dry_run(
    recipe_fragment: dict | None,
    instr_list: list[tuple[str, str]],
    *,
    parameters: dict,
    resource_name: str,
) -> dict[str, Any]:
    """export 時に梱包レシピをコンパイル検証し dry_run 記録を返す。

    成功: recipe_to_plan が通り、同梱 instrument 定義が (非 strict) 検証で
    errors 0 なら ``ok=True`` + step_count。
    失敗 (定義 or レシピ不在 / コンパイル例外 / 検証 errors) は ``ok=False`` +
    一行 error を返す。**呼び出し側は export を失敗させないこと。**
    """
    import lab_executor as _le
    from lab_executor.recipe_executor import recipe_to_plan
    from lab_executor.models.instrument_def import RecipeDefinition

    runtime = f"lab-executor-mcp {getattr(_le, '__version__', '?')}"
    base: dict[str, Any] = {
        "performed_at": _now_iso(),
        "method": "recipe_to_plan+validate@export",
        "runtime": runtime,
    }

    def _fail(msg: str) -> dict[str, Any]:
        return {**base, "ok": False, "step_count": None, "error": msg}

    if not recipe_fragment or not recipe_fragment.get("definition"):
        return _fail("recipe fragment not found (レシピ定義が同梱されていない)")
    if not instr_list:
        return _fail("instrument definition not found (装置定義が同梱されていない)")

    # 1. レシピをコンパイル (recipe_to_plan)
    try:
        recipe = RecipeDefinition(**(recipe_fragment.get("definition") or {}))
        plan = recipe_to_plan(
            recipe, dict(parameters or {}),
            primary_resource=resource_name or None,
        )
        step_count = len(plan.steps)
    except Exception as e:  # noqa: BLE001 (一行要約に集約)
        return _fail(f"recipe compile failed: {type(e).__name__}: {e}")

    # 2. 同梱装置定義を (非 strict) 検証
    from lab_executor.registry import validate_instrument_file

    for slug, text in instr_list:
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".yaml", delete=False, encoding="utf-8",
            ) as tf:
                tf.write(text)
                tmp = tf.name
            report = validate_instrument_file(tmp, strict=False)
            if report.errors:
                return _fail(
                    f"instrument validation failed ({slug}): "
                    f"{report.errors[0]}")
        except Exception as e:  # noqa: BLE001
            return _fail(
                f"instrument validation error ({slug}): "
                f"{type(e).__name__}: {e}")
        finally:
            if tmp:
                try:
                    Path(tmp).unlink()
                except Exception:
                    pass

    return {**base, "ok": True, "step_count": step_count}


def _count_result_rows_bytes(files: dict[str, bytes]) -> int:
    """in-memory bundle bytes から results 行数を数える (checker と同ロジック)。"""
    n = 0
    blob = files.get("bundle/results.jsonl")
    if blob is not None:
        try:
            n = sum(1 for line in blob.decode("utf-8").splitlines()
                    if line.strip())
        except Exception:
            n = 0
    if n == 0 and "bundle/results.csv" in files:
        try:
            data_lines = [
                x for x in files["bundle/results.csv"].decode(
                    "utf-8").splitlines() if x.strip()
            ]
            n = max(0, len(data_lines) - 1)
        except Exception:
            n = 0
    return n


def _raw_value_paired_bytes(files: dict[str, bytes]) -> bool:
    """in-memory bundle bytes で raw↔numeric 対応を確認 (checker と同ロジック)。"""
    blob = files.get("bundle/results.jsonl")
    if blob is None:
        return False
    try:
        txt = blob.decode("utf-8")
    except Exception:
        return False
    has_numeric = False
    for line in txt.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = _json.loads(line)
        except Exception:
            continue
        v = obj.get("value")
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            has_numeric = True
    return has_numeric


def _instrument_strict_ok_texts(
    instr_list: list[tuple[str, str]],
) -> bool:
    """同梱予定の装置定義 (text) が strict 検証 pass するか (checker と同判定)。"""
    if not instr_list:
        return False
    from lab_executor.registry import validate_instrument_file

    for _slug, text in instr_list:
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".yaml", delete=False, encoding="utf-8",
            ) as tf:
                tf.write(text)
                tmp = tf.name
            report = validate_instrument_file(tmp, strict=True)
            if report.errors:
                return False
        except Exception:
            return False
        finally:
            if tmp:
                try:
                    Path(tmp).unlink()
                except Exception:
                    pass
    return True


def _auto_declare_level(
    *,
    files: dict[str, bytes],
    job_record: dict,
    recipe_fragment: dict | None,
    instr_list: list[tuple[str, str]],
    conditions: dict | None,
    hazards: dict | None,
    expected_results: list | None,
    dry_run: dict | None,
) -> int:
    """梱包物そのものから独立可用性レベルを自己判定する (level_declared 用)。

    checker.check_asset と同じ levels.py 判定関数を、build 直後の in-memory
    材料で呼ぶ。build 直後ゆえ ``schema_ok`` / ``checksums_ok`` は True 扱い。
    これにより export 直後の check で level_declared == level_verified になる。
    """
    from lab_executor.asset.manifest import ConditionsInfo
    from lab_executor.models.instrument_def import (
        CapabilityRequirements,
        InstrumentDefinition,
    )

    # checker は asset.yaml に直列化された conditions を読む。conditions 未指定でも
    # manifest は ConditionsInfo の既定 (calibration/environment = not_recorded) を
    # 書き込むため、L2 のキー存在判定はここでも同じ直列化形で行う。
    try:
        eff_conditions_serialized = ConditionsInfo(
            **(conditions or {})).model_dump(mode="json")
    except Exception:
        eff_conditions_serialized = conditions or {}

    results_row_count = _count_result_rows_bytes(files)
    raw_value_paired = _raw_value_paired_bytes(files)
    has_instrument_def = bool(instr_list)
    has_timeline = "bundle/timeline.jsonl" in files
    has_analysis = any(
        n.startswith("analysis/") and not n.endswith("/") for n in files
    )

    # L4: recipe requires と同梱装置の capability 照合
    recipe_requires = None
    if recipe_fragment:
        defn_frag = recipe_fragment.get("definition") or {}
        recipe_requires = defn_frag.get("requires")
    has_requires = recipe_requires is not None
    capability_match = None
    if has_requires:
        try:
            req = CapabilityRequirements(**recipe_requires)
        except Exception:
            req = None
        instr_def = None
        for _slug, text in instr_list:
            try:
                data = yaml.safe_load(text) or {}
                instr_def = InstrumentDefinition(**data)
                break
            except Exception:
                continue
        capability_match = match_capabilities(req, instr_def)

    instrument_strict_ok = _instrument_strict_ok_texts(instr_list)

    lv: dict[str, dict] = {}
    lv["L0"] = _L.judge_l0(results_row_count=results_row_count)
    lv["L1"] = _L.judge_l1(job_record=job_record)
    lv["L2"] = _L.judge_l2(
        has_instrument_def=has_instrument_def,
        has_timeline=has_timeline,
        conditions=eff_conditions_serialized,
    )
    lv["L3"] = _L.judge_l3(
        checksums_ok=True,   # build 直後: sha256 は今計算した値そのもの
        raw_value_paired=raw_value_paired,
        has_analysis=has_analysis,
        schema_ok=True,      # build 直後: 自前生成した manifest ゆえ有効
    )
    lv["L4"] = _L.judge_l4(
        has_requires=has_requires,
        capability_match=capability_match,
    )
    lv["L5"] = _L.judge_l5(
        hazards=hazards,
        expected_results=expected_results,
        dry_run=dry_run,
        instrument_strict_ok=instrument_strict_ok,
    )
    return _L.summarize_verified_level(lv)


def build_asset(
    *,
    job_id: str,
    db_path: Path | str,
    instruments_dir: Path | str,
    out_path: Path | str | None = None,
    title: str = "",
    license_id: str = "UNLICENSED",
    analysis_path: Path | str | None = None,
    declare_level: int | None = None,
    git_commit: str | None = None,
    conditions: dict | None = None,
    hazards: dict | None = None,
    expected_results: list | None = None,
    sample: dict | None = None,
    dry_run_now: bool = False,
    meta: dict | None = None,
) -> dict[str, Any]:
    """完了 Job から実験資産 zip を生成する。

    Returns:
        {"path", "asset_id", "level_declared", "contents_count", "sha256"}
    """
    from lab_executor.tools.export import build_bundle_files, _resolve_export_dir
    from lab_executor.job import JobManager, JobStore
    from lab_executor.backends import MockBackend

    db_path = Path(db_path)
    instruments_dir = Path(instruments_dir) if instruments_dir else None

    store = JobStore(db_path)
    try:
        # session_mgr は bundle 生成に不要 (store のみ使用) なので軽量 stub。
        class _NullSessions:
            def get_session(self, name):  # noqa: D401
                return None

        job_mgr = JobManager(
            backend=MockBackend(), session_mgr=_NullSessions(), store=store,
        )
        rec = job_mgr.get(job_id)  # KeyError if not found
        job_record = rec.to_dict()

        # 1. bundle/ 部分 (export bundle をそのまま内包)
        bundle_files = build_bundle_files(job_mgr, job_id)

        files: dict[str, bytes] = {}
        for name, blob in bundle_files.items():
            files[f"bundle/{name}"] = blob

        # 2. recipe/ + instrument/
        recipe_fragment, instr_list = _find_recipe_and_instruments(
            instruments_dir, job_record.get("recipe", ""),
            job_record.get("resource_name", ""),
        )
        recipe_doc = {
            "recipe": job_record.get("recipe", ""),
            "resolved_parameters": job_record.get("parameters", {}),
            "fragment": recipe_fragment,
        }
        files["recipe/recipe.yaml"] = yaml.safe_dump(
            recipe_doc, allow_unicode=True, sort_keys=False,
        ).encode("utf-8")

        for slug, text in instr_list:
            files[f"instrument/{slug}.yaml"] = text.encode("utf-8")

        # 2b. meta マージ (meta が個別引数より優先。両方指定時は meta 勝ち)
        meta = meta or {}
        eff_conditions = meta.get("conditions", conditions)
        eff_hazards = meta.get("hazards", hazards)
        eff_expected_results = meta.get("expected_results", expected_results)
        eff_sample = meta.get("sample", sample)

        # 2c. dry-run 検証 (--dry-run-now): 失敗しても export は継続する
        dry_run = None
        if dry_run_now:
            dry_run = _run_dry_run(
                recipe_fragment, instr_list,
                parameters=job_record.get("parameters", {}) or {},
                resource_name=job_record.get("resource_name", "") or "",
            )

        # 3. analysis/
        if analysis_path is not None:
            ap = Path(analysis_path)
            if ap.exists():
                files["analysis/README.md"] = ap.read_bytes()

        # 4. contents (各ファイルの sha256 + kind)
        contents = [
            ContentEntry(
                path=name,
                sha256=hashlib.sha256(files[name]).hexdigest(),
                kind=_classify_kind(name),  # type: ignore[arg-type]
            )
            for name in sorted(files.keys())
        ]

        asset_id = str(uuid.uuid4())

        # level_declared:
        # - 明示指定 (declare_level) があればそれを優先。
        # - None のとき: 梱包物そのものを check 相当で自己判定し、その値を宣言する。
        #   これにより「export 直後の check で level_declared == level_verified」が
        #   成立する (計画 v0.1 の仕様)。判定は checker と同じ levels.py の純関数を
        #   in-memory 材料で呼ぶ。build 直後ゆえ schema_ok / checksums_ok は True。
        auto_declare = declare_level is None
        if auto_declare:
            level_declared = _auto_declare_level(
                files=files,
                job_record=job_record,
                recipe_fragment=recipe_fragment,
                instr_list=instr_list,
                conditions=eff_conditions,
                hazards=eff_hazards,
                expected_results=eff_expected_results,
                dry_run=dry_run,
            )
            level_declared = max(0, level_declared)
        else:
            level_declared = declare_level

        import lab_executor as _le
        manifest = AssetManifest(
            asset_version=ASSET_VERSION,
            asset_id=asset_id,
            level_declared=level_declared,
            level_verified=None,
            title=title,
            created_at=_now_iso(),
            license=license_id,
            provenance={
                "producer": "",
                "runtime": f"lab-executor-mcp {getattr(_le, '__version__', '?')}",
                "git_commit": git_commit,
            },
            conditions=eff_conditions or {},
            sample=eff_sample,
            hazards=eff_hazards,
            expected_results=eff_expected_results or [],
            dry_run=dry_run,
            # v2.32.0 (SP-6): py / dll を含むレシピの表示義務 (spec §6.1)
            contains_code=_detect_contains_code(recipe_fragment),
            contents=[c.model_dump() for c in contents],
        )
        asset_yaml = yaml.safe_dump(
            manifest.to_yaml_dict(), allow_unicode=True, sort_keys=False,
        ).encode("utf-8")
        files["asset.yaml"] = asset_yaml

        # 5. 出力 path
        if out_path is not None:
            final_path = Path(out_path)
        else:
            final_path = _resolve_export_dir() / f"{asset_id}.asset.zip"
        final_path.parent.mkdir(parents=True, exist_ok=True)

        with zipfile.ZipFile(
            final_path, "w", compression=zipfile.ZIP_DEFLATED,
        ) as zf:
            # asset.yaml を先頭に置く
            zf.writestr("asset.yaml", files.pop("asset.yaml"))
            for name in sorted(files.keys()):
                zf.writestr(name, files[name])

        zip_bytes = final_path.read_bytes()
        return {
            "path": str(final_path),
            "asset_id": asset_id,
            "level_declared": level_declared,
            "contents_count": len(contents),
            "sha256": hashlib.sha256(zip_bytes).hexdigest(),
            "dry_run": dry_run,
        }
    finally:
        store.close()
