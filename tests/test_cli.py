import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.test_library import make_zip

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "fables-cli.py"


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.library = self.home / "library"
        self.bundle = make_zip(self.home / "export.zip", text="find this passage")
        self.env = os.environ.copy()
        self.env.update({"HOME": str(self.home), "FABLES_LIBRARY": str(self.library)})

    def tearDown(self):
        self.temp.cleanup()

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(CLI), *args], cwd=ROOT, env=self.env,
            text=True, capture_output=True,
        )

    def test_bare_groups_are_help_only_and_syntax_errors_are_status_2(self):
        for group in ("import", "session"):
            result = self.run_cli(group)
            self.assertEqual(result.returncode, 0)
            self.assertIn("usage:", result.stdout)
            self.assertFalse(self.library.exists())
        bad = self.run_cli("not-a-command")
        self.assertEqual(bad.returncode, 2)

    def test_json_inspect_apply_verify_and_session_read(self):
        inspected = self.run_cli("import", "inspect", str(self.bundle),
                                 "--origin", "m1-air", "--format", "json")
        self.assertEqual(inspected.returncode, 0, inspected.stderr)
        inspection = json.loads(inspected.stdout)
        self.assertTrue(inspection["ok"])
        digest = inspection["result"]["sha256"]

        applied = self.run_cli(
            "import", "apply", str(self.bundle), "--origin", "m1-air",
            "--expect-sha256", digest, "--format", "json",
        )
        self.assertEqual(applied.returncode, 0, applied.stderr)
        outcome = json.loads(applied.stdout)["result"]
        sid = outcome["created"][0]
        import_id = outcome["import_id"]

        verified = self.run_cli("import", "get", import_id, "--format", "json")
        self.assertEqual(json.loads(verified.stdout)["result"]["state"], "complete")

        listed = self.run_cli("session", "list", "--scope", "imported",
                              "--origin", "m1-air", "--format", "json")
        rows = json.loads(listed.stdout)["result"]["sessions"]
        self.assertEqual([row["id"] for row in rows], [sid])

        searched = self.run_cli("session", "search", "find this", "--scope",
                                "imported", "--format", "json")
        self.assertEqual(json.loads(searched.stdout)["result"]["matches"][0]["id"], sid)

        markdown = self.run_cli("session", "get", sid, "--format", "markdown")
        self.assertIn("## user\nfind this passage", markdown.stdout)

        provenance = self.run_cli("session", "provenance", sid, "--format", "json")
        value = json.loads(provenance.stdout)["result"]
        self.assertEqual(value["provenance"][0]["origin"], "m1-air")

    def test_json_operational_errors_are_on_stderr_with_status_1(self):
        result = self.run_cli(
            "import", "apply", str(self.bundle), "--origin", "m1-air",
            "--expect-sha256", "sha256:" + "0" * 64, "--format", "json",
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        error = json.loads(result.stderr)
        self.assertFalse(error["ok"])
        self.assertEqual(error["error"]["code"], "input_changed")


if __name__ == "__main__":
    unittest.main()
