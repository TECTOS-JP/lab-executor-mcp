"""
v0.9.1+v1.0: 測定結果 export API (3 ツール構成)

- `get_experiment_results` (stable v1.0): MCP 応答で少量確認用 JSON
- `export_experiment_results` (stable v1.0): CSV / JSONL ファイル出力
- `export_experiment_bundle` (experimental v1.0): 再現性 bundle (zip)
  with manifest + checksums。`import_*` は v1.1+ 候補。

設計原則:
- MCP 応答に大量データを返さない (LLM context 浪費を避ける)
- 大量データは default export dir (`~/.visa-mcp/exports/`) 配下のファイルへ
- monitor_data はデフォルト除外 (詳細時系列は既存 get_monitor_data に任せる)
- bundle は **再検証・共有・監査・記事化** のためのパッケージ
  (別環境での完全再現実行は v1.x 内ではサポートしない)
"""
from __future__ import annotations
import csv
import hashlib
import io
import json
import logging
import os
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

from lab_executor.job import JobManager
from lab_executor.response_envelope import make_envelope, make_error

logger = logging.getLogger(__name__)


# 既定 export ディレクトリ
DEFAULT_EXPORT_DIR = Path.home() / ".visa-mcp" / "exports"

# 結果行の標準 columns (v0.9.1, v1.0 で凍結候補)
RESULT_COLUMNS = (
    "timestamp",
    "target_id",
    "instrument",
    "measurement",
    "value",
    "unit",
    "step_index",
    "step_path",
    "sweep_index",
    "sweep_value",
)


def _resolve_export_dir() -> Path:
    """export 先ディレクトリを優先順で決定する。"""
    raw = os.environ.get("VISA_MCP_EXPORT_DIR", "").strip()
    if raw:
        return Path(raw).expanduser()
    return DEFAULT_EXPORT_DIR


def _safe_export_path(
    output_path: str | None, *, default_filename: str,
    overwrite: bool,
) -> tuple[Path | None, dict | None]:
    """output_path を検証して安全な絶対パスに正規化する。

    Returns:
        (path, error_dict)
        path: 安全な絶対パス (失敗時 None)
        error_dict: 失敗理由 (成功時 None)
    """
    base = _resolve_export_dir().resolve()
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return (None, {
            "error_class": "export_dir_not_writable",
            "message": (
                f"export ディレクトリを作成できません: {base} ({e}). "
                "VISA_MCP_EXPORT_DIR で書き込み可能な場所を指定してください。"
            ),
            "recoverable": True,
            "details": {"export_dir": str(base)},
            "recommended_next_actions": [
                {"action": "set_export_dir_env",
                 "reason": "VISA_MCP_EXPORT_DIR=<書き込み可能 path> を設定"},
            ],
        })

    if output_path is None or not output_path.strip():
        # v0.9.1: default パスでも existence チェックを通す
        default_path = (base / default_filename).resolve()
        if default_path.exists() and not overwrite:
            return (None, {
                "error_class": "invalid_export_path",
                "message": (
                    f"既定 export path がすでに存在します。overwrite=true を指定するか "
                    f"output_path を別名にしてください: {default_path}"
                ),
                "recoverable": True,
                "details": {"existing_path": str(default_path)},
                # v0.9.1.1: recommended_next_actions (レビュー指摘 P1)
                "recommended_next_actions": [
                    {"action": "set_overwrite_true",
                     "reason": "前回の export を意図的に上書きする"},
                    {"action": "choose_different_output_path",
                     "reason": "別 path を指定して前回の export を保持する"},
                ],
            })
        return (default_path, None)

    candidate = Path(output_path).expanduser()
    # default_dir 配下に強制
    try:
        if candidate.is_absolute():
            resolved = candidate.resolve()
        else:
            resolved = (base / candidate).resolve()
    except (OSError, ValueError) as e:
        return (None, {
            "error_class": "invalid_export_path",
            "message": f"output_path を解決できません: {e}",
            "recoverable": True,
        })

    # path traversal 検出: resolved が base の subpath でない
    try:
        resolved.relative_to(base)
    except ValueError:
        return (None, {
            "error_class": "invalid_export_path",
            "message": (
                f"output_path は {base} 配下である必要があります "
                f"(absolute paths / .. による traversal は拒否)"
            ),
            "recoverable": True,
            "details": {"base_dir": str(base), "rejected_path": str(resolved)},
        })

    if resolved.exists() and not overwrite:
        return (None, {
            "error_class": "invalid_export_path",
            "message": (
                f"output_path はすでに存在します。overwrite=true を指定するか "
                f"別のパスを使ってください: {resolved}"
            ),
            "recoverable": True,
            "details": {"existing_path": str(resolved)},
            "recommended_next_actions": [
                {"action": "set_overwrite_true",
                 "reason": "前回の export を意図的に上書きする"},
                {"action": "choose_different_output_path",
                 "reason": "別 path を指定して前回の export を保持する"},
            ],
        })
    return (resolved, None)


