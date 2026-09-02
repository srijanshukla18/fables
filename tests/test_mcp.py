import importlib.util
import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

from tests.test_library import make_zip

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location("fables_mcp", ROOT / "fables-mcp.py")
fables_mcp = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(fables_mcp)

handle_message = fables_mcp.handle_message


def write_jsonl(path, values):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(value) + "\n" for value in values),
        encoding="utf-8",
    )
    return path


class McpProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scratch = ROOT / ".test-work"
        cls.scratch.mkdir(exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.scratch, ignore_errors=True)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=self.scratch)
        self.home = Path(self.temp.name)
        self._old_home = fables_mcp._HOME
        fables_mcp._HOME = self.home
        self._write_sessions()

    def tearDown(self):
        fables_mcp._HOME = self._old_home
        self.temp.cleanup()

    def _write_sessions(self):
        write_jsonl(
            self.home / ".prime/agent/sessions/019fdc23-44e1-75d9-ae35-1b8d60f712ad.jsonl",
            [
                {"type": "session", "version": 3, "id": "x",
                 "timestamp": "2026-08-07T12:12:06.753Z",
                 "cwd": str(self.home / "work/demo")},
                {"type": "message", "id": "u1",
                 "timestamp": "2026-08-07T12:12:08Z",
                 "message": {"role": "user",
                              "content": "Prime question"}},
                {"type": "message", "id": "a1",
                 "timestamp": "2026-08-07T12:12:09Z",
                 "message": {"role": "assistant", "content": [
                     {"type": "text", "text": "Prime answer"}]}},
            ],
        )
        write_jsonl(
            self.home / ".commandcode/projects/users-work-demo/sess-1.jsonl",
            [
                {"id": "e1", "timestamp": "2026-05-25T10:00:14.964Z",
                 "sessionId": "sess-1", "parentId": None, "role": "user",
                 "content": [{"type": "text", "text": "Command Code question"}]},
                {"id": "e2", "timestamp": "2026-05-25T10:00:20.153Z",
                 "sessionId": "sess-1", "parentId": "e1", "role": "assistant",
                 "content": [{"type": "reasoning", "text": "Think first."},
                              {"type": "text", "text": "Command Code answer"}]},
            ],
        )
        write_jsonl(
            self.home / ".kimi/sessions/user-1/conv-1/context.jsonl",
            [
                {"role": "_checkpoint", "id": 0},
                {"role": "user", "content": "Kimi question"},
                {"role": "assistant", "content": [
                    {"type": "think", "think": "Hmm."},
                    {"type": "text", "text": "Kimi answer"}]},
                {"role": "tool", "content": [{"type": "text", "text": "kimi output"}],
                 "tool_call_id": "tool_9"},
            ],
        )
        write_jsonl(
            self.home / ".kimi/sessions/user-1/conv-1/wire.jsonl",
            [{"message": {"type": "ToolCall", "payload": {
                "id": "tool_9",
                "function": {"name": "Shell",
                              "arguments": "{\"command\": \"ls\"}"}}}}],
        )
        write_jsonl(
            self.home / ".cursor/projects/Users-work-demo/agent-transcripts/s-1/s-1.jsonl",
            [
                {"role": "user", "message": {"content": [
                    {"type": "text", "text": "Cursor CLI question"}]}},
                {"role": "assistant", "message": {"content": [
                    {"type": "tool_use", "name": "Shell", "input": {"command": "ls"}}]}},
            ],
        )
        write_jsonl(
            self.home / ".pi/agent/sessions/--work-demo--/"
            "2026-08-11T17-10-33-643Z_019ffc61-f135-743c-902d-aecdc76d6975.jsonl",
            [
                {"type": "session", "version": "3", "id": "019ffc61-f135-743c-902d-aecdc76d6975",
                 "timestamp": "2026-08-11T17:10:33.643Z",
                 "cwd": str(self.home / "work/demo")},
                {"type": "model_change", "id": "m1",
                 "timestamp": "2026-08-11T17:10:34Z",
                 "provider": "anthropic", "modelId": "claude-fixture"},
                {"type": "message", "id": "u1",
                 "timestamp": "2026-08-11T17:10:36Z",
                 "message": {"role": "user",
                             "content": "Please inspect the needle fixture."}},
                {"type": "message", "id": "a1",
                 "timestamp": "2026-08-11T17:10:37Z",
                 "message": {"role": "assistant", "content": [
                     {"type": "thinking", "thinking": "Need a tool."},
                     {"type": "text", "text": "Let me look."},
                     {"type": "toolCall", "id": "call_1", "name": "read",
                      "arguments": {"path": "fixture.txt"}}]}},
                {"type": "message", "id": "r1",
                 "timestamp": "2026-08-11T17:10:38Z",
                 "message": {"role": "toolResult", "toolCallId": "call_1",
                             "toolName": "read",
                             "content": [{"type": "text", "text": "fixture contents"}],
                             "isError": False}},
                {"type": "message", "id": "b1",
                 "timestamp": "2026-08-11T17:10:39Z",
                 "message": {"role": "bashExecution", "command": "ls demo",
                             "output": "file.txt", "exitCode": 0}},
            ],
        )
        write_jsonl(
            self.home / ".claude/projects/-work-demo/session.jsonl",
            [
                {"type": "user", "message": {"content": "First question"}},
                {"type": "assistant", "message": {"content": "First answer"}},
            ],
        )
        hermes_db = self.home / ".hermes/state.db"
        hermes_db.parent.mkdir(parents=True)
        connection = sqlite3.connect(hermes_db)
        connection.execute(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, model TEXT, "
            "started_at REAL, ended_at REAL, message_count INTEGER, tool_call_count INTEGER, "
            "title TEXT, cwd TEXT, hidden INTEGER DEFAULT 0)"
        )
        connection.execute(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, "
            "content TEXT, tool_call_id TEXT, tool_calls TEXT, tool_name TEXT, "
            "timestamp REAL, finish_reason TEXT, reasoning_content TEXT, active INTEGER DEFAULT 1)"
        )
        connection.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("20260831_hermes_fixture", "cli", "gpt-fixture", 10.0, 20.0,
             4, 1, "Hermes MCP fixture", str(self.home / "work/hermes"), 0),
        )
        calls = json.dumps([{
            "id": "call_hermes", "type": "function",
            "function": {"name": "terminal", "arguments": "{\"command\":\"echo hermes\"}"},
        }])
        connection.executemany(
            "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (1, "20260831_hermes_fixture", "user", "Hermes question", None,
                 None, None, 10.0, None, None, 1),
                (2, "20260831_hermes_fixture", "assistant", "", None, calls,
                 None, 11.0, "tool_calls", "Need the terminal.", 1),
                (3, "20260831_hermes_fixture", "tool",
                 '{"output":"hermes output","exit_code":0,"error":null}',
                 "call_hermes", None, "terminal", 12.0, None, None, 1),
                (4, "20260831_hermes_fixture", "assistant", "Hermes answer", None,
                 None, None, 13.0, "stop", None, 1),
            ],
        )
        connection.commit()
        connection.close()

    def call(self, method, params=None, version="2026-07-28"):
        message = {"jsonrpc": "2.0", "id": 1, "method": method}
        merged = {} if params is None else dict(params)
        if version is not None:
            merged["_meta"] = {"io.modelcontextprotocol/protocolVersion": version}
        if merged:
            message["params"] = merged
        return handle_message(json.dumps(message))

    def tool_call(self, name, arguments):
        result = self.call("tools/call", {"name": name, "arguments": arguments})
        return result["result"]

    def tool_text(self, name, arguments):
        result = self.tool_call(name, arguments)
        self.assertFalse(result.get("isError"), result["content"][0]["text"])
        return result["content"][0]["text"]

    def test_discover_advertises_stateless_protocol(self):
        result = self.call("server/discover")["result"]
        self.assertEqual(result["resultType"], "complete")
        self.assertEqual(result["supportedVersions"], ["2026-07-28"])
        self.assertIn("tools", result["capabilities"])
        self.assertIn("instructions", result)
        self.assertEqual(result["ttlMs"], 3_600_000)
        self.assertEqual(result["cacheScope"], "public")
        meta = self.call("server/discover")["result"]["_meta"]
        self.assertEqual(meta["io.modelcontextprotocol/serverInfo"]["name"], "fables-mcp")

    def test_tools_list_is_deterministic_and_cacheable(self):
        result = self.call("tools/list")["result"]
        self.assertEqual(result["resultType"], "complete")
        self.assertEqual(
            [tool["name"] for tool in result["tools"]],
            ["list_sessions", "get_session", "search_sessions", "inspect_import",
             "apply_import", "get_import", "get_session_provenance"],
        )
        for tool in result["tools"]:
            self.assertIn("inputSchema", tool)
        schemas = {tool["name"]: tool["inputSchema"]["properties"]
                   for tool in result["tools"]}
        self.assertIn("include_thinking", schemas["get_session"])
        self.assertIn("include_tools", schemas["get_session"])
        self.assertIn("include_thinking", schemas["search_sessions"])
        self.assertIn("include_tools", schemas["search_sessions"])
        self.assertEqual(result["ttlMs"], 300_000)
        self.assertEqual(result["cacheScope"], "public")

    def test_import_tools_use_envelopes_confirmation_and_stable_ids(self):
        bundle = make_zip(self.home / "export.zip", text="MCP imported passage")
        inspected = json.loads(self.tool_text(
            "inspect_import", {"input": str(bundle), "origin": "m1-air"}))
        self.assertTrue(inspected["ok"])
        self.assertEqual(inspected["result"]["sessions"]["new"], 1)

        refused = self.call("tools/call", {"name": "apply_import", "arguments": {
            "input": str(bundle), "origin": "m1-air",
            "expect_sha256": inspected["result"]["sha256"],
        }})
        self.assertEqual(refused["error"]["code"], -32602)

        applied = json.loads(self.tool_text("apply_import", {
            "input": str(bundle), "origin": "m1-air",
            "expect_sha256": inspected["result"]["sha256"], "confirmed": True,
        }))
        sid = applied["result"]["created"][0]
        import_id = applied["result"]["import_id"]
        verified = json.loads(self.tool_text("get_import", {"import_id": import_id}))
        self.assertEqual(verified["result"]["state"], "complete")
        provenance = json.loads(self.tool_text(
            "get_session_provenance", {"id": sid}))
        self.assertEqual(provenance["result"]["provenance"][0]["origin"], "m1-air")
        imported = json.loads(self.tool_text(
            "list_sessions", {"scope": "imported", "origin": "m1-air"}))
        self.assertEqual([row["id"] for row in imported], [sid])
        transcript = self.tool_text("get_session", {"id": sid})
        self.assertIn("MCP imported passage", transcript)

    def test_live_session_provenance_is_read_only_provider_owned(self):
        rows = json.loads(self.tool_text("list_sessions", {"source": "pi"}))
        value = json.loads(self.tool_text(
            "get_session_provenance", {"id": rows[0]["id"]}))
        self.assertTrue(value["ok"])
        self.assertFalse(value["result"]["session"]["archived"])
        self.assertTrue(value["result"]["provenance"][0]["provider_owned"])
        self.assertTrue(value["result"]["provenance"][0]["read_only"])

    def test_unsupported_protocol_version_returns_32022(self):
        response = self.call("tools/list", version="2025-11-25")
        self.assertEqual(response["error"]["code"], -32022)
        self.assertIn("2026-07-28", response["error"]["data"]["supportedVersions"])

    def test_missing_meta_is_tolerated(self):
        message = {"jsonrpc": "2.0", "id": 7, "method": "server/discover"}
        response = handle_message(json.dumps(message))
        self.assertEqual(response["id"], 7)
        self.assertIn("result", response)

    def test_list_sessions_filters_and_limits(self):
        rows = json.loads(self.tool_text("list_sessions", {}))
        self.assertEqual(len(rows), 7)
        self.assertEqual({row["source"] for row in rows},
                         {"pi", "claude", "kimi", "cursor-cli", "prime",
                          "commandcode", "hermes"})
        pi_rows = json.loads(self.tool_text("list_sessions", {"source": "pi"}))
        self.assertEqual([row["source"] for row in pi_rows], ["pi"])
        needle = json.loads(self.tool_text(
            "list_sessions", {"query": "needle fixture"}))
        self.assertEqual(len(needle), 1)
        self.assertEqual(needle[0]["source"], "pi")
        by_native = json.loads(self.tool_text(
            "list_sessions", {"query": "019ffc61-f135-743c-902d-aecdc76d6975"}))
        self.assertEqual(len(by_native), 1)
        self.assertEqual(by_native[0]["native_id"],
                         "019ffc61-f135-743c-902d-aecdc76d6975")

    def test_get_session_renders_transcript_with_tools(self):
        rows = json.loads(self.tool_text("list_sessions", {"source": "pi"}))
        sid = rows[0]["id"]
        # Default: messages only — no tool calls, results, or thinking.
        transcript = self.tool_text("get_session", {"id": sid})
        self.assertIn("## user\nPlease inspect the needle fixture.", transcript)
        self.assertIn("## assistant\nLet me look.", transcript)
        self.assertNotIn("## tool", transcript)
        self.assertNotIn("fixture contents", transcript)
        self.assertNotIn("> thinking", transcript)
        self.assertIn("include_thinking=true", transcript)
        self.assertIn("include_tools=true", transcript)
        # Thinking without tool payloads.
        thinking = self.tool_text(
            "get_session", {"id": sid, "include_thinking": True})
        self.assertIn("> thinking\nNeed a tool.", thinking)
        self.assertNotIn("## tool", thinking)
        self.assertNotIn("fixture contents", thinking)
        self.assertIn("include_tools=true", thinking)
        self.assertNotIn("include_thinking=true", thinking)
        # Tools without thinking.
        tools = self.tool_text(
            "get_session", {"id": sid, "include_tools": True})
        self.assertIn("## tool · read", tools)
        self.assertIn('"path": "fixture.txt"', tools)
        self.assertIn("↳ fixture contents", tools)
        self.assertIn("## tool · bash", tools)
        self.assertNotIn("> thinking", tools)
        self.assertIn("include_thinking=true", tools)
        # Both: full transcript with thinking, tools, and results.
        full = self.tool_text("get_session", {
            "id": sid, "include_tools": True, "include_thinking": True})
        self.assertIn("## tool · read", full)
        self.assertIn('"path": "fixture.txt"', full)
        self.assertIn("↳ fixture contents", full)
        self.assertIn("## tool · bash", full)
        self.assertIn("> thinking\nNeed a tool.", full)
        self.assertIn("claude-fixture", full)
        self.assertNotIn("omitted", full)

    def test_get_hermes_session_preserves_reasoning_and_paired_tool(self):
        rows = json.loads(self.tool_text("list_sessions", {"source": "hermes"}))
        self.assertEqual(len(rows), 1)
        transcript = self.tool_text("get_session", {
            "id": rows[0]["id"], "include_tools": True,
            "include_thinking": True,
        })
        self.assertIn("models: gpt-fixture", transcript)
        self.assertIn("## user\nHermes question", transcript)
        self.assertIn("> thinking\nNeed the terminal.", transcript)
        self.assertIn("## tool · terminal", transcript)
        self.assertIn('"command": "echo hermes"', transcript)
        self.assertIn("↳ hermes output", transcript)
        self.assertIn("## assistant\nHermes answer", transcript)

    def test_get_session_json_returns_raw_archive(self):
        rows = json.loads(self.tool_text("list_sessions", {"source": "claude"}))
        raw = self.tool_text("get_session", {"id": rows[0]["id"], "format": "json"})
        self.assertIn("First question", raw)
        self.assertTrue(raw.lstrip().startswith("{") or "\n" in raw)

    def test_get_session_unknown_id_is_tool_error_not_protocol_error(self):
        result = self.tool_call("get_session", {"id": "does-not-exist"})
        self.assertTrue(result["isError"])
        self.assertIn("not found", result["content"][0]["text"])
        self.assertIn("native provider id", result["content"][0]["text"])

    def test_get_session_accepts_native_provider_id(self):
        native = "019ffc61-f135-743c-902d-aecdc76d6975"
        transcript = self.tool_text("get_session", {"id": native})
        self.assertIn("## user\nPlease inspect the needle fixture.", transcript)
        prefixed = self.tool_text("get_session", {"id": f"pi:{native}"})
        self.assertIn("## user\nPlease inspect the needle fixture.", prefixed)
        prime = "019fdc23-44e1-75d9-ae35-1b8d60f712ad"
        prime_text = self.tool_text("get_session", {"id": prime})
        self.assertIn("## user\nPrime question", prime_text)

    def test_get_session_ambiguous_native_id(self):
        write_jsonl(
            self.home / ".pi/agent/sessions/--work-demo--/"
            "2026-08-11T18-00-00-000Z_dup.jsonl",
            [
                {"type": "session", "version": 3, "id": "sess-1",
                 "timestamp": "2026-08-11T18:00:00Z",
                 "cwd": str(self.home / "work/demo")},
                {"type": "message", "id": "u1",
                 "timestamp": "2026-08-11T18:00:01Z",
                 "message": {"role": "user", "content": "Pi duplicate id"}},
            ],
        )
        result = self.tool_call("get_session", {"id": "sess-1"})
        self.assertTrue(result["isError"])
        text = result["content"][0]["text"]
        self.assertIn("ambiguous", text.lower())
        self.assertIn("pi:sess-1", text)
        self.assertIn("commandcode:sess-1", text)
        pi_only = self.tool_text("get_session", {"id": "pi:sess-1"})
        self.assertIn("Pi duplicate id", pi_only)
        cc_only = self.tool_text("get_session", {"id": "commandcode:sess-1"})
        self.assertIn("Command Code question", cc_only)

    def test_get_session_missing_id_is_invalid_params(self):
        response = self.call("tools/call", {"name": "get_session", "arguments": {}})
        self.assertEqual(response["error"]["code"], -32602)

    def test_search_sessions_finds_snippets(self):
        # Message text matches by default.
        matches = json.loads(self.tool_text("search_sessions", {"query": "needle"}))
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["source"], "pi")
        self.assertIn("needle", matches[0]["snippet"])
        # Tool output does NOT match by default…
        none = self.tool_text("search_sessions", {"query": "fixture contents"})
        self.assertIn("No sessions match", none)
        # …and include_thinking does not search tool payloads.
        none = self.tool_text(
            "search_sessions",
            {"query": "fixture contents", "include_thinking": True})
        self.assertIn("No sessions match", none)
        # …but does with include_tools.
        tool_match = json.loads(self.tool_text(
            "search_sessions", {"query": "fixture contents", "include_tools": True}))
        self.assertEqual(len(tool_match), 1)
        # Thinking text matches only when include_thinking is set.
        none = self.tool_text("search_sessions", {"query": "Need a tool"})
        self.assertIn("No sessions match", none)
        none = self.tool_text(
            "search_sessions", {"query": "Need a tool", "include_tools": True})
        self.assertIn("No sessions match", none)
        think_match = json.loads(self.tool_text(
            "search_sessions",
            {"query": "Need a tool", "include_thinking": True}))
        self.assertEqual(len(think_match), 1)
        none = self.tool_text("search_sessions", {"query": "nonexistent-term"})
        self.assertIn("No sessions match", none)
        by_uuid = json.loads(self.tool_text(
            "search_sessions", {"query": "019ffc61-f135-743c-902d-aecdc76d6975"}))
        self.assertEqual(len(by_uuid), 1)
        self.assertEqual(by_uuid[0]["source"], "pi")
        self.assertIn("native_id", by_uuid[0]["snippet"])

    def test_legacy_initialize_handshake(self):
        # Codex CLI 0.147 and other clients still speak the pre-2026-07-28
        # protocol: they open with initialize (no _meta) and then send
        # tools/list without per-request metadata.
        response = handle_message(json.dumps({
            "jsonrpc": "2.0", "id": 0, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18",
                       "capabilities": {}, "clientInfo": {"name": "codex"}},
        }))
        result = response["result"]
        self.assertEqual(result["protocolVersion"], "2025-06-18")
        self.assertEqual(result["capabilities"], {"tools": {}})
        self.assertEqual(result["serverInfo"]["name"], "fables-mcp")
        # tools/list follows without _meta and still works.
        listed = handle_message(json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "tools/list",
            "params": {},
        }))
        names = [tool["name"] for tool in listed["result"]["tools"]]
        self.assertEqual(names, [
            "list_sessions", "get_session", "search_sessions", "inspect_import",
            "apply_import", "get_import", "get_session_provenance",
        ])
        # ping is answered for legacy keepalives.
        pong = handle_message(json.dumps({
            "jsonrpc": "2.0", "id": 2, "method": "ping", "params": {},
        }))
        self.assertEqual(pong["result"]["resultType"], "complete")

    def test_kimi_and_cursor_cli_render(self):
        kimi = json.loads(self.tool_text("list_sessions", {"source": "kimi"}))
        self.assertEqual(len(kimi), 1)
        transcript = self.tool_text("get_session", {"id": kimi[0]["id"],
                                                    "include_tools": True})
        self.assertIn("## user\nKimi question", transcript)
        self.assertIn("## tool · Shell", transcript)
        self.assertIn("kimi output", transcript)
        messages_only = self.tool_text("get_session", {"id": kimi[0]["id"]})
        self.assertNotIn("## tool", messages_only)
        self.assertIn("Kimi answer", messages_only)

        cli = json.loads(self.tool_text("list_sessions", {"source": "cursor-cli"}))
        self.assertEqual(len(cli), 1)
        transcript = self.tool_text("get_session", {"id": cli[0]["id"],
                                                    "include_tools": True})
        self.assertIn("## user\nCursor CLI question", transcript)
        self.assertIn("## tool · Shell", transcript)
        self.assertIn('"command": "ls"', transcript)

    def test_prime_and_commandcode_render(self):
        prime = json.loads(self.tool_text("list_sessions", {"source": "prime"}))
        self.assertEqual(len(prime), 1)
        transcript = self.tool_text("get_session", {"id": prime[0]["id"]})
        self.assertIn("## user\nPrime question", transcript)
        self.assertIn("Prime answer", transcript)

        cc = json.loads(self.tool_text("list_sessions", {"source": "commandcode"}))
        self.assertEqual(len(cc), 1)
        transcript = self.tool_text("get_session", {"id": cc[0]["id"],
                                                    "include_thinking": True})
        self.assertIn("## user\nCommand Code question", transcript)
        self.assertIn("> thinking\nThink first.", transcript)  # reasoning block
        self.assertIn("Command Code answer", transcript)
        tools_only = self.tool_text("get_session", {"id": cc[0]["id"],
                                                    "include_tools": True})
        self.assertNotIn("> thinking", tools_only)

    def test_http_transport(self):
        import http.client
        import threading

        server = fables_mcp  # module loaded via importlib at the top of this file

        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode("utf-8", "replace")
                response = server.handle_message(raw)
                body = json.dumps(response).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            port = httpd.server_address[1]
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list",
                                  "params": {"_meta": {
                                      "io.modelcontextprotocol/protocolVersion":
                                          "2026-07-28"}}})
            connection.request("POST", "/mcp", payload,
                               {"Content-Type": "application/json"})
            response = json.loads(connection.getresponse().read())
            names = [tool["name"] for tool in response["result"]["tools"]]
            self.assertEqual(names[0], "list_sessions")
        finally:
            httpd.shutdown()

    def test_notifications_get_no_response(self):
        response = handle_message(json.dumps({
            "jsonrpc": "2.0",
            "method": "notifications/cancelled",
            "params": {"requestId": 1},
        }))
        self.assertIsNone(response)

    def test_malformed_json_returns_parse_error(self):
        response = handle_message("{not json")
        self.assertEqual(response["error"]["code"], -32700)
        self.assertIsNone(response["id"])

    def test_unknown_method_and_tool(self):
        response = self.call("completion/complete")
        self.assertEqual(response["error"]["code"], -32601)
        response = self.call("tools/call", {"name": "nope", "arguments": {}})
        self.assertEqual(response["error"]["code"], -32602)

    def test_invalid_request(self):
        response = handle_message(json.dumps({"jsonrpc": "2.0", "id": 1}))
        self.assertEqual(response["error"]["code"], -32600)


if __name__ == "__main__":
    unittest.main()
