#!/usr/bin/env python3
"""
Fables MCP — a stateless MCP server for local agent sessions.

Exposes the same session library as the Fables reading room over the
Model Context Protocol (2026-07-28 stateless spec): any MCP client
(Codex CLI, Claude Code, pi, ...) can list, fetch, and search the
conversation transcripts already stored on this machine — pi, Claude
Code, Codex, Gemini CLI, Goose, Cline, Roo Code, OpenCode, Cursor,
Cursor CLI, Kimi CLI, Command Code, Copilot CLI, Amp, Qwen Code, Aider,
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
SERVER_VERSION = "0.1.0"

_HOME: Path | None = None  # overridden by --home or by tests
_SCAN_TTL = 1.0            # seconds a discovery scan is cached in the backend


def session_home() -> Path:
    return _HOME or Path.home()


class LocalBackend(McpBackend):
    """Serves the local providers.py discovery and loaders."""

    def __init__(self) -> None:
        self._cache: tuple[float, list[dict], dict[str, Any], list[dict]] | None = None
        self._lock = threading.Lock()

    def _discover(self) -> tuple[list[dict], dict[str, Any], list[dict]]:
        with self._lock:
            now = time.monotonic()
            key = str(session_home())
            if self._cache is not None and self._cache[0] == key and \
                    now - self._cache[1] < _SCAN_TTL:
                return self._cache[2], self._cache[3], self._cache[4]
            sessions, targets, statuses = discover(session_home())
            self._cache = (key, now, sessions, targets, statuses)
            return sessions, targets, statuses

    def list_sessions(self) -> tuple[list[dict], list[str]]:
        _sessions, _targets, statuses = self._discover()
        return _sessions, [status["source"] for status in statuses]

    def load(self, sid: str) -> str:
        _sessions, targets, _statuses = self._discover()
        target = targets.get(sid)
        if target is None:
            raise KeyError(sid)
        return load_target(target)


handle_message = make_handler(
    LocalBackend(),
    server_name=SERVER_NAME,
    server_version=SERVER_VERSION,
    scope="stored on this machine",
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
    parser.add_argument("--http", action="store_true",
                        help="serve over HTTP (Streamable-HTTP style) instead of stdio")
    parser.add_argument("--port", type=int, default=8322,
                        help="port for --http (default: 8322)")
    args = parser.parse_args(argv)
    global _HOME
    if args.home:
        _HOME = Path(args.home).expanduser()
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
