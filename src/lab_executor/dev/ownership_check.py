"""
v1.10: module_ownership.yaml と実装の整合性検証 + dependency graph 生成

Usage:
    python -m lab_executor.dev.ownership_check
    python -m lab_executor.dev.ownership_check --json
    python -m lab_executor.dev.ownership_check --graph-md docs/separation/dependency_graph.md

CI で「未分類 module が無いこと」を保証し、`lab-executor-mcp` owner の
module が `visa-mcp` owner の module を import していないこと (許容
例外を除く) を AST で検出する。
"""
from __future__ import annotations
import argparse
import ast
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml


_THIS = Path(__file__).resolve()
REPO_ROOT = _THIS.parent.parent.parent.parent
SRC_ROOT = REPO_ROOT / "src" / "visa_mcp"
MANIFEST = REPO_ROOT / "docs" / "separation" / "module_ownership.yaml"

# lab-executor 側 module が import してよい visa-mcp owner の例外
# (lazy import / Protocol 経由化が v1.11 までに完了する予定)
LAZY_EXCEPTIONS = {
    # mock_instruments が VISA timeout error 互換のため lazy import
    ("lab_executor.testing.mock_instruments", "visa_mcp.visa_manager"),
}


# v1.11: KNOWN_V111_TO_RESOLVE = 0 件
# v1.10 で 10 件あった既知 violation はすべて
# `if TYPE_CHECKING:` ブロックまたは関数内 lazy import に移動して
# top-level の backend layer 依存を排除した。
# 詳細: docs/separation/notes.md
# 新規 violation が混入したら CI で即 fail させる。
KNOWN_V111_TO_RESOLVE: set[tuple[str, str]] = set()


def _module_name_for_path(path: Path) -> str | None:
    """src/visa_mcp/foo/bar.py → visa_mcp.foo.bar、
    src/visa_mcp/foo/__init__.py → visa_mcp.foo"""
    try:
        rel = path.relative_to(REPO_ROOT / "src")
    except ValueError:
        return None
    parts = list(rel.parts)
    if parts[-1] == "__init__.py":
        parts = parts[:-1]
    else:
        parts[-1] = parts[-1].removesuffix(".py")
    return ".".join(parts)


def _scan_all_modules() -> list[str]:
    out: list[str] = []
    for p in SRC_ROOT.rglob("*.py"):
        name = _module_name_for_path(p)
        if name:
            out.append(name)
    return sorted(set(out))


def _load_manifest() -> dict[str, Any]:
    return yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))


