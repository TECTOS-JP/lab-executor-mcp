"""
v1.9: separation 準備のための簡易 dependency レポート

Usage:
    python -m lab_executor.dev.dependency_report
    python -m lab_executor.dev.dependency_report --json

runtime 候補 module 群が lab_visa_mcp.visa_manager や pyvisa を直接 import
していないことを確認する。v1.10 で `docs/separation/module_ownership.yaml`
が確定すれば、それを基準にした厳密 check に置き換える。
"""
from __future__ import annotations
import argparse
import ast
import json
import sys
from pathlib import Path
from typing import Any


_THIS = Path(__file__).resolve()
# .../src/lab_visa_mcp/dev/dependency_report.py → repo root
REPO_ROOT = _THIS.parent.parent.parent.parent


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

FORBIDDEN_DIRECT_IMPORTS = {"lab_visa_mcp.visa_manager"}


def _iter_runtime_python_files() -> list[Path]:
    out: list[Path] = []
    for rel in RUNTIME_CANDIDATE_PATHS:
        full = REPO_ROOT / rel
        if not full.exists():
            continue
        if full.is_file():
            out.append(full)
        else:
            out.extend(full.rglob("*.py"))
    return out


def _ast_top_level_imports(source: str) -> list[str]:
    """source の **module top-level** で import される module 名のみ
    列挙する。関数 / メソッド / class 内の lazy import (`def foo():
    ... import ...`) は **除外**する。

    これは「import 時に triggered import される module」を見るのが
    目的だから (v1.9 separation boundary check と整合)。
    """
    try:
        tree = ast.parse(source)
    except Exception:
        return []
    imports: list[str] = []
    for node in tree.body:  # tree.body のみ = module top-level
        if isinstance(node, ast.ImportFrom):
            if node.module:
                imports.append(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name)
    return imports


def collect_report() -> dict[str, Any]:
    runtime_files = _iter_runtime_python_files()

    forbidden_violations: list[dict[str, str]] = []
    pyvisa_violations: list[dict[str, str]] = []

    for p in runtime_files:
        rel = str(p.relative_to(REPO_ROOT)).replace("\\", "/")
        try:
            src = p.read_text(encoding="utf-8")
        except Exception:
            continue
        for imp in _ast_top_level_imports(src):
            if imp in FORBIDDEN_DIRECT_IMPORTS:
                forbidden_violations.append({"file": rel, "import": imp})
            if imp == "pyvisa" or imp.startswith("pyvisa."):
                pyvisa_violations.append({"file": rel, "import": imp})

    return {
        "runtime_candidate_modules": [
            str(p.relative_to(REPO_ROOT)).replace("\\", "/")
            for p in runtime_files
        ],
        "runtime_candidate_module_count": len(runtime_files),
        "forbidden_imports": sorted(FORBIDDEN_DIRECT_IMPORTS),
        "forbidden_import_violations": forbidden_violations,
        "pyvisa_direct_import_violations": pyvisa_violations,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m lab_executor.dev.dependency_report",
        description=(
            "v1.9: separation boundary dependency report. "
            "Checks runtime candidate modules for forbidden imports."
        ),
    )
    parser.add_argument(
        "--json", action="store_true",
        help="JSON 出力 (CI 向け)",
    )
    args = parser.parse_args(argv)

    report = collect_report()

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2,
                          default=str))
    else:
        print("Runtime candidate modules:")
        print(f"  checked: {report['runtime_candidate_module_count']}")
        print(
            f"  forbidden imports detected: "
            f"{len(report['forbidden_import_violations'])}"
        )
        print(
            f"  pyvisa direct imports detected: "
            f"{len(report['pyvisa_direct_import_violations'])}"
        )
        if report["forbidden_import_violations"]:
            print("\n  Violations (forbidden):")
            for v in report["forbidden_import_violations"]:
                print(f"    {v['file']} -> {v['import']}")
        if report["pyvisa_direct_import_violations"]:
            print("\n  Violations (pyvisa direct):")
            for v in report["pyvisa_direct_import_violations"]:
                print(f"    {v['file']} -> {v['import']}")

    # 違反があれば非ゼロ終了
    has_violation = (
        bool(report["forbidden_import_violations"])
        or bool(report["pyvisa_direct_import_violations"])
    )
    return 1 if has_violation else 0


if __name__ == "__main__":
    raise SystemExit(main())
