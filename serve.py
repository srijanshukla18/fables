#!/usr/bin/env python3
"""
Fables: a reader for agent session chronicles
=============================================

Serves a local viewer for supported agent session transcripts.
Zero dependencies, stdlib only. Zero configuration: auto-discovers
sessions from the standard locations.

Usage:
    python3 serve.py            # port 8321
    python3 serve.py 3000       # custom port

Then open: http://localhost:<port>
"""

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from fables_library import Library, LibraryError
from providers import SessionTarget, discover, load_target, resolve_session_id, AmbiguousSessionId

HERE = Path(__file__).resolve().parent
CLAUDE_ROOT = Path.home() / ".claude" / "projects"
CODEX_ROOT = Path.home() / ".codex" / "sessions"
STATIC_ASSETS = {
    "/fables.css": ("fables.css", "text/css; charset=utf-8"),
    "/fables-core.js": ("fables-core.js", "text/javascript; charset=utf-8"),
    "/fables-app.js": ("fables-app.js", "text/javascript; charset=utf-8"),
    "/fables-worker.js": ("fables-worker.js", "text/javascript; charset=utf-8"),
}

# Replaced wholesale after every scan, so deleted or moved sessions cannot
# remain addressable through an old hash.
_paths: dict[str, SessionTarget] = {}
_sessions: list[dict] = []
_provider_statuses: list[dict] = []


def scan_sessions(home: Path | None = None) -> list[dict]:
    global _paths, _sessions, _provider_statuses
    sessions, targets, statuses = discover(home)
    _paths = targets
    _sessions = sessions
    _provider_statuses = statuses
    imported = Library().list_sessions(limit=5000)
    combined = [*sessions, *imported]
    combined.sort(key=lambda item: float(item.get("mtime") or 0), reverse=True)
    return combined


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # keep the terminal quiet

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline' blob:; worker-src 'self' blob:; "
            "connect-src 'self'; img-src 'self' data:; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'",
        )
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        host_header = self.headers.get("Host", "").lower()
        if host_header.startswith("["):
            host = host_header.split("]", 1)[0] + "]"
        else:
            host = host_header.split(":", 1)[0]
        if host not in {"", "localhost", "127.0.0.1", "[::1]"}:
            self._send(403, b"for local use only", "text/plain")
            return
        route = urlparse(self.path).path
        try:
            if route == "/" or route == "/index.html":
                body = (HERE / "index.html").read_bytes()
                self._send(200, body, "text/html; charset=utf-8")
            elif route in STATIC_ASSETS:
                filename, content_type = STATIC_ASSETS[route]
                self._send(200, (HERE / filename).read_bytes(), content_type)
            elif route == "/api/sessions":
                sessions = scan_sessions()
                body = json.dumps({
                    "sessions": sessions, "providers": _provider_statuses,
                }).encode()
                self._send(200, body, "application/json")
            elif route.startswith("/api/provenance/"):
                sid = unquote(route.rsplit("/", 1)[-1])
                try:
                    result = Library().provenance(sid)
                except LibraryError as exc:
                    code = 404 if exc.code == "session_not_found" else 409
                    body = json.dumps(exc.envelope()).encode()
                    self._send(code, body, "application/json")
                    return
                self._send(200, json.dumps(result).encode(), "application/json")
            elif route.startswith("/api/session/"):
                sid = unquote(route.rsplit("/", 1)[-1])
                if sid.startswith("s_"):
                    try:
                        text = Library().get_session_text(sid)
                    except LibraryError as exc:
                        code = 404 if exc.code == "session_not_found" else 500
                        self._send(code, exc.message.encode(), "text/plain; charset=utf-8")
                        return
                    self._send(200, text.encode("utf-8"), "application/json; charset=utf-8")
                    return
                try:
                    _opaque, target = resolve_session_id(sid, _sessions, _paths)
                except AmbiguousSessionId as exc:
                    self._send(409, str(exc).encode(), "text/plain; charset=utf-8")
                    return
                except KeyError:
                    target = None
                if target is None:
                    scan_sessions()  # id from a fresh client; rebuild the live map
                    try:
                        _opaque, target = resolve_session_id(sid, _sessions, _paths)
                    except (AmbiguousSessionId, KeyError):
                        target = None
                if target is None:
                    self._send(404, b"session not found", "text/plain")
                    return
                try:
                    text = load_target(target)
                except FileNotFoundError:
                    self._send(404, b"session not found", "text/plain")
                    return
                self._send(200, text.encode("utf-8"), "text/plain; charset=utf-8")
            else:
                self._send(404, b"not found", "text/plain")
        except BrokenPipeError:
            pass
        except Exception as e:  # never take the server down over one request
            self._send(500, str(e).encode(), "text/plain")


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 8321
    print("~ Fables · a reader for agent chronicles ~")
    n = len(scan_sessions())
    counts = ", ".join(f"{item['source']}: {item['count']}" for item in _provider_statuses)
    print(f"  found {n} sessions ({counts})")
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"  reading room open at http://localhost:{port}")
    print("  ctrl-c to close")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  the library closes.")


if __name__ == "__main__":
    main()
