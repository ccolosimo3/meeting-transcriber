# WIN-QUAL-1 sanitized Windows evidence

This export preserves the local operator qualification evidence for the private-file boundary. It is not a new test suite or independent review verdict. Product source is unchanged at `de3226afe30ad8f26257a470df9389d12f13a6ce`; the relevant source, tests, manifests and lockfile match reviewed `c390b35a931bac8bfc4e5f0db73e66e1a0e3dccb`.

Personal account SID/path values have been replaced with semantic labels. `owner_matches_current_user` preserves the independently observed equality. SDDL uses the same labels for personal SID values; well-known SDDL abbreviations remain Windows notation. Permissive EVERYONE/USERS/AUTHENTICATED_USERS trustees occur only on deliberately unsafe fixture paths or rejected observations. Local raw evidence remains unchanged.

The supported-runtime first pass has six passing behavior groups and an overall harness failure at the summary oracle, which omitted OWNER RIGHTS. `summary-results.json` records the narrowed successful continuation after grounding that SID in Microsoft documentation and separately checking the actual owner. No product fix was made, and preceding green checks were retained. Together, the two ACL files contain **12 zero-byte temp/partial observations**, including the rejected unsafe temporary. Earlier Python3.12 evidence is excluded.

Selected artifacts:

- [Initial supported-runtime result](private-results.json) and [ACL observations](acl-observations.json).
- [Summary continuation result](summary-results.json) and [ACL observations](summary-acl-observations.json).
- [Source/proof hashes](source-and-proof-hashes.json) and [sanitized setup recovery](setup-recovery.json).
- Archived proof references: [private qualification](private_qualification.py.txt), [summary continuation](summary_qualification.py.txt), [OS ACL observer](acl_snapshot.ps1.txt). These are reference text, not installed runners; their original ignored-artifact path layout is retained in source.

Source hashes identify original local proof bytes and product files. Each original has LF internal lines and exactly one final CRLF added by PowerShell Set-Content. Git replaced only that final CRLF with LF. For all three proof files, current raw bytes still match the originally recorded hash, and `archived_bytes == original_bytes[:-2] + b"\n"` is true. There is no BOM, transcoding, sanitization or code edit in these references. `archive_transformation` records byte lengths/counts and exact equality; `archived_git_blob_sha256` binds tracked bytes separately. The report below is sanitized, with original artifact names retained as historical references. The selected export files above are the remote evidence owner.

## Local qualification report (sanitized)

WIN-QUAL-1 Windows private-file qualification â€” PASS (first boundary only)

Candidate: de3226afe30ad8f26257a470df9389d12f13a6ce
Checkout: current task's clean clone; branch qual/windows-private-files-20261102-enb016.
Authoring/qualification invocation: Codex GPT-6.1 Sol / xhigh. No helpers or source edits.
Relevant product source, tests, configuration and locked dependencies are identical to reviewed c390b35a931bac8bfc4e5f0db73e66e1a0e3dccb (git diff --exit-code c390b35 HEAD -- lib desktop pyproject.toml uv.lock tests: exit 0). Remote source matched at clone; source review verdicts themselves remain the Mac lead's supplied claims/records.

Readiness: Windows x64 (AMD64); task-local uv 0.12.5 from official PyPI win_amd64 wheel; managed CPython 3.13.15. Frozen sync installed pywebview 6.2.1, Bottle 0.13.4 and PyInstaller 6.22.3 with unchanged pyproject/lock. WebView2 Runtime 156.0.4314.50 is registered and its installed binary exists; native engine not launched. FFprobe absent from PATH and inspected installed locations; not needed at this boundary, remains a later prerequisite.

Setup commands (PowerShell, no activation):
  python -m venv .local-artifacts/win-qual-tooling/bootstrap
  <bootstrap-python> -m pip --isolated install --index-url https://pypi.org/simple --only-binary=:all: --no-deps uv==0.12.5
  <absolute-uv> python install 3.13.15
  <absolute-uv> sync --frozen --python 3.13.15 --extra desktop --group desktop-build
UV_CACHE_DIR and UV_PYTHON_INSTALL_DIR were task-local, UV_PYTHON_PREFERENCE=only-managed in the child PowerShell environment. Frozen sync built the existing locked pure-Python proxy-tools package; no Rust/global toolchain or strategy change.

Setup deviation and recovery: initial `uv python install` omitted --no-bin --no-registry. It created a user python3.13 launcher and HKCU Software/Python/Astral/CPython3.13.15 registration. Both were inspected and proven to point to this exact task-owned runtime, then only that launcher, exact registry leaf/InstallPath and empty Astral container were removed. py --list-paths returned to the original Python3.12-only registration. Managed runtime and .venv remain task-local. Future install must include --no-bin --no-registry. No PATH/profile/admin/policy edits were made.

