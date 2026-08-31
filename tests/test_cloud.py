import importlib.util
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location(
    "fables_cloud", ROOT / "cloud" / "fables-cloud.py")
fables_cloud = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(fables_cloud)


class MockGoogle(BaseHTTPRequestHandler):
    """Fake Google token + userinfo endpoints."""

    def log_message(self, *_args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        body = json.dumps({"access_token": "fake-access", "token_type": "Bearer",
                           "expires_in": 3600}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        body = json.dumps({"email": "owner@example.com",
                           "email_verified": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class CloudTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scratch = ROOT / ".test-work"
        cls.scratch.mkdir(exist_ok=True)
        cls.google = ThreadingHTTPServer(("127.0.0.1", 0), MockGoogle)
        cls.google_thread = threading.Thread(
            target=cls.google.serve_forever, daemon=True)
        cls.google_thread.start()
        cls.google_url = f"http://127.0.0.1:{cls.google.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.google.shutdown()
        shutil.rmtree(cls.scratch, ignore_errors=True)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=self.scratch)
        self.home = Path(self.temp.name)
        self.data = self.home / "data"
        self.old_env = {key: os.environ.get(key) for key in (
            "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "ALLOWED_EMAILS",
            "BASE_URL", "GOOGLE_TOKEN_URL", "GOOGLE_USERINFO_URL")}
        os.environ.update({
            "GOOGLE_CLIENT_ID": "test-client",
            "GOOGLE_CLIENT_SECRET": "test-secret",
            "ALLOWED_EMAILS": "owner@example.com",
            "BASE_URL": "http://127.0.0.1:1",
            "GOOGLE_TOKEN_URL": self.google_url + "/token",
            "GOOGLE_USERINFO_URL": self.google_url + "/userinfo",
        })
        self.cloud = fables_cloud.Cloud(self.data, {"owner@example.com"}, set())
        self.server = fables_cloud.make_server(self.cloud, 0)
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.token = self.cloud.mint_token("owner@example.com")

    def tearDown(self):
        self.server.shutdown()
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temp.cleanup()

    def request(self, path, method="GET", body=None, token=None):
        headers = {}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(
            self.base + path, method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                raw = response.read()
                try:
                    return response.status, json.loads(raw)
                except json.JSONDecodeError:
                    return response.status, raw.decode("utf-8", "replace")
        except urllib.error.HTTPError as error:
            raw = error.read()
            try:
                return error.code, json.loads(raw)
            except json.JSONDecodeError:
                return error.code, raw.decode("utf-8", "replace")

    def test_google_login_mints_device_token(self):
        status, _body = self.request("/callback?code=fake-code")
        self.assertEqual(status, 200)

    def test_google_login_rejects_unknown_email(self):
        self.cloud.allowed_emails = {"someone-else@example.com"}
        status, _body = self.request("/callback?code=fake-code")
        self.assertEqual(status, 403)

    def test_mcp_requires_auth(self):
        status, _body = self.request("/mcp", method="POST", body={
            "jsonrpc": "2.0", "id": 1, "method": "tools/list",
            "params": {"_meta": {"io.modelcontextprotocol/protocolVersion":
                                 "2026-07-28"}}})
        self.assertEqual(status, 401)

    def test_upload_then_mcp_roundtrip(self):
        status, result = self.request("/api/upload", method="POST", token=self.token,
                                      body={"machine": "macbook",
                                            "sessions": [{
                                                "local_id": "abc123",
                                                "source": "pi",
                                                "title": "Cloud session",
                                                "cwd": "/work",
                                                "mtime": 1_700_000_000,
                                                "size": 123,
                                                "transcript":
                                                    '{"type":"message","message":'
                                                    '{"role":"user","content":'
                                                    '"Remote hello"}}\n',
                                                "native_id":
                                                    "019ffc61-f135-743c-902d-aecdc76d6975"}]})
        self.assertEqual(status, 200)
        self.assertEqual(result["uploaded"], 1)

        def mcp(method, params):
            payload = {"jsonrpc": "2.0", "id": 1, "method": method}
            if params is not None:
                payload["params"] = {**params, "_meta": {
                    "io.modelcontextprotocol/protocolVersion": "2026-07-28"}}
            _status, response = self.request("/mcp", method="POST",
                                             body=payload, token=self.token)
            return response

        listed = mcp("tools/call", {"name": "list_sessions", "arguments": {}})
        rows = json.loads(listed["result"]["content"][0]["text"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "pi")
        self.assertEqual(rows[0]["native_id"],
                         "019ffc61-f135-743c-902d-aecdc76d6975")
        sid = rows[0]["id"]

        transcript = mcp("tools/call", {"name": "get_session",
                                        "arguments": {"id": sid}})
        text = transcript["result"]["content"][0]["text"]
        self.assertIn("Remote hello", text)

        by_native = mcp("tools/call", {"name": "get_session",
                                       "arguments": {
                                           "id": "019ffc61-f135-743c-902d-aecdc76d6975"}})
        native_text = by_native["result"]["content"][0]["text"]
        self.assertIn("Remote hello", native_text)

        matches = json.loads(mcp("tools/call", {"name": "search_sessions",
                                                "arguments": {"query": "Remote"}})
                             ["result"]["content"][0]["text"])
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["id"], sid)

    def test_upload_prune(self):
        self.request("/api/upload", method="POST", token=self.token,
                     body={"machine": "m1", "sessions": [
                         {"local_id": "one", "transcript": "a\n"},
                         {"local_id": "two", "transcript": "b\n"}]})
        status, result = self.request("/api/upload", method="POST", token=self.token,
                                      body={"machine": "m1", "prune": ["one"]})
        self.assertEqual(status, 200)
        self.assertEqual(result["pruned"], 1)
        _status, me = self.request("/api/me", token=self.token)
        self.assertEqual(me["sessions"], 1)

    def test_sync_client_end_to_end(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "fables_sync", ROOT / "fables-sync.py")
        fables_sync = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(fables_sync)

        import providers as providers_mod

        def write_jsonl(path, values):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("".join(json.dumps(v) + "\n" for v in values),
                            encoding="utf-8")
            return path

        home = self.home / "machine-home"
        write_jsonl(home / ".pi/agent/sessions/--work--/sess-1.jsonl", [
            {"type": "session", "version": 3, "id": "x",
             "timestamp": "2026-08-11T17:10:33.643Z", "cwd": str(home / "work")},
            {"type": "message", "id": "u1", "timestamp": "2026-08-11T17:10:36Z",
             "message": {"role": "user", "content": "Sync me please"}},
        ])
        # Patch the session home used by providers via sync's discover call.
        original_discover = providers_mod.discover
        providers_mod.discover = lambda h=None: original_discover(h or home)

        try:
            first = fables_sync.sync_once(self.base, self.token, "macbook", home,
                                          quiet=False)
            self.assertGreaterEqual(first, 1)
            # Idempotent second run uploads nothing new.
            second = fables_sync.sync_once(self.base, self.token, "macbook", home,
                                           quiet=True)
            self.assertEqual(second, 0)
            # The cloud now serves it over MCP.
            payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "list_sessions", "arguments": {},
                                  "_meta": {"io.modelcontextprotocol/"
                                            "protocolVersion": "2026-07-28"}}}
            request = urllib.request.Request(
                self.base + "/mcp", method="POST",
                data=json.dumps(payload).encode(),
                headers={"Authorization": f"Bearer {self.token}",
                         "Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=15) as response:
                result = json.loads(response.read())["result"]
            rows = json.loads(result["content"][0]["text"])
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["source"], "pi")
        finally:
            providers_mod.discover = original_discover


if __name__ == "__main__":
    unittest.main()