def _extract_result_rows(
    job_mgr: JobManager, job_id: str, *, include_monitor: bool = False,
) -> list[dict[str, Any]]:
    """job_steps / target_runs / (オプションで monitor_data) から
    測定結果行を抽出して標準 columns に正規化する。
    """
    store = job_mgr.store
    rows: list[dict[str, Any]] = []

    # job_steps: result/error に measurement / value / unit が入っている可能性
    try:
        steps = store.list_steps(job_id)
    except Exception:
        steps = []
    for s in steps:
        r = s.get("result") or s.get("error") or {}
        if not isinstance(r, dict):
            continue
        # query 系: response_parsed が dict なら個別の measurement / value / unit
        # を抽出。それ以外は command + response_raw を保持。
        ts = s.get("ended_at") or s.get("started_at")
        common = {
            "timestamp": ts,
            "target_id": s.get("target_id"),
            "instrument": r.get("instrument"),
            "step_index": s.get("step_index"),
            "step_path": (r.get("step_path") or
                          (f"steps[{s.get('step_index')}]"
                           if s.get("step_index") is not None else None)),
            "sweep_index": r.get("sweep_index"),
            "sweep_value": r.get("sweep_value"),
        }
        # parsed measurement (v0.8.x response_parsed dict)
        # v2.13.2: step_executor は `parsed` キー (response_parser 経由) も
        # 使う可能性があるので両方対応
        # v2.14.1: response_parser の出力には metadata keys
        # (matched / fallback_used / matched_pattern_index / raw / fields)
        # が含まれる。これらは measurement 列に出すべきでない。
        # `fields` 内の numeric / `value_numeric` だけを rows 化する。
        parsed = (
            r.get("response_parsed")
            or r.get("parsed")
        ) if isinstance(r, dict) else None
        _PARSED_METADATA_KEYS = {
            "matched", "matched_pattern_index", "raw",
            "fallback_used", "fields", "error",
        }
        emitted_from_parsed = False
        if isinstance(parsed, dict) and parsed:
            cmd_name = r.get("command") or s.get("step_type")
            # 旧形式: response_parsed = {"value": 1.23, "unit": "V"} のように
            # 平 dict (response_parser 経由でない自前 parsed)。これは従来
            # 通り key 単位で rows 化するが、metadata key は skip する。
            top_numeric_keys = [
                k for k, v in parsed.items()
                if k not in _PARSED_METADATA_KEYS
                and isinstance(v, (int, float))
            ]
            # 新形式 (v2.14.0 response_parser): `fields` dict + 別途
            # `value_numeric` / `matched` / `raw` / `matched_pattern_index`
            new_fields = parsed.get("fields") if isinstance(
                parsed.get("fields"), dict) else None
            value_numeric = parsed.get("value_numeric")
            if new_fields or value_numeric is not None:
                # 構造化 fields の numeric だけを emit
                for k, v in (new_fields or {}).items():
                    if not isinstance(v, (int, float)):
                        continue
                    rows.append({
                        **common,
                        "measurement": f"{cmd_name}.{k}" if cmd_name else k,
                        "value": v,
                        "unit": "",
                    })
                    emitted_from_parsed = True
                if value_numeric is not None and isinstance(
                    value_numeric, (int, float)
                ):
                    rows.append({
                        **common,
                        "measurement": (
                            f"{cmd_name}.value_numeric"
                            if cmd_name else "value_numeric"),
                        "value": value_numeric,
                        "unit": "",
                    })
                    emitted_from_parsed = True
            elif top_numeric_keys:
                # 旧形式 (response_parsed = {"value": 1.23, "unit": "V"})
                for k in top_numeric_keys:
                    rows.append({
                        **common,
                        "measurement": k,
                        "value": parsed[k],
                        "unit": "",
                    })
                    emitted_from_parsed = True
        if not emitted_from_parsed:
            # 数値 raw response or value
            # v2.13.2 fix: step_executor が保存するキー名は `raw_response`。
            # 旧名 `response_raw` / `response` は後方互換のため残す。
            v = r.get("value")
            if v is None:
                v = (
                    r.get("raw_response")
                    or r.get("response_raw")
                    or r.get("response")
                )
            if v is not None:
                rows.append({
                    **common,
                    "measurement": r.get("command") or s.get("step_type"),
                    "value": v,
                    "unit": r.get("unit", ""),
                })

    # target_runs: 同じく result 内のキー抽出
    try:
        truns = store.list_target_runs(job_id)
    except Exception:
        truns = []
    for t in truns:
        r = t.get("result") or {}
        if not isinstance(r, dict):
            continue
        ts = t.get("ended_at") or t.get("started_at")
        # target_runs.result は target ごとの最終結果。measurement key を直接
        # 持つこともあれば、step_results を内包することもある。
        for key, val in r.items():
            if key in ("step_results", "success", "summary"):
                continue
            if isinstance(val, (int, float, str, bool)):
                rows.append({
                    "timestamp": ts,
                    "target_id": t.get("target_id"),
                    "instrument": None,
                    "measurement": key,
                    "value": val,
                    "unit": "",
                    "step_index": None,
                    "step_path": None,
                    "sweep_index": None,
                    "sweep_value": None,
                })

    # monitor_data (オプション、monitor_id == job_id 前提の慣用に従う)
    # 注: monitor_data は monitor_id でキー付けされており、Job との関連は
    # 既存実装で monitor_id を job 経由で渡す慣習。MVP では job_id を monitor_id
    # として直接照会する (start_monitor が同名で動く想定の最小実装)。
    if include_monitor:
        try:
            mcount = store.count_monitor_data(job_id)
            md = store.list_monitor_data(job_id, limit=min(mcount, 100000)
                                          if mcount else 100000)
        except Exception:
            md = []
        for m in md:
            rows.append({
                "timestamp": m.get("timestamp"),
                "target_id": None,
                "instrument": m.get("instrument"),
                "measurement": m.get("measurement") or m.get("key"),
                "value": m.get("value"),
                "unit": m.get("unit", ""),
                "step_index": None,
                "step_path": None,
                "sweep_index": None,
                "sweep_value": None,
            })

    return rows


