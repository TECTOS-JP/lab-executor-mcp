"""
py / dll ステップの親プロセス側実行 (v2.32.0 SP-6)

``code_worker`` を subprocess として起動し、JSON (stdin/stdout) で受け渡す。
timeout_s で強制終了。ndarray は一時ディレクトリの npy ファイル経由。

**注意 (信頼モデル, spec §6.1)**: subprocess 分離は安定性のため
(クラッシュ・ハング・メモリ暴走からランタイムを守る) であり、
**セキュリティサンドボックスではない**。ファイル・ネットワークへの
アクセスを技術的には妨げない。制御は code_policy (実行可否) と
timeline (来歴の完全記録) で行う。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import numpy as np


class CodeExecError(RuntimeError):
    """ワーカー実行の失敗 (timeout / ワーカー死 / プロトコル不正)。"""

    def __init__(self, error: str, message: str, traceback_text: str = ""):
        super().__init__(message)
        self.error = error
        self.message = message
        self.traceback_text = traceback_text


def _encode_for_worker(v: Any, npy_dir: Path, name: str) -> Any:
    if isinstance(v, np.generic):
        return v.item()
    if isinstance(v, np.ndarray):
        path = npy_dir / f"in_{name}.npy"
        np.save(str(path), np.ascontiguousarray(v), allow_pickle=False)
        return {"__npy__": str(path)}
    return v


def _decode_from_worker(v: Any) -> Any:
    if isinstance(v, dict) and "__npy__" in v:
        return np.load(v["__npy__"], allow_pickle=False)
    return v


def _run_worker_sync(request: dict, timeout_s: float) -> dict:
    """ワーカー subprocess を起動して 1 リクエストを処理する (同期)。"""
    proc = None
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "lab_executor.code_worker"],
            input=json.dumps(request, ensure_ascii=False).encode("utf-8"),
            capture_output=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        raise CodeExecError(
            "timeout",
            f"コード実行が timeout_s ({timeout_s}s) を超過しました "
            "(ワーカーは強制終了されました)",
        )
    if proc.returncode != 0 and not proc.stdout:
        # アクセス違反等でワーカーが死んだ (JSON 応答なし)
        stderr = proc.stderr.decode("utf-8", "replace")[-2000:]
        raise CodeExecError(
            "worker_crashed",
            f"ワーカープロセスが異常終了しました (exit={proc.returncode})。"
            "DLL のアクセス違反やインタプリタクラッシュの可能性があります",
            stderr,
        )
    try:
        resp = json.loads(proc.stdout.decode("utf-8"))
    except Exception:
        stderr = proc.stderr.decode("utf-8", "replace")[-2000:]
        raise CodeExecError(
            "protocol_error",
            f"ワーカー応答の JSON 解析に失敗しました (exit={proc.returncode})",
            stderr,
        )
    if not resp.get("ok"):
        raise CodeExecError(
            resp.get("error", "worker_error"),
            resp.get("message", "?"),
            resp.get("traceback", ""),
        )
    return resp


async def run_py(
    *,
    code: str | None,
    file_path: str | None,
    inputs: dict[str, Any],
    outputs: list[str],
    params: dict[str, Any],
    env: dict[str, Any],
    timeout_s: float,
    source_name: str | None = None,
) -> dict[str, Any]:
    """py ステップをワーカーで実行し、outputs (宣言分のみ) を返す。

    エラーは ``CodeExecError``。返り値の ndarray は復元済み。
    """
    npy_dir = Path(tempfile.mkdtemp(prefix="labexec_py_"))
    try:
        request = {
            "mode": "py",
            "code": code,
            "file": file_path,
            "source_name": source_name,
            "inputs": {
                k: _encode_for_worker(v, npy_dir, k)
                for k, v in inputs.items()
            },
            "outputs": list(outputs),
            "params": {k: _encode_for_worker(v, npy_dir, f"p_{k}")
                       for k, v in params.items()},
            "env": dict(env),
            "npy_dir": str(npy_dir),
        }
        resp = await asyncio.to_thread(_run_worker_sync, request, timeout_s)
        return {
            k: _decode_from_worker(v)
            for k, v in (resp.get("outputs") or {}).items()
        }
    finally:
        shutil.rmtree(npy_dir, ignore_errors=True)


async def run_dll(
    *,
    path: str,
    function: str,
    argtypes: list[str],
    restype: str,
    args: list[Any],
    out_args: dict[str, str],
    timeout_s: float,
    expected_sha256: str,
) -> dict[str, Any]:
    """dll 呼び出しをワーカーで実行する。

    返り値: {"result": <restype 値 | None>, "outputs": {out_args 名: ndarray}}
    アクセス違反によるワーカー死は ``CodeExecError(worker_crashed)`` として
    回収される (ランタイムは無事)。
    """
    npy_dir = Path(tempfile.mkdtemp(prefix="labexec_dll_"))
    try:
        # source pathをhash検査後にworkerが再openすると差し替え可能になる。
        # 一度だけ読んだbytesを照合し、その同じbytesのprivate copyをloadする。
        source = Path(path)
        try:
            dll_bytes = source.read_bytes()
        except OSError as e:
            raise CodeExecError(
                "integrity_error", f"DLLの読み込みに失敗: {source} ({e})",
            )
        actual = hashlib.sha256(dll_bytes).hexdigest()
        if actual != expected_sha256:
            raise CodeExecError(
                "integrity_error",
                f"DLLのsha256が実行直前に変化しました: {source}",
            )
        staged = npy_dir / f"verified{source.suffix or '.dll'}"
        staged.write_bytes(dll_bytes)
        request = {
            "mode": "dll",
            "path": str(staged),
            "function": function,
            "argtypes": list(argtypes),
            "restype": restype,
            "args": [
                _encode_for_worker(v, npy_dir, f"a{i}")
                for i, v in enumerate(args)
            ],
            "out_args": dict(out_args),
            "npy_dir": str(npy_dir),
        }
        resp = await asyncio.to_thread(_run_worker_sync, request, timeout_s)
        return {
            "result": _decode_from_worker(resp.get("result")),
            "outputs": {
                k: _decode_from_worker(v)
                for k, v in (resp.get("outputs") or {}).items()
            },
        }
    finally:
        shutil.rmtree(npy_dir, ignore_errors=True)
