"""Exercise the Linux menu without installing software or generating media."""
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class LinuxMenuTests(unittest.TestCase):
    def menu(self, answers):
        source = (ROOT / "scripts/setup-linux.sh").read_text()
        functions = source.split("run_video_menu_command() {", 1)[1].split("run_wizard() {", 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "bin").mkdir()
            cli = root / "bin/narration-video-gen"
            cli.write_text('#!/bin/bash\nprintf "CALLED:%s\\n" "$*"\n[ "$1" != plan ]\n')
            cli.chmod(0o755)
            script = ('set -euo pipefail\nroot="$1"\nui_language=en\n'
                      'say() { printf "%s\\n" "$2"; }\n'
                      'run_video_menu_command() {' + functions + '\nshow_video_menu\n')
            return subprocess.run(["bash", "-c", script, "menu-test", directory],
                                  input=answers, text=True, capture_output=True, timeout=5)

    def test_failed_preparation_returns_to_menu_and_next_steps(self):
        result = self.menu("1\n1\n2\n3\n4\n0\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([line for line in result.stdout.splitlines() if line.startswith("CALLED:")],
                         ["CALLED:tts web start", "CALLED:plan", "CALLED:run", "CALLED:status"])
        self.assertIn("operation did not complete", result.stdout)

    def test_lan_start_uses_the_password_protected_mode(self):
        result = self.menu("1\n2\n0\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([line for line in result.stdout.splitlines() if line.startswith("CALLED:")],
                         ["CALLED:tts web start --lan"])

    def test_cancel_requires_confirmation(self):
        result = self.menu("5\nn\n5\ny\n0\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count("CALLED:cancel"), 1)

    def test_eof_and_invalid_input_do_not_start_work(self):
        for answers in ("", "invalid\n", "5\n"):
            result = self.menu(answers)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("CALLED:", result.stdout)


if __name__ == "__main__":
    unittest.main()
