"""
py / dll ステップの subprocess ワーカー (v2.32.0 SP-6)

``python -m lab_executor.code_worker`` として親プロセス (code_exec.py) から
起動され、stdin から JSON リクエストを 1 件読み、結果 JSON を stdout に書く。

プロトコル (JSON / stdin-stdout 方式 — Windows 前提で multiprocessing を避け、
pickle 依存や spawn の import 副作用を持ち込まない):

- py:  {"mode": "py", "code": str|null, "file": str|null,
        "inputs": {...}, "outputs": [...], "params": {...}, "env": {...},
        "npy_dir": str}
- dll: {"mode": "dll", "path": str, "function": str, "argtypes": [...],
        "restype": str, "args": [...], "out_args": {"<index>": "<名前>"},
        "npy_dir": str}

ndarray は JSON に直接載せず、``{"__npy__": "<path>"}`` として npy ファイル
経由で受け渡す (npy_dir は親が管理する一時ディレクトリ)。

応答: {"ok": true, "outputs": {...}, "result": ...}
      {"ok": false, "error": "<class>", "message": str, "traceback": str}

**注意**: この分離は安定性 (クラッシュ・ハング・メモリ暴走からランタイムを
守る) のためであり、**セキュリティサンドボックスではない** (spec §6.1)。
"""
from __future__ import annotations

import ctypes
import json
import sys
import traceback
from pathlib import Path
from typing import Any

import numpy as np


def _decode_value(v: Any) -> Any:
    """JSON 値 → Python 値 (npy 参照は ndarray へ)。"""
    if isinstance(v, dict) and "__npy__" in v:
        return np.load(v["__npy__"], allow_pickle=False)
    return v


def _encode_value(v: Any, npy_dir: str, name: str) -> Any:
    """Python 値 → JSON 値 (ndarray は npy へ)。許可外の型は TypeError。"""
    if isinstance(v, bool) or isinstance(v, (int, float, str)):
        return v
    if isinstance(v, np.generic):
        return v.item()
    if isinstance(v, np.ndarray):
        path = str(Path(npy_dir) / f"out_{name}.npy")
        np.save(path, np.ascontiguousarray(v), allow_pickle=False)
        return {"__npy__": path}
    raise TypeError(
        f"サポートされない出力型: {type(v).__name__} "
        "(float/int/bool/str/ndarray のみ)"
    )


# ============================================================
# py 実行
# ============================================================


def _run_py(req: dict) -> dict:
    inputs = {k: _decode_value(v) for k, v in (req.get("inputs") or {}).items()}
    # 実行契約: ctx = inputs のローカル名 + params / env の読み取りコピー
    ctx: dict[str, Any] = dict(inputs)
    ctx["params"] = dict(req.get("params") or {})
    ctx["env"] = dict(req.get("env") or {})

    namespace: dict[str, Any] = {"ctx": ctx, "out": {}}
    file_path = req.get("file")
    if file_path:
        code = Path(file_path).read_text(encoding="utf-8")
    else:
        code = req.get("code") or ""

    # source_nameはtraceback表示専用。verified file stepは親が検証したcode bytesを
    # 渡すため、worker側で元pathを再openしない。
    source_name = req.get("source_name") or file_path or "<py step>"
    exec(compile(code, source_name, "exec"), namespace)  # noqa: S102

    out = namespace.get("out")
    # file 型は def main(ctx) -> dict も可 (戻り値が out になる)
    main_fn = namespace.get("main")
    if callable(main_fn):
        ret = main_fn(ctx)
        if not isinstance(ret, dict):
            raise TypeError("main(ctx) は dict を返す必要があります")
        out = ret
    if not isinstance(out, dict):
        raise TypeError("out は dict である必要があります")

    npy_dir = req["npy_dir"]
    outputs: dict[str, Any] = {}
    for name in req.get("outputs") or []:
        if name not in out:
            raise KeyError(
                f"outputs に宣言された '{name}' が out に存在しません"
            )
        outputs[name] = _encode_value(out[name], npy_dir, name)
    # outputs に列挙されなかった out キーは黙って捨てる (暗黙の全取り込みはしない)
    return {"ok": True, "outputs": outputs}


# ============================================================
# dll 実行
# ============================================================

_SCALAR_TYPES = {
    "double": ctypes.c_double,
    "float": ctypes.c_float,
    "int": ctypes.c_int,
    "long": ctypes.c_long,
    "int64": ctypes.c_int64,
    "size_t": ctypes.c_size_t,
    "bool": ctypes.c_bool,
    "char*": ctypes.c_char_p,
    "void": None,
}

