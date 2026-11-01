"""One bounded, cancellable summary job. Only final app-owned records persist."""
from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

from meeting_digests import DigestError, ActionError, MAX_SUMMARY, prompt, record

TIMEOUT = 300
MAX_STDOUT = 2 * 1024 * 1024
MAX_STDERR = 64 * 1024


def child_environment() -> dict[str, str]:
    # Retain OS identity/keychain/CLI auth location, never parent provider keys,
    # alternate endpoints, desktop tool pipes, hook or telemetry overrides.
    names = {"PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "TMPDIR", "TMP", "TEMP",
             "USERPROFILE", "LOCALAPPDATA", "APPDATA", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT",
             "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMDATA", "HOMEDRIVE", "HOMEPATH",
             "CODEX_HOME", "CLAUDE_CONFIG_DIR"}
    return {name: value for name, value in os.environ.items() if name.upper() in names}


def resolve_cli(provider, env):
    """Finder launches may omit the ordinary per-user native CLI install path."""
    executable = shutil.which(provider, path=env.get("PATH", os.defpath))
    if not executable:
        home = env.get("USERPROFILE" if os.name == "nt" else "HOME")
        candidate = (Path(home) if home else Path.home()) / '.local' / 'bin' / (provider + ('.exe' if os.name == 'nt' else ''))
        if candidate.is_file() and os.access(candidate, os.X_OK):
            executable = str(candidate)
    if not executable or Path(executable).suffix.lower() in {".cmd", ".bat"}:
        raise DigestError("Install the native " + provider.title() + " CLI and sign in with its subscription account outside this app.")
    return str(Path(executable).resolve(strict=True))


class Process:
    """POSIX process group or Windows Job Object, including root-exited children."""
    def __init__(self, command, *, cwd, env):
        self.job = None
        self.header = b""
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes as w

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
            kernel.CreateJobObjectW.restype = w.HANDLE
            kernel.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
            kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
            kernel.CloseHandle.argtypes = [w.HANDLE]
            class Basic(ctypes.Structure):
                _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                            ("flags", w.DWORD), ("min_working", ctypes.c_size_t), ("max_working", ctypes.c_size_t),
                            ("active_processes", w.DWORD), ("affinity", ctypes.c_size_t),
                            ("priority", w.DWORD), ("scheduling", w.DWORD)]
            class IO(ctypes.Structure):
                _fields_ = [(name, ctypes.c_uint64) for name in
                            ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]
            class Limits(ctypes.Structure):
                _fields_ = [("basic", Basic), ("io", IO), ("process_memory", ctypes.c_size_t),
                            ("job_memory", ctypes.c_size_t), ("peak_process", ctypes.c_size_t),
                            ("peak_job", ctypes.c_size_t)]
            handle = kernel.CreateJobObjectW(None, None)
            limits = Limits()
            limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not handle or not kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                if handle:
                    kernel.CloseHandle(handle)
                raise DigestError("Summary process ownership is unavailable.")
            self.job = (kernel, handle)
            self.header = (json.dumps(command) + "\n").encode("utf-8")
            command = ([sys.executable, "--digest-child"] if getattr(sys, "frozen", False) else
                       [sys.executable, str(Path(__file__).with_name("digest_child.py"))])
        try:
            self.process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.PIPE,
                                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False,
                                            start_new_session=os.name != "nt",
                                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            if self.job and not self.job[0].AssignProcessToJobObject(self.job[1], int(self.process._handle)):
                self.process.kill()
                self.process.wait()
                raise DigestError("Summary process ownership is unavailable.")
        except BaseException:
            if self.job:
                self.job[0].CloseHandle(self.job[1])
                self.job = None
            raise

    def close(self):
        if self.job:
            self.job[0].CloseHandle(self.job[1])
            self.job = None
        elif os.name != "nt":
            self._signal(signal.SIGTERM)
            try:
                self.process.wait(timeout=0.4)
            except subprocess.TimeoutExpired:
                pass
            # A root may already have exited while descendants still own pipes.
            self._signal(signal.SIGKILL)
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            raise DigestError("Summary process could not stop safely.") from None

    def _signal(self, signum):
        try:
            os.killpg(self.process.pid, signum)
        except ProcessLookupError:
            pass
        except PermissionError:
            # macOS reports EPERM when a group contains only zombies (including
            # an orphaned child killed by TERM). Reap our root, then ask the OS
            # about group liveness without inspecting command lines. Zombies
            # cannot act or fork; a live member or failed observation is an error.
            if self.process.poll() is None:
                raise
            observed = subprocess.run(['/bin/ps', '-o', 'stat=', '-g', str(self.process.pid)],
                                      capture_output=True, text=True, env=child_environment(), timeout=2)
            states = observed.stdout.split()
            if observed.returncode not in (0, 1) or any(not state.startswith('Z') for state in states):
                raise


