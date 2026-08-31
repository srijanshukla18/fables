#!/usr/bin/env python3
"""
fables-cloud.py — the remote, centralized Fables service.

Aggregates sessions synced from any number of machines (via
``fables-sync.py``) and serves them to any MCP-capable agent over HTTPS,
so a session started in pi on one laptop can be listed, searched, and
fetched from Codex on another.

Endpoints
---------
    GET  /             status page (login link, privacy note)
    GET  /login        start Google sign-in
    GET  /callback     Google OAuth callback (email allowlist)
    POST /mcp          stateless MCP endpoint (Bearer token auth)
    POST /api/upload   sync endpoint for fables-sync.py (Bearer token)
    GET  /api/me       current user (Bearer token or cookie)
    GET  /health       liveness probe

Configuration (environment variables)
-------------------------------------
    GOOGLE_CLIENT_ID       OAuth client id (Google Cloud Console)
    GOOGLE_CLIENT_SECRET   OAuth client secret
    ALLOWED_EMAILS         comma-separated allowlist, e.g. me@gmail.com
    BASE_URL               public base, e.g. https://fables.example.com
    DATA_DIR               storage directory (default: ./data)
    SYNC_TOKENS            optional comma-separated pre-shared upload tokens
    GOOGLE_TOKEN_URL       test override (default: oauth2.googleapis.com)
    GOOGLE_USERINFO_URL    test override (default: www.googleapis.com)

Privacy: transcripts contain prompts, code, and possibly secrets. The
service must be able to read them to serve them. Restrict the email
allowlist to yourself and treat the database as sensitive.

Run:
    GOOGLE_CLIENT_ID=... GOOGLE_CLIENT_SECRET=... ALLOWED_EMAILS=me@gmail.com \\
    BASE_URL=https://fables.example.com python3 fables-cloud.py
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp_protocol import McpBackend, make_handler  # noqa: E402

SERVER_NAME = "fables-cloud"
SERVER_VERSION = "0.1.0"


class Cloud:
    """Auth + storage behind the HTTP handlers."""

    def __init__(self, data_dir: Path, allowed_emails: set[str],
                 sync_tokens: set[str]):
        self.data_dir = data_dir
        self.allowed_emails = allowed_emails
        self.sync_tokens = {self._hash(token) for token in sync_tokens}
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(data_dir / "fables.db"), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS users ("
            " email TEXT PRIMARY KEY, created_at INTEGER NOT NULL)")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS tokens ("
            " token_hash TEXT PRIMARY KEY, email TEXT NOT NULL, kind TEXT NOT NULL,"
            " created_at INTEGER NOT NULL, last_seen INTEGER NOT NULL DEFAULT 0)")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS sessions ("
            " machine TEXT NOT NULL, local_id TEXT NOT NULL,"
            " sid TEXT NOT NULL UNIQUE, source TEXT NOT NULL DEFAULT '',"
            " title TEXT NOT NULL DEFAULT '', cwd TEXT NOT NULL DEFAULT '',"
            " project TEXT NOT NULL DEFAULT '', mtime REAL NOT NULL DEFAULT 0,"
            " size INTEGER NOT NULL DEFAULT 0, transcript TEXT NOT NULL DEFAULT '',"
            " native_id TEXT NOT NULL DEFAULT '',"
            " updated_at INTEGER NOT NULL,"
            " PRIMARY KEY (machine, local_id))")
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_sessions_updated"
                        " ON sessions(updated_at)")
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(sessions)")}
        if "native_id" not in columns:
            self.db.execute(
                "ALTER TABLE sessions ADD COLUMN native_id TEXT NOT NULL DEFAULT ''")
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_sessions_native_id"
                        " ON sessions(native_id)")
        self.db.commit()

    @staticmethod
    def _hash(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def mint_token(self, email: str) -> str:
        token = secrets.token_urlsafe(24)
        now = int(time.time())
        self.db.execute(
            "INSERT OR REPLACE INTO tokens (token_hash, email, kind, created_at,"
            " last_seen) VALUES (?, ?, 'device', ?, ?)",
            (self._hash(token), email, now, now))
        self.db.commit()
        return token

    def check_token(self, token: str | None) -> str | None:
        if not token:
            return None
        row = self.db.execute(
            "SELECT email FROM tokens WHERE token_hash = ?",
            (self._hash(token),)).fetchone()
        if row is None and token in self.sync_tokens:
            return "sync"
        if row is None:
            return None
        self.db.execute(
            "UPDATE tokens SET last_seen = ? WHERE token_hash = ?",
            (int(time.time()), self._hash(token)))
        self.db.commit()
        return row["email"]

    def add_user(self, email: str) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO users (email, created_at) VALUES (?, ?)",
            (email, int(time.time())))
        self.db.commit()

    def _sid(self, machine: str, local_id: str) -> str:
        return hashlib.sha1(f"{machine}\0{local_id}".encode()).hexdigest()[:12]

    def upload(self, machine: str, sessions: list[dict]) -> int:
        now = int(time.time())
        count = 0
        for session in sessions:
            if not isinstance(session, dict) or not session.get("local_id"):
                continue
            local_id = str(session["local_id"])[:200]
            sid = self._sid(machine, local_id)
            self.db.execute(
                "INSERT OR REPLACE INTO sessions (machine, local_id, sid, source,"
                " title, cwd, project, mtime, size, transcript, native_id, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (machine, local_id, sid,
                 str(session.get("source") or "")[:50],
                 str(session.get("title") or "")[:200],
                 str(session.get("cwd") or "")[:500],
                 str(session.get("project") or "")[:500],
                 float(session.get("mtime") or 0),
                 int(session.get("size") or 0),
                 str(session.get("transcript") or ""),
                 str(session.get("native_id") or "")[:200],
                 now))
            count += 1
        self.db.commit()
        return count

    def prune(self, machine: str, local_ids: list[str]) -> int:
        if not local_ids:
            return 0
        placeholders = ",".join("?" for _ in local_ids)
        cursor = self.db.execute(
            f"DELETE FROM sessions WHERE machine = ? AND local_id IN ({placeholders})",
            [machine, *local_ids])
        self.db.commit()
        return cursor.rowcount

    def entries(self) -> tuple[list[dict], list[str]]:
        rows = self.db.execute(
            "SELECT sid, source, title, cwd, project, mtime, native_id FROM sessions"
            " ORDER BY updated_at DESC").fetchall()
        entries = [{
            "id": row["sid"],
            "source": row["source"],
            "title": row["title"],
            "cwd": row["cwd"],
            "project": row["project"],
            "mtime": row["mtime"],
            **({"native_id": row["native_id"]} if row["native_id"] else {}),
        } for row in rows]
        sources = sorted({str(row["source"]) for row in rows if row["source"]})
        return entries, sources

    def load(self, sid: str) -> str:
        row = self.db.execute(
            "SELECT transcript FROM sessions WHERE sid = ?", (sid,)).fetchone()
        if row is None:
            raise KeyError(sid)
        return row["transcript"]


class CloudBackend(McpBackend):
    def __init__(self, cloud: Cloud):
        self.cloud = cloud

    def list_sessions(self) -> tuple[list[dict], list[str]]:
        return self.cloud.entries()

    def load(self, sid: str) -> str:
        return self.cloud.load(sid)


class Handler(BaseHTTPRequestHandler):
    server_version = "fables-cloud"

    def log_message(self, *_args):
        pass

    # -- helpers -----------------------------------------------------------

    def _send(self, code: int, body: bytes, ctype: str, headers: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload: Any):
        self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json")

    def _redirect(self, location: str):
        self._send(302, b"", "text/plain", {"Location": location})

    def _html(self, code: int, title: str, body: str):
        page = f"""<!doctype html><html><head><meta charset="utf-8">
