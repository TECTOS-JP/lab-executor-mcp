"""lab-executor CLI (v2.1.0).

v2.0 では minimal CLI / `serve` placeholder のみだったが、v2.1.0 で
`serve --backend mock` を実装し、backend-independent な MCP server を
起動できるようにした。v1.x `visa-mcp` CLI の段階的 port も開始する
(validate / extension の subset)。

サブコマンド (v2.1.0):

- ``lab-executor --version``
- ``lab-executor --help``
- ``lab-executor serve --backend mock``: MCP server 起動
- ``lab-executor validate instrument <path>``
- ``lab-executor validate extension <path>``
- ``lab-executor extension doctor <pack_dir>``
- ``lab-executor extension package <pack_dir>``
- ``lab-executor extension verify-package <zip>``

実機 backend (PyVISA / VISA resource discovery / raw VISA) は
**`visa-mcp serve`** を継続利用。
"""
from __future__ import annotations
import argparse
import json
import sys


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lab-executor",
        description=(
            "lab-executor-mcp: backend-independent experiment execution "
            "runtime CLI (v2.1). For hardware-backed operations, use "
            "`visa-mcp` CLI."
        ),
    )
    parser.add_argument(
        "--version", action="store_true",
        help="show version and exit",
    )
    sub = parser.add_subparsers(dest="command")

    # ---- serve (v2.1.0: MockBackend で実起動) -----------------
    sp_serve = sub.add_parser(
        "serve",
        help="Start MCP server (v2.1: MockBackend only; v2.2+ for "
              "other backends / plugins)",
    )
    sp_serve.add_argument(
        "--backend", default=None,
        choices=["mock"],
        help="Backend to use (v2.1: mock only); required argument",
    )
    sp_serve.add_argument(
        "--dry-run", action="store_true",
        help="Compose server and list tools only (no transport start)",
    )

    # ---- validate -----------------------------------------------
    sp_val = sub.add_parser(
        "validate",
        help="Validate instrument / extension (v2.1: 2 targets)",
    )
    sp_val.add_argument(
        "target", nargs="?",
        choices=["instrument", "extension"],
    )
    sp_val.add_argument("path", nargs="?", help="path to validate")
    sp_val.add_argument("--strict", action="store_true")
    sp_val.add_argument("--json", action="store_true")

    # ---- extension subcommands (v2.1.0: doctor / package /
    #      verify-package) ----------------------------------------
    sp_ext = sub.add_parser(
        "extension",
        help="Extension pack tools (v2.1: doctor / package / verify-package)",
    )
    ext_sub = sp_ext.add_subparsers(dest="ext_command")

    ext_doctor = ext_sub.add_parser(
        "doctor",
        help="Run health check on extension pack",
    )
    ext_doctor.add_argument("pack_dir", help="extension pack directory")
    ext_doctor.add_argument("--strict", action="store_true")
    ext_doctor.add_argument("--json", action="store_true")

    ext_package = ext_sub.add_parser(
        "package",
        help="Package extension pack into .visa-mcp-ext.zip",
    )
    ext_package.add_argument("pack_dir", help="extension pack directory")
    ext_package.add_argument(
        "--output", default=None,
        help="Output .zip path (default: <pack_dir>.visa-mcp-ext.zip)",
    )
    ext_package.add_argument("--dry-run", action="store_true")
    ext_package.add_argument("--json", action="store_true")

    ext_verify = ext_sub.add_parser(
        "verify-package",
        help="Verify checksums / manifest of an existing .visa-mcp-ext.zip",
    )
    ext_verify.add_argument("zip_path", help=".visa-mcp-ext.zip path")
    ext_verify.add_argument("--json", action="store_true")

    # ---- extension init (v2.2.0) ---------------------------------
    ext_init = ext_sub.add_parser(
        "init",
        help="Scaffold a new definition pack directory",
    )
    ext_init.add_argument("pack_name", help="pack directory name")
    ext_init.add_argument(
        "--target-dir", default=None,
        help="parent directory (default: cwd)",
    )
    ext_init.add_argument(
        "--id", dest="extension_id", default=None,
        help="reverse-DNS extension id (default: local.<pack_name>)",
    )
    ext_init.add_argument(
        "--template", default="minimal",
        choices=["minimal", "mock_basic", "instrument_pack"],
    )
    ext_init.add_argument("--author", default="")
    ext_init.add_argument("--force", action="store_true")
    ext_init.add_argument("--json", action="store_true")

    # ---- instrument (v2.2.0: scaffold / review-report port) ------
    sp_inst = sub.add_parser(
        "instrument",
        help="Instrument definition authoring (v2.2: scaffold / "
              "review-report)",
    )
    inst_sub = sp_inst.add_subparsers(dest="inst_command")

    inst_sc = inst_sub.add_parser(
        "scaffold",
        help="Generate instrument YAML from category template "
              "(support_level=draft)",
    )
    inst_sc.add_argument(
        "category",
        choices=["power_supply", "dmm", "temperature_meter",
                  "generic_scpi"],
    )
    inst_sc.add_argument(
        "--output", required=True,
        help="output file path (e.g. instruments/kikusui_pmx.yaml)",
    )
    inst_sc.add_argument("--manufacturer", default="TODO")
    inst_sc.add_argument("--model", default="TODO")
    inst_sc.add_argument("--force", action="store_true")
    inst_sc.add_argument("--json", action="store_true")

    inst_rr = inst_sub.add_parser(
        "review-report",
        help="Convert instrument YAML into PR review markdown "
              "(strict validate + promote-check aggregated)",
    )
    inst_rr.add_argument("path", help="instrument YAML path")
    inst_rr.add_argument(
        "--output", default=None,
        help="markdown output path (default: stdout)",
    )
    inst_rr.add_argument("--json", action="store_true")

    # ---- diagnose (v2.2.0: tool-surface CLI) ---------------------
    sp_diag = sub.add_parser(
        "diagnose",
        help="Diagnose runtime / tool surface",
    )
    diag_sub = sp_diag.add_subparsers(dest="diag_command")

    diag_ts = diag_sub.add_parser(
        "tool-surface",
        help="Report declared vs registered MCP tool count",
    )
    diag_ts.add_argument(
        "--backend", default="mock", choices=["mock"],
    )
    diag_ts.add_argument("--json", action="store_true")

    return parser