def run_process(command, payload, cwd, env, cancel, *, timeout=TIMEOUT):
    owner = Process(command, cwd=cwd, env=env)
    process = owner.process
    output, errors = bytearray(), bytearray()
    overflow = threading.Event()

    def drain(stream, target, limit):
        try:
            while chunk := stream.read(8192):
                remaining = max(0, limit - len(target))
                target.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    overflow.set()
        finally:
            stream.close()

    def write():
        try:
            process.stdin.write(owner.header + payload)
        except (BrokenPipeError, OSError):
            pass
        finally:
            try:
                process.stdin.close()
            except (BrokenPipeError, OSError):
                pass

    workers = [threading.Thread(target=drain, args=(process.stdout, output, MAX_STDOUT)),
               threading.Thread(target=drain, args=(process.stderr, errors, MAX_STDERR)),
               threading.Thread(target=write)]
    for worker in workers:
        worker.start()
    deadline = time.monotonic() + timeout
    failure = None
    try:
        while process.poll() is None:
            if cancel.wait(0.03):
                failure = DigestError("Summary generation cancelled.")
                break
            if overflow.is_set():
                failure = DigestError("The CLI response exceeded the summary limit.")
                break
            if time.monotonic() >= deadline:
                failure = DigestError("Summary generation timed out. Try again when ready.")
                break
    finally:
        try:
            owner.close()
        finally:
            for worker in workers:
                worker.join(4)
        if any(worker.is_alive() for worker in workers):
            raise DigestError("Summary process streams could not stop safely.")
    if failure:
        raise failure
    if cancel.is_set():
        raise DigestError("Summary generation cancelled.")
    if overflow.is_set():
        raise DigestError("The CLI response exceeded the summary limit.")
    return process.returncode, bytes(output), bytes(errors)


def cli_command(executable: str) -> list[str]:
    return [executable, "--print", "--safe-mode", "--restricted", "--tools", "", "--disallowedTools", "*",
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--permission-mode", "dontAsk",
            "--permission-prompts", "none", "--disable-slash-commands", "--no-session-persistence",
            "--settings", '{"disableAllHooks":true}', "--input-format", "text",
            "--output-format", "stream-json", "--verbose"]


def parse_result(raw) -> tuple[str, str | None, dict]:
    try:
        events = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
        final, model, completed, tools = None, None, False, None
        initialized = False
        if not events:
            raise ValueError()
        for event in events:
            kind = event["type"]
            if kind == "system" and event.get("subtype") == "init":
                tools = event.get("tools")
                if initialized or not isinstance(tools, list):
                    raise ValueError()
                if tools:
                    raise DigestError("The CLI exposed unexpected tools. No summary was saved.")
                initialized = True
                model = event.get("model")
            elif kind == "assistant":
                if not initialized:
                    raise ValueError()
                if any(block.get("type") in {"tool_use", "server_tool_use"} for block in event["message"]["content"]):
                    raise ActionError("tool_use")
            elif kind == "result":
                if not initialized or completed or event.get("is_error") or event.get("subtype") != "success" or event.get("permission_denials"):
                    raise ValueError()
                final, completed = event["result"], True
            elif kind not in {"system", "rate_limit_event", "user"}:
                raise ValueError()
        if not completed or not isinstance(final, str) or not final.strip() or len(final.encode("utf-8")) > MAX_SUMMARY:
            raise ValueError()
        if model is not None and (not isinstance(model, str) or len(model) > 200):
            raise ValueError()
        return final.strip(), model, {"completed": True, "action_events": 0, "init_tools": tools,
                                     "provider_warning": False}
    except DigestError:
        raise
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        raise DigestError("The CLI did not return a complete summary. No summary was saved.") from None


