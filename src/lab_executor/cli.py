"""lab-executor CLI (v2.0.0-rc2).

v2.0.0 では minimal CLI を提供する。v1.x の `visa-mcp` CLI と機能互換
にするのは v2.1+ の段階的作業。

サブコマンド (v2.0.0-rc2 時点):

- ``lab-executor validate [instrument|plan|extension|benchmark] <path>``
- ``lab-executor instrument {scaffold|promote-check|review-report} ...``
- ``lab-executor extension {install|list|uninstall|check|catalog|...}``
- ``lab-executor serve`` (placeholder, v2.1 で MCP server を起動)

実装方針:
  v1.x の `visa_mcp.cli:main` には server / list-resources / raw VISA /
  validate / extension / instrument / registry の全部が含まれていたが、
  v2.0 では backend 非依存の subcommand のみ残す。実機 backend が必要
  なものは `visa-mcp` CLI 側で提供される。

将来 (v2.1+): visa-mcp v1.x からの完全 port (registry / catalog 等)。
"""
from __future__ import annotations
import argparse
import sys


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lab-executor",
        description=(
            "lab-executor-mcp: backend-independent experiment execution "
            "runtime CLI (v2.0). PyVISA backend が必要な操作は "
            "`visa-mcp` CLI を使ってください。"
        ),
    )
    parser.add_argument(
        "--version", action="store_true",
        help="show version and exit",
    )
    sub = parser.add_subparsers(dest="command")

    # serve placeholder
    sp_serve = sub.add_parser(
        "serve",
        help="(placeholder, v2.1) MCP server を起動",
    )
    sp_serve.add_argument(
        "--backend", default="mock",
        choices=["mock"],
        help="使用する backend (v2.0 では mock のみ)",
    )

    # validate placeholder (visa-mcp validate と互換)
    sp_val = sub.add_parser(
        "validate",
        help="(placeholder, v2.1) instrument / plan / extension / "
              "benchmark を検証",
    )
    sp_val.add_argument(
        "target", nargs="?",
        choices=["instrument", "plan", "extension", "benchmark",
                 "registry", "instrument-yaml"],
    )
    sp_val.add_argument("path", nargs="?", help="検証対象 path")
    sp_val.add_argument("--strict", action="store_true")
    sp_val.add_argument("--json", action="store_true")

    return parser


def main(argv: list[str] | None = None) -> int:
    """lab-executor CLI entry point.

    Returns: exit code (0 = success).
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.version:
        import lab_executor
        print(f"lab-executor-mcp {lab_executor.__version__}")
        return 0

    if args.command == "serve":
        # ASCII-only message to avoid Windows cp932 / locale issues
        # when invoked from subprocess.
        print(
            "lab-executor serve: placeholder in v2.0.x. "
            "MCP server will be implemented in v2.1.\n"
            "For hardware-backed MCP server, install visa-mcp v2.x "
            "and run `visa-mcp serve`.",
            file=sys.stderr,
        )
        return 2

    if args.command == "validate":
        if args.target == "instrument" and args.path:
            from lab_executor.registry import validate_instrument_file
            rep = validate_instrument_file(args.path, strict=args.strict)
            if args.json:
                import json
                print(json.dumps(rep.to_dict(), ensure_ascii=False,
                                  indent=2, default=str))
            else:
                print(f"errors: {len(rep.errors)}")
                for e in rep.errors:
                    print(f"  - {e.get('error_class')}: "
                          f"{e.get('message')}")
            return 0 if not rep.errors else 1
        print(
            "lab-executor validate: v2.0.0 では instrument のみ対応。\n"
            "詳細は v2.1 で visa-mcp v1.x から port 予定です。",
            file=sys.stderr,
        )
        return 2

    if args.command is None:
        parser.print_help()
        return 0

    print(f"unknown subcommand: {args.command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