def _filter_rows(
    rows: list[dict],
    *,
    instrument: str | None = None,
    sweep_index: int | None = None,
    measurement: str | None = None,
) -> list[dict]:
    """結果 row を instrument / sweep_index / measurement で AND フィルタ。

    空文字 / None のフィルタはそのフィールドで絞らない。
    sweep_index=0 は有効な絞り込みとして扱う。
    """
    out = rows
    if instrument:
        out = [r for r in out if r.get("instrument") == instrument]
    if sweep_index is not None:
        out = [r for r in out if r.get("sweep_index") == sweep_index]
    if measurement:
        out = [r for r in out if r.get("measurement") == measurement]
    return out


def build_bundle_files(
    job_mgr: JobManager,
    job_id: str,
    *,
    include_monitor_data: bool = False,
    include_audit: bool = False,
) -> dict[str, bytes]:
    """v2.25.0: bundle 内容 (manifest.json 込み) を name -> bytes で構築する。

    ``export_experiment_bundle`` の bundle 生成コアを **抽出** した純ロジック。
    zip 書き込みや path 安全策・MCP envelope は含まない (呼び出し側が担う)。
    `asset.builder` はこれを再利用して bundle/ 配下を展開する。

    返り値には ``manifest.json`` (bundle_version=1.0 / checksums 込み) を含む。
    job が存在しない場合は ``KeyError`` 相当を呼び出し側で扱えるよう、ここでは
    ``job_mgr.get(job_id)`` を先に呼んで rec を得る。
    """
    rec = job_mgr.get(job_id)
    store = job_mgr.store
    files: dict[str, bytes] = {}

    # plan + compiled_summary
    plan_row = None
    try:
        plan_row = store.get_experiment_plan_for_job(job_id)
    except Exception:
        pass
    if plan_row:
        files["plan.json"] = json.dumps(
            plan_row.get("original_plan") or {},
            ensure_ascii=False, indent=2, default=str,
        ).encode("utf-8")
        files["compiled_summary.json"] = json.dumps(
            plan_row.get("compiled_summary") or {},
            ensure_ascii=False, indent=2, default=str,
        ).encode("utf-8")

    # job record
    files["job_record.json"] = json.dumps(
        rec.to_dict(), ensure_ascii=False, indent=2, default=str,
    ).encode("utf-8")

    # job summary (build_run_summary)
    try:
        from lab_executor.observation import build_run_summary
        steps = store.list_steps(job_id)
        target_runs = store.list_target_runs(job_id)
        monitor_count = 0
        try:
            monitor_count = store.count_monitor_data(job_id)
        except Exception:
            pass
        summary = build_run_summary(
            rec.to_dict(), steps, target_runs,
            monitor_count=monitor_count,
        )
        files["job_summary.json"] = json.dumps(
            summary, ensure_ascii=False, indent=2, default=str,
        ).encode("utf-8")
    except Exception:
        pass

    # timeline (job_events all)
    try:
        events = store.list_events(job_id, limit=100000)
    except Exception:
        events = []
    timeline_lines = [
        json.dumps(e, ensure_ascii=False, default=str) for e in events
    ]
    files["timeline.jsonl"] = (
        "\n".join(timeline_lines) + ("\n" if timeline_lines else "")
    ).encode("utf-8")

    # results (json rows + csv)
    try:
        rows = _extract_result_rows(
            job_mgr, job_id, include_monitor=include_monitor_data,
        )
    except Exception:
        rows = []
    files["results.jsonl"] = (
        "\n".join(
            json.dumps({k: r.get(k) for k in RESULT_COLUMNS},
                        ensure_ascii=False, default=str)
            for r in rows
        ) + ("\n" if rows else "")
    ).encode("utf-8")
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(RESULT_COLUMNS))
    writer.writeheader()
    for r in rows:
        writer.writerow({k: r.get(k, "") for k in RESULT_COLUMNS})
    files["results.csv"] = buf.getvalue().encode("utf-8")

    # monitor_data (optional)
    if include_monitor_data:
        try:
            mcount = store.count_monitor_data(job_id)
            md = store.list_monitor_data(
                job_id, limit=min(max(mcount, 1), 100000),
            )
        except Exception:
            md = []
        files["monitor_data.jsonl"] = (
            "\n".join(
                json.dumps(m, ensure_ascii=False, default=str)
                for m in md
            ) + ("\n" if md else "")
        ).encode("utf-8")

    # audit (optional)
    if include_audit and getattr(job_mgr, "audit", None) is not None:
        try:
            aud_events, _ = job_mgr.audit.query(
                job_id=job_id, limit=5000, include_details=True,
            )
        except Exception:
            aud_events = []
        files["audit.jsonl"] = (
            "\n".join(
                json.dumps(e, ensure_ascii=False, default=str)
                for e in aud_events
            ) + ("\n" if aud_events else "")
        ).encode("utf-8")

    # manifest (checksums を計算)
    from visa_mcp import __version__ as VMV
    checksums = {
        name: hashlib.sha256(blob).hexdigest()
        for name, blob in files.items()
    }
    manifest = {
        "bundle_version": "1.0",
        "visa_mcp_version": VMV,
        "job_id": job_id,
        "created_at": datetime.now(timezone.utc).isoformat(
            timespec="seconds",
        ),
        "include_monitor_data": include_monitor_data,
        "include_audit": include_audit,
        "contents": sorted(files.keys()),
        "checksums": checksums,
        "note": (
            "再検証 / 共有 / 監査 / 記事化用パッケージ。"
            "v1.x では別環境での完全再現実行はサポートしない"
        ),
    }
    files["manifest.json"] = json.dumps(
        manifest, ensure_ascii=False, indent=2,
    ).encode("utf-8")
    return files