<title>{title}</title><style>
body{{font-family:system-ui;max-width:640px;margin:3rem auto;padding:0 1rem;color:#222}}
code{{background:#f4f4f4;padding:2px 6px;border-radius:4px}}
pre{{background:#f4f4f4;padding:1rem;border-radius:8px;overflow:auto}}
</style></head><body><h1>{title}</h1>{body}</body></html>"""
        self._send(code, page.encode("utf-8"), "text/html; charset=utf-8")

    def _bearer(self) -> str | None:
        header = self.headers.get("Authorization", "")
        if header.startswith("Bearer "):
            return header[len("Bearer "):].strip()
        return None

    def _body_json(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        raw = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        try:
            value = json.loads(raw or "{}")
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, dict) else None

    # -- google auth -------------------------------------------------------

    def _google_auth_url(self, state: str) -> str:
        params = urllib.parse.urlencode({
            "client_id": os.environ["GOOGLE_CLIENT_ID"],
            "redirect_uri": self._base_url() + "/callback",
            "response_type": "code",
            "scope": "openid email",
            "state": state,
        })
        return "https://accounts.google.com/o/oauth2/v2/auth?" + params

    def _base_url(self) -> str:
        return os.environ.get("BASE_URL", "").rstrip("/")

    def _google_token(self, code: str) -> dict | None:
        token_url = os.environ.get("GOOGLE_TOKEN_URL",
                                   "https://oauth2.googleapis.com/token")
        body = urllib.parse.urlencode({
            "code": code,
            "client_id": os.environ["GOOGLE_CLIENT_ID"],
            "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
            "redirect_uri": self._base_url() + "/callback",
            "grant_type": "authorization_code",
        }).encode()
        request = urllib.request.Request(token_url, data=body)
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.loads(response.read())
        except Exception:
            return None

    def _google_email(self, access_token: str) -> str | None:
        userinfo_url = os.environ.get(
            "GOOGLE_USERINFO_URL", "https://www.googleapis.com/oauth2/v3/userinfo")
        request = urllib.request.Request(
            userinfo_url, headers={"Authorization": f"Bearer {access_token}"})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                data = json.loads(response.read())
        except Exception:
            return None
        email = str(data.get("email") or "")
        return email if email else None

    # -- routes ------------------------------------------------------------

    def do_GET(self):
        cloud: Cloud = self.server.cloud
        path = urllib.parse.urlparse(self.path).path
        if path == "/health":
            self._json(200, {"ok": True, "server": SERVER_NAME})
            return
        if path == "/":
            email = cloud.check_token(self._bearer())
            if email:
                self._html(200, "Fables cloud", f"<p>Signed in as <b>{email}</b>.</p>"
                           f"<p>MCP endpoint: <code>{self._base_url()}/mcp</code></p>"
                           "<p>See <code>fables-sync.py --help</code> for the sync "
                           "client.</p>")
            else:
                self._html(200, "Fables cloud",
                           '<p><a href="/login">Sign in with Google</a></p>'
                           "<p>Privacy: transcripts contain code and possibly "
                           "secrets. This service is private; do not share "
                           "access.</p>")
            return
        if path == "/login":
            state = secrets.token_urlsafe(12)
            self._redirect(self._google_auth_url(state))
            return
        if path == "/callback":
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            code = (query.get("code") or [""])[0]
            if not code:
                self._html(400, "Fables cloud", "<p>Missing code.</p>")
                return
            token_data = self._google_token(code)
            if not token_data or not token_data.get("access_token"):
                self._html(401, "Fables cloud", "<p>Google sign-in failed.</p>")
                return
            email = self._google_email(token_data["access_token"])
            if not email or email not in cloud.allowed_emails:
                self._html(403, "Fables cloud",
                           f"<p><b>{email or 'unknown email'}</b> is not allowed "
                           "on this Fables cloud.</p>")
                return
            cloud.add_user(email)
            device_token = cloud.mint_token(email)
            self._html(200, "Fables cloud",
                       f"<p>Signed in as <b>{email}</b>.</p>"
                       "<p>Device token (save it; use for MCP and sync):</p>"
                       f"<pre>{device_token}</pre>"
                       f"<p>MCP endpoint: <code>{self._base_url()}/mcp</code></p>")
            return
        if path == "/api/me":
            email = cloud.check_token(self._bearer())
            if email is None:
                self._json(401, {"error": "unauthorized"})
            else:
                self._json(200, {"email": email, "sessions": self._count()})
            return
        self._json(404, {"error": "not found"})

    def _count(self) -> int:
        row = self.server.cloud.db.execute(
            "SELECT COUNT(*) AS n FROM sessions").fetchone()
        return int(row["n"])

    def do_POST(self):
        cloud: Cloud = self.server.cloud
        path = urllib.parse.urlparse(self.path).path
        if path == "/mcp":
            email = cloud.check_token(self._bearer())
            if email is None:
                self._json(401, {"error": "unauthorized"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode("utf-8", "replace")
            response = self.server.mcp_handler(raw)
            if response is None:
                response = {"jsonrpc": "2.0", "id": None,
                            "result": {"resultType": "complete"}}
            self._json(200, response)
            return
        if path == "/api/upload":
            email = cloud.check_token(self._bearer())
            if email is None:
                self._json(401, {"error": "unauthorized"})
                return
            body = self._body_json()
            if body is None:
                self._json(400, {"error": "invalid JSON body"})
                return
            machine = str(body.get("machine") or "default")[:80]
            uploaded = cloud.upload(machine, body.get("sessions") or [])
            pruned = cloud.prune(machine, body.get("prune") or [])
            self._json(200, {"uploaded": uploaded, "pruned": pruned,
                             "sessions": self._count()})
            return
        self._json(404, {"error": "not found"})


def make_server(cloud: Cloud, port: int = 8000) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.cloud = cloud
    backend = CloudBackend(cloud)
    server.mcp_handler = make_handler(
        backend, server_name=SERVER_NAME, server_version=SERVER_VERSION,
        scope="in your Fables cloud")
    return server


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Fables cloud server")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--data", default=None,
                        help="storage directory (default: ./data)")
    args = parser.parse_args(argv)
    for required in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "ALLOWED_EMAILS",
                     "BASE_URL"):
        if not os.environ.get(required):
            print(f"error: {required} is required", file=sys.stderr)
            return 1
    data_dir = Path(args.data or "data").expanduser()
    allowed = {item.strip() for item in
               os.environ.get("ALLOWED_EMAILS", "").split(",") if item.strip()}
    sync_tokens = {item.strip() for item in
                   os.environ.get("SYNC_TOKENS", "").split(",") if item.strip()}
    cloud = Cloud(data_dir, allowed, sync_tokens)
    server = make_server(cloud, args.port)
    print(f"fables-cloud {SERVER_VERSION}: https://{os.environ['BASE_URL']}")
    print(f"  allowlist: {', '.join(sorted(allowed))}")
    print(f"  data: {data_dir.resolve()}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
