import http.client
import json
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import providers
import serve


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def write_jsonl(path, values):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(value) + "\n" for value in values),
        encoding="utf-8",
    )
    return path


class ProviderTests(unittest.TestCase):
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

    def tearDown(self):
        self.temp.cleanup()

    def test_pi_scan_and_load(self):
        native = "019ffc61-f135-743c-902d-aecdc76d6975"
        path = write_jsonl(
            self.home / f".pi/agent/sessions/--work-demo--/"
            f"2026-08-11T17-10-33-643Z_{native}.jsonl",
            [
                {"type": "session", "version": "3", "id": native,
                 "timestamp": "2026-08-11T17:10:33.643Z",
                 "cwd": str(self.home / "work/demo")},
                {"type": "model_change", "id": "m1",
                 "timestamp": "2026-08-11T17:10:34Z",
                 "provider": "anthropic", "modelId": "claude-fixture"},
                {"type": "message", "id": "u1",
                 "timestamp": "2026-08-11T17:10:36Z",
                 "message": {"role": "user",
                              "content": "Please inspect the fixture."}},
                {"type": "message", "id": "a1",
                 "timestamp": "2026-08-11T17:10:37Z",
                 "message": {"role": "assistant", "content": [
                     {"type": "toolCall", "id": "call_1", "name": "read",
                      "arguments": {"path": "fixture.txt"}}]}},
            ],
        )
        result = providers.scan_pi(self.home)
        self.assertEqual(len(result.sessions), 1)
        entry = result.sessions[0]
        self.assertEqual(entry["source"], "pi")
        self.assertEqual(entry["title"], "Please inspect the fixture.")
        self.assertEqual(entry["project"], "~/work/demo")
        self.assertEqual(entry["native_id"], native)
        target = result.targets[entry["id"]]
        self.assertEqual(providers.load_target(target), path.read_text())
        self.assertEqual(result.status["source"], "pi")
        self.assertEqual(result.status["count"], 1)
        resolved = providers.resolve_session_entry(native, result.sessions)
        self.assertEqual(resolved["id"], entry["id"])
        prefixed = providers.resolve_session_entry(f"pi:{native}", result.sessions)
        self.assertEqual(prefixed["id"], entry["id"])

    def test_pi_scan_sorts_from_last_message_beyond_metadata_preview(self):
        path = write_jsonl(
            self.home / ".pi/agent/sessions/large.jsonl",
            [
                {"type": "session", "id": "large", "timestamp": "2026-01-01T00:00:00Z"},
                {"type": "message", "timestamp": "2026-01-01T00:00:01Z",
                 "message": {"role": "user", "content": "first prompt"}},
                {"type": "message", "timestamp": "2026-01-01T00:00:02Z",
                 "message": {"role": "assistant", "content": [
                     {"type": "text", "text": "x" * (providers.PREVIEW_BYTES + 1000)}]}},
                {"type": "message", "timestamp": "2026-01-02T03:04:05Z",
                 "message": {"role": "user", "content": "latest prompt"}},
            ],
        )
        self.assertGreater(path.stat().st_size, providers.PREVIEW_BYTES)

        entry = providers.scan_pi(self.home).sessions[0]

        self.assertEqual(entry["title"], "first prompt")
        self.assertEqual(entry["mtime"], providers._timestamp("2026-01-02T03:04:05Z"))

    def test_claude_and_codex_metadata_and_raw_loaders(self):
        claude = write_jsonl(
            self.home / ".claude/projects/-work-demo/session.jsonl",
            [
                {"type": "user", "message": {"content": "First question"}},
                {"type": "summary", "summary": "Claude summary"},
            ],
        )
        subagent = write_jsonl(
            self.home / ".claude/projects/-work-demo/subagents/agent.jsonl",
            [{"type": "user", "message": {"content": "Sub story"}}],
        )
        codex_values = [
            {"type": "session_meta", "payload": {"cwd": str(self.home / "code/app")}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": "Fix it"}},
            {
                "type": "event_msg",
                "payload": {"type": "thread_name_updated", "thread_name": "Codex title"},
            },
        ]
        codex = write_jsonl(
            self.home / ".codex/sessions/2026/session.jsonl", codex_values
        )
        archived = self.home / ".codex/archived_sessions/old.jsonl"
        archived.parent.mkdir(parents=True)
        archived.symlink_to(codex)

        claude_result = providers.scan_claude(self.home)
        codex_result = providers.scan_codex(self.home)

        self.assertEqual(len(claude_result.sessions), 2)
        main = next(item for item in claude_result.sessions if not item["sub"])
        sub = next(item for item in claude_result.sessions if item["sub"])
        self.assertEqual(main["title"], "Claude summary")
        self.assertEqual(main["format"], "claude")
        self.assertTrue(sub["sub"])
        self.assertEqual(
            providers.load_target(claude_result.targets[main["id"]]),
            claude.read_text(),
        )
        self.assertEqual(len(codex_result.sessions), 1)
        self.assertEqual(codex_result.sessions[0]["title"], "Codex title")
        self.assertEqual(codex_result.sessions[0]["project"], "~/code/app")
        self.assertEqual(
            providers.load_target(codex_result.targets[codex_result.sessions[0]["id"]]),
            codex.read_text(),
        )

    def test_cowork_uses_sibling_metadata_and_omits_audit(self):
        base = (
            self.home
            / "Library/Application Support/Claude/local-agent-mode-sessions/task/local_abc"
        )
        metadata = {
            "title": "Cowork task",
            "cwd": str(self.home / "cowork-project"),
            "createdAt": "2026-01-01T00:00:00Z",
            "lastActivityAt": "2026-01-02T00:00:00Z",
            "model": "claude-test",
        }
        write_json(base.with_suffix(".json"), metadata)
        transcript = write_jsonl(
            base / ".claude/projects/-ignored/session.jsonl",
            [{"type": "user", "message": {"content": "Cowork prompt"}}],
        )
        write_jsonl(
            base / ".claude/projects/-ignored/audit.jsonl",
            [{"type": "user", "message": {"content": "duplicate"}}],
        )
        write_jsonl(
            base / ".claude/projects/-ignored/subagents/child.jsonl",
            [{"type": "user", "message": {"content": "child"}}],
        )

        result = providers.scan_cowork(self.home)

        self.assertEqual(len(result.sessions), 2)
        self.assertTrue(all(item["title"] == "Cowork task" for item in result.sessions))
        self.assertTrue(all(item["project"] == "~/cowork-project" for item in result.sessions))
        self.assertTrue(all(item["experimental"] for item in result.sessions))
        self.assertEqual(result.status["stability"], "experimental")
        self.assertEqual(result.status["status"], "ok")
        self.assertEqual(
            providers.load_target(
                result.targets[next(item["id"] for item in result.sessions if not item["sub"])]
            ),
            transcript.read_text(),
        )

    def test_copilot_yaml_metadata_and_user_fallback(self):
        first = self.home / ".copilot/session-state/one"
        events = write_jsonl(
            first / "events.jsonl",
            [{"type": "user.message", "data": {"content": "Copilot prompt"}}],
        )
        (first / "workspace.yaml").write_text(
            f'id: one\ncwd: "{self.home}/repo"\nname: Named session\n'
            "user_named: true\nupdated_at: 2026-02-03T04:05:06Z\n",
            encoding="utf-8",
        )
        second = self.home / ".copilot/session-state/two"
        write_jsonl(
            second / "events.jsonl",
            [{"type": "user.message", "data": {"content": "Fallback title"}}],
        )

        result = providers.scan_copilot(self.home)

        self.assertEqual(len(result.sessions), 2)
        named = next(item for item in result.sessions if item["title"] == "Named session")
        self.assertEqual(named["project"], "~/repo")
        self.assertEqual(named["format"], "copilot")
        self.assertNotIn("experimental", named)  # promoted to stable
        self.assertNotIn("stability", result.status)
        self.assertEqual(providers.load_target(result.targets[named["id"]]), events.read_text())
        self.assertIn("Fallback title", {item["title"] for item in result.sessions})

    def test_gemini_metadata_and_stable_project_fallback(self):
        chat_dir = self.home / ".gemini/tmp/hash-one/chats"
        (chat_dir.parent / ".project_root").parent.mkdir(parents=True, exist_ok=True)
        (chat_dir.parent / ".project_root").write_text(
            str(self.home / "gemini-project"), encoding="utf-8"
        )
        payload = {
            "sessionId": "gem-1",
            "projectHash": "hash-one",
            "startTime": "2026-03-01T00:00:00Z",
            "lastUpdated": "2026-03-02T00:00:00Z",
            "messages": [
                {"id": "m1", "type": "user", "content": "Gemini prompt"},
                {"id": "m2", "type": "gemini", "content": "Answer"},
            ],
        }
        path = write_json(chat_dir / "session-one.json", payload)
        fallback = dict(payload, sessionId="gem-2", projectHash="stablehash")
        write_json(self.home / ".gemini/tmp/hash-two/chats/session-two.json", fallback)

        result = providers.scan_gemini(self.home)

        self.assertEqual(len(result.sessions), 2)
        rooted = next(item for item in result.sessions if item["project"] == "~/gemini-project")
        self.assertEqual(rooted["title"], "Gemini prompt")
        self.assertEqual(providers.load_target(result.targets[rooted["id"]]), path.read_text())
        self.assertIn(
            "Gemini project stablehash",
            {item["project"] for item in result.sessions},
        )

    def test_kimi_scan_and_load_merges_wire_tool_calls(self):
        conv = self.home / ".kimi/sessions/user-1/conv-1"
        context = write_jsonl(conv / "context.jsonl", [
            {"role": "_checkpoint", "id": 0},
            {"role": "user", "content": "What files are here?"},
            {"role": "assistant", "content": [
                {"type": "think", "think": "Need a tool."},
                {"type": "text", "text": "Let me look."}]},
            {"role": "tool", "content": [{"type": "text", "text": "file.txt"}],
             "tool_call_id": "tool_1"},
        ])
        write_jsonl(conv / "wire.jsonl", [
            {"type": "metadata", "protocol_version": "1.1"},
            {"message": {"type": "ToolCall", "payload": {
                "id": "tool_1",
                "function": {"name": "Shell", "arguments": "{\"command\": \"ls\"}"}}}},
        ])
        result = providers.scan_kimi(self.home)
        self.assertEqual(len(result.sessions), 1)
        entry = result.sessions[0]
        self.assertEqual(entry["title"], "What files are here?")
        archive = json.loads(providers.load_target(result.targets[entry["id"]]))
        self.assertTrue(archive["kimiArchive"])
        messages = archive["messages"]
        self.assertEqual(messages[0]["message"]["role"], "user")
        tool_msg = messages[2]["message"]
        self.assertEqual(tool_msg["role"], "tool")
        self.assertEqual(tool_msg["toolName"], "Shell")
        self.assertIn("ls", tool_msg["arguments"])
        self.assertEqual(tool_msg["content"][0]["text"], "file.txt")
        self.assertEqual(messages[1]["message"]["content"][0]["type"], "thinking")

    def test_cursor_cli_scan(self):
        transcript = write_jsonl(
            self.home / ".cursor/projects/Users-work-demo/agent-transcripts/"
            "sess-1/sess-1.jsonl",
            [
                {"role": "user", "message": {"content": [
                    {"type": "text", "text": "Inspect the fixture."}]}},
                {"role": "assistant", "message": {"content": [
                    {"type": "text", "text": "Let me look."},
                    {"type": "tool_use", "name": "Shell", "input": {"command": "ls"}}]}},
            ],
        )
        result = providers.scan_cursor_cli(self.home)
        self.assertEqual(len(result.sessions), 1)
        entry = result.sessions[0]
        self.assertEqual(entry["source"], "cursor-cli")
        self.assertEqual(entry["title"], "Inspect the fixture.")
        self.assertEqual(entry["project"], "/Users/work/demo")
        self.assertEqual(
            providers.load_target(result.targets[entry["id"]]),
            transcript.read_text(),
        )

    def test_prime_uses_pi_schema(self):
        write_jsonl(
            self.home / ".prime/agent/sessions/019fdc23-44e1-75d9-ae35-1b8d60f712ad.jsonl",
            [
                {"type": "session", "version": 3, "id": "x",
                 "timestamp": "2026-08-07T12:12:06.753Z",
                 "cwd": str(self.home / "work/demo")},
                {"type": "model_change", "id": "m1",
                 "timestamp": "2026-08-07T12:12:07Z",
                 "provider": "openai-codex", "modelId": "gpt-fixture"},
                {"type": "message", "id": "u1",
                 "timestamp": "2026-08-07T12:12:08Z",
                 "message": {"role": "user",
                              "content": "What is in the demo directory?"}},
            ],
        )
        result = providers.scan_prime(self.home)
        self.assertEqual(len(result.sessions), 1)
        entry = result.sessions[0]
        self.assertEqual(entry["source"], "prime")
        self.assertEqual(entry["title"], "What is in the demo directory?")
        self.assertEqual(entry["project"], "~/work/demo")
        target = result.targets[entry["id"]]
        loaded = providers.load_target(target)
        self.assertIn("What is in the demo directory?", loaded)

    def test_commandcode_scan_uses_meta_title_and_skips_checkpoints(self):
        session = write_jsonl(
            self.home / ".commandcode/projects/users-work-demo/sess-1.jsonl",
            [
                {"id": "e1", "timestamp": "2026-05-25T10:00:14.964Z",
                 "sessionId": "sess-1", "parentId": None, "role": "user",
                 "content": [{"type": "text", "text": "Check the page."}]},
            ],
        )
        write_json(self.home / ".commandcode/projects/users-work-demo/sess-1.meta.json",
                   {"model": "deepseek/deepseek-v4-pro", "title": "Pricing check"})
        write_jsonl(
            self.home / ".commandcode/projects/users-work-demo/sess-1.checkpoints.jsonl",
            [{"type": "file-history-snapshot", "messageId": "x"}],
        )
        result = providers.scan_commandcode(self.home)
        self.assertEqual(len(result.sessions), 1)
        entry = result.sessions[0]
        self.assertEqual(entry["source"], "commandcode")
        self.assertEqual(entry["title"], "Pricing check")
        self.assertEqual(entry["model"], "deepseek/deepseek-v4-pro")
        self.assertEqual(entry["project"], "/Users/work/demo")
        self.assertEqual(
            providers.load_target(result.targets[entry["id"]]),
            session.read_text(),
        )

    def test_qwen_aider_kiro_kilo_and_amp_scan(self):
        qwen = write_jsonl(
            self.home / ".qwen/tmp/proj-1/chats/abcd1234.jsonl",
            [
                {"uuid": "u1", "parentUuid": None, "role": "user",
                 "content": [{"type": "text", "text": "Qwen question"}]},
                {"uuid": "a1", "parentUuid": "u1", "role": "assistant",
                 "content": [{"type": "text", "text": "Qwen answer"}]},
            ],
        )
        qwen_result = providers.scan_qwen(self.home)
        self.assertEqual(len(qwen_result.sessions), 1)
        self.assertEqual(qwen_result.sessions[0]["title"], "Qwen question")

        aider = self.home / "code/demo/.aider.chat.history.md"
        aider.parent.mkdir(parents=True)
        aider.write_text("# aider chat started at 2024-12-03 17:45:38\n\n"
                         ">>> user\nfix it\n", encoding="utf-8")
        aider_result = providers.scan_aider(self.home)
        self.assertEqual(len(aider_result.sessions), 1)
        self.assertEqual(aider_result.sessions[0]["project"], "~/code/demo")
        self.assertIn("2024-12-03",
                      providers.load_target(aider_result.targets[
                          aider_result.sessions[0]["id"]]))

        kiro = write_jsonl(
            self.home / ".kiro/sessions/cli/sess-1.jsonl",
            [{"type": "user_message", "content": "Kiro question"}],
        )
        kiro_result = providers.scan_kiro(self.home)
        self.assertEqual(kiro_result.sessions[0]["title"], "Kiro question")
        self.assertEqual(
            providers.load_target(kiro_result.targets[kiro_result.sessions[0]["id"]]),
            kiro.read_text(),
        )

        amp = write_json(self.home / ".local/share/amp/threads/T-abc.json",
                         {"v": "0", "id": "T-abc", "created": "1760265314280",
                          "messages": json.dumps([
                              {"role": "user", "content": "Amp question"},
                              {"role": "assistant", "content": "Amp answer"}])
                          })
        amp_result = providers.scan_amp(self.home)
        self.assertEqual(len(amp_result.sessions), 1)
        self.assertEqual(amp_result.sessions[0]["title"], "Amp question")
        archive = json.loads(providers.load_target(
            amp_result.targets[amp_result.sessions[0]["id"]]))
        self.assertTrue(archive["ampArchive"])
        self.assertEqual(len(archive["messages"]), 2)

    def test_kilo_and_zed_scan(self):
        task = self.home / "Library/Application Support/Code/User/globalStorage/" \
            "kilocode.kilo-code/tasks/task-1"
        write_json(task / "task_metadata.json", {
            "taskId": "task-1", "task": "Kilo task",
            "workspaceDirectory": str(self.home / "work/kilo"),
        })
        write_json(task / "api_conversation_history.json", [{
            "role": "user", "content": "Kilo request"}])
        kilo_result = providers._scan_extension_tasks(self.home, "kilo")
        self.assertEqual(len(kilo_result.sessions), 1)
        self.assertEqual(kilo_result.sessions[0]["title"], "Kilo task")
        self.assertEqual(kilo_result.sessions[0]["source"], "kilo")

        db = self.home / "Library/Application Support/Zed/threads/threads.db"
        db.parent.mkdir(parents=True)
        connection = sqlite3.connect(db)
        connection.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, data BLOB, data_type TEXT)")
        connection.execute("INSERT INTO threads VALUES (?, ?, ?)",
                           ("t1", json.dumps({"messages": [
                               {"role": "user", "content": "Zed question"},
                               {"role": "assistant", "content": "Zed answer"}]}).encode(),
                            "json"))
        connection.commit()
        connection.close()
        zed_result = providers.scan_zed(self.home)
        self.assertEqual(len(zed_result.sessions), 1)
        archive = json.loads(providers.load_target(
            zed_result.targets[zed_result.sessions[0]["id"]]))
        self.assertTrue(archive["zedArchive"])
        self.assertEqual(len(archive["messages"]), 2)

    def test_cline_current_wins_legacy_duplicate_and_roo_is_compact(self):
        current = self.home / ".cline/data/tasks/task-1"
        write_json(current / "task_metadata.json", {
            "taskId": "task-1",
            "task": "Current Cline task",
            "workspaceDirectory": str(self.home / "cline-project"),
            "updatedAt": "2026-04-03T00:00:00Z",
            "totalCost": 0.25,
            "tokensIn": 10,
        })
        write_json(current / "api_conversation_history.json", [{"role": "user"}])
        write_json(current / "ui_messages.json", [{"type": "say", "text": "safe"}])
        legacy = (
            self.home / "Library/Application Support/Code/User/globalStorage"
            / "saoudrizwan.claude-dev/tasks/task-1"
        )
        write_json(legacy / "history_item.json", {
            "id": "task-1", "task": "Legacy duplicate",
        })
        write_json(legacy / "api_conversation_history.json", [{"role": "legacy"}])
        write_json(legacy / "ui_messages.json", [])

        roo = (
            self.home / "Library/Application Support/Code/User/globalStorage"
            / "rooveterinaryinc.roo-cline/tasks/roo-1"
        )
        roo_metadata = {
            "id": "roo-1",
            "task": "Roo task",
            "workspace": str(self.home / "roo-project"),
            "ts": 1_700_000_000_000,
        }
        write_json(roo / "history_item.json", roo_metadata)
        write_json(roo / "api_conversation_history.json", [{"role": "assistant"}])
        write_json(roo / "ui_messages.json", [{"type": "ask"}])
        write_json(roo.parent.parent / "settings.json", {"secret": "not scanned"})

        cline_result = providers.scan_cline(self.home)
        roo_result = providers.scan_roo(self.home)
        cline_payload = json.loads(providers.load_target(
            cline_result.targets[cline_result.sessions[0]["id"]]
        ))
        roo_payload = json.loads(providers.load_target(
            roo_result.targets[roo_result.sessions[0]["id"]]
        ))

        self.assertEqual(len(cline_result.sessions), 1)
        self.assertEqual(cline_result.sessions[0]["title"], "Current Cline task")
        self.assertEqual(cline_result.sessions[0]["usage"]["cost"], 0.25)
        self.assertEqual(cline_payload["metadata"]["taskId"], "task-1")
        self.assertEqual(set(cline_payload), {"metadata", "apiMessages", "uiMessages"})
        self.assertEqual(roo_result.sessions[0]["title"], "Roo task")
        self.assertEqual(roo_payload["metadata"], roo_metadata)

    def test_goose_sqlite_metadata_and_filtered_archive(self):
        db = (
            self.home / "Library/Application Support/Block/goose/sessions/sessions.db"
        )
        db.parent.mkdir(parents=True)
        connection = sqlite3.connect(db)
        connection.execute(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, name TEXT, working_dir TEXT, "
            "created_at TEXT, updated_at TEXT, provider_name TEXT, "
            "model_config_json TEXT, total_tokens INTEGER, total_cost REAL)"
        )
        connection.execute(
            "CREATE TABLE messages (message_id TEXT PRIMARY KEY, session_id TEXT, "
            "role TEXT, content_json TEXT, created_timestamp TEXT, tokens INTEGER, "
            "metadata_json TEXT)"
        )
        connection.execute(
            "CREATE TABLE usage_ledger (id TEXT, session_id TEXT, model TEXT, "
            "input_tokens INTEGER, details_json TEXT)"
        )
        connection.execute("CREATE TABLE unrelated (secret TEXT)")
        connection.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("goose-1", "Goose task", str(self.home / "goose-project"),
             "2026-05-01T00:00:00Z", "2026-05-02T00:00:00Z", "test-provider",
             json.dumps({"model": "test-model"}), 30, 0.1),
        )
        connection.execute(
            "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("message-1", "goose-1", "user", json.dumps({"text": "Safe prompt"}),
             "2026-05-01T00:01:00Z", 5, json.dumps({"kind": "fixture"})),
        )
        connection.execute(
            "INSERT INTO usage_ledger VALUES (?, ?, ?, ?, ?)",
            ("usage-1", "goose-1", "test-model", 5, json.dumps({"cached": 1})),
        )
        connection.execute("INSERT INTO unrelated VALUES ('must not escape')")
        connection.commit()
        connection.close()

        result = providers.scan_goose(self.home)
        payload = json.loads(providers.load_target(
            result.targets[result.sessions[0]["id"]]
        ))

        self.assertEqual(result.status["status"], "ok")
        self.assertEqual(result.sessions[0]["project"], "~/goose-project")
        self.assertEqual(payload["session"]["model_config_json"]["model"], "test-model")
        self.assertEqual(payload["messages"][0]["content_json"]["text"], "Safe prompt")
        self.assertEqual(payload["usage"][0]["details_json"]["cached"], 1)
        self.assertNotIn("unrelated", json.dumps(payload))

    def test_hermes_sqlite_metadata_and_bounded_archive(self):
        db = self.home / ".hermes/state.db"
        db.parent.mkdir(parents=True)
        connection = sqlite3.connect(db)
        connection.execute(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, model TEXT, "
            "system_prompt TEXT, started_at REAL, ended_at REAL, message_count INTEGER, "
            "tool_call_count INTEGER, input_tokens INTEGER, output_tokens INTEGER, "
            "reasoning_tokens INTEGER, title TEXT, cwd TEXT, git_branch TEXT, "
            "last_activity_at REAL, profile_name TEXT, hidden INTEGER DEFAULT 0)"
        )
        connection.execute(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, "
            "content TEXT, tool_call_id TEXT, tool_calls TEXT, tool_name TEXT, "
            "timestamp REAL, finish_reason TEXT, reasoning_content TEXT, "
            "effect_disposition TEXT, active INTEGER DEFAULT 1, api_content TEXT)"
        )
        connection.execute("CREATE TABLE unrelated (secret TEXT)")
        connection.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("20260831_fixture", "cli", "gpt-fixture", "must not escape",
             100.0, 110.0, 4, 1, 20, 8, 2, "Hermes fixture",
             str(self.home / "code/hermes"), "main", 111.0, "default", 0),
        )
        calls = json.dumps([{
            "id": "call_fixture", "type": "function",
            "function": {"name": "terminal", "arguments": "{\"command\":\"echo ok\"}"},
        }])
        connection.executemany(
            "INSERT INTO messages (id, session_id, role, content, tool_call_id, "
            "tool_calls, tool_name, timestamp, finish_reason, reasoning_content, "
            "effect_disposition, active, api_content) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (1, "20260831_fixture", "user", "Run it", None, None, None,
                 100.0, None, None, None, 1, "must not escape"),
                (2, "20260831_fixture", "assistant", "", None, calls, None,
                 101.0, "tool_calls", "Use a tool", None, 1, None),
                (3, "20260831_fixture", "tool", '{"output":"ok","exit_code":0}',
                 "call_fixture", None, "terminal", 102.0, None, None, None, 1, None),
                (4, "20260831_fixture", "assistant", "Done", None, None, None,
                 103.0, "stop", None, None, 1, None),
                (5, "20260831_fixture", "assistant", "rewound", None, None, None,
                 104.0, "stop", None, None, 0, None),
            ],
        )
        connection.execute("INSERT INTO unrelated VALUES ('must not escape')")
        connection.commit()
        connection.close()

        result = providers.scan_hermes(self.home)
        self.assertEqual(result.status["status"], "ok")
        self.assertEqual(len(result.sessions), 1)
        entry = result.sessions[0]
        self.assertEqual(entry["source"], "hermes")
        self.assertEqual(entry["format"], "hermes")
        self.assertEqual(entry["title"], "Hermes fixture")
        self.assertEqual(entry["project"], "~/code/hermes")
        self.assertEqual(entry["native_id"], "20260831_fixture")
        payload = json.loads(providers.load_target(result.targets[entry["id"]]))
        self.assertTrue(payload["hermesArchive"])
        self.assertEqual(len(payload["messages"]), 4)
        self.assertNotIn("system_prompt", payload["session"])
        serialized = json.dumps(payload)
        self.assertNotIn("must not escape", serialized)
        self.assertNotIn("rewound", serialized)

    def test_vscode_legacy_and_mutation_log_replay(self):
        root = (
            self.home / "Library/Application Support/Code/User/workspaceStorage"
            / "workspace/chatSessions"
        )
        legacy = {
            "version": 2,
            "creationDate": 1_700_000_000_000,
            "customTitle": "Legacy VS Code chat",
            "sessionId": "vscode-legacy",
            "workingDirectory": str(self.home / "legacy-project"),
            "inputState": {"selectedModel": {"identifier": "legacy-model"},
                           "mode": {"id": "ask"}},
            "requests": [],
        }
        write_json(root / "legacy.json", legacy)
        initial = {
            "version": 3,
            "creationDate": 1_700_000_100_000,
            "sessionId": "vscode-log",
            "workingDirectory": str(self.home / "log-project"),
            "inputState": {"inputText": "remove", "selectedModel": "model-1",
                           "mode": "agent"},
            "requests": [{"requestId": "r1", "message": {"text": "First"}}],
        }
        log = root / "current.jsonl"
        write_jsonl(log, [
            {"kind": 0, "v": initial},
            {"kind": 1, "k": ["customTitle"], "v": "Replayed VS Code chat"},
            {"kind": 2, "k": ["requests"], "v": [
                {"requestId": "r2", "message": {"text": "Second"}}
            ]},
            {"kind": 3, "k": ["inputState", "inputText"]},
        ])
        with log.open("a", encoding="utf-8") as handle:
            handle.write('{"kind":1')

        result = providers.scan_vscode(self.home)
        replayed = next(item for item in result.sessions if item["sessionId"] == "vscode-log")
        legacy_item = next(
            item for item in result.sessions if item["sessionId"] == "vscode-legacy"
        )
        payload = json.loads(providers.load_target(result.targets[replayed["id"]]))
        legacy_payload = json.loads(
            providers.load_target(result.targets[legacy_item["id"]])
        )

        self.assertEqual(result.status["status"], "ok")
        self.assertEqual(replayed["title"], "Replayed VS Code chat")
        self.assertEqual(len(payload["session"]["requests"]), 2)
        self.assertNotIn("inputText", payload["session"]["inputState"])
        self.assertEqual(payload["diagnostics"]["ignoredTornLines"], 1)
        self.assertEqual(legacy_payload["session"], legacy)

    def test_gemini_current_jsonl_replaces_messages_and_marks_subagents(self):
        chat_root = self.home / ".gemini/tmp/current-hash/chats"
        records = [
            {
                "sessionId": "gem-current", "projectHash": "current-hash",
                "startTime": "2026-06-01T00:00:00Z",
                "lastUpdated": "2026-06-01T01:00:00Z",
                "kind": "main", "directories": [str(self.home / "gemini-current")],
            },
            {"id": "m1", "timestamp": "2026-06-01T00:01:00Z",
             "type": "user", "content": "Initial synthetic question"},
            {"id": "m2", "timestamp": "2026-06-01T00:02:00Z",
             "type": "gemini", "content": "Old answer", "model": "test-model",
             "tokens": {"input": 1, "output": 2, "total": 3}},
            {"id": "m2", "timestamp": "2026-06-01T00:03:00Z",
             "type": "gemini", "content": "Updated answer", "toolCalls": []},
            {"$set": {"summary": "Synthetic summary",
                      "lastUpdated": "2026-06-01T02:00:00Z"}},
        ]
        write_jsonl(chat_root / "session-current.jsonl", records)
        write_jsonl(chat_root / "gem-current/subagent.jsonl", [
            {
                "sessionId": "gem-sub", "projectHash": "current-hash",
                "startTime": "2026-06-01T00:10:00Z",
                "lastUpdated": "2026-06-01T00:20:00Z", "kind": "subagent",
            },
            {"id": "sub-1", "timestamp": "2026-06-01T00:11:00Z",
             "type": "user", "content": "Nested synthetic request"},
        ])

        result = providers.scan_gemini(self.home)
        main = next(item for item in result.sessions if not item["sub"])
        sub = next(item for item in result.sessions if item["sub"])
        payload = json.loads(providers.load_target(result.targets[main["id"]]))

        self.assertEqual(main["project"], "~/gemini-current")
        self.assertEqual(sub["title"], "Nested synthetic request")
        self.assertEqual(len(payload["messages"]), 2)
        self.assertEqual(payload["messages"][1]["content"], "Updated answer")
        self.assertEqual(payload["summary"], "Synthetic summary")

    def test_opencode_legacy_synthetic_archive(self):
        storage = self.home / ".local/share/opencode/storage"
        session = {
            "id": "ses_1",
            "projectID": "proj_1",
            "directory": str(self.home / "project"),
            "title": "OpenCode task",
            "version": "1",
            "time": {"created": 1000, "updated": 4000},
        }
        write_json(storage / "session/proj_1/ses_1.json", session)
        message = {"id": "msg_1", "sessionID": "ses_1", "time": {"created": 2000}}
        write_json(storage / "message/ses_1/msg_1.json", message)
        write_json(
            storage / "part/msg_1/part_2.json",
            {
                "id": "part_2",
                "sessionID": "ses_1",
                "messageID": "msg_1",
                "type": "tool",
                "time": {"created": 3000},
            },
        )
        write_json(
            storage / "part/msg_1/part_1.json",
            {
                "id": "part_1",
                "sessionID": "ses_1",
                "messageID": "msg_1",
                "type": "text",
                "text": "hello",
                "time": {"created": 2500},
            },
        )

        result = providers.scan_opencode(self.home)
        payload = json.loads(providers.load_target(result.targets[result.sessions[0]["id"]]))

        self.assertEqual(result.sessions[0]["project"], "~/project")
        self.assertTrue(result.sessions[0]["experimental"])
        self.assertEqual(result.status["stability"], "experimental")
        self.assertEqual(payload["session"], session)
        self.assertEqual(payload["messages"][0]["message"], message)
        self.assertEqual(
            [part["id"] for part in payload["messages"][0]["parts"]],
            ["part_1", "part_2"],
        )

    def test_opencode_sqlite_synthetic_archive(self):
        db = self.home / ".local/share/opencode/opencode.db"
        db.parent.mkdir(parents=True)
        connection = sqlite3.connect(db)
        connection.execute(
            "CREATE TABLE session (id TEXT PRIMARY KEY, directory TEXT, title TEXT, "
            "time_created INTEGER, time_updated INTEGER)"
        )
        connection.execute(
            "CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, "
            "time_created INTEGER, data TEXT)"
        )
        connection.execute(
            "CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT, "
            "time_created INTEGER, data TEXT)"
        )
        connection.execute(
            "INSERT INTO session VALUES (?, ?, ?, ?, ?)",
            ("ses_db", str(self.home / "db-project"), "DB task", 1000, 4000),
        )
        connection.execute(
            "INSERT INTO message VALUES (?, ?, ?, ?)",
            ("msg_db", "ses_db", 2000, json.dumps({"role": "user"})),
        )
        connection.execute(
            "INSERT INTO part VALUES (?, ?, ?, ?, ?)",
            (
                "part_db", "msg_db", "ses_db", 3000,
                json.dumps({"type": "text", "text": "from sqlite"}),
            ),
        )
        connection.commit()
        connection.close()

        result = providers.scan_opencode(self.home)
        item = next(item for item in result.sessions if item["title"] == "DB task")
        payload = json.loads(providers.load_target(result.targets[item["id"]]))

        self.assertEqual(item["project"], "~/db-project")
        self.assertEqual(payload["session"]["id"], "ses_db")
        self.assertEqual(payload["messages"][0]["message"]["role"], "user")
        self.assertEqual(payload["messages"][0]["parts"][0]["text"], "from sqlite")

    def test_opencode_prefers_sqlite_over_migrated_legacy_copy(self):
        storage = self.home / ".local/share/opencode/storage"
        write_json(storage / "session/project/ses_shared.json", {
            "id": "ses_shared", "title": "Legacy copy", "directory": "/legacy",
        })
        db = self.home / ".local/share/opencode/opencode.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(db)
        connection.execute(
            "CREATE TABLE session (id TEXT PRIMARY KEY, directory TEXT, title TEXT)"
        )
        connection.execute(
            "INSERT INTO session VALUES (?, ?, ?)",
            ("ses_shared", "/current", "Current copy"),
        )
        connection.commit()
        connection.close()

        result = providers.scan_opencode(self.home)

        self.assertEqual(len(result.sessions), 1)
        self.assertEqual(result.sessions[0]["title"], "Current copy")

    def test_cursor_sqlite_listing_and_filtered_archive(self):
        db = (
            self.home
            / "Library/Application Support/Cursor/User/globalStorage/state.vscdb"
        )
        db.parent.mkdir(parents=True)
        connection = sqlite3.connect(db)
        connection.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value BLOB)")
        connection.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value BLOB)")
        composer = {
            "composerId": "composer-1",
            "name": "",
            "createdAt": 1_700_000_000_000,
            "lastUpdatedAt": 1_700_000_100_000,
            "model": "cursor-model",
            "workspaceProjectDir": str(self.home / "cursor-project"),
            "fullConversationHeadersOnly": [
                {"bubbleId": "user-1", "type": 1},
                {"bubbleId": "assistant-1", "type": 2},
            ],
            "attachedFileContents": "must not escape",
        }
        user = {
            "bubbleId": "user-1",
            "type": 1,
            "text": "Cursor prompt",
            "createdAt": 1_700_000_001_000,
            "secretContext": "must not escape",
        }
        assistant = {
            "bubbleId": "assistant-1",
            "type": 2,
            "text": "Cursor answer",
            "thinking": "selected thought",
            "editorState": "must not escape",
        }
        connection.executemany(
            "INSERT INTO cursorDiskKV(key, value) VALUES (?, ?)",
            [
                ("composerData:composer-1", json.dumps(composer)),
                ("bubbleId:composer-1:user-1", json.dumps(user)),
                ("bubbleId:composer-1:assistant-1", json.dumps(assistant)),
                ("unrelated:secret", json.dumps({"token": "no"})),
            ],
        )
        connection.commit()
        connection.close()

        result = providers.scan_cursor(self.home)
        payload = json.loads(providers.load_target(result.targets[result.sessions[0]["id"]]))

        self.assertEqual(result.status["status"], "ok")
        self.assertEqual(result.status["stability"], "experimental")
        self.assertTrue(result.sessions[0]["experimental"])
        self.assertEqual(result.sessions[0]["title"], "Cursor prompt")
        self.assertEqual(result.sessions[0]["project"], "~/cursor-project")
        self.assertEqual(payload["composer"]["composerId"], "composer-1")
        self.assertNotIn("attachedFileContents", payload["composer"])
        self.assertEqual([item["bubbleId"] for item in payload["bubbles"]], ["user-1", "assistant-1"])
        self.assertNotIn("secretContext", payload["bubbles"][0])
        self.assertNotIn("editorState", payload["bubbles"][1])

    def test_resolve_native_id_is_ambiguous_across_sources(self):
        write_jsonl(
            self.home / ".pi/agent/sessions/--work--/dup.jsonl",
            [
                {"type": "session", "version": 3, "id": "shared-id",
                 "timestamp": "2026-08-11T17:10:33Z",
                 "cwd": str(self.home / "work")},
                {"type": "message", "message": {"role": "user", "content": "Pi copy"}},
            ],
        )
        write_jsonl(
            self.home / ".commandcode/projects/users-work/shared-id.jsonl",
            [{"id": "e1", "role": "user",
              "content": [{"type": "text", "text": "CC copy"}]}],
        )
        sessions, targets, _statuses = providers.discover(self.home)
        with self.assertRaises(providers.AmbiguousSessionId) as raised:
            providers.resolve_session_entry("shared-id", sessions)
        self.assertIn("pi:shared-id", str(raised.exception))
        self.assertIn("commandcode:shared-id", str(raised.exception))
        pi = providers.resolve_session_entry("pi:shared-id", sessions)
        self.assertEqual(pi["source"], "pi")
        cc = providers.resolve_session_entry("commandcode:shared-id", sessions)
        self.assertEqual(cc["source"], "commandcode")
        opaque, target = providers.resolve_session_id(pi["id"], sessions, targets)
        self.assertEqual(opaque, pi["id"])
        self.assertEqual(providers.load_target(target).count("Pi copy"), 1)

    def test_corrupt_cursor_database_is_a_provider_warning(self):
        db = (
            self.home
            / "Library/Application Support/Cursor/User/globalStorage/state.vscdb"
        )
        db.parent.mkdir(parents=True)
        db.write_bytes(b"not a sqlite database")

        result = providers.scan_cursor(self.home)

        self.assertEqual(result.sessions, [])
        self.assertEqual(result.status["status"], "warning")
        self.assertIn("Cursor database unavailable", result.status["message"])

    def test_discovery_has_all_statuses_and_opaque_ids(self):
        write_jsonl(
            self.home / ".claude/projects/-example/secret-name.jsonl",
            [{"type": "user", "message": {"content": "hello"}}],
        )

        sessions, targets, statuses = providers.discover(self.home)

        self.assertEqual({item["source"] for item in statuses}, {
            "claude", "codex", "pi", "prime", "commandcode", "cowork", "copilot",
            "cline", "roo", "goose", "vscode", "gemini",
            "hermes",
            "opencode", "cursor", "cursor-cli", "kimi",
            "amp", "qwen", "aider", "trae", "kiro", "kilo", "zed",
        })
        self.assertEqual(len(sessions), 1)
        self.assertRegex(sessions[0]["id"], r"^[0-9a-f]{12}$")
        self.assertNotIn("secret-name", sessions[0]["id"])
        self.assertNotIn("native_id", sessions[0])  # generic filename is not a native id
        self.assertEqual(set(targets), {sessions[0]["id"]})
        required = {"id", "source", "format", "project", "title", "mtime", "size"}
        self.assertTrue(required.issubset(sessions[0]))


class HandlerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original_discover = serve.discover
        serve.discover = lambda home=None: (
            [],
            {},
            [{"source": "claude", "count": 0, "status": "ok"}],
        )
        cls.server = serve.ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        serve.discover = cls.original_discover

    def request(self, path, host):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        connection.request("GET", path, headers={"Host": host})
        response = connection.getresponse()
        body = response.read()
        headers = dict(response.getheaders())
        connection.close()
        return response.status, headers, body

    def test_local_host_security_headers_and_provider_response(self):
        status, headers, body = self.request("/api/sessions", "localhost")
        self.assertEqual(status, 200)
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertIn("providers", json.loads(body))

        status, _, _ = self.request("/api/session/../../etc/passwd", "localhost")
        self.assertEqual(status, 404)
        status, _, _ = self.request("/", "example.com")
        self.assertEqual(status, 403)

        status, headers, body = self.request("/fables-core.js", "localhost")
        self.assertEqual(status, 200)
        self.assertIn("text/javascript", headers["Content-Type"])
        self.assertIn(b"parseSession", body)


if __name__ == "__main__":
    unittest.main()
