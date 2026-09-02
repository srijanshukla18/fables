#!/usr/bin/env python3
"""
Fables MCP — a stateless MCP server for local agent sessions.

Exposes the same session library as the Fables reading room over the
Model Context Protocol (2026-07-28 stateless spec): any MCP client
(Codex CLI, Claude Code, pi, ...) can list, fetch, and search the
conversation transcripts already stored on this machine — pi, Claude
Code, Codex, Gemini CLI, Goose, Cline, Roo Code, OpenCode, Cursor,
Cursor CLI, Kimi CLI, Command Code, Copilot CLI, Hermes Agent, Amp, Qwen Code, Aider,
Trae, Kiro, Kilo Code, Zed, Prime Agent — without knowing any provider's
on-disk format.

The protocol layer and renderer live in ``mcp_protocol.py`` so the same
handler also serves the remote Fables cloud (``cloud/fables-cloud.py``).

Transport: newline-delimited JSON-RPC 2.0 over stdio. No initialize
handshake, no protocol sessions: every request carries the protocol
version in ``_meta`` and every response is self-contained, so the
server is stateless by construction. See:
https://modelcontextprotocol.io/specification/2026-07-28

Run directly:
    python3 fables-mcp.py
Or after install:
    fables mcp

Register with Codex CLI in ~/.codex/config.toml:

    [mcp_servers.fables]
    command = "fables"
    args = ["mcp"]

Usage: fables-mcp.py [--home DIR] [--http [--port N]]
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from pathlib import Path
from typing import Any

from fables_library import Library, LibraryError
from mcp_protocol import (
    McpBackend,
    PROTOCOL_VERSION,
    TOOLS,
    make_handler,
    parse_transcript,  # re-exported for tests and embedders
    render_transcript,  # re-exported
)

from providers import discover, load_target

SERVER_NAME = "fables-mcp"
SERVER_VERSION = "0.2.0"

_HOME: Path | None = None  # overridden by --home or by tests
_LIBRARY: Path | None = None
_SCAN_TTL = 1.0            # seconds a discovery scan is cached in the backend


def session_home() -> Path:
    return _HOME or Path.home()


def session_library() -> Library:
    if _LIBRARY is not None:
        return Library(_LIBRARY)
    if _HOME is not None:
        return Library(_HOME / ".local/share/fables")
    return Library()


class LocalBackend(McpBackend):
    """Serves the local providers.py discovery and loaders."""

    def __init__(self) -> None:
        self._cache: tuple[str, float, list[dict], dict[str, Any], list[dict]] | None = None
        self._lock = threading.Lock()

    def _discover(self) -> tuple[list[dict], dict[str, Any], list[dict]]:
        with self._lock:
            now = time.monotonic()
            key = str(session_home())
            if self._cache is not None and self._cache[0] == key and \
                    now - self._cache[1] < _SCAN_TTL:
                return self._cache[2], self._cache[3], self._cache[4]
            sessions, targets, statuses = discover(session_home())
            sessions.extend(session_library().list_sessions(limit=5000))
            sessions.sort(key=lambda item: float(item.get("mtime") or 0), reverse=True)
            self._cache = (key, now, sessions, targets, statuses)
            return sessions, targets, statuses

    def list_sessions(self) -> tuple[list[dict], list[str]]:
        _sessions, _targets, statuses = self._discover()
        return _sessions, [status["source"] for status in statuses]

    def load(self, sid: str) -> str:
        if sid.startswith("s_"):
            try:
                return session_library().get_session_text(sid)
            except Exception as exc:
                if getattr(exc, "code", "") == "session_not_found":
                    raise KeyError(sid) from None
                raise
        _sessions, targets, _statuses = self._discover()
        target = targets.get(sid)
        if target is None:
            raise KeyError(sid)
        return load_target(target)

    def inspect_import(self, input_path: str, origin: str | None = None) -> dict:
        return session_library().inspect(input_path, origin=origin)

    def apply_import(self, input_path: str, origin: str,
                     expect_sha256: str) -> dict:
        result = session_library().apply(input_path, origin, expect_sha256)
        self._cache = None
        return result

    def get_import(self, import_id: str) -> dict:
        return session_library().get_import(import_id)

    def get_provenance(self, session_id: str) -> dict:
        try:
            return session_library().provenance(session_id)
        except LibraryError as exc:
            if exc.code != "session_not_found":
                raise
        sessions, _targets, _statuses = self._discover()
        entry = next((item for item in sessions if item.get("id") == session_id), None)
        if entry is None:
            raise LibraryError("session_not_found", "No session has that identifier.",
                               {"id": session_id})
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


handle_message = make_handler(
    LocalBackend(),
    server_name=SERVER_NAME,
    server_version=SERVER_VERSION,
    scope="stored on this machine",
    import_tools=True,
)


def http_main(port: int = 8322) -> int:
    """Streamable-HTTP transport over the same stateless handler.

    Serves JSON-RPC requests over POST at 127.0.0.1, bound to localhost only.
    Used by agents that cannot spawn local stdio servers (e.g. Prime Agent,
    whose kernel only wires HTTP MCP endpoints).
    """
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def _reply(self, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self._reply({"jsonrpc": "2.0", "id": None,
                         "error": {"code": -32600, "message": "use POST"}})

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode("utf-8", "replace")
            response = handle_message(raw)
            self._reply(response if response is not None else {
                "jsonrpc": "2.0", "id": None,
                "result": {"resultType": "complete"}})

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    sys.stderr.write(f"fables-mcp {SERVER_VERSION}: HTTP on http://127.0.0.1:{port}/mcp\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fables MCP server (stateless, stdio)")
    parser.add_argument("--home", default=None,
                        help="session home directory (default: $HOME)")
    parser.add_argument("--library", default=None,
                        help="durable Fables library directory")
    parser.add_argument("--http", action="store_true",
                        help="serve over HTTP (Streamable-HTTP style) instead of stdio")
    parser.add_argument("--port", type=int, default=8322,
                        help="port for --http (default: 8322)")
    args = parser.parse_args(argv)
    global _HOME, _LIBRARY
    if args.home:
        _HOME = Path(args.home).expanduser()
    if args.library:
        _LIBRARY = Path(args.library).expanduser()
    if args.http:
        return http_main(args.port)
    try:
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    sys.stderr.write(
        f"fables-mcp {SERVER_VERSION}: stateless MCP server "
        f"(protocol {PROTOCOL_VERSION}) — {len(TOOLS)} tools\n"
    )
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        response = handle_message(raw)
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