_POINTER_DTYPES = {
    "double*": (ctypes.c_double, np.float64),
    "float*": (ctypes.c_float, np.float32),
    "int*": (ctypes.c_int, np.int32),
    "long*": (ctypes.c_long, np.int32),
    "int64*": (ctypes.c_int64, np.int64),
}


def _ctype_for(name: str):
    name = name.strip()
    if name in _POINTER_DTYPES:
        return ctypes.POINTER(_POINTER_DTYPES[name][0])
    if name in _SCALAR_TYPES:
        return _SCALAR_TYPES[name]
    raise ValueError(
        f"未対応の ctypes 型名: {name!r} "
        f"(許可: {sorted(_SCALAR_TYPES) + sorted(_POINTER_DTYPES)})"
    )


def _run_dll(req: dict) -> dict:
    path = req["path"]
    func_name = req["function"]
    argtypes = [str(t) for t in (req.get("argtypes") or [])]
    restype = str(req.get("restype") or "void")
    raw_args = req.get("args") or []
    out_args = {int(k): str(v) for k, v in (req.get("out_args") or {}).items()}
    npy_dir = req["npy_dir"]

    if len(raw_args) != len(argtypes):
        raise ValueError(
            f"args ({len(raw_args)}) と argtypes ({len(argtypes)}) の数が"
            "一致しません"
        )

    lib = ctypes.CDLL(str(path))
    try:
        func = getattr(lib, func_name)
    except AttributeError:
        raise AttributeError(f"DLL に関数 '{func_name}' が存在しません: {path}")

    func.argtypes = [_ctype_for(t) for t in argtypes]
    func.restype = _ctype_for(restype)

    # 引数マーシャリング (array は連続バッファ)
    call_args: list[Any] = []
    buffers: dict[int, np.ndarray] = {}
    for i, (t, v) in enumerate(zip(argtypes, raw_args)):
        v = _decode_value(v)
        if t in _POINTER_DTYPES:
            ct, dt = _POINTER_DTYPES[t]
            if not isinstance(v, np.ndarray):
                raise TypeError(
                    f"argtypes[{i}]={t} には array が必要です: {type(v).__name__}"
                )
            buf = np.ascontiguousarray(v, dtype=dt)
            buffers[i] = buf
            call_args.append(buf.ctypes.data_as(ctypes.POINTER(ct)))
        elif t == "char*":
            call_args.append(str(v).encode("utf-8"))
        elif t in ("int", "long", "int64", "size_t"):
            call_args.append(int(v))          # 式評価は float を返し得るため明示変換
        elif t in ("double", "float"):
            call_args.append(float(v))
        elif t == "bool":
            call_args.append(bool(v))
        else:
            call_args.append(v)

    result = func(*call_args)

    out: dict[str, Any] = {}
    # 書き換えバッファの回収 (out_args 宣言のみ)
    for idx, name in out_args.items():
        if idx not in buffers:
            raise ValueError(
                f"out_args のインデックス {idx} はポインタ引数ではありません"
            )
        out[name] = _encode_value(np.copy(buffers[idx]), npy_dir, name)

    encoded_result = None
    if func.restype is not None and result is not None:
        if isinstance(result, bytes):
            encoded_result = result.decode("utf-8", "replace")
        else:
            encoded_result = _encode_value(result, npy_dir, "__result__")
    return {"ok": True, "outputs": out, "result": encoded_result}


# ============================================================
# entry
# ============================================================


def main() -> int:
    try:
        # stdin/stdout は UTF-8 バイトで扱う (Windows の既定 cp932 では
        # 日本語を含む JSON の write が UnicodeEncodeError で壊れるため)。
        raw_in = sys.stdin.buffer.read().decode("utf-8")
        req = json.loads(raw_in)
        if req.get("mode") == "py":
            resp = _run_py(req)
        elif req.get("mode") == "dll":
            resp = _run_dll(req)
        else:
            resp = {
                "ok": False, "error": "InvalidRequest",
                "message": f"未知の mode: {req.get('mode')!r}",
                "traceback": "",
            }
    except SystemExit:
        raise
    except BaseException as e:  # noqa: BLE001 - ワーカーは全例外を JSON で返す
        resp = {
            "ok": False,
            "error": type(e).__name__,
            "message": str(e),
            "traceback": traceback.format_exc(),
        }
    sys.stdout.buffer.write(json.dumps(resp, ensure_ascii=False).encode("utf-8"))
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
