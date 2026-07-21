"""v1.9.0: Separation Boundary Smoke Tests

v2.0 で `lab-executor-mcp` (runtime + 機器定義エコシステム) と
`visa-mcp` (PyVISA backend) を分離する前提で、**runtime 候補 module が
PyVISA / visa_manager に依存していないこと**を CI で常時保証する。

詳細は `docs/separation/notes.md` 参照。
"""
from __future__ import annotations
import ast
import importlib
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent


# v2.0 で lab-executor-mcp 側へ移行する候補 module 群
RUNTIME_CANDIDATE_MODULES = [
    "lab_executor.dsl",
    "lab_executor.extension",
    "lab_executor.extension_packaging",
    "lab_executor.extension_install",
    "lab_executor.extension_catalog",
    "lab_executor.extension_authoring",
    "lab_executor.extension_integrity",
    "lab_executor.instrument_authoring",
    "lab_executor.observation",
    "lab_executor.testing",
]

# 上記 module 群が **直接 import してはいけない** modules
# (visa-mcp 側 / PyVISA 透過 layer)
FORBIDDEN_DIRECT_IMPORTS = {
    "lab_visa_mcp.visa_manager",
}

# AST 走査対象 path (RUNTIME_CANDIDATE_MODULES に対応)
RUNTIME_CANDIDATE_PATHS = [
    Path("src/lab_visa_mcp/dsl"),
    Path("src/lab_visa_mcp/extension.py"),
    Path("src/lab_visa_mcp/extension_packaging.py"),
    Path("src/lab_visa_mcp/extension_install.py"),
    Path("src/lab_visa_mcp/extension_catalog.py"),
    Path("src/lab_visa_mcp/extension_authoring.py"),
    Path("src/lab_visa_mcp/extension_integrity.py"),
    Path("src/lab_visa_mcp/instrument_authoring.py"),
    Path("src/lab_visa_mcp/observation.py"),
    Path("src/lab_visa_mcp/testing"),
]


# =========================================================
# 1. import-time に pyvisa を triggered import しないこと
# =========================================================


def test_runtime_modules_do_not_trigger_pyvisa_import_in_fresh_process():
    """clean subprocess で runtime 候補 module を import し、
    sys.modules に pyvisa が現れないことを確認する。

    既に他 test が pyvisa を import 済みだと in-process では検出
    できないため、subprocess で隔離する。
    """
    code = textwrap.dedent("""
        import importlib, sys
        mods = [
            "lab_executor.dsl",
            "lab_executor.extension",
            "lab_executor.extension_packaging",
            "lab_executor.extension_install",
            "lab_executor.extension_catalog",
            "lab_executor.extension_authoring",
            "lab_executor.extension_integrity",
            "lab_executor.instrument_authoring",
            "lab_executor.observation",
            "lab_executor.testing",
        ]
        for m in mods:
            importlib.import_module(m)
        leaked = [k for k in sys.modules
                  if k == "pyvisa" or k.startswith("pyvisa.")]
        if leaked:
            print("LEAKED:" + ",".join(sorted(leaked)))
            raise SystemExit(1)
        print("OK")
    """).strip()
    r = subprocess.run(
        [sys.executable, "-c", code],
        text=True, capture_output=True, cwd=str(ROOT),
    )
    assert r.returncode == 0, (
        f"pyvisa was triggered-imported by runtime candidate modules: "
        f"stdout={r.stdout!r} stderr={r.stderr!r}"
    )
    assert "OK" in r.stdout


# =========================================================
# 2. AST で visa_manager の直接 import を検出
# =========================================================


def _iter_runtime_python_files():
    for root in RUNTIME_CANDIDATE_PATHS:
        full = ROOT / root
        if not full.exists():
            continue
        if full.is_file():
            yield full
        else:
            yield from full.rglob("*.py")


def _top_level_imports(source: str) -> list[str]:
    """module top-level の import のみ列挙 (関数内 lazy import は除外)"""
    try:
        tree = ast.parse(source)
    except Exception:
        return []
    out: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            if node.module:
                out.append(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                out.append(alias.name)
    return out


def test_runtime_modules_do_not_directly_import_visa_manager():
    """runtime 候補 module の **top-level** で
    `from lab_visa_mcp.visa_manager import ...` / `import
    lab_visa_mcp.visa_manager` が無いことを AST で確認する。

    関数 / メソッド内の lazy import は許容 (mock backend が VISA timeout
    error 互換を投げるためなど)。v1.11 で `InstrumentBackend` Protocol
    経由に書き直す予定。
    """
    violations: list[tuple[str, str]] = []
    for p in _iter_runtime_python_files():
        for imp in _top_level_imports(p.read_text(encoding="utf-8")):
            if imp in FORBIDDEN_DIRECT_IMPORTS:
                violations.append((str(p.relative_to(ROOT)), imp))
    assert not violations, (
        "Runtime candidate modules directly import backend "
        f"layer at module top-level (forbidden): {violations}"
    )


def test_runtime_modules_do_not_import_pyvisa_directly_in_source():
    """source の **top-level** で `import pyvisa` /
    `from pyvisa import ...` が無いこと"""
    violations: list[tuple[str, str]] = []
    for p in _iter_runtime_python_files():
        for imp in _top_level_imports(p.read_text(encoding="utf-8")):
            if imp == "pyvisa" or imp.startswith("pyvisa."):
                violations.append((str(p.relative_to(ROOT)), imp))
    assert not violations, (
        f"Runtime candidate modules import pyvisa at top-level: "
        f"{violations}"
    )


# =========================================================
# 3. 各 runtime 候補 module が単体で import 可能
# =========================================================


@pytest.mark.parametrize("module_name", RUNTIME_CANDIDATE_MODULES)
def test_runtime_candidate_module_importable(module_name):
    """各 runtime 候補 module が in-process で import 可能"""
    importlib.import_module(module_name)


# =========================================================
# 4. dependency report (簡易版) も subprocess 経由で動くこと
# =========================================================


def test_dependency_report_runs_in_clean_subprocess():
    """`python -m lab_executor.dev.dependency_report --json` が clean
    subprocess で 0 終了し、JSON を返すこと"""
    r = subprocess.run(
        [sys.executable, "-m", "lab_executor.dev.dependency_report", "--json"],
        text=True, capture_output=True, cwd=str(ROOT),
    )
    assert r.returncode == 0, (
        f"dependency_report failed: stderr={r.stderr!r}"
    )
    import json
    data = json.loads(r.stdout)
    # 必須 keys
    for k in ("runtime_candidate_modules", "forbidden_import_violations",
              "pyvisa_direct_import_violations"):
        assert k in data, f"missing key {k!r}"
    # 違反ゼロを v1.9 時点で保証
    assert data["forbidden_import_violations"] == []
    assert data["pyvisa_direct_import_violations"] == []