# ============================================================
# serve
# ============================================================


def _cmd_serve(args: argparse.Namespace) -> int:
    if args.backend is None:
        # ASCII-only stderr (subprocess decode 安全性)
        print(
            "lab-executor serve requires --backend.\n"
            "  v2.1: only --backend mock is supported.\n"
            "  For hardware-backed MCP server, install visa-mcp v2.x "
            "and run `visa-mcp serve`.",
            file=sys.stderr,
        )
        return 2

    if args.backend == "mock":
        from lab_executor.backends import MockBackend
        from lab_executor.server import create_server, list_registered_tools
        backend = MockBackend()
        server = create_server(backend=backend, name="lab-executor")
        tools = list_registered_tools(server)
        if args.dry_run:
            print(
                f"lab-executor MCP server (backend=mock) composed OK\n"
                f"  registered tools: {len(tools)}\n"
                f"  backend_id: {backend.backend_id}",
            )
            for t in sorted(tools):
                print(f"  - {t}")
            return 0
        # 実 transport 起動
        print(
            f"lab-executor MCP server starting (backend=mock, "
            f"tools={len(tools)})...",
            file=sys.stderr,
        )
        try:
            server.run()
        except KeyboardInterrupt:
            return 0
        return 0

    print(f"unsupported backend: {args.backend}", file=sys.stderr)
    return 2


# ============================================================
# validate
# ============================================================


