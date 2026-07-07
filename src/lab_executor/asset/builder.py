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

        # level_declared: 明示が無ければ 0 を宣言 (check が verified を出す)
        level_declared = declare_level if declare_level is not None else 0

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
