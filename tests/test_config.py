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
        self,
        body: str,
        *arguments: str,
        key: str | None = None,
        path_prefix: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        if key is None:
            environment.pop("ASSEMBLYAI_API_KEY", None)
        else:
            environment["ASSEMBLYAI_API_KEY"] = key
        if path_prefix is not None:
            environment["PATH"] = f"{path_prefix}:{environment['PATH']}"
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

    def test_temporary_file_failure_is_truthful_and_preserves_existing_key(self) -> None:
        self.env_file.write_text("ASSEMBLYAI_API_KEY=old-value\n", encoding="utf-8")
        self.env_file.chmod(0o600)
        before = self.env_file.read_bytes()
        tools = self.root / "tools"
        tools.mkdir()
        failing_mktemp = tools / "mktemp"
        failing_mktemp.write_text("#!/usr/bin/env bash\nexit 71\n", encoding="utf-8")
        failing_mktemp.chmod(0o755)
        result = self.run_shell(
            'meeting_store_assemblyai_key replacement', path_prefix=tools
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.env_file.read_bytes(), before)
        self.assertEqual(list(self.root.glob(".env.*")), [])

    def test_installer_children_never_inherit_a_process_override(self) -> None:
        install_root = self.root / "install-root"
        (install_root / "bin").mkdir(parents=True)
        shutil.copy2(PROJECT_ROOT / "install.sh", install_root / "install.sh")
        leak_marker = self.root / "installer-key-leak"
        tools = self.root / "installer-tools"
        tools.mkdir()
        for name in ("brew", "uv"):
            tool = tools / name
            tool.write_text(
                '#!/usr/bin/env bash\n'
                '[[ -z "${ASSEMBLYAI_API_KEY:-}" ]] || touch "$STUB_INSTALL_KEY_LEAK"\n',
                encoding="utf-8",
            )
            tool.chmod(0o755)
        uname = tools / "uname"
        uname.write_text(
            '#!/usr/bin/env bash\n'
            '[[ -z "${ASSEMBLYAI_API_KEY:-}" ]] || touch "$STUB_INSTALL_KEY_LEAK"\n'
            'printf "Darwin\\n"\n',
            encoding="utf-8",
        )
        uname.chmod(0o755)
        setup = install_root / "bin" / "meeting-setup"
        setup.write_text(
            '#!/usr/bin/env bash\n'
            '[[ -z "${ASSEMBLYAI_API_KEY:-}" ]] || touch "$STUB_INSTALL_KEY_LEAK"\n',
            encoding="utf-8",
        )
        setup.chmod(0o755)
        environment = os.environ.copy()
        environment.update(
            {
                "ASSEMBLYAI_API_KEY": SENTINEL,
                "PATH": f"{tools}:{environment['PATH']}",
                "STUB_INSTALL_KEY_LEAK": str(leak_marker),
            }
        )
        result = subprocess.run(
            ["/bin/bash", str(install_root / "install.sh"), "--yes"],
            text=True,
            capture_output=True,
            env=environment,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(leak_marker.exists())
        self.assert_secret_absent(result)

    def test_doctor_requires_the_expected_token_on_the_override_fd(self) -> None:
        app_root = self.root / "doctor-app"
        (app_root / "bin").mkdir(parents=True)
        (app_root / "lib").mkdir()
        shutil.copy2(PROJECT_ROOT / "bin" / "meeting-doctor", app_root / "bin")
        shutil.copy2(PROJECT_ROOT / "bin" / "meeting", app_root / "bin")
        shutil.copy2(PROJECT_ROOT / "lib" / "config.sh", app_root / "lib")
        (app_root / ".venv").symlink_to(PROJECT_ROOT / ".venv", target_is_directory=True)
        environment = os.environ.copy()
        environment.pop("ASSEMBLYAI_API_KEY", None)
        environment.update(
            {
                "HOME": str(self.root / "doctor-home"),
                "MEETING_DATA_DIR": str(self.root / "doctor-data"),
                "MEETING_CONFIG_DIR": str(self.root / "doctor-config"),
                "MEETING_TRANSCRIBER_KEY_OVERRIDE_FD": "9",
            }
        )
        closed_fd_result = subprocess.run(
            ["/bin/bash", str(app_root / "bin" / "meeting-doctor")],
            text=True,
            capture_output=True,
            env=environment,
            check=False,
        )
        self.assertIn("No AssemblyAI API key", closed_fd_result.stderr)
        self.assertNotIn("Bad file descriptor", closed_fd_result.stderr)
        self.assertNotIn(
            "available from the one-command environment override",
            closed_fd_result.stderr,
        )

        wrong_token_result = subprocess.run(
            [
                "/bin/bash",
                "-c",
                'exec 9<<< wrong-token; exec "$1"',
                "test",
                str(app_root / "bin" / "meeting-doctor"),
            ],
            text=True,
            capture_output=True,
            env=environment,
            check=False,
        )
        self.assertIn("No AssemblyAI API key", wrong_token_result.stderr)
        self.assertNotIn(
            "available from the one-command environment override",
            wrong_token_result.stderr,
        )


if __name__ == "__main__":
    unittest.main()