def _cmd_validate(args: argparse.Namespace) -> int:
    if args.target == "instrument" and args.path:
        from lab_executor.registry import validate_instrument_file
        rep = validate_instrument_file(args.path, strict=args.strict)
        if args.json:
            print(json.dumps(rep.to_dict(), ensure_ascii=False,
                              indent=2, default=str))
        else:
            print(f"errors: {len(rep.errors)}")
            for e in rep.errors:
                print(f"  - {e.get('error_class')}: "
                      f"{e.get('message')}")
        return 0 if not rep.errors else 1

    if args.target == "extension" and args.path:
        from lab_executor.extension import validate_extension_file
        rep = validate_extension_file(args.path, strict=args.strict)
        data = rep.to_dict() if hasattr(rep, "to_dict") else rep
        if args.json:
            print(json.dumps(data, ensure_ascii=False, indent=2,
                              default=str))
        else:
            errs = data.get("errors") or []
            print(f"extension validation: {len(errs)} error(s)")
            for e in errs:
                print(f"  - {e.get('error_class')}: "
                      f"{e.get('message')}")
        return 0 if not (data.get("errors") or []) else 1

    print(
        "lab-executor validate: usage: validate {instrument|extension} "
        "<path> [--strict] [--json]",
        file=sys.stderr,
    )
    return 2


# ============================================================
# extension
# ============================================================


def _cmd_extension(args: argparse.Namespace) -> int:
    sub = args.ext_command

    if sub == "doctor":
        from lab_executor.extension_authoring import doctor_extension
        rep = doctor_extension(args.pack_dir, strict=args.strict)
        data = rep.to_dict() if hasattr(rep, "to_dict") else rep
        if args.json:
            print(json.dumps(data, ensure_ascii=False, indent=2,
                              default=str))
        else:
            status = data.get("status", "?")
            print(f"extension doctor: status={status}")
            for e in (data.get("errors") or []):
                print(f"  ERROR  {e.get('error_class')}: "
                      f"{e.get('message')}")
            for w in (data.get("warnings") or []):
                print(f"  WARN   {w.get('warning_class')}: "
                      f"{w.get('message')}")
        return 0 if data.get("status") == "ok" else 1

    if sub == "package":
        from lab_executor.extension_packaging import package_extension
        rep = package_extension(
            args.pack_dir,
            output_path=args.output,
            dry_run=args.dry_run,
        )
        data = rep.to_dict() if hasattr(rep, "to_dict") else rep
        if args.json:
            print(json.dumps(data, ensure_ascii=False, indent=2,
                              default=str))
        else:
            print(f"package: status={data.get('status', '?')}")
            if data.get("output_path"):
                print(f"  output: {data['output_path']}")
        return 0 if data.get("status") == "ok" else 1

    if sub == "init":
        from lab_executor.extension_authoring import init_extension_pack
        rep = init_extension_pack(
            args.pack_name,
            target_dir=args.target_dir,
            extension_id=args.extension_id,
            template=args.template,
            author=args.author,
            force=args.force,
        )
        data = rep.to_dict() if hasattr(rep, "to_dict") else rep
        if args.json:
            print(json.dumps(data, ensure_ascii=False, indent=2,
                              default=str))
        else:
            print(f"extension init: status={data.get('status', '?')}")
            if data.get("pack_dir"):
                print(f"  pack_dir: {data['pack_dir']}")
            for e in (data.get("errors") or []):
                print(f"  ERROR  {e.get('error_class')}: "
                      f"{e.get('message')}")
        return 0 if data.get("status") == "ok" else 1

    if sub == "verify-package":
        from lab_executor.extension_packaging import (
            verify_extension_package,
        )
        rep = verify_extension_package(args.zip_path)
        data = rep.to_dict() if hasattr(rep, "to_dict") else rep
        if args.json:
            print(json.dumps(data, ensure_ascii=False, indent=2,
                              default=str))
        else:
            print(f"verify-package: status={data.get('status', '?')}")
            for e in (data.get("errors") or []):
                print(f"  - {e.get('error_class')}: "
                      f"{e.get('message')}")
        return 0 if data.get("status") == "ok" else 1

    print(
        "lab-executor extension: subcommand required "
        "(doctor / package / verify-package)",
        file=sys.stderr,
    )
    return 2


