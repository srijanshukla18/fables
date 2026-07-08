#!/usr/bin/env python3
"""
Fables: a reader for agent session chronicles
=============================================

Serves a local viewer for Claude Code and Codex session transcripts.
Zero dependencies, stdlib only. Zero configuration: auto-discovers
sessions from the standard locations.

Usage:
    python3 serve.py            # port 8321
    python3 serve.py 3000       # custom port

Then open: http://localhost:<port>
"""

import hashlib
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
CLAUDE_ROOT = Path.home() / ".claude" / "projects"
CODEX_ROOT = Path.home() / ".codex" / "sessions"

# Session id -> absolute path. Ids are content-free hashes so the client
# can never request an arbitrary filesystem path.
_paths: dict[str, Path] = {}
# Listing cache keyed by path, invalidated by (mtime, size).
_meta_cache: dict[str, dict] = {}

PREVIEW_BYTES = 262144  # read at most 256 KB per file when building the listing


def _sid(path: Path) -> str:
    return hashlib.sha1(str(path).encode()).hexdigest()[:12]


def _decode_claude_project(dirname: str) -> str:
    """'-Users-srijanshukla-code-foo-bar' -> '~/code/foo-bar'.

    Claude flattens '/' to '-', which collides with real hyphens in dir
    names. Walk the filesystem to disambiguate: at each part, prefer
    descending into a new path segment, else glue it onto the previous
    segment with a '-'.
    """
    parts = [p for p in dirname.split("-") if p != ""]

    def walk(base: Path, i: int):
        if i == len(parts):
            return base
        # a segment may span several '-'-separated parts; try growing it
        seg = parts[i]
        j = i
        while True:
            cand = base / seg
            if cand.exists():
                found = walk(cand, j + 1)
                if found is not None:
                    return found
            j += 1
            if j >= len(parts):
                return None
            seg = seg + "-" + parts[j]

    resolved = walk(Path("/"), 0)
    display = str(resolved) if resolved else "/" + "/".join(parts)
    return display.replace(str(Path.home()), "~")


def _first_lines(path: Path, max_bytes: int = PREVIEW_BYTES):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            chunk = f.read(max_bytes)
    except OSError:
        return
    for line in chunk.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue  # possibly truncated by the byte cap


def _claude_title(path: Path) -> str:
    title = ""
    first_user = ""
    for obj in _first_lines(path):
        t = obj.get("type")
        if t == "summary" and obj.get("summary"):
            title = obj["summary"]
        elif t == "ai-title" and obj.get("aiTitle"):
            title = obj["aiTitle"]
            break
        elif t == "user" and not first_user and not obj.get("isMeta"):
            content = (obj.get("message") or {}).get("content")
            if isinstance(content, str):
                text = content.strip()
            elif isinstance(content, list):
                text = " ".join(
                    b.get("text", "") for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                ).strip()
            else:
                text = ""
            if text and not text.startswith("<") and not text.startswith("Caveat:"):
                first_user = text
    return title or first_user


def _codex_scan(path: Path) -> tuple[str, str]:
    """Returns (title, cwd) from the head of a Codex rollout file."""
    title = ""
    cwd = ""
    first_user = ""
    for obj in _first_lines(path):
        t = obj.get("type")
        payload = obj.get("payload") or {}
        if t == "session_meta":
            cwd = payload.get("cwd", "")
        elif t == "event_msg":
            pt = payload.get("type")
            if pt == "thread_name_updated" and payload.get("thread_name"):
                title = payload["thread_name"]
                break
            if pt == "user_message" and not first_user:
                text = (payload.get("message") or "").strip()
                if text and not text.startswith(("<", "#")):
                    first_user = text
    return (title or first_user), cwd


def _describe(path: Path, source: str, project: str) -> dict:
    key = str(path)
    try:
        stat = path.stat()
    except OSError:
        return {}
    cached = _meta_cache.get(key)
    if cached and cached["_stamp"] == (stat.st_mtime, stat.st_size):
        return cached
    if source == "claude":
        title = _claude_title(path)
    else:
        title, cwd = _codex_scan(path)
        if cwd:
            project = cwd.replace(str(Path.home()), "~")
    meta = {
        "_stamp": (stat.st_mtime, stat.st_size),
        "id": _sid(path),
        "source": source,
        "project": project,
        "title": (title or path.stem)[:120],
        "mtime": stat.st_mtime,
        "size": stat.st_size,
    }
    _meta_cache[key] = meta
    return meta


def scan_sessions() -> list[dict]:
    sessions = []
    if CLAUDE_ROOT.is_dir():
        for proj_dir in CLAUDE_ROOT.iterdir():
            if not proj_dir.is_dir():
                continue
            project = _decode_claude_project(proj_dir.name)
            for f in proj_dir.rglob("*.jsonl"):
                meta = _describe(f, "claude", project)
                if meta:
                    meta["sub"] = "subagents" in f.parts  # story within a story
                    _paths[meta["id"]] = f
                    sessions.append(meta)
    if CODEX_ROOT.is_dir():
        for f in CODEX_ROOT.rglob("*.jsonl"):
            meta = _describe(f, "codex", "")
            if meta:
                _paths[meta["id"]] = f
                sessions.append(meta)
    sessions.sort(key=lambda s: s["mtime"], reverse=True)
    return [{k: v for k, v in s.items() if k != "_stamp"} for s in sessions]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # keep the terminal quiet

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
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
            elif route == "/api/sessions":
                body = json.dumps({"sessions": scan_sessions()}).encode()
                self._send(200, body, "application/json")
            elif route.startswith("/api/session/"):
                sid = route.rsplit("/", 1)[-1]
                path = _paths.get(sid)
                if path is None:
                    scan_sessions()  # id from a fresh client; rebuild the map
                    path = _paths.get(sid)
                if path is None or not path.is_file():
                    self._send(404, b"session not found", "text/plain")
                    return
                self._send(200, path.read_bytes(), "text/plain; charset=utf-8")
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
    print(f"  found {n} sessions "
          f"(claude: {CLAUDE_ROOT if CLAUDE_ROOT.is_dir() else 'none'}, "
          f"codex: {CODEX_ROOT if CODEX_ROOT.is_dir() else 'none'})")
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"  reading room open at http://localhost:{port}")
    print("  ctrl-c to close")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  the library closes.")


if __name__ == "__main__":
    main()
