#!/usr/bin/env python3
"""Agent-friendly Fables command-line interface."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

# Read-only commands must not create local bytecode cache files as a side
# effect of inspection.
sys.dont_write_bytecode = True

from fables_library import Library, LibraryError
from mcp_protocol import parse_transcript, render_transcript
from providers import AmbiguousSessionId, discover, load_target, resolve_session_id, session_haystack


def _json_requested(argv: list[str]) -> bool:
    for index, value in enumerate(argv):
        if value == "--format" and index + 1 < len(argv) and argv[index + 1] == "json":
            return True
        if value == "--format=json":
            return True
    return False


def _emit_json(result: Any, stream=sys.stdout) -> None:
    json.dump({"ok": True, "result": result}, stream, ensure_ascii=False, indent=2)
    stream.write("\n")


def _emit_error(error: LibraryError, json_output: bool) -> int:
    if json_output:
        json.dump(error.envelope(), sys.stderr, ensure_ascii=False, indent=2)
        sys.stderr.write("\n")
    else:
        sys.stderr.write(f"fables: {error.message}\n")
        if error.details:
            sys.stderr.write(json.dumps(error.details, ensure_ascii=False) + "\n")
    return 1


def _add_common_format(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--format", choices=("text", "json"), default="text",
                        help="output format (agents should use json)")
    parser.add_argument("--library", metavar="DIR", help=argparse.SUPPRESS)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fables",
        description="Read live and imported coding-agent sessions without mutating provider stores.",
    )
    groups = parser.add_subparsers(dest="group", metavar="COMMAND")

    imports = groups.add_parser(
        "import",
        help="inspect and add external sessions to the durable Fables library",
        description="Import is separate from native restore and cross-harness handoff. Bare `fables import` only displays this help.",
    )
    import_commands = imports.add_subparsers(dest="import_command", metavar="COMMAND")
    inspect = import_commands.add_parser("inspect", help="read-only validation and import classification")
    inspect.add_argument("input", help="one exact Fables ZIP, session JSONL, or standalone HTML file")
    inspect.add_argument("--origin", help="optional user-supplied origin for precise revision/conflict classification")
    _add_common_format(inspect)
    apply = import_commands.add_parser("apply", help="atomically apply an inspected input")
    apply.add_argument("input", help="the exact input that was inspected")
    apply.add_argument("--origin", required=True, help="user-supplied source machine or system label")
    apply.add_argument("--expect-sha256", required=True, help="sha256 returned by import inspect")
    _add_common_format(apply)
    get_import = import_commands.add_parser("get", help="verify an import by opaque import ID")
    get_import.add_argument("import_id", help="opaque im_... ID returned by import apply")
    _add_common_format(get_import)

    sessions = groups.add_parser(
        "session", help="list, search, read, and inspect session provenance",
        description="Live and imported sessions share this read surface. Mutations are not available in this group.",
    )
    session_commands = sessions.add_subparsers(dest="session_command", metavar="COMMAND")
    list_parser = session_commands.add_parser("list", help="list live and imported sessions")
    list_parser.add_argument("--origin", help="restrict imported sessions to one origin")
    list_parser.add_argument("--source", help="restrict to one provider/harness")
    list_parser.add_argument("--scope", choices=("all", "live", "imported"), default="all")
    list_parser.add_argument("--query", help="filter session metadata")
    list_parser.add_argument("--limit", type=int, default=100)
    list_parser.add_argument("--home", help="alternate home for read-only live provider discovery")
    _add_common_format(list_parser)
    search = session_commands.add_parser("search", help="search normalized session passages")
    search.add_argument("query")
    search.add_argument("--origin")
    search.add_argument("--source")
    search.add_argument("--scope", choices=("all", "live", "imported"), default="all")
    search.add_argument("--include-tools", action="store_true")
    search.add_argument("--include-thinking", action="store_true")
    search.add_argument("--limit", type=int, default=20)
    search.add_argument("--home", help="alternate home for read-only live provider discovery")
    _add_common_format(search)
    get_session = session_commands.add_parser("get", help="read one session using a returned opaque ID")
    get_session.add_argument("session_id")
    get_session.add_argument("--include-tools", action="store_true")
    get_session.add_argument("--include-thinking", action="store_true")
    get_session.add_argument("--home", help="alternate home for read-only live provider discovery")
    get_session.add_argument("--format", choices=("markdown", "json"), default="markdown")
    get_session.add_argument("--library", metavar="DIR", help=argparse.SUPPRESS)
    provenance = session_commands.add_parser("provenance", help="show identity, hashes, import, and revision history")
    provenance.add_argument("session_id")
    provenance.add_argument("--home", help="alternate home for read-only live provider discovery")
    _add_common_format(provenance)

    export = groups.add_parser(
        "export", help="sharing and private migration profiles (separate operations)",
        description="Sharing and private migration are deliberately distinct. The browser currently creates sharing exports; lossless provider migration export remains a future compatibility-sensitive operation.",
    )
    export_commands = export.add_subparsers(dest="export_command", metavar="COMMAND")
    export_share = export_commands.add_parser(
        "share", help="sharing profile (currently created in the reading-room UI)")
    _add_common_format(export_share)
    export_migrate = export_commands.add_parser(
        "migrate", help="future lossless private migration bundle")
    _add_common_format(export_migrate)

    handoff = groups.add_parser(
        "handoff", help="future: create a new target-harness context package",
        description="Handoff is not native resume or restore. No provider-private transcript is forged by Fables.",
    )
    handoff.add_argument("session_id", nargs="?")
    handoff.add_argument("--to", choices=("codex", "claude", "pi"))
    handoff.add_argument("--format", choices=("text", "json"), default="text")

    return parser


def _library(args: argparse.Namespace) -> Library:
    return Library(getattr(args, "library", None))


def _live(home: str | None = None):
    root = Path(home).expanduser() if home else None
    return discover(root)


def _live_rows(home: str | None, source: str | None, query: str | None) -> tuple[list[dict], dict, list[dict]]:
    sessions, targets, statuses = _live(home)
    rows = []
    needle = str(query or "").casefold()
    for original in sessions:
        if source and original.get("source") != source:
            continue
        if needle and needle not in session_haystack(original):
            continue
        row = dict(original)
        row.update({"archived": False, "origin": None, "state": "live"})
        rows.append(row)
    return rows, targets, statuses


def _list(args: argparse.Namespace) -> dict[str, Any]:
    limit = max(1, min(args.limit, 5000))
    rows: list[dict] = []
    statuses: list[dict] = []
    if args.scope in {"all", "imported"}:
        rows.extend(_library(args).list_sessions(
            origin=args.origin, source=args.source, query=args.query, limit=limit,
        ))
    if args.scope in {"all", "live"} and not args.origin:
        live, _targets, statuses = _live_rows(args.home, args.source, args.query)
        rows.extend(live)
    rows.sort(key=lambda row: float(row.get("mtime") or 0), reverse=True)
    rows = rows[:limit]
    return {
        "sessions": rows,
        "count": len(rows),
        "sources": sorted({str(row.get("source")) for row in rows if row.get("source")}),
        "provider_status": statuses,
    }


def _search_live(args: argparse.Namespace, remaining: int) -> list[dict]:
    sessions, targets, _statuses = _live(args.home)
    query = args.query.casefold()
    matches: list[dict] = []
    for entry in sessions[:250]:
        if args.source and entry.get("source") != args.source:
            continue
        if query in session_haystack(entry):
            snippet = "identifier or metadata match"
        else:
            target = targets.get(entry["id"])
            if target is None:
                continue
            try:
                raw = load_target(target)
            except (OSError, KeyError):
                continue
            rendered = render_transcript(
                raw, max_chars=60_000,
                include_tools=args.include_tools,
                include_thinking=args.include_thinking,
            )
            index = rendered.casefold().find(query)
            if index < 0:
                continue
            snippet = " ".join(rendered[max(0, index - 100):index + len(args.query) + 180].split())[:320]
        row = dict(entry)
        row.update({"archived": False, "origin": None, "state": "live", "snippet": snippet})
        matches.append(row)
        if len(matches) >= remaining:
            break
    return matches


def _search(args: argparse.Namespace) -> dict[str, Any]:
    limit = max(1, min(args.limit, 100))
    matches: list[dict] = []
    if args.scope in {"all", "imported"}:
        matches.extend(_library(args).search(
            args.query, origin=args.origin, source=args.source,
            include_tools=args.include_tools, include_thinking=args.include_thinking,
            limit=limit,
        ))
    if args.scope in {"all", "live"} and not args.origin and len(matches) < limit:
        matches.extend(_search_live(args, limit - len(matches)))
    matches.sort(key=lambda row: float(row.get("mtime") or 0), reverse=True)
    return {"matches": matches[:limit], "count": min(len(matches), limit)}


def _try_imported(lib: Library, session_id: str) -> dict[str, Any] | None:
    try:
        return lib.get_session(session_id)
    except LibraryError as exc:
        if exc.code == "session_not_found":
            return None
        raise


def _get(args: argparse.Namespace) -> tuple[str, Any]:
    lib = _library(args)
    imported = _try_imported(lib, args.session_id)
    if imported is not None:
        raw = json.dumps(imported["archive"], ensure_ascii=False)
        if args.format == "json":
            return "json", imported
        return "markdown", render_transcript(
            raw, include_tools=args.include_tools,
            include_thinking=args.include_thinking,
        )
    sessions, targets, _statuses = _live(args.home)
    try:
        sid, target = resolve_session_id(args.session_id, sessions, targets)
    except AmbiguousSessionId as exc:
        raise LibraryError("ambiguous_session", str(exc)) from None
    except KeyError:
        raise LibraryError("session_not_found", "No live or imported session has that identifier.", {"id": args.session_id}) from None
    raw = load_target(target)
    entry = next(row for row in sessions if row["id"] == sid)
    if args.format == "json":
        parsed = parse_transcript(raw)
        archive = {"fablesVersion": 2, "meta": parsed["meta"], "items": parsed["items"]}
        return "json", {"session": {**entry, "archived": False, "origin": None}, "archive": archive}
    return "markdown", render_transcript(
        raw, include_tools=args.include_tools,
        include_thinking=args.include_thinking,
    )


def _provenance(args: argparse.Namespace) -> dict[str, Any]:
    lib = _library(args)
    try:
        return lib.provenance(args.session_id)
    except LibraryError as exc:
        if exc.code != "session_not_found":
            raise
    sessions, targets, _statuses = _live(args.home)
    try:
        sid, target = resolve_session_id(args.session_id, sessions, targets)
    except AmbiguousSessionId as exc:
        raise LibraryError("ambiguous_session", str(exc)) from None
    except KeyError:
        raise LibraryError("session_not_found", "No live or imported session has that identifier.", {"id": args.session_id}) from None
    entry = next(row for row in sessions if row["id"] == sid)
    return {
        "session": {**entry, "archived": False, "origin": None},
        "provenance": [{
            "state": "live", "provider": entry.get("source"),
            "native_id": entry.get("native_id") or None,
            "provider_owned": True, "read_only": True,
        }],
        "relationships": {"revision_of": None, "revised_by": []},
        "attachments": [],
    }


def _print_inspection(result: dict[str, Any]) -> None:
    sessions = result["sessions"]
    print(f"{result['format']} · {result['sha256']}")
    print(
        f"{sessions['found']} found: {sessions['new']} new, "
        f"{sessions['duplicates']} duplicates, {sessions['revisions']} revisions, "
        f"{sessions['conflicts']} conflicts, {sessions['unreadable']} unreadable"
    )
    if result["sources"]:
        print("sources: " + ", ".join(f"{key} {value}" for key, value in result["sources"].items()))
    for warning in result["warnings"]:
        print("warning: " + warning)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    json_output = _json_requested(argv)
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.group is None:
        parser.print_help()
        return 0
    group_parser = next(action for action in parser._actions if isinstance(action, argparse._SubParsersAction)).choices[args.group]
    if args.group == "import" and args.import_command is None:
        group_parser.print_help()
        return 0
    if args.group == "session" and args.session_command is None:
        group_parser.print_help()
        return 0
    if args.group == "export" and args.export_command is None:
        group_parser.print_help()
        return 0
    if args.group == "handoff" and args.session_id is None:
        group_parser.print_help()
        return 0
    try:
        if args.group == "export":
            profile = "sharing" if args.export_command == "share" else "private migration"
            raise LibraryError(
                "operation_not_available",
                f"The {profile} export CLI is not part of the initial import release. "
                + ("Use the reading-room export review for sharing archives."
                   if args.export_command == "share" else
                   "No lossless provider bundle will be claimed until provider data and attachments can be preserved precisely."),
            )
        if args.group == "handoff":
            raise LibraryError(
                "operation_not_available",
                "Handoff adapters are not part of the initial import release; Fables will not forge target-harness history.",
            )
        if args.group == "import":
            lib = _library(args)
            if args.import_command == "inspect":
                result = lib.inspect(args.input, origin=args.origin)
                if args.format == "json":
                    _emit_json(result)
                else:
                    _print_inspection(result)
            elif args.import_command == "apply":
                result = lib.apply(args.input, args.origin, args.expect_sha256)
                if args.format == "json":
                    _emit_json(result)
                else:
                    print(f"{result['import_id']} · {result['state']} · {len(result['created'])} created, {len(result['revisions'])} revisions, {len(result['duplicates'])} duplicates")
            else:
                result = lib.get_import(args.import_id)
                if args.format == "json":
                    _emit_json(result)
                else:
                    print(f"{result['import_id']} · {result['state']} · {result['origin']}")
        elif args.session_command == "list":
            result = _list(args)
            if args.format == "json":
                _emit_json(result)
            else:
                for row in result["sessions"]:
                    suffix = f" · archived from {row['origin']}" if row.get("archived") else " · live"
                    print(f"{row['id']}  {row.get('source')}  {row.get('title')}{suffix}")
        elif args.session_command == "search":
            result = _search(args)
            if args.format == "json":
                _emit_json(result)
            else:
                for row in result["matches"]:
                    print(f"{row['id']}  {row.get('source')}  {row.get('title')}\n  {row.get('snippet')}")
        elif args.session_command == "get":
            kind, result = _get(args)
            if kind == "json":
                _emit_json(result)
            else:
                print(result)
        else:
            result = _provenance(args)
            if args.format == "json":
                _emit_json(result)
            else:
                session = result["session"]
                state = f"archived from {session.get('origin')}" if session.get("archived") else "live provider session"
                print(f"{session['id']} · {session.get('source')} · {state}")
        return 0
    except LibraryError as exc:
        return _emit_error(exc, json_output)
    except (OSError, sqlite3.Error) as exc:
        return _emit_error(LibraryError("operation_failed", "The operation failed without changing provider storage.", {"reason": str(exc)}), json_output)
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
