"""Web UI M4: UI プロセス側のコントロールプレーン client。

UI プロセスは実行系操作を **自分では行わず**、serve プロセス内のコントロール
プレーンへ **プロキシ** する。この client は ``control.json`` を読んで token を
取得し (ブラウザには渡さない)、``urllib`` で HTTP 転送する。

- ``available()``: control.json 読み → ``/control/health`` を token 付きで叩き、
  2xx なら info を返す。無い / 接続失敗 / 401 は None。timeout 2s。
- ``cancel(job_id, mode, timeout_s)`` / ``start_recipe(...)``: 転送し
  ``(status_code, json_body)`` を返す。接続不可は ``(503, {...})``。

必須依存を増やさないため標準ライブラリ ``urllib`` のみを使う。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from lab_executor.control_plane import default_control_path, read_control_file

_DEFAULT_TIMEOUT_S = 2.0


class ControlClient:
    """control.json 経由でコントロールプレーンへ転送する薄い client。"""

    def __init__(
        self,
        path: Path | str | None = None,
        *,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
    ) -> None:
        self._path = Path(path) if path is not None else default_control_path()
        self._timeout_s = timeout_s

    # ---- discovery ----

    def _load(self) -> dict | None:
        return read_control_file(self._path)

    def available(self) -> dict | None:
        """control.json が有効で /control/health が 2xx なら info を返す。

        戻り値: ``{"backend_id": ..., "pid": ..., "started_at": ...}`` または
        None (control.json 無し / 接続失敗 / 401 / 非 2xx)。
        """
        info = self._load()
        if info is None:
            return None
        status, body = self._request(
            "GET", info, "/control/health", None,
        )
        if status == 200 and isinstance(body, dict) and body.get("ok"):
            return {
                "backend_id": body.get("backend_id"),
                "pid": body.get("pid"),
                "started_at": body.get("started_at"),
            }
        return None

    # ---- 転送 ----

    def cancel(
        self,
        job_id: str,
        cancel_mode: str = "after_current_step",
        timeout_s: float = 30.0,
        *,
        owner: str = "web-ui",
    ) -> tuple[int, dict]:
        info = self._load()
        if info is None:
            return 503, {
                "error": "control_unavailable",
                "detail": "control.json が見つかりません",
            }
        return self._request(
            "POST",
            info,
            f"/control/jobs/{job_id}/cancel",
            {
                "cancel_mode": cancel_mode,
                "timeout_s": timeout_s,
                "owner": owner,
            },
        )

    def start_recipe(
        self,
        resource_name: str,
        recipe_name: str,
        parameters: dict | None = None,
        *,
        owner: str = "web-ui",
        job_timeout_s: float | None = None,
    ) -> tuple[int, dict]:
        info = self._load()
        if info is None:
            return 503, {
                "error": "control_unavailable",
                "detail": "control.json が見つかりません",
            }
        payload: dict[str, Any] = {
            "resource_name": resource_name,
            "recipe_name": recipe_name,
            "parameters": parameters or {},
            "owner": owner,
        }
        if job_timeout_s is not None:
            payload["job_timeout_s"] = job_timeout_s
        return self._request(
            "POST", info, "/control/jobs/start-recipe", payload,
        )

    # ---- 内部: HTTP ----

    def _request(
        self,
        method: str,
        info: dict,
        path: str,
        body: dict | None,
    ) -> tuple[int, dict]:
        url = info["url"].rstrip("/") + path
        token = info["token"]
        data = None
        headers = {"X-Control-Token": token}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            url, data=data, method=method, headers=headers,
        )
        try:
            with urllib.request.urlopen(
                req, timeout=self._timeout_s
            ) as resp:
                raw = resp.read().decode("utf-8")
                return resp.status, _parse_json(raw)
        except urllib.error.HTTPError as exc:
            raw = ""
            try:
                raw = exc.read().decode("utf-8")
            except Exception:  # noqa: BLE001
                raw = ""
            return exc.code, _parse_json(raw)
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            return 503, {
                "error": "control_unreachable",
                "detail": str(exc),
            }


def _parse_json(raw: str) -> dict:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return {"raw": raw}
    return parsed if isinstance(parsed, dict) else {"data": parsed}
