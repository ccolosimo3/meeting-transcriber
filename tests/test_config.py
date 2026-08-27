from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SENTINEL = "config-test-sentinel"


class CredentialFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "lib").mkdir()
        shutil.copy2(PROJECT_ROOT / "lib" / "config.sh", self.root / "lib" / "config.sh")
        self.env_file = self.root / ".env"

    def run_shell(
        self, body: str, *arguments: str, key: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        if key is None:
            environment.pop("ASSEMBLYAI_API_KEY", None)
        else:
            environment["ASSEMBLYAI_API_KEY"] = key
        return subprocess.run(
            [
                "/bin/bash", "-c", 'source "$1"; ' + body, "test",
                str(self.root / "lib" / "config.sh"), *arguments,
            ],
            text=True,
            capture_output=True,
            env=environment,
            check=False,
        )

    def assert_secret_absent(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertNotIn(SENTINEL, result.stdout + result.stderr)

    def test_atomic_store_creates_exact_private_assignment_and_loads_it(self) -> None:
        result = self.run_shell(
            'meeting_store_assemblyai_key "$2"; meeting_load_assemblyai_key; '
            '[[ "$meeting_assemblyai_key" == "$2" ]]',
            SENTINEL,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.env_file.read_text(encoding="utf-8"), f"ASSEMBLYAI_API_KEY={SENTINEL}\n")
        self.assertEqual(self.env_file.stat().st_mode & 0o777, 0o600)
        self.assert_secret_absent(result)

    def test_process_override_precedes_file_without_modifying_it(self) -> None:
        self.env_file.write_text("ASSEMBLYAI_API_KEY=file-value\n", encoding="utf-8")
        self.env_file.chmod(0o600)
        before = self.env_file.read_bytes()
        result = self.run_shell(
            'meeting_load_assemblyai_key; [[ "$meeting_assemblyai_key" == "$meeting_inherited_assemblyai_key" ]]; '
            '[[ -z "${ASSEMBLYAI_API_KEY:-}" ]]',
            key=SENTINEL,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.env_file.read_bytes(), before)
        self.assert_secret_absent(result)

    def test_malformed_extraneous_and_loose_files_are_refused_without_overwrite(self) -> None:
        cases = {
            "extraneous": "ASSEMBLYAI_API_KEY=value\nOTHER=value\n",
            "empty": "ASSEMBLYAI_API_KEY=\n",
            "shell_syntax": "export ASSEMBLYAI_API_KEY=value\n",
        }
        for name, contents in cases.items():
            with self.subTest(name=name):
                self.env_file.write_text(contents, encoding="utf-8")
                self.env_file.chmod(0o600)
                before = self.env_file.read_bytes()
                result = self.run_shell('meeting_store_assemblyai_key replacement')
                self.assertEqual(result.returncode, 2)
                self.assertEqual(self.env_file.read_bytes(), before)
        self.env_file.write_text("ASSEMBLYAI_API_KEY=value\n", encoding="utf-8")
        self.env_file.chmod(0o644)
        result = self.run_shell('meeting_load_assemblyai_key')
        self.assertEqual(result.returncode, 2)

    def test_symlink_is_refused_without_touching_target(self) -> None:
        target = self.root / "target"
        target.write_text("unrelated\n", encoding="utf-8")
        self.env_file.symlink_to(target)
        result = self.run_shell('meeting_store_assemblyai_key replacement')
        self.assertEqual(result.returncode, 2)
        self.assertEqual(target.read_text(encoding="utf-8"), "unrelated\n")


if __name__ == "__main__":
    unittest.main()
