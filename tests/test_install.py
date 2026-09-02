import os
import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.home = Path(self.scratch.name)
        self.fake_bin = self.home / "fake-bin"
        self.fake_bin.mkdir()
        self.command_log = self.home / "commands.log"

    def tearDown(self):
        self.scratch.cleanup()

    def fake_command(self, name):
        command = self.fake_bin / name
        command.write_text(
            "#!/bin/sh\n"
            'printf \'%s\\n\' "$*" >> "$FABLES_TEST_LOG"\n',
            encoding="utf-8",
        )
        command.chmod(0o755)

    def environment(self, platform):
        env = os.environ.copy()
        env.update(
            {
                "HOME": str(self.home),
                "FABLES_PLATFORM": platform,
                "FABLES_PYTHON": sys.executable,
                "FABLES_TEST_LOG": str(self.command_log),
                "PATH": f"{self.fake_bin}{os.pathsep}{env['PATH']}",
            }
        )
        return env

    def run_installer(self, platform, *arguments):
        return subprocess.run(
            [str(ROOT / "install.sh"), "--no-open", *arguments],
            cwd=ROOT,
            env=self.environment(platform),
            check=True,
            capture_output=True,
            text=True,
        )

    def test_macos_install_is_idempotent_and_uninstall_is_precise(self):
        self.fake_command("launchctl")

        first = self.run_installer("Darwin", "--port", "9876")
        second = self.run_installer("Darwin", "--port", "9876")

        install_dir = self.home / ".local/share/fables"
        control = self.home / ".local/bin/fables"
        plist = self.home / "Library/LaunchAgents/com.srijanshukla.fables.plist"
        with plist.open("rb") as stream:
            service = plistlib.load(stream)

        self.assertIn("Fables is installed", first.stdout)
        self.assertIn("Fables is installed", second.stdout)
        self.assertEqual(
            service["ProgramArguments"],
            [sys.executable, str(install_dir / "serve.py"), "9876"],
        )
        self.assertTrue(service["RunAtLoad"])
        self.assertTrue(service["KeepAlive"])
        self.assertEqual(service["EnvironmentVariables"]["PYTHONUNBUFFERED"], "1")
        self.assertTrue(control.exists())
        self.assertEqual((install_dir / ".port").read_text(), "9876\n")
        self.assertTrue((install_dir / "fables-cli.py").exists())
        self.assertTrue((install_dir / "fables_library.py").exists())
        self.assertTrue((install_dir / "skills/fables/SKILL.md").exists())
        import_help = subprocess.run(
            [str(control), "import"], env=self.environment("Darwin"),
            check=True, capture_output=True, text=True,
        )
        self.assertIn("usage: fables import", import_help.stdout)
        self.assertFalse((install_dir / "library.db").exists())
        url = subprocess.run(
            [str(control), "url"],
            env=self.environment("Darwin"),
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(url.stdout, "http://localhost:9876\n")
        calls = self.command_log.read_text()
        self.assertEqual(calls.count("bootstrap"), 2)

        subprocess.run(
            [str(install_dir / "uninstall.sh")],
            env=self.environment("Darwin"),
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertFalse(plist.exists())
        self.assertFalse(control.exists())
        self.assertFalse(install_dir.exists())

    def test_uninstall_preserves_a_durable_library(self):
        self.fake_command("launchctl")
        self.run_installer("Darwin")
        install_dir = self.home / ".local/share/fables"
        (install_dir / "library.db").write_bytes(b"library")
        (install_dir / "objects").mkdir()
        (install_dir / "imports").mkdir()
        result = subprocess.run(
            [str(install_dir / "uninstall.sh")], env=self.environment("Darwin"),
            check=True, capture_output=True, text=True,
        )
        self.assertTrue((install_dir / "library.db").exists())
        self.assertTrue((install_dir / "objects").is_dir())
        self.assertFalse((install_dir / "serve.py").exists())
        self.assertIn("durable session library was preserved", result.stdout)

    def test_linux_install_generates_and_enables_user_unit(self):
        self.fake_command("systemctl")

        result = self.run_installer("Linux")

        install_dir = self.home / ".local/share/fables"
        unit = self.home / ".config/systemd/user/fables.service"
        unit_text = unit.read_text()
        self.assertIn("Fables is installed", result.stdout)
        self.assertIn(
            f'ExecStart="{sys.executable}" "{install_dir / "serve.py"}" "8321"',
            unit_text,
        )
        self.assertIn("Restart=always", unit_text)
        calls = self.command_log.read_text()
        self.assertIn("--user show-environment", calls)
        self.assertIn("--user daemon-reload", calls)
        self.assertIn("--user enable fables.service", calls)
        self.assertIn("--user restart fables.service", calls)

        subprocess.run(
            [str(install_dir / "uninstall.sh")],
            env=self.environment("Linux"),
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertFalse(unit.exists())
        self.assertFalse((self.home / ".local/bin/fables").exists())
        self.assertFalse(install_dir.exists())

    def test_rejects_invalid_port_without_writing_files(self):
        self.fake_command("launchctl")

        result = subprocess.run(
            [str(ROOT / "install.sh"), "--no-open", "--port", "70000"],
            cwd=ROOT,
            env=self.environment("Darwin"),
            capture_output=True,
            text=True,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("port must be a number", result.stderr)
        self.assertFalse((self.home / ".local/share/fables").exists())
