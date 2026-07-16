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
import ctypes
import hashlib
import json
import os
import shutil
import signal
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


def _parse_worker_response(
    returncode: int, stdout: bytes, stderr_bytes: bytes,
) -> dict:
    """ワーカー応答を検証してデコードする。"""
    if returncode != 0 and not stdout:
        # アクセス違反等でワーカーが死んだ (JSON 応答なし)
        stderr = stderr_bytes.decode("utf-8", "replace")[-2000:]
        raise CodeExecError(
            "worker_crashed",
            f"ワーカープロセスが異常終了しました (exit={returncode})。"
            "DLL のアクセス違反やインタプリタクラッシュの可能性があります",
            stderr,
        )
    try:
        resp = json.loads(stdout.decode("utf-8"))
    except Exception:
        stderr = stderr_bytes.decode("utf-8", "replace")[-2000:]
        raise CodeExecError(
            "protocol_error",
            f"ワーカー応答の JSON 解析に失敗しました (exit={returncode})",
            stderr,
        )
    if not resp.get("ok"):
        raise CodeExecError(
            resp.get("error", "worker_error"),
            resp.get("message", "?"),
            resp.get("traceback", ""),
        )
    return resp


def _os_kill_win(pid: int) -> None:
    """Windows: pid を OpenProcess+TerminateProcess で直接落とす (権威的)。

    ``communicate()`` を cancel すると asyncio の subprocess transport は
    プロセスをまだ生きているのに「終了 (returncode=0)」と誤検知し、以降
    ``proc.kill()`` が ProcessLookupError を投げて効かなくなる (Python 3.14
    Proactor)。さらに taskkill は生成直後の pid 可視化前ウィンドウで "not
    found" を返すことがある。OpenProcess による直接 kill はこの双方に依存
    しないため、ワーカーを確実に終了できる。既に終了/アクセス不可なら黙認。
    """
    PROCESS_TERMINATE = 0x0001
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, int(pid))
    if not handle:
        return  # 既に終了済み、またはアクセス権なし
    try:
        kernel32.TerminateProcess(handle, 1)
    finally:
        kernel32.CloseHandle(handle)


def _os_kill(pid: int) -> None:
    """プラットフォーム別にプロセス (POSIX はグループ) を確実に落とす。"""
    if sys.platform == "win32":
        _os_kill_win(pid)
    else:
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass


async def _terminate_worker(proc: asyncio.subprocess.Process) -> None:
    """ワーカー (と子孫) を終了し、必ず回収する。

    順序が要: まず ``_os_kill`` (Windows は OpenProcess+TerminateProcess、
    POSIX は killpg) でワーカー本体を **即座に** 権威的に落とす。これは
    transport の誤検知や taskkill のレースに依存しない。続けて taskkill /T
    (Windows) を best-effort で流し、ユーザコードが spawn した子孫を回収する
    (親を先に落とすため取りこぼしはあり得るが、taskkill は数秒かかることが
    あり authoritative kill を遅延させてはならないので後段に置く)。
    """
    if proc.returncode is not None:
        return
    pid = proc.pid
    _os_kill(pid)
    if sys.platform == "win32":
        try:
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/PID", str(pid), "/T", "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(killer.wait(), timeout=5.0)
        except (OSError, asyncio.TimeoutError):
            pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=5.0)
    except asyncio.TimeoutError:
        _os_kill(pid)
        await proc.wait()


async def _run_worker(request: dict, timeout_s: float) -> dict:
    """1リクエストを実行し、cancel/timeout時はワーカーを終了する。"""
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    # プロセス生成中 (create_subprocess_exec の await 中) に cancel が届くと、
    # OS が生成した子プロセスの参照を失い孤児化する。生成コルーチンを shield し、
    # cancel された場合でも生成完了を待ってから確実に終了・回収する。
    spawn = asyncio.ensure_future(asyncio.create_subprocess_exec(
        sys.executable, "-m", "lab_executor.code_worker",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **kwargs,
    ))
    try:
        proc = await asyncio.shield(spawn)
    except asyncio.CancelledError:
        # spawn は shield 済みで生成は継続している。完了を待って回収する。
        proc = await spawn
        await asyncio.shield(_terminate_worker(proc))
        raise
    payload = json.dumps(request, ensure_ascii=False).encode("utf-8")
    try:
        stdout, stderr_bytes = await asyncio.wait_for(
            proc.communicate(payload), timeout=timeout_s,
        )
    except asyncio.TimeoutError:
        await asyncio.shield(_terminate_worker(proc))
        raise CodeExecError(
            "timeout",
            f"コード実行が timeout_s ({timeout_s}s) を超過しました "
            "(ワーカーは強制終了されました)",
        )
    except asyncio.CancelledError:
        await asyncio.shield(_terminate_worker(proc))
        raise
    return _parse_worker_response(proc.returncode or 0, stdout, stderr_bytes)


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
        resp = await _run_worker(request, timeout_s)
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
        resp = await _run_worker(request, timeout_s)
        return {
            "result": _decode_from_worker(resp.get("result")),
            "outputs": {
                k: _decode_from_worker(v)
                for k, v in (resp.get("outputs") or {}).items()
            },
        }
    finally:
        shutil.rmtree(npy_dir, ignore_errors=True)