Qualifying commands and results:
  .venv/Scripts/python.exe tests/artifacts/win-qual-1/private_qualification.py fixture-qualified-313
    Six preceding behavior groups passed. Initial overall exit 1 was a disposable ACL-oracle false failure at summary parent: oracle omitted OWNER RIGHTS OWNER_RIGHTS, while production accepted it with actual owner equal to current user. No product defect or edit. Raw initial output and results retained unchanged.
  .venv/Scripts/python.exe tests/artifacts/win-qual-1/summary_qualification.py
    Exit 0, resumed only the summary boundary against the same synthetic supported-runtime tree, correcting the independently grounded oracle. Existing green behavior was not rerun.
  git diff --check: exit 0; git status: clean; product-source comparison above: exit 0.

Observed at real Windows filesystem boundary:
- Production private_mkdir and Directory create protected owner/SYSTEM/Administrators directories beneath a deliberately permissive parent with Everyone read inheritance.
- Direct atomic write under permissive inheritance rejects the unsafe temp while still zero bytes, removes it, and preserves original destination bytes.
- atomic_write_private creates/replaces files with actual private ACL, distinct file identity after replacement, byte reload match.
- store_key/read_key save, read and replace only synthetic credentials. Adding Everyone read to the task's fake credential causes both read and attempted replacement to fail closed with original fake bytes unchanged.
- Production import_recording verifies its zero-byte partial before copying, saves private audio, and preserves synthetic source hash.
- Production save_canonical, atomic_write_json, Markdown/HTML publishers save/reload canonical, receipt and both views; receipt atomic replacement works; Library reload sees canonical text and unconfirmed-cleanup status.
- Production Library.publish_digest saves a validated record with zero-byte private temp, private final artifact, and reloads through summary_history. No CLI process or model call used.

PowerShell Get-Acl independently inspected actual owner SID, SDDL, allow trustees and protected inheritance. The live observer called the unchanged production verifier and inspected temp metadata before returning control to its writer; sensitive fixture bytes had not been written at that point. There are 12 zero-byte temporary/partial observations across the supported-runtime first pass and summary continuation, including the deliberately rejected unsafe temp. Owner must always match whoami /user. Allowed trustees are that owner, SYSTEM, Administrators and (for Python3.13-created directories/files) OWNER RIGHTS; no Everyone/Users/Authenticated Users allows on accepted private paths. This is ACL inspection, not a second-account access attempt or a broad race/security audit.

OWNER RIGHTS rationale: Microsoft documents OWNER_RIGHTS as representing the object's current owner: https://learn.microsoft.com/en-us/windows-server/identity/ad-ds/manage/understand-security-identifiers . Python documents Windows mkdir0700 access control: https://docs.python.org/3.13/library/os.html#os.mkdir . The oracle verifies actual owner independently before allowing this SID. Earlier Python3.12 observations were provisional and are not qualification evidence; its first expanded script also called the wrong HTML API, corrected only in disposable proof.

Evidence owners (all ignored/task-local):
- qualified-313-output.txt, fixture-qualified-313/results.json and acl-observations.jsonl: first supported-runtime observations and explicit oracle error.
- summary-313-output.txt, fixture-qualified-313/summary-results.json and summary-acl-observations.json: narrowed completion.
- private_qualification.py, summary_qualification.py, acl_snapshot.ps1: disposable proof source.
- source-and-proof-hashes.json: binding to source/proof bytes.

Remaining deliberately unselected checks: actual relocated package/WebView2 navigation, pickers/key setup, FFprobe preflight, loopback transcription/busy Close/Quit/restart/DELETE-only cleanup, summary Job Object descendants/cancel/timeout/close, native CLI discovery and bundled skill, owned listener/session/process cleanup. No claim on those boundaries. No personal recording/transcript/key/auth cache read, live provider/model call, GUI launch, product/dependency change, publication, commit/push or new source review was performed. Only fresh synthetic trees were used; process HOME/USERPROFILE/LOCALAPPDATA/MEETING_DATA_DIR/MEETING_CONFIG_DIR were set before product imports/scan, inherited API keys/MEETING_TRANSCRIBER_* overrides cleared. Durable documentation impact: none; this ignored evidence report is the lead's reconciliation input. Past source reviewers remain with Mac lead; no independent certification is claimed here.

Post-return bounded addendum (no product launch or further registry/shell changes):
- Native Windows UI automation is available: @oai/sky initialized successfully; sky.list_windows returned 5 targetable windows and no LockApp entry. No window activated, screenshot captured, app launched or UI input sent. Capture/input/native transcriber behavior remains unexercised until packaged-stage assignment.
- setup-recovery.json retains sanitized before/after task-owned launcher/registration identity and removal observations. Original PythonCore3.12 registration remains present; added launcher and exact3.13 registration absent; managed interpreter still task-local. User and machine PATH contain no task checkout reference. The existing PowerShell profile last-write time is 2026-05-13, predating this task, with its current SHA256 retained. No pre-setup PATH/profile byte snapshot exists, so exact before/after equality cannot be claimed; executed command history and current metadata support that no PATH/profile edit occurred. No additional registry/shell settings changes were made.
- Final narrow reload observation asserted 'Transcript ready; remote cleanup unconfirmed'; aggregate supported-runtime temporary/partial count rechecked as 12, all zero bytes at observation.
