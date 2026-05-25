"""
標準レスポンス形式 (v0.5.0)

v0.5.0+ で新規追加される MCP ツールはすべてこの envelope で返す。
既存ツール (v0.4.1 以前) は後方互換のため従来形式のまま (破壊的変更を避ける)。

設計:
- top-level `status` は最小に絞る: ok / error / partial_failure / running
- 詳細な分類は `errors[*].error_class` に逃がす
- 部分失敗 (Group / Map / Workflow ジョブ) は `partial_failure` で集約
- `metadata` には常に timestamp、必要に応じ elapsed_s / job_id 等
"""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Any, Literal

EnvelopeStatus = Literal["ok", "error", "partial_failure", "running"]
ErrorClass = Literal[
    "timeout",
    "safety",
    "protocol",
    "hardware",
    "validation",
    "not_found",
    "blocked",
    "internal",
    # v0.9.1.1: export 系を独立 error_class へ昇格
    "invalid_export_path",
    "export_failed",
    "unsupported_export_format",
    # v0.9.0: resume_job (将来 error_class taxonomy 拡張時に正式化)
    "resume_not_allowed",
]


def make_error(
    error_class: ErrorClass,
    message: str,
    *,
    instrument: str | None = None,
    target_id: str | None = None,
    recoverable: bool = True,
    details: dict[str, Any] | None = None,
    recommended_next_actions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """envelope の errors[*] に入れる 1 件のエラー dict を作る。"""
    e: dict[str, Any] = {
        "error_class": error_class,
        "message": message,
        "recoverable": recoverable,
    }
    if instrument is not None:
        e["instrument"] = instrument
    if target_id is not None:
        e["target_id"] = target_id
    if details:
        e["details"] = details
    if recommended_next_actions:
        e["recommended_next_actions"] = recommended_next_actions
    return e


def make_envelope(
    status: EnvelopeStatus,
    *,
    data: dict[str, Any] | None = None,
    errors: list[dict[str, Any]] | None = None,
    elapsed_s: float | None = None,
    job_id: str | None = None,
    extra_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    標準レスポンス envelope を生成する。

    例:
        make_envelope("ok", data={"job_id": "job_001"}, elapsed_s=0.5)
        make_envelope("error", errors=[make_error("timeout", "...")])
        make_envelope("partial_failure",
            data={"summary": {...}, "results": [...]},
            errors=[make_error("timeout", "...", target_id="sample057")])
    """
    metadata: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if elapsed_s is not None:
        metadata["elapsed_s"] = round(float(elapsed_s), 4)
    if job_id is not None:
        metadata["job_id"] = job_id
    if extra_metadata:
        metadata.update(extra_metadata)

    return {
        "status": status,
        "data": data or {},
        "errors": errors or [],
        "metadata": metadata,
    }


def is_envelope(resp: Any) -> bool:
    """与えられた dict が標準 envelope 形式かを判定 (テスト用)。"""
    if not isinstance(resp, dict):
        return False
    if "status" not in resp or "data" not in resp or "errors" not in resp or "metadata" not in resp:
        return False
    if resp["status"] not in ("ok", "error", "partial_failure", "running"):
        return False
    if not isinstance(resp["errors"], list):
        return False
    if "timestamp" not in resp.get("metadata", {}):
        return False
    return True