class DigestJob:
    def __init__(self, skill_path: Path):
        self.skill_path = skill_path
        self._lock = threading.RLock()
        self._cancel = threading.Event()
        self._thread = None
        self._closed = False
        self._status = {"state": "idle"}

    def start(self, source, publish, provider, meeting_id, run_id):
        with self._lock:
            if self._closed:
                raise DigestError("The app is closing.")
            if self._thread and self._thread.is_alive():
                raise DigestError("A summary is already running. Cancel it or wait for completion.")
            if provider not in {"codex", "claude"}:
                raise DigestError("Choose Codex or Claude.")
            skill = self.skill_path.read_bytes()
            if not skill or len(skill) > 65536:
                raise DigestError("The bundled meeting skill is unavailable.")
            self._cancel = threading.Event()
            self._status = {"id": secrets.token_urlsafe(24), "state": "running", "provider": provider,
                            "meeting_id": meeting_id, "run_id": run_id, "title": source.title,
                            "source_run": source.run_label}
            self._thread = threading.Thread(target=self._work, args=(source, publish, provider, skill), name="meeting-summary")
            self._thread.start()
            return dict(self._status)

    def _run(self, command, data, cwd, env, *, timeout=TIMEOUT):
        return run_process(command, data, cwd, env, self._cancel, timeout=timeout)

    def _work(self, source, publish, provider, skill):
        try:
            if provider == "codex":
                # No supported inspection shares exec's --ignore-user-config
                # bootstrap/cloud selection, and no blanket MCP disable exists.
                # Stop before prompt assembly or any Codex process is started.
                raise DigestError("Codex generation is unavailable: the app cannot verify that all MCP integrations are disabled. Choose Claude; saved Codex summaries remain available.")
            env = child_environment()
            executable = resolve_cli(provider, env)
            with tempfile.TemporaryDirectory(prefix="MeetingTranscriber-digest-") as temporary:
                cwd = Path(temporary)
                code, version, _ = self._run([executable, "--version"], b"", cwd, env, timeout=20)
                version = version.decode("utf-8").strip()
                if code or not version or len(version) > 200:
                    raise DigestError("The selected CLI version could not be checked.")
                code, auth_out, _ = self._run([executable, "auth", "status"], b"", cwd, env, timeout=20)
                try:
                    status = json.loads(auth_out)
                    authorized = (code == 0 and status.get("loggedIn") is True and
                                  status.get("authMethod") == "claude.ai" and status.get("apiProvider") == "firstParty" and
                                  str(status.get("subscriptionType", "")).lower() in {"pro", "max", "team", "enterprise", "ultra"})
                except (ValueError, AttributeError):
                    authorized = False
                if not authorized:
                    raise DigestError("Sign in to the selected CLI with a subscription account outside this app. API-key generation is unavailable.")
                code, help_text, _ = self._run([executable, "--help"], b"", cwd, env, timeout=20)
                required = [b"--safe-mode", b"--restricted", b"--tools", b"--strict-mcp-config", b"--no-session-persistence", b"--permission-prompts"]
                if code or any(flag not in help_text for flag in required):
                    raise DigestError("This CLI lacks required summary controls. Update it outside this app.")
                code, output, _ = self._run(cli_command(executable), prompt(source, skill), cwd, env)
                if code:
                    raise DigestError("The CLI could not generate a summary. Check its subscription availability outside this app.")
                markdown, model, admission = parse_result(output)
                value = record(source, skill, provider, version, markdown, model,
                               provider_warning=admission['provider_warning'])
                with self._lock:
                    if self._cancel.is_set() or self._closed:
                        raise DigestError("Summary generation cancelled.")
                    saved = publish(source, value)
                    self._status.update(state="succeeded", summary_id=saved["id"], admission=admission,
                                        cli_version=version, source_sha256=source.sha256)
        except Exception as error:
            with self._lock:
                self._status.update(state="cancelled" if self._cancel.is_set() else "failed",
                                    error=str(error) if isinstance(error, DigestError) else "Summary generation failed safely. Refresh or try again when ready.")
                if isinstance(error, ActionError):
                    self._status["admission"] = error.admission

    def status(self, identifier):
        with self._lock:
            if identifier != self._status.get("id"):
                raise DigestError("This summary job has expired.")
            return dict(self._status)

    def cancel(self, identifier):
        with self._lock:
            result = self.status(identifier)
            if result["state"] == "running":
                self._cancel.set()
                result["state"] = "cancelling"
            return result

    def close(self):
        with self._lock:
            self._closed = True
            self._cancel.set()
            worker = self._thread
        if worker:
            worker.join(12)
            if worker.is_alive():
                raise DigestError("Summary cleanup has not finished.")