def register_tools(mcp: FastMCP, job_mgr: JobManager) -> None:

    @mcp.tool()
    async def get_experiment_results(
        job_id: str,
        limit: int = 1000,
        offset: int = 0,
        include_monitor_data: bool = False,
        instrument: str = "",
        sweep_index: int | None = None,
        measurement: str = "",
    ) -> dict:
        """**(experimental, v0.9.1)** Job の測定結果を少量確認用 JSON で取得

        job_steps / target_runs に保存された最終的な測定結果を、標準 columns
        (timestamp / target_id / instrument / measurement / value / unit /
        step_index / step_path) に正規化して返す。

        - **monitor_data はデフォルト除外** (大量データを混ぜないため)。
          詳細時系列は `get_monitor_data` を使ってください。
        - limit 上限 10000 (`get_monitor_data` と同じクランプ)。
        - 大量データは `export_experiment_results` (CSV/JSONL) を使ってください。

        Returns: data.{columns, rows, pagination}
        """
        try:
            job_mgr.get(job_id)
        except Exception:
            return make_envelope(
                "error",
                errors=[make_error("not_found",
                    f"job not found: {job_id}", recoverable=False)],
            )

        # limit クランプ
        if limit <= 0:
            limit = 1000
        clamp_warning = None
        if limit > 10000:
            clamp_warning = f"limit={limit} は上限 10000 にクランプ"
            limit = 10000
        if offset < 0:
            offset = 0

        try:
            all_rows = _extract_result_rows(
                job_mgr, job_id, include_monitor=include_monitor_data,
            )
            all_rows = _filter_rows(
                all_rows,
                instrument=instrument or None,
                sweep_index=sweep_index,
                measurement=measurement or None,
            )
        except Exception as e:
            return make_envelope(
                "error",
                errors=[make_error("internal", str(e), recoverable=False)],
            )

        total = len(all_rows)
        end = offset + limit
        page = all_rows[offset:end]
        has_more = end < total

        # v2.13.3 fix sentinel: response に server version を埋め込み、
        # client が rows=0 を見たとき「自分の MCP server がどのバージョン
        # の export 経路を走らせているか」を即座に判定できるようにする。
        # この _meta.versions.export_fix が "v2.13.3" 以上なら本修正適用済み。
        try:
            import lab_executor as _le
            _versions = {
                "lab_executor": getattr(_le, "__version__", "?"),
                "export_fix": "v2.13.3",
            }
            try:
                import visa_mcp as _vm
                _versions["visa_mcp"] = getattr(_vm, "__version__", "?")
            except Exception:
                pass
        except Exception:
            _versions = {"export_fix": "v2.13.3"}

        data: dict = {
            "job_id": job_id,
            "columns": list(RESULT_COLUMNS),
            "rows": page,
            "pagination": {
                "limit": limit, "offset": offset,
                "returned": len(page), "total": total,
                "has_more": has_more,
            },
            "include_monitor_data": include_monitor_data,
            "filters": {
                "instrument": instrument or None,
                "sweep_index": sweep_index,
                "measurement": measurement or None,
            },
            "_meta": {"versions": _versions},
        }
        if clamp_warning:
            data["clamp_warning"] = clamp_warning
        return make_envelope("ok", data=data, job_id=job_id)

    @mcp.tool()
    async def export_experiment_results(
        job_id: str,
        format: str = "csv",
        include_monitor_data: bool = False,
        output_path: str = "",
        overwrite: bool = False,
        instrument: str = "",
        sweep_index: int | None = None,
        measurement: str = "",
    ) -> dict:
        """**(experimental, v0.9.1)** Job の測定結果を CSV / JSONL ファイル出力

        format: "csv" | "jsonl"
        include_monitor_data: True で monitor_data も含める
        output_path: 既定 `~/.visa-mcp/exports/` 配下。絶対パスや .. は拒否。
        overwrite: True で既存ファイルを上書き

        安全策 (P0):
          - default export dir 配下のみ許可
          - path traversal (.. / 絶対パス) 拒否 (invalid_export_path)
          - 既存ファイル overwrite=False で拒否

        Returns: data.{job_id, format, path, rows, sha256, include_monitor_data}
        """
        try:
            job_mgr.get(job_id)
        except Exception:
            return make_envelope(
                "error",
                errors=[make_error("not_found",
                    f"job not found: {job_id}", recoverable=False)],
            )

        if format not in ("csv", "jsonl"):
            # v0.9.1.1: sub_class から独立 error_class に昇格 (レビュー指摘 P1)
            return make_envelope(
                "error",
                errors=[make_error(
                    "unsupported_export_format",
                    f"unsupported export format: {format!r} (csv/jsonl のみ対応)",
                    recoverable=True,
                    details={"requested_format": format,
                             "supported_formats": ["csv", "jsonl"]},
                    recommended_next_actions=[
                        {"action": "use_csv_format",
                         "reason": "format='csv' で再試行"},
                        {"action": "use_jsonl_format",
                         "reason": "format='jsonl' で再試行"},
                    ],
                )],
            )

        default_name = f"{job_id}_results.{format}"
        path, err = _safe_export_path(
            output_path or None, default_filename=default_name,
            overwrite=overwrite,
        )
        if err is not None:
            return make_envelope(
                "error",
                errors=[make_error(
                    err["error_class"], err["message"],
                    recoverable=err.get("recoverable", True),
                    details=err.get("details"),
                    recommended_next_actions=err.get(
                        "recommended_next_actions"),
                )],
            )
        assert path is not None

        try:
            rows = _extract_result_rows(
                job_mgr, job_id, include_monitor=include_monitor_data,
            )
            rows = _filter_rows(
                rows,
                instrument=instrument or None,
                sweep_index=sweep_index,
                measurement=measurement or None,
            )
        except Exception as e:
            return make_envelope(
                "error",
                errors=[make_error("export_failed", str(e), recoverable=False)],
            )

        # 出力
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if format == "csv":
                buf = io.StringIO()
                writer = csv.DictWriter(buf, fieldnames=list(RESULT_COLUMNS))
                writer.writeheader()
                for r in rows:
                    writer.writerow({k: r.get(k, "") for k in RESULT_COLUMNS})
                text = buf.getvalue()
            else:  # jsonl
                lines = []
                for r in rows:
                    obj = {k: r.get(k) for k in RESULT_COLUMNS}
                    lines.append(json.dumps(obj, ensure_ascii=False, default=str))
                text = "\n".join(lines) + ("\n" if lines else "")
            data_bytes = text.encode("utf-8")
            path.write_bytes(data_bytes)
        except Exception as e:
            return make_envelope(
                "error",
                errors=[make_error("export_failed", str(e), recoverable=False)],
            )

        sha = hashlib.sha256(data_bytes).hexdigest()

        return make_envelope(
            "ok",
            data={
                "job_id": job_id,
                "format": format,
                "path": str(path),
                "rows": len(rows),
                "size_bytes": len(data_bytes),
                "sha256": sha,
                "include_monitor_data": include_monitor_data,
                "columns": list(RESULT_COLUMNS),
                "filters": {
                    "instrument": instrument or None,
                    "sweep_index": sweep_index,
                    "measurement": measurement or None,
                },
            },
            job_id=job_id,
        )

    # ====================================================
    # v1.0: export_experiment_bundle (experimental)
    # ====================================================

    @mcp.tool()
    async def export_experiment_bundle(
        job_id: str,
        output_path: str = "",
        include_monitor_data: bool = False,
        include_audit: bool = False,
        overwrite: bool = False,
    ) -> dict:
        """**(experimental, v1.0)** Job の実験記録を再現性 bundle (zip) へ

        bundle の目的: **再検証・共有・監査・記事化**。
        ⚠ v1.x では別環境での完全再現実行 (import / replay) はサポートしない。

        bundle 内容:
        ```
        bundle.zip
        ├── manifest.json          (bundle_version / visa_mcp_version / contents / checksums)
        ├── plan.json              (DSL plan original)
        ├── compiled_summary.json  (compile summary)
        ├── job_record.json        (jobs テーブル row)
        ├── job_summary.json       (build_run_summary)
        ├── timeline.jsonl         (job_events 全件)
        ├── results.jsonl          (測定結果)
        ├── results.csv            (同上 CSV 版)
        ├── monitor_data.jsonl     (include_monitor_data=True のみ)
        └── audit.jsonl            (include_audit=True のみ)
        ```

        manifest.contents 各エントリには SHA-256 が含まれる。

        path 安全策は `export_experiment_results` と同等
        (default dir / path traversal 拒否 / overwrite=False 既定)。
        """
        try:
            job_mgr.get(job_id)
        except Exception:
            return make_envelope(
                "error",
                errors=[make_error("not_found",
                    f"job not found: {job_id}", recoverable=False)],
            )

        default_name = f"{job_id}_bundle.zip"
        path, err = _safe_export_path(
            output_path or None, default_filename=default_name,
            overwrite=overwrite,
        )
        if err is not None:
            return make_envelope(
                "error",
                errors=[make_error(
                    err["error_class"], err["message"],
                    recoverable=err.get("recoverable", True),
                    details=err.get("details"),
                )],
            )
        assert path is not None

        try:
            # v2.25.0: bundle 生成コアを build_bundle_files に抽出。
            # manifest.json (checksums 込み) まで含めて構築される。
            files = build_bundle_files(
                job_mgr, job_id,
                include_monitor_data=include_monitor_data,
                include_audit=include_audit,
            )

            # zip 出力
            path.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(
                path, "w", compression=zipfile.ZIP_DEFLATED,
            ) as zf:
                for name, blob in files.items():
                    zf.writestr(name, blob)
        except Exception as e:
            logger.exception("export_experiment_bundle 失敗")
            return make_envelope(
                "error",
                errors=[make_error("export_failed", str(e),
                                    recoverable=False)],
            )

        bundle_bytes = path.read_bytes()
        return make_envelope(
            "ok",
            data={
                "job_id": job_id,
                "bundle_version": "1.0",
                "path": str(path),
                "size_bytes": len(bundle_bytes),
                "sha256": hashlib.sha256(bundle_bytes).hexdigest(),
                "contents": sorted(files.keys()),
                "include_monitor_data": include_monitor_data,
                "include_audit": include_audit,
            },
            job_id=job_id,
        )

    # ====================================================
    # v1.1: validate / inspect bundle (experimental, read-only)
    # ====================================================

    SUPPORTED_BUNDLE_VERSIONS = ("1.0",)
    # plan.json は DSL Job 由来でないと作られないため必須から外す
    REQUIRED_BUNDLE_FILES = (
        "manifest.json", "job_record.json", "timeline.jsonl",
        "results.jsonl", "results.csv",
    )

    def _read_bundle_safe(p: Path) -> tuple[zipfile.ZipFile | None, dict | None]:
        """安全に bundle を開く (path 内に存在するかチェック)"""
        if not p.exists():
            return None, {
                "error_class": "not_found",
                "message": f"bundle not found: {p}",
            }
        try:
            zf = zipfile.ZipFile(p, "r")
            return zf, None
        except (zipfile.BadZipFile, OSError) as e:
            return None, {
                "error_class": "validation",
                "message": f"invalid bundle file: {e}",
                "details": {"sub_class": "invalid_bundle_format"},
            }

    @mcp.tool()
    async def validate_experiment_bundle(path: str) -> dict:
        """**(experimental, v1.1)** bundle zip の整合性を実行なしに検証

        チェック項目:
          - bundle が読める zip である
          - manifest.json が存在し JSON として読める
          - bundle_version が `SUPPORTED_BUNDLE_VERSIONS` に含まれる
          - 必須 files (`manifest.json` / `plan.json` / `job_record.json` /
            `timeline.jsonl` / `results.jsonl` / `results.csv`) が揃う
          - manifest.checksums の各 sha256 が zip 内 file の実 sha256 と一致
          - visa_mcp_version が記録されている

        Returns:
          `data.{bundle_valid, bundle_version, job_id, files_checked,
                checksum_errors, missing_files, warnings}`
        """
        p = Path(path).expanduser()
        zf, err = _read_bundle_safe(p)
        if err is not None:
            return make_envelope(
                "error",
                errors=[make_error(
                    err["error_class"], err["message"],
                    recoverable=False,
                    details=err.get("details"),
                )],
            )
        assert zf is not None
        warnings: list[dict] = []
        checksum_errors: list[dict] = []
        missing_files: list[str] = []
        manifest: dict = {}
        try:
            names = set(zf.namelist())
            if "manifest.json" not in names:
                zf.close()
                return make_envelope(
                    "error",
                    errors=[make_error(
                        "validation",
                        "manifest.json が bundle 内に存在しません",
                        recoverable=False,
                        details={"sub_class": "missing_manifest"},
                    )],
                )
            try:
                manifest = json.loads(zf.read("manifest.json"))
            except Exception as e:
                zf.close()
                return make_envelope(
                    "error",
                    errors=[make_error(
                        "validation",
                        f"manifest.json が JSON として読めません: {e}",
                        recoverable=False,
                        details={"sub_class": "invalid_manifest"},
                    )],
                )

            bv = manifest.get("bundle_version")
            if bv not in SUPPORTED_BUNDLE_VERSIONS:
                warnings.append({
                    "warning_class": "version_mismatch",
                    "message": (
                        f"bundle_version={bv!r} は SUPPORTED_BUNDLE_VERSIONS"
                        f" {list(SUPPORTED_BUNDLE_VERSIONS)} の範囲外"
                    ),
                })

            for req in REQUIRED_BUNDLE_FILES:
                if req not in names:
                    missing_files.append(req)

            # checksums 検証
            for name, expected in (manifest.get("checksums") or {}).items():
                if name not in names:
                    checksum_errors.append({
                        "file": name, "reason": "missing_in_zip",
                        "expected": expected,
                    })
                    continue
                actual = hashlib.sha256(zf.read(name)).hexdigest()
                if actual != expected:
                    checksum_errors.append({
                        "file": name, "reason": "sha256_mismatch",
                        "expected": expected, "actual": actual,
                    })

            if not manifest.get("visa_mcp_version"):
                warnings.append({
                    "warning_class": "missing_visa_mcp_version",
                    "message": "manifest に visa_mcp_version が無い",
                })
        finally:
            zf.close()

        bundle_valid = not (missing_files or checksum_errors)
        files_checked = len((manifest.get("checksums") or {}))
        data: dict = {
            "bundle_valid": bundle_valid,
            "bundle_version": manifest.get("bundle_version"),
            "visa_mcp_version": manifest.get("visa_mcp_version"),
            "job_id": manifest.get("job_id"),
            "files_checked": files_checked,
            "checksum_errors": checksum_errors,
            "missing_files": missing_files,
            "warnings": warnings,
        }
        # errors[] は致命的整合性違反のみ
        envelope_errors: list[dict] = []
        if missing_files:
            envelope_errors.append(make_error(
                "validation",
                f"必須ファイルが不足: {missing_files}",
                recoverable=True,
                details={"sub_class": "missing_required_files",
                          "missing": missing_files},
            ))
        if checksum_errors:
            envelope_errors.append(make_error(
                "validation",
                f"checksum 不一致が {len(checksum_errors)} 件",
                recoverable=False,
                details={"sub_class": "checksum_mismatch",
                          "checksum_errors": checksum_errors},
            ))
        status = "ok" if bundle_valid else "error"
        return make_envelope(
            status, data=data,
            errors=envelope_errors if envelope_errors else None,
        )

    @mcp.tool()
    async def inspect_experiment_bundle(
        path: str,
        include_plan: bool = True,
        include_summary: bool = True,
    ) -> dict:
        """**(experimental, v1.1)** bundle 中身を実行なしに要約取得

        `validate_experiment_bundle` より弱い検証 (checksum 検証はしない) で、
        bundle 内容のサマリを返す:

          - manifest (bundle_version / visa_mcp_version / job_id / contents)
          - plan (optional)
          - job_summary (optional)
          - result rows 行数
          - audit / monitor_data が含まれるか

        import / replay は **行わない**。analysis-only。
        """
        p = Path(path).expanduser()
        zf, err = _read_bundle_safe(p)
        if err is not None:
            return make_envelope(
                "error",
                errors=[make_error(
                    err["error_class"], err["message"],
                    recoverable=False,
                    details=err.get("details"),
                )],
            )
        assert zf is not None
        data: dict = {}
        try:
            names = zf.namelist()
            data["files"] = sorted(names)
            try:
                manifest = json.loads(zf.read("manifest.json"))
            except Exception:
                manifest = {}
            data["manifest"] = {
                "bundle_version": manifest.get("bundle_version"),
                "visa_mcp_version": manifest.get("visa_mcp_version"),
                "job_id": manifest.get("job_id"),
                "created_at": manifest.get("created_at"),
                "contents": manifest.get("contents"),
                "include_monitor_data": manifest.get("include_monitor_data"),
                "include_audit": manifest.get("include_audit"),
            }
            data["has_audit"] = "audit.jsonl" in names
            data["has_monitor_data"] = "monitor_data.jsonl" in names

            if include_plan and "plan.json" in names:
                try:
                    plan = json.loads(zf.read("plan.json"))
                    data["plan"] = {
                        "dsl_version": plan.get("dsl_version"),
                        "name": plan.get("name"),
                        "unit": plan.get("unit"),
                        "step_count": len(plan.get("steps") or []),
                    }
                except Exception:
                    data["plan"] = None
            if include_summary and "job_summary.json" in names:
                try:
                    data["job_summary"] = json.loads(zf.read("job_summary.json"))
                except Exception:
                    data["job_summary"] = None

            # result rows 数 (jsonl line count)
            if "results.jsonl" in names:
                try:
                    txt = zf.read("results.jsonl").decode("utf-8")
                    data["result_row_count"] = sum(
                        1 for line in txt.splitlines() if line.strip()
                    )
                except Exception:
                    data["result_row_count"] = None

            # compatibility judgment (v1.1.1: AI エージェントが誤って
            # "replay できる" と解釈しないよう can_be_replayed=false を明示)
            warnings: list[dict] = []
            bv = (manifest or {}).get("bundle_version")
            bv_supported = bv in SUPPORTED_BUNDLE_VERSIONS
            if not bv_supported:
                warnings.append({
                    "warning_class": "version_mismatch",
                    "message": (
                        f"bundle_version={bv!r} は現バージョン support 範囲外"
                    ),
                })
            data["compatibility"] = {
                "bundle_version_supported": bv_supported,
                "created_by_current_major_version": True,
                "can_be_validated": True,
                "can_be_replayed": False,
                "reason": (
                    "Replay / import is not implemented in v1.1. "
                    "Use validate_experiment_bundle / inspect_experiment_bundle "
                    "for analysis only."
                ),
            }
            data["warnings"] = warnings
        finally:
            zf.close()
        return make_envelope("ok", data=data)
