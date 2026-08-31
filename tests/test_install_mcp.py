import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "install-mcp.py"
SERVER_NAME = "fables"

ENV = {"PATH": "/usr/bin:/bin"}  # excludes copilot and uv; explicit server args used


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    return path


def run_installer(home, *extra):
    return subprocess.run(
        [sys.executable, str(INSTALLER), "--home", str(home),
         "--server-cmd", sys.executable, "--server-args", str(ROOT / "fables-mcp.py"),
         *extra],
        capture_output=True, text=True, env={**os.environ, **ENV, "HOME": str(home)},
    )


class InstallMcpTests(unittest.TestCase):
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
        self._seed()

    def tearDown(self):
        self.temp.cleanup()

    def _seed(self):
        home = self.home
        write_json(home / ".claude.json", {"installMethod": "native",
                                           "projects": {"/tmp/x": {"history": []}},
                                           "mcpServers": {}})
        write_json(home / ".gemini/settings.json",
                   {"general": {}, "mcpServers": {"context7": {
                       "command": "npx", "args": ["-y", "@upstash/context7-mcp"]}}})
        write_json(home / ".cursor/mcp.json", {"mcpServers": {"fetch": {
            "type": "stdio", "command": "uvx", "args": ["mcp-server-fetch"], "env": {}}}})
        opencode = home / ".config/opencode/opencode.jsonc"
        opencode.parent.mkdir(parents=True)
        opencode.write_text(
            '{\n'
            '  "$schema": "https://opencode.ai/config.json",\n'
            '  // shared servers\n'
            '  "mcp": {\n'
            '    "atlassian-rovo": {\n'
            '      "type": "remote",\n'
            '      "url": "https://mcp.atlassian.com/v1/mcp/authv2",\n'
            '      "enabled": true\n'
            '    }\n'
            '  }\n'
            '}\n', encoding="utf-8")
        write_json(home / ".cline/data/settings/cline_mcp_settings.json",
                   {"mcpServers": {}})
        cline_editor = (home / "Library/Application Support/Code/User/globalStorage/"
                        "saoudrizwan.claude-dev/settings")
        write_json(cline_editor / "cline_mcp_settings.json", {"mcpServers": {}})
        roo_editor = (home / "Library/Application Support/Code/User/globalStorage/"
                      "rooveterinaryinc.roo-cline/settings")
        write_json(roo_editor / "mcp_settings.json", {"mcpServers": {}})
        (home / "Library/Application Support/Code/User/settings.json").parent.mkdir(
            parents=True, exist_ok=True)
        (home / "Library/Application Support/Code/User/settings.json").write_text(
            "{}", encoding="utf-8")
        codex = home / ".codex/config.toml"
        codex.parent.mkdir(parents=True)
        codex.write_text('model = "gpt-5.6-sol"\n\n# [mcp_servers.context7]\n',
                         encoding="utf-8")
        goose = home / ".config/goose/config.yaml"
        goose.parent.mkdir(parents=True)
        goose.write_text("extensions:\n  existing:\n    type: stdio\n    enabled: true\n",
                         encoding="utf-8")
        pi_ext = home / ".pi/agent/extensions"
        pi_ext.mkdir(parents=True)
        (pi_ext / "feature-end.ts").write_text("export default function x() {}\n",
                                               encoding="utf-8")
        write_json(home / ".prime/agent/settings.json", {"defaultModel": "gpt-5.6-luna"})
        hermes = home / ".hermes/config.yaml"
        hermes.parent.mkdir(parents=True)
        hermes.write_text(
            "model:\n  default: gpt-fixture\n\n"
            "mcp_servers:\n"
            "  context7:\n"
            "    command: uvx\n"
            "    args:\n"
            "      - context7-mcp\n",
            encoding="utf-8",
        )
        write_json(home / ".qwen/settings.json", {"general": {}})
        (home / "Library/Application Support/Trae/User").mkdir(parents=True, exist_ok=True)
        write_json(home / ".config/zed/settings.json", {"theme": "dark"})

    def test_install_registers_every_agent(self):
        result = run_installer(self.home)
        self.assertEqual(result.returncode, 0, result.stderr)

        codex = (self.home / ".codex/config.toml").read_text()
        self.assertIn("[mcp_servers.fables]", codex)
        self.assertIn('command = "%s"' % sys.executable, codex)
        self.assertNotIn(", %s" % sys.executable, codex)  # command not duplicated in args
        self.assertIn("model = \"gpt-5.6-sol\"", codex)  # untouched

        claude = json.loads((self.home / ".claude.json").read_text())
        self.assertEqual(claude["mcpServers"][SERVER_NAME]["type"], "stdio")
        self.assertEqual(claude["mcpServers"][SERVER_NAME]["command"], sys.executable)
        self.assertIn("/tmp/x", claude["projects"])

        gemini = json.loads((self.home / ".gemini/settings.json").read_text())
        self.assertIn(SERVER_NAME, gemini["mcpServers"])
        self.assertIn("context7", gemini["mcpServers"])  # preserved

        cursor = json.loads((self.home / ".cursor/mcp.json").read_text())
        self.assertIn(SERVER_NAME, cursor["mcpServers"])
        self.assertIn("fetch", cursor["mcpServers"])
        self.assertEqual(
            cursor["mcpServers"][SERVER_NAME]["env"]["UV_CACHE_DIR"],
            "/private/tmp/fables-uv-cache",
        )

        opencode = (self.home / ".config/opencode/opencode.jsonc").read_text()
        self.assertIn('// shared servers', opencode)  # comments preserved
        self.assertIn('"atlassian-rovo"', opencode)   # preserved
        self.assertIn('"fables"', opencode)

        for path in (self.home / ".cline/data/settings/cline_mcp_settings.json",
                     self.home / "Library/Application Support/Code/User/globalStorage/"
                     "saoudrizwan.claude-dev/settings/cline_mcp_settings.json"):
            data = json.loads(path.read_text())
            self.assertIn(SERVER_NAME, data["mcpServers"])
            self.assertEqual(data["mcpServers"][SERVER_NAME]["disabled"], False)

        roo = json.loads((self.home / "Library/Application Support/Code/User/globalStorage/"
                          "rooveterinaryinc.roo-cline/settings/mcp_settings.json").read_text())
        self.assertIn(SERVER_NAME, roo["mcpServers"])

        vscode = json.loads((self.home / "Library/Application Support/Code/User/mcp.json")
                            .read_text())
        self.assertIn(SERVER_NAME, vscode["servers"])  # VS Code uses "servers"

        goose = (self.home / ".config/goose/config.yaml").read_text()
        self.assertIn("  fables:", goose)
        self.assertIn(f"    cmd: \"{sys.executable}\"", goose)
        self.assertIn("  existing:", goose)  # preserved

        pi_ext = self.home / ".pi/agent/extensions"
        self.assertEqual((pi_ext / "fables-mcp.ts").read_text(),
                         (ROOT / "fables-mcp.ts").read_text())
        sidecar = json.loads((pi_ext / "fables-mcp.json").read_text())
        self.assertEqual(sidecar, {"cmd": sys.executable,
                                   "args": [str(ROOT / "fables-mcp.py")]})
        self.assertTrue((pi_ext / "feature-end.ts").exists())

        prime_settings = json.loads(
            (self.home / ".prime/agent/settings.json").read_text())
        self.assertEqual(prime_settings["mcpServers"][SERVER_NAME],
                         {"type": "http", "url": "http://127.0.0.1:8322/mcp",
                          "enabled": True})
        self.assertEqual(prime_settings["defaultModel"], "gpt-5.6-luna")
        skill = self.home / ".prime/agent/skills/fables"
        self.assertTrue((skill / "SKILL.md").is_file())
        self.assertTrue((skill / "pyproject.toml").is_file())
        self.assertTrue((skill / "src/fables/__init__.py").is_file())
        self.assertIn("127.0.0.1:8322/mcp",
                      (skill / "src/fables/__init__.py").read_text())
        hermes = (self.home / ".hermes/config.yaml").read_text()
        self.assertIn("mcp_servers:", hermes)
        self.assertIn("  fables:", hermes)
        self.assertIn(f"    command: \"{sys.executable}\"", hermes)
        self.assertIn("  context7:", hermes)
        self.assertIn("  default: gpt-fixture", hermes)
        self.assertIn("commandcode", result.stdout)
        self.assertIn("skipped", result.stdout)  # cmd CLI absent from test PATH

        qwen = json.loads((self.home / ".qwen/settings.json").read_text())
        self.assertIn(SERVER_NAME, qwen["mcpServers"])
        self.assertEqual(qwen["general"], {})

        trae = json.loads((self.home / "Library/Application Support/Trae/User/mcp.json")
                          .read_text())
        self.assertIn(SERVER_NAME, trae["mcpServers"])

        kiro = json.loads((self.home / ".kiro/settings/mcp.json").read_text())
        self.assertIn(SERVER_NAME, kiro["mcpServers"])

        kilo = (self.home / ".config/kilo/kilo.jsonc").read_text()
        self.assertIn('"mcp"', kilo)
        self.assertIn('"fables"', kilo)
        self.assertIn('"type": "local"', kilo)

        zed = json.loads((self.home / ".config/zed/settings.json").read_text())
        self.assertIn(SERVER_NAME, zed["context_servers"])
        self.assertEqual(zed["theme"], "dark")
        self.assertIn("aider", result.stdout)
        self.assertIn("no native MCP client support", result.stdout)

    def test_install_is_idempotent(self):
        first = run_installer(self.home)
        self.assertEqual(first.returncode, 0, first.stderr)
        second = run_installer(self.home)
        self.assertEqual(second.returncode, 0, second.stderr)
        codex_text = (self.home / ".codex/config.toml").read_text()
        self.assertEqual(codex_text.count("[mcp_servers.fables]"), 1)
        gemini_text = (self.home / ".gemini/settings.json").read_text()
        self.assertEqual(gemini_text.count('"fables"'), 1)
        goose_text = (self.home / ".config/goose/config.yaml").read_text()
        self.assertEqual(goose_text.count("  fables:"), 1)
        hermes_text = (self.home / ".hermes/config.yaml").read_text()
        self.assertEqual(hermes_text.count("  fables:"), 1)
        # A third run must not change anything.
        before = {p: p.read_bytes() for p in
                  (self.home / ".codex/config.toml",
                   self.home / ".gemini/settings.json",
                   self.home / ".cursor/mcp.json",
                   self.home / ".config/opencode/opencode.jsonc",
                   self.home / ".config/goose/config.yaml",
                   self.home / ".hermes/config.yaml",
                   self.home / ".claude.json")}
        run_installer(self.home)
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content, f"{path} changed on re-run")

    def test_check_reports_registered(self):
        run_installer(self.home)
        result = run_installer(self.home, "--check")
        self.assertEqual(result.returncode, 0)
        self.assertIn("codex", result.stdout)
        self.assertIn("registered", result.stdout)
        self.assertNotIn("not registered", result.stdout)

    def test_remove_restores_every_config(self):
        run_installer(self.home)
        result = run_installer(self.home, "--remove")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("removed", result.stdout)

        codex = (self.home / ".codex/config.toml").read_text()
        self.assertNotIn("mcp_servers.fables", codex)
        self.assertIn("model = \"gpt-5.6-sol\"", codex)

        gemini = json.loads((self.home / ".gemini/settings.json").read_text())
        self.assertNotIn(SERVER_NAME, gemini["mcpServers"])
        self.assertIn("context7", gemini["mcpServers"])

        opencode = (self.home / ".config/opencode/opencode.jsonc").read_text()
        self.assertNotIn('"fables"', opencode)
        self.assertIn('"atlassian-rovo"', opencode)
        self.assertIn('// shared servers', opencode)

        goose = (self.home / ".config/goose/config.yaml").read_text()
        self.assertNotIn("fables", goose)
        self.assertIn("existing:", goose)

        self.assertFalse((self.home / ".pi/agent/extensions/fables-mcp.ts").exists())
        self.assertFalse((self.home / ".pi/agent/extensions/fables-mcp.json").exists())
        self.assertTrue((self.home / ".pi/agent/extensions/feature-end.ts").exists())

        prime_settings = json.loads(
            (self.home / ".prime/agent/settings.json").read_text())
        self.assertNotIn("mcpServers", prime_settings)
        self.assertFalse((self.home / ".prime/agent/skills/fables").exists())
        self.assertTrue(
            (self.home / ".prime/agent/skills/fables.fables.bak/SKILL.md").exists())
        hermes = (self.home / ".hermes/config.yaml").read_text()
        self.assertNotIn("  fables:", hermes)
        self.assertIn("  context7:", hermes)
        self.assertIn("  default: gpt-fixture", hermes)
        self.assertNotIn(SERVER_NAME, json.loads(
            (self.home / ".qwen/settings.json").read_text()).get("mcpServers", {}))
        self.assertNotIn(SERVER_NAME, json.loads(
            (self.home / ".config/zed/settings.json").read_text()).get(
                "context_servers", {}))
        kilo_text = (self.home / ".config/kilo/kilo.jsonc").read_text()
        self.assertNotIn('"fables"', kilo_text)

        # Backups were taken before modification.
        self.assertTrue((self.home / ".codex/config.toml.fables.bak").exists())
        self.assertTrue((self.home / ".claude.json.fables.bak").exists())

        # Removing again is a no-op.
        again = run_installer(self.home, "--remove")
        self.assertEqual(again.returncode, 0, again.stderr)

    def test_fresh_home_creates_configs(self):
        home = Path(self.temp.name) / "fresh"
        home.mkdir()
        result = run_installer(home)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((home / ".codex/config.toml").exists())
        self.assertTrue((home / ".claude.json").exists())
        self.assertTrue((home / ".gemini/settings.json").exists())
        self.assertTrue((home / ".cursor/mcp.json").exists())
        self.assertTrue((home / ".config/opencode/opencode.jsonc").exists())
        self.assertTrue((home / ".config/goose/config.yaml").exists())
        # Editor-dependent and CLI-dependent targets are skipped.
        self.assertIn("skipped", result.stdout)
        self.assertNotIn("error", result.stdout)


if __name__ == "__main__":
    unittest.main()