def _module_imports(path: Path) -> tuple[list[str], list[str]]:
    """(top-level imports, lazy imports) を返す"""
    top: list[str] = []
    lazy: list[str] = []
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except Exception:
        return top, lazy

    def _from_node(node, dest):
        if isinstance(node, ast.ImportFrom) and node.module:
            dest.append(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                dest.append(alias.name)

    for node in tree.body:
        _from_node(node, top)
    # 関数 / class 内の lazy import (再帰)
    for outer in ast.walk(tree):
        if outer in tree.body:
            continue
        if isinstance(outer, (ast.ImportFrom, ast.Import)):
            _from_node(outer, lazy)
    return top, lazy


def collect_report() -> dict[str, Any]:
    manifest = _load_manifest()
    modules_spec = manifest.get("modules", {}) or {}
    all_modules = _scan_all_modules()

    declared = set(modules_spec.keys())
    actual = set(all_modules)

    # owner 別分類
    owner_of: dict[str, str] = {}
    for mod, info in modules_spec.items():
        owner_of[mod] = (info or {}).get("owner", "unknown")

    # 未分類 = 実体はあるが manifest 未登録
    unclassified = sorted(actual - declared)
    # 幽霊 = manifest 登録だが実体無し
    ghosts = sorted(declared - actual)

    # 各 module の top-level / lazy import を集計
    edges: list[dict[str, Any]] = []
    violations: list[dict[str, Any]] = []
    for path in SRC_ROOT.rglob("*.py"):
        mod = _module_name_for_path(path)
        if mod is None:
            continue
        top, lazy = _module_imports(path)
        for imp in top:
            if imp.startswith("visa_mcp."):
                edges.append({
                    "from": mod, "to": imp, "kind": "top_level",
                })
        for imp in lazy:
            if imp.startswith("visa_mcp."):
                edges.append({
                    "from": mod, "to": imp, "kind": "lazy",
                })

    # 違反判定: lab-executor owner の module が visa-mcp owner の
    # module を **top-level** で import している
    known_pending: list[dict[str, Any]] = []
    for e in edges:
        if e["kind"] != "top_level":
            continue
        src = e["from"]
        dst = e["to"]
        src_owner = owner_of.get(src)
        dst_owner = owner_of.get(dst)
        if src_owner == "lab-executor-mcp" and dst_owner == "visa-mcp":
            if (src, dst) in LAZY_EXCEPTIONS:
                continue
            if (src, dst) in KNOWN_V111_TO_RESOLVE:
                known_pending.append({
                    "from": src, "to": dst,
                    "resolve_at": "v1.11",
                    "method": "InstrumentBackend Protocol 経由化",
                })
                continue
            violations.append({
                "from": src, "to": dst,
                "kind": "lab_to_visa_top_level",
                "src_owner": src_owner, "dst_owner": dst_owner,
            })

    # statistics
    by_owner: dict[str, int] = defaultdict(int)
    for mod, info in modules_spec.items():
        by_owner[(info or {}).get("owner", "unknown")] += 1

    return {
        "manifest_path": str(MANIFEST.relative_to(REPO_ROOT)),
        "declared_modules_count": len(declared),
        "actual_modules_count": len(actual),
        "unclassified_modules": unclassified,
        "manifest_ghost_modules": ghosts,
        "owner_counts": dict(by_owner),
        "edges_count": len(edges),
        "lab_to_visa_top_level_violations": violations,
        "known_v1_11_to_resolve": known_pending,
        "known_v1_11_to_resolve_count": len(known_pending),
        "lazy_exceptions_count": len(LAZY_EXCEPTIONS),
    }


def render_graph_md(report: dict[str, Any]) -> str:
    """簡易 dependency graph レポート (Mermaid 風 + table)"""
    out: list[str] = []
    out.append("# Dependency Graph Report (v1.10, auto-generated)\n")
    out.append("> `python -m lab_executor.dev.ownership_check --graph-md "
                "docs/separation/dependency_graph.md` で再生成。")
    out.append("> 手で編集しない。\n")

    out.append("## Statistics\n")
    out.append(
        f"- declared modules: {report['declared_modules_count']}")
    out.append(f"- actual modules: {report['actual_modules_count']}")
    out.append(
        f"- unclassified: {len(report['unclassified_modules'])}")
    out.append(
        f"- manifest ghosts: {len(report['manifest_ghost_modules'])}")
    out.append(f"- edges total: {report['edges_count']}")
    out.append(
        f"- lab→visa top-level violations: "
        f"{len(report['lab_to_visa_top_level_violations'])}")
    out.append(
        f"- lazy exceptions: {report['lazy_exceptions_count']}\n")

    out.append("## Owner counts\n")
    out.append("| owner | count |")
    out.append("|-------|-------|")
    for owner, count in sorted(report["owner_counts"].items()):
        out.append(f"| {owner} | {count} |")
    out.append("")

    if report["unclassified_modules"]:
        out.append("## ⚠ Unclassified modules\n")
        for m in report["unclassified_modules"]:
            out.append(f"- {m}")
        out.append("")

    if report["lab_to_visa_top_level_violations"]:
        out.append("## ⚠ NEW lab→visa top-level violations\n")
        out.append("(known_v1_11_to_resolve に登録すること)\n")
        out.append("| from (lab-executor) | to (visa-mcp) |")
        out.append("|-----|-----|")
        for v in report["lab_to_visa_top_level_violations"]:
            out.append(f"| {v['from']} | {v['to']} |")
        out.append("")
    else:
        out.append("## ✅ No NEW lab→visa top-level violations\n")

    if report["known_v1_11_to_resolve"]:
        out.append("## Known v1.11-to-resolve "
                    f"({report['known_v1_11_to_resolve_count']} 件)\n")
        out.append(
            "v1.11 で InstrumentBackend Protocol 経由化により解消する "
            "既知の violation。新規追加禁止 / 削減のみ。\n")
        out.append("| from | to | resolve at | method |")
        out.append("|------|----|-----------|--------|")
        for v in report["known_v1_11_to_resolve"]:
            out.append(
                f"| {v['from']} | {v['to']} | {v['resolve_at']} | "
                f"{v['method']} |")
        out.append("")

    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m lab_executor.dev.ownership_check",
        description=(
            "v1.10: module_ownership.yaml の完全性検証 + "
            "dependency graph 生成"
        ),
    )
    parser.add_argument("--json", action="store_true",
                         help="JSON 出力")
    parser.add_argument(
        "--graph-md", default=None,
        help="dependency graph を markdown file に書き出す",
    )
    args = parser.parse_args(argv)
    report = collect_report()

    if args.graph_md:
        Path(args.graph_md).write_text(
            render_graph_md(report), encoding="utf-8")
        print(f"wrote: {args.graph_md}")

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2,
                          default=str))
    else:
        print(f"declared: {report['declared_modules_count']}, "
              f"actual: {report['actual_modules_count']}, "
              f"unclassified: {len(report['unclassified_modules'])}, "
              f"ghosts: {len(report['manifest_ghost_modules'])}")
        print(f"edges: {report['edges_count']}, "
              f"violations: "
              f"{len(report['lab_to_visa_top_level_violations'])}")
        if report["unclassified_modules"]:
            print("\nUnclassified:")
            for m in report["unclassified_modules"]:
                print(f"  - {m}")
        if report["lab_to_visa_top_level_violations"]:
            print("\nlab→visa top-level violations:")
            for v in report["lab_to_visa_top_level_violations"]:
                print(f"  - {v['from']} -> {v['to']}")

    # CI 用 exit code
    if (report["unclassified_modules"]
            or report["lab_to_visa_top_level_violations"]):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
