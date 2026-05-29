"""v2.14.3: Codex v2.14.2 レビュー対応テスト (P3 重複 close 削除)."""
from __future__ import annotations
import inspect
from pathlib import Path


def test_jobstore_close_single_definition():
    """v2.14.3: store.py に `close` が 1 つだけ定義されていること
    (v2.14.2 までは line 318 / 1048 の 2 箇所に重複)。"""
    from lab_executor.job import store as store_mod
    src = Path(store_mod.__file__).read_text(encoding="utf-8")
    close_defs = [
        i for i, line in enumerate(src.splitlines(), start=1)
        if line.lstrip().startswith("def close(self)")
    ]
    assert len(close_defs) == 1, (
        f"`def close(self)` が複数定義されている (行: {close_defs})")


def test_jobstore_close_authoritative_implementation(job_store):
    """authoritative impl は __enter__ / __exit__ を伴う方
    (line ~318)。close 後の再利用 (lazy reconnect) も動くこと。"""
    job_store.close()
    # 再利用 OK (lazy reconnect)
    job_store._connect().execute("SELECT 1")
    job_store.close()


def test_v2_14_3_version():
    import lab_executor
    parts = lab_executor.__version__.split(".")
    assert tuple(int(p) for p in parts[:3]) >= (2, 14, 3)