# ============================================================
# instrument (v2.2.0)
# ============================================================


def _cmd_instrument(args: argparse.Namespace) -> int:
    sub = args.inst_command
    if sub == "scaffold":
        from lab_executor.instrument_authoring import (
            scaffold_instrument_definition,
        )
        res = scaffold_instrument_definition(
            args.category,
            output=args.output,
            manufacturer=args.manufacturer,
            model=args.model,
            force=args.force,
        )
        data = res.to_dict() if hasattr(res, "to_dict") else res
        if args.json:
            print(json.dumps({"scaffold": data},
                              ensure_ascii=False, indent=2,
                              default=str))
        else:
            print(f"scaffold {args.category} -> "
                  f"{data.get('output_path')}")
            for e in (data.get("errors") or []):
                print(f"  ERROR  {e.get('error_class')}: "
                      f"{e.get('message')}")
        return 0 if data.get("status") == "ok" else 1

    if sub == "review-report":
        from lab_executor.instrument_authoring import (
            review_report_instrument,
        )
        res = review_report_instrument(args.path)
        if args.json:
            print(json.dumps({"review_report": res},
                              ensure_ascii=False, indent=2,
                              default=str))
        else:
            md = res["markdown"]
            if args.output:
                from pathlib import Path as _P
                _P(args.output).write_text(md, encoding="utf-8")
                print(f"review-report {res['file']} -> {args.output}")
            else:
                print(md)
        return 0 if res["status"] != "error" else 1

    print(
        "lab-executor instrument: subcommand required "
        "(scaffold / review-report)",
        file=sys.stderr,
    )
    return 2


# ============================================================
# diagnose (v2.2.0)
# ============================================================


def _cmd_diagnose(args: argparse.Namespace) -> int:
    sub = args.diag_command
    if sub == "tool-surface":
        from lab_executor.backends import MockBackend
        from lab_executor.server import (
            create_server, diagnose_tool_surface,
        )
        backend = MockBackend()
        server = create_server(backend=backend)
        diag = diagnose_tool_surface(server)
        # status: ok / warning
        miss = diag.get("missing_from_registry") or []
        diag["status"] = "ok" if not miss else "warning"
        if args.json:
            print(json.dumps(diag, ensure_ascii=False, indent=2,
                              default=str))
        else:
            print(f"tool-surface diagnostic (backend=mock)")
            print(f"  declared: stable={diag['declared_stable']}, "
                  f"experimental={diag['declared_experimental']}, "
                  f"total={diag['declared_total']}")
            print(f"  registered: {diag['registered_count']}")
            print(f"  status: {diag['status']}")
            if miss:
                print(f"  missing ({len(miss)}):")
                for m in miss[:20]:
                    print(f"    - {m}")
        return 0 if diag["status"] == "ok" else 1

    print(
        "lab-executor diagnose: subcommand required (tool-surface)",
        file=sys.stderr,
    )
    return 2


# ============================================================
# main
# ============================================================


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.version:
        import lab_executor
        print(f"lab-executor-mcp {lab_executor.__version__}")
        return 0

    if args.command == "serve":
        return _cmd_serve(args)
    if args.command == "validate":
        return _cmd_validate(args)
    if args.command == "extension":
        return _cmd_extension(args)
    if args.command == "instrument":
        return _cmd_instrument(args)
    if args.command == "diagnose":
        return _cmd_diagnose(args)

    if args.command is None:
        parser.print_help()
        return 0

    print(f"unknown subcommand: {args.command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
