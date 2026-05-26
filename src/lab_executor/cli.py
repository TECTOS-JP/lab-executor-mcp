"""lab-executor CLI (v2.5.x).

v2.0 では minimal CLI / `serve` placeholder のみだったが、v2.1.0 で
`serve --backend mock` を実装、v2.2.0 で authoring workflow CLI を
追加、v2.3.0 で extension lifecycle (install/check/catalog/paths)、
v2.4.0 で **dual-path extension discovery + duplicate conflict
detection** を追加した。実機 backend (PyVISA / VISA resource
discovery / raw VISA) は **`visa-mcp serve`** を継続利用する。

サブコマンド一覧:

v2.1.0 追加:
- ``lab-executor --version`` / ``--help``
- ``lab-executor serve --backend mock``
- ``lab-executor validate {instrument,extension} <path>``
- ``lab-executor extension {doctor,package,verify-package}``

v2.2.0 追加:
- ``lab-executor extension init <pack_name>``
- ``lab-executor instrument {scaffold,review-report}``
- ``lab-executor diagnose tool-surface``

v2.3.0 追加:
- ``lab-executor extension install <zip> [--dry-run|--force|--skip-verify]``
- ``lab-executor extension check [--extension-id|--strict]``
- ``lab-executor extension catalog``
- ``lab-executor extension paths``

v2.4.0 追加 (Dual-path Extension Discovery + Duplicate Conflict
Detection):

- ``extension paths``: dual-path read 構成 (legacy + new) と
  ``duplicate_policy = report_conflict_no_implicit_precedence`` を表示
- ``extension catalog``: dual-path discovery を経由して install 済
  pack を列挙。duplicate 時は ``status=warning`` + ``duplicates``
  block 出力。``--strict`` で exit 1
- ``extension check``: dual-path 経由の integrity check。
  ``summary.duplicate_extension_ids`` を返す。``--strict`` で exit 1

v2.5.0 追加 (Extension Migration Plan + Conflict Resolution Guidance):

- ``extension migration-plan``: 現状 path 状態 (legacy_only /
  new_only / duplicates / invalid) を分類し、推奨 action を出力。
  実ファイルは一切変更しない (plan only)。``--strict`` で warning も
  exit 1
- API ``resolve_extension_by_id()``: ``extension_id`` から 1 件の
  ``InstalledExtension`` を返す。duplicate 時は
  ``ExtensionResolveError("duplicate_extension_id")`` を raise し、
  「黙って先頭採用」を API 境界で禁止する

Exit code policy (v2.2.1 明文化、v2.4 で extension lifecycle 拡張、
v2.5 で migration-plan 追加):

- 0: 正常終了 (`status == "ok"`)
- 1: validation / doctor warning / mismatch / ``--strict`` で warning
- 2: usage error / 引数不足

書き込み default は v2.4 でも ``~/.visa-mcp/extensions/`` 維持
(legacy)。``~/.lab-executor/extensions/`` への切替は v2.5+ で判断。
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
            "runtime CLI with dual-path extension discovery and "
            "migration planning (v2.5). For hardware-backed operations, "
            "use `visa-mcp` CLI."
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

    # ---- extension install / check / catalog / paths (v2.3.0) ---
    ext_install = ext_sub.add_parser(
        "install",
        help="Install a definition pack zip (.visa-mcp-ext.zip)",
    )
    ext_install.add_argument("zip_path", help=".visa-mcp-ext.zip path")
    ext_install.add_argument(
        "--force", action="store_true",
        help="overwrite existing install",
    )
    ext_install.add_argument(
        "--skip-verify", action="store_true",
        help="(test only) skip checksum / manifest verification",
    )
    ext_install.add_argument(
        "--dry-run", action="store_true",
        help="show what would happen without writing",
    )
    ext_install.add_argument("--json", action="store_true")

    ext_check = ext_sub.add_parser(
        "check",
        help="Check installed extensions (checksum / metadata)",
    )
    ext_check.add_argument(
        "--extension-id", default=None,
        help="check specific extension id (default: all)",
    )
    ext_check.add_argument("--json", action="store_true")
    ext_check.add_argument(
        "--strict", action="store_true",
        help="warning -> exit 1 (default: warning ok)",
    )

    ext_catalog = ext_sub.add_parser(
        "catalog",
        help="List installed extensions catalog (v2.4: dual-path)",
    )
    ext_catalog.add_argument("--json", action="store_true")
    ext_catalog.add_argument(
        "--strict", action="store_true",
        help="v2.4: duplicate_extension_id -> exit 1 (default: exit 0)",
    )

    # ---- extension migration-plan (v2.5.0) ----------------------
    ext_mig = ext_sub.add_parser(
        "migration-plan",
        help=(
            "v2.5: Report extension path migration plan "
            "(no file changes; plan only)"
        ),
    )
    ext_mig.add_argument("--json", action="store_true")
    ext_mig.add_argument(
        "--strict", action="store_true",
        help="treat warning/error as exit 1",
    )

    ext_paths = ext_sub.add_parser(
        "paths",
        help=(
            "Show extension path resolver state "
            "(v2.4: dual-read, legacy write default)"
        ),
    )
    ext_paths.add_argument("--json", action="store_true")

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
        help=(
            "reverse-DNS extension id (default: 'local.<pack_name>', "
            "e.g. 'local.my_pack')"
        ),
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
    diag_ts.add_argument(
        "--strict", action="store_true",
        help=(
            "v2.2.1: --strict なら warning でも exit 1。指定なしは "
            "warning を許容して exit 0 (手元診断向け、CI gate には "
            "--strict 推奨)"
        ),
    )

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

    if sub == "install":
        from lab_executor.extension_install import (
            install_definition_pack_from_zip,
        )
        if args.dry_run:
            # v2.3: dry-run は zip verify のみ実行 (実 install せず)
            from lab_executor.extension_integrity import (
                verify_extension_package,
            )
            rep = verify_extension_package(args.zip_path)
            data = rep if isinstance(rep, dict) else (
                rep.to_dict() if hasattr(rep, "to_dict") else {}
            )
            data["dry_run"] = True
            if args.json:
                print(json.dumps(data, ensure_ascii=False, indent=2,
                                  default=str))
            else:
                status = data.get("status", "?")
                print(f"extension install --dry-run: verify "
                      f"status={status}")
            return 0 if data.get("status") == "ok" else 1
        rep = install_definition_pack_from_zip(
            args.zip_path,
            force=args.force,
            skip_verify=args.skip_verify,
        )
        data = rep.to_dict() if hasattr(rep, "to_dict") else rep
        if args.json:
            print(json.dumps(data, ensure_ascii=False, indent=2,
                              default=str))
        else:
            print(f"extension install: status="
                  f"{data.get('status', '?')}")
            if data.get("install_path"):
                print(f"  install_path: {data['install_path']}")
        return 0 if data.get("status") == "ok" else 1

    if sub == "check":
        # v2.4.0: dual-path discovery + duplicate detection を統合
        from lab_executor.extension_integrity import (
            check_installed_extension,
        )
        from lab_executor.extension_discovery import (
            discover_installed_extensions,
        )
        disc = discover_installed_extensions()
        exts = list(disc.extensions)
        if args.extension_id:
            exts = [e for e in exts
                    if e.extension_id == args.extension_id]
        results: list[dict] = []
        worst_status = "ok"
        for e in exts:
            rep = check_installed_extension(str(e.path))
            d = rep if isinstance(rep, dict) else (
                rep.to_dict() if hasattr(rep, "to_dict") else {}
            )
            d["extension_id"] = e.extension_id
            d["source_path"] = str(e.source_path)
            results.append(d)
            if d.get("status") == "error":
                worst_status = "error"
            elif (d.get("status") == "warning"
                    and worst_status != "error"):
                worst_status = "warning"
        # duplicate を warning に格上げ (整合性検査の主戦場)
        duplicate_warnings: list[dict] = []
        if disc.has_duplicates():
            for eid, entries in disc.duplicates.items():
                duplicate_warnings.append({
                    "warning_class": "duplicate_extension_id",
                    "extension_id": eid,
                    "locations": [str(x.path) for x in entries],
                    "recommended_actions": [
                        {"action": "remove_one_copy"},
                        {"action": "run_migration_plan"},
                    ],
                })
            if worst_status != "error":
                worst_status = "warning"
        out = {
            "status": worst_status,
            "summary": {
                "checked_extensions": len(results),
                "duplicate_extension_ids": len(disc.duplicates),
            },
            "checked_count": len(results),
            "results": results,
            "warnings": duplicate_warnings,
            "duplicate_policy": disc.duplicate_policy,
        }
        if args.json:
            print(json.dumps(out, ensure_ascii=False, indent=2,
                              default=str))
        else:
            print(f"extension check: status={worst_status}, "
                  f"checked={len(results)}, "
                  f"duplicates={len(disc.duplicates)}")
            for r in results:
                print(f"  - {r.get('extension_id')}: "
                      f"{r.get('status')}")
            for w in duplicate_warnings:
                print(f"  WARN duplicate_extension_id: "
                      f"{w['extension_id']}")
                for loc in w["locations"]:
                    print(f"      - {loc}")
        if worst_status == "ok":
            return 0
        if worst_status == "warning":
            return 1 if args.strict else 0
        return 1

    if sub == "catalog":
        # v2.4.0: dual-path discovery + duplicate detection を統合
        from lab_executor.extension_catalog import _entry_from_installed
        from lab_executor.extension_discovery import (
            discover_installed_extensions,
        )
        disc = discover_installed_extensions()
        entries: list[dict] = []
        for e in disc.extensions:
            entry = _entry_from_installed({
                "extension_id": e.extension_id,
                "path": str(e.path),
            })
            if entry is None:
                # extension.yaml は読めたが entry 化失敗 → 簡易 entry
                entry = {
                    "extension_id": e.extension_id,
                    "version": e.metadata.get("version", ""),
                    "source": {
                        "kind": "installed",
                        "path": str(e.path),
                    },
                }
            entry["source_path"] = str(e.source_path)
            entries.append(entry)
        duplicates_block: list[dict] = []
        for eid, entries_list in disc.duplicates.items():
            duplicates_block.append({
                "extension_id": eid,
                "locations": [str(x.path) for x in entries_list],
                "error_class": "duplicate_extension_id",
            })
        status = "warning" if disc.has_duplicates() else "ok"
        data = {
            "status": status,
            "data": {
                "extensions": entries,
                "duplicates": duplicates_block,
            },
            "count": len(entries),
            "duplicate_count": len(disc.duplicates),
            "duplicate_policy": disc.duplicate_policy,
        }
        if args.json:
            print(json.dumps(data, ensure_ascii=False, indent=2,
                              default=str))
        else:
            print(f"extension catalog: {len(entries)} pack(s) "
                  f"installed (status={status})")
            for e in entries:
                print(f"  - {e.get('extension_id')} "
                      f"v{e.get('version', '?')}")
            for d in duplicates_block:
                print(f"  WARN duplicate_extension_id: "
                      f"{d['extension_id']}")
                for loc in d["locations"]:
                    print(f"      - {loc}")
            if duplicates_block:
                print(
                    "  Resolve by removing one copy or use "
                    "migration tooling."
                )
        if status == "ok":
            return 0
        # warning: --strict なら exit 1, なければ exit 0
        return 1 if getattr(args, "strict", False) else 0

    if sub == "migration-plan":
        # v2.5.0: plan only (no file changes)
        from lab_executor.extension_migration import (
            plan_extension_migration,
        )
        plan = plan_extension_migration()
        data = plan.to_dict()
        if args.json:
            print(json.dumps(data, ensure_ascii=False, indent=2,
                              default=str))
        else:
            s = data["summary"]
            print(f"extension migration-plan: status={data['status']}")
            print(f"  legacy_path:   {data['legacy_path']}")
            print(f"  new_path:      {data['new_path']}")
            print(f"  write_default: {data['write_default']}")
            print(f"  summary: legacy_only={s['legacy_only']} "
                  f"new_only={s['new_only']} "
                  f"duplicates={s['duplicates']} "
                  f"invalid={s['invalid']} "
                  f"migration_required={s['migration_required']}")
            for a in data["actions"]:
                print(f"  [{a['severity']:7s}] {a['action']}"
                      f" ({a.get('extension_id') or '-'})")
                for loc in a["locations"]:
                    print(f"      - {loc}")
                if a.get("recommendation"):
                    print(f"      -> {a['recommendation']}")
            if not data["actions"]:
                print("  (no actions; everything looks good)")
        # exit code policy:
        # ok / warning -> 0 ; warning + --strict -> 1 ; error -> 1
        st = data["status"]
        if st == "ok":
            return 0
        if st == "warning":
            return 1 if getattr(args, "strict", False) else 0
        return 1

    if sub == "paths":
        from lab_executor.extension_paths import get_extension_paths
        paths = get_extension_paths()
        data = paths.to_dict()
        if args.json:
            print(json.dumps(data, ensure_ascii=False, indent=2,
                              default=str))
        else:
            print("extension paths (v2.4, dual-path read):")
            print(f"  current_default:          "
                  f"{data['current_default']}")
            print(f"  future_default_candidate: "
                  f"{data['future_default_candidate']}")
            print(f"  legacy_path:              {data['legacy_path']}")
            print(f"  new_path:                 {data['new_path']}")
            print(f"  write_default:            "
                  f"{data['write_default']}")
            print(f"  active_read_paths:")
            for p in data["active_read_paths"]:
                print(f"    - {p}")
            print(f"  duplicate_policy:         "
                  f"{data['duplicate_policy']}")
            print(f"  migration_required:       "
                  f"{data['migration_required']}")
        return 0

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
        miss = diag.get("missing_from_registry") or []
        diag["status"] = "ok" if not miss else "warning"
        # v2.2.1: --strict のときだけ warning で exit 1。default は
        # 手元診断向けに exit 0 (warning は表示するが fail させない)。
        strict = getattr(args, "strict", False)
        diag["strict_mode"] = strict
        if args.json:
            print(json.dumps(diag, ensure_ascii=False, indent=2,
                              default=str))
        else:
            print(f"tool-surface diagnostic (backend=mock)")
            print(f"  declared: stable={diag['declared_stable']}, "
                  f"experimental={diag['declared_experimental']}, "
                  f"total={diag['declared_total']}")
            print(f"  registered: {diag['registered_count']}")
            print(f"  status: {diag['status']} "
                  f"(strict={strict})")
            if miss:
                print(f"  missing ({len(miss)}):")
                for m in miss[:20]:
                    print(f"    - {m}")
        if diag["status"] == "ok":
            return 0
        return 1 if strict else 0

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
