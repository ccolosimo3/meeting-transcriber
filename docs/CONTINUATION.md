# Continue from another machine

Checkpoint: November 2, 2026. This document is the portable handoff; local chat
history, ignored `plans/`, Mac artifacts and the original Windows computer are
not prerequisites for continuing from the source below.

## Source to use

Clone this repository and select **`codex/desktop-import-transcription`**.
That branch contains the reader, summaries and desktop transcription together.
Do not start from `main`, which was still the CLI-only PR #4 baseline when this
checkpoint was prepared. The current product checkpoint is
`2640f2903f4b9e4ebda97697410798bdfaf514a7`, including the Windows summary
console-window correction.

A later rendered-summary candidate is preserved separately on
`codex/rendered-summaries-windows-enb016` at
`2dda750389550e553cecd68bd59ec49fa9f37196`. Both fresh source reviews and the
Windows package checks passed; final Mac compatibility and landing remain
pending. The [rendered-summary checkpoint](handoffs/windows-2026-11-02/rendered-summaries/README.md)
preserves factual results, artifact hashes, sanitized review reports and five
original fictional captures. No new provider call was made for that change.

```text
git clone --branch codex/desktop-import-transcription https://github.com/ccolosimo3/meeting-transcriber.git
cd meeting-transcriber
```

The preserved layers are:

| Remote branch | Code checkpoint | Contents |
| --- | --- | --- |
| `main` | `76959539785b31da544eea6e6d08f217aedcf95c` | Merged macOS AssemblyAI CLI and credential/cleanup hardening |
| `codex/desktop-library-viewer` | `cea92c4d30abba215e021d49ae1d2108c1a9eb12` | Desktop reader/player and reader refinements; PR #5 |
| `codex/meeting-digests` | `8c718b2fd2cad08e8154587ad0cfdf7fc656bb0a` | Reader plus explicit Codex/Claude subscription summaries |
| `codex/desktop-import-transcription` | `2640f2903f4b9e4ebda97697410798bdfaf514a7` | All preceding work plus shared import/transcription, reviewed fixes and Windows summary console suppression |

These are a local-development stack now preserved remotely, not a claim that
all layers have merged. Preserve these commits; no rebase or force-push is
needed to continue testing. Check current PR/branch state before integrating.
Source and locked build inputs are tracked; generated application packages are
rebuilt on the target OS rather than committed to Git.

## What is implemented

- Library, audio playback/seeking, passage following, literal search/speaker
  filters, saved transcript versions, collapsible controls and light/dark UI.
- Explicit summary generation through an already authenticated Codex or Claude
  CLI subscription, immutable history and source-version provenance. No automatic
  follow-up actions. Codex MCP restrictions are per invocation; global config
  and auth stores are not changed or copied.
- Single-file local import, then explicit AssemblyAI upload/transcription with
  progress, four-file publication and remote cleanup/recovery. CLI and desktop
  share Python import/preflight/provider/cleanup owners.
- One in-memory transcription operation. Native Close/Quit waits for it to
  finish; no Cancel, queue, resume service or background daemon.
- Shared per-user `.env`: macOS `~/Library/Application Support/MeetingTranscriber/.env`;
  Windows `%LOCALAPPDATA%/MeetingTranscriber/.env`. Setup accepts a masked key.
  Existing checkout-root keys are not searched or automatically moved.
- Installed or explicitly Located FFprobe; no bundled FFmpeg/FFprobe. Transcript
  data defaults to `~/Transcriptions`, separately from credentials.

The bundle remains `recording.*` plus `transcripts/<run>/transcript.json`,
`transcript.md`, `transcript.html`, and `.assemblyai.json`. Optional summaries
live in their own existing `summaries/<generation>/summary.json` records.
The provider owner persists uncertainty before disclosure, saves local views
before remote deletion, and preserves a usable transcript when deletion fails.
An unknown outcome or model mismatch retains support-before-retry after reload.
Cleanup only targets a validated published run and never retranscribes.

See [README](../README.md) for user behavior and
[DEVELOPMENT](DEVELOPMENT.md) for architecture, build and verification commands.
Recording remains the existing macOS CLI feature. Desktop recording, streaming,
batch jobs, a full cross-platform CLI rewrite, CI and release signing are outside
this slice; streaming and release/onboarding polish remain future work.

## Review and proof checkpoint

Reader source and bounded native reader QA passed on Windows and Mac at their
recorded reader revisions. Mac summary generation was exercised with both
subscription routes at `8c718b2`. November 2 Windows synthetic qualification is
described below; its required source and scoped runtime evidence reviews are
complete. This does not qualify live Windows vendor accounts or a release.

The original Windows machine's unique reader records, synthetic proof scripts,
design snapshots and safe captures are preserved separately on
`codex/windows-handoff-20261101` at
`4c3bba1e81958df8b1bf87a48e2a7f1b817819b2`:
[Windows preservation report](https://github.com/ccolosimo3/meeting-transcriber/blob/4c3bba1e81958df8b1bf87a48e2a7f1b817819b2/docs/handoffs/windows-2026-11-01/README.md).
That machine's audit found all four worktrees clean and no unique unpushed
production commit. The archived proofs apply to older reader revisions; use the
current source branch above for later digest/import qualification. The archive
excludes credentials, personal captures, reproducible binaries and raw private
data; its historical scripts are reference material, not current test runners.

The import/transcription implementation was reviewed over `8c718b2..c390b35` by
two fresh Sol/xhigh reviewers. Both initially returned actionable findings,
then approved after the same author corrected all five. The inner closed first;
the same outer certified the complete final range. The fixes preserve selected
meeting/run identity through upload and cleanup, persist uncertainty before
upload, retain model-mismatch restrictions after reload, and restore relative
CLI output paths. Five real-boundary regression tests reproduced nine failing
cases before correction; the corrected tests and 87 affected tests passed.
Interface smoke, shell/static checks and additional unchanged focused evidence
also passed; these counts overlap and must not be added as unique test totals.

Relocated arm64 Mac/WKWebView QA used synthetic homes/libraries, fake credentials,
installed FFprobe and a localhost provider fixture. It observed import/setup,
explicit upload/progress, responsive navigation and pinned destination, saved
reload, persistent warnings, DELETE-only cleanup retry, failure preservation,
and native busy Close/Command-Q blocking followed by resource cleanup. Corrected
package proof also observed model-mismatch blocking after restart and durable
uncertainty during held upload. Final synthetic flow made two deliberate uploads
and three DELETEs; no live AssemblyAI call was part of this slice.

An initial misconfigured QA launch locally scanned the operator's real library;
no upload or library write occurred. It was excluded from proof. Accepted QA
used a positively verified persistent synthetic launch. Do not let automation
reopen an app with default environment after its intended process exits.

Full raw Mac evidence, builds and private planning remain on the Mac in ignored
task artifacts; they are not needed to reconstruct the source/build. For forensic
reference, the reviewed Mac executable SHA-256 was
`ec6c206817329aa728baa0283ffe72f26d340445dd61a1fabbda8ce0ebfa1c63`, and its
42-file manifest SHA-256 was
`fec8da841b0def3493e9c0adee6cb777bb8272fb83ecd9838219da47b256e11e`.
A new machine's rebuild need not have identical hashes. No whole-delivery
`review-delivery` gate is claimed beyond the stated implementation reviews.

## Windows synthetic proof checkpoint

Windows 11 x64 qualification used Python 3.13.15, uv 0.12.5, WebView2
156.0.4314.50 and relocated native packages built with frozen dependencies.
The Windows author observed the following results using only owned synthetic
homes, libraries, credentials and executable/provider fixtures:

- [WIN-QUAL-1 at `1dd42c7`](https://github.com/ccolosimo3/meeting-transcriber/blob/1dd42c7176038b1f3ff75ed5eeab6006639b3ea4/docs/handoffs/windows-2026-11-02/win-qual-1/README.md):
  private-file/DACL protection, fail-closed unsafe writes, zero-byte temporary
  checks, atomic replacement and private publication/reload passed at the
  unchanged `c390b35` product. The corrected oracle independently verified the
  current owner before accepting Windows OWNER RIGHTS.
- [WIN-QUAL-2 at `93a1206`](https://github.com/ccolosimo3/meeting-transcriber/blob/93a1206fbc5783e9479270708fc53a03dd11a1d6/docs/handoffs/windows-2026-11-02/win-qual-2/README.md):
  native pickers/setup/import, explicit localhost transcription, responsive
  navigation and pinned destination, busy Close/Alt-F4 blocking, restart
  warnings/restrictions, DELETE-only recovery, preservation and normal-shutdown
  cleanup passed on the relocated package. External FFprobe 9.0.2 was task-local;
  no dependency or global configuration change was needed in this stage.
- [Pre-fix WIN-QUAL-3 at `8c97149`](https://github.com/ccolosimo3/meeting-transcriber/blob/8c97149ffaed3e473b5172bd81043dc6cd32d259/docs/handoffs/windows-2026-11-02/win-qual-3/README.md)
  retains passing Windows Job membership/admission, headless cancellation and
  timeout, CLI discovery/control refusal, bundled-skill and synthetic summary
  publication/reload observations. It also preserves the console intrusion and
  the native descendant-cancellation timing gap; it is not a passing whole report.

The same import/transcription source outer approved WIN-QUAL-1 and WIN-QUAL-2
in evidence-only mode at the linked revisions. These assessments inspected
supplied Windows artifacts on Mac; they did not rerun the Windows binaries.

The correction at `2640f29` adds `CREATE_NO_WINDOW` to the actual Windows
provider launch in `desktop/digest_child.py`, retaining the existing Job and pipe
owners. Its fresh inner and original outer source reviews both approved.
No further Windows product edit was made. Windows-specific corrections and their
native verification are owned by the Windows agent; the Mac lead coordinates
reviews and integration.

The [corrected WIN-QUAL-3 report](handoffs/windows-2026-11-02/win-qual-3-corrected/README.md)
records author-observed native PASS for console suppression, Cancel and normal
app Close during a fresh active summary. Exact birth-bound handles proved the
provider, child and grandchild live immediately before each action and terminated
afterward, with hold mode unchanged. All four prior summary records, source/media
and synthetic config hashes stayed unchanged. The owned app/WebView process
tree, listener and private session were gone after Close. The corrected executable
SHA-256 is `d5100f870006597c9698a4f92b50fb7b8054a8e7eb71d20cde13a2eba18191db`;
its original 102-file manifest SHA-256 is
`686b95151906c2b8165efbb99444b84907f77aef64590cabb321bb8998271918`.
Earlier passing evidence is reused where this narrow source delta cannot affect
it. The same original digest outer subsequently approved the corrected runtime
evidence at `3c0e4e0654c6d40930b4d0f1a97e7fe9d403d77f`, against unchanged
source `2640f29`, with no findings. It independently assessed the exported
artifacts and captures on Mac; it did not execute Windows or make provider calls.

Actual OS Job-assignment rejection was not forced. The prior launch-PATH
transformation origin remains unresolved; per-job environment consistency and
synthetic auth/config isolation were verified without claiming launch-PATH
equality. Those synthetic reports excluded real provider/CLI authentication,
model calls and live AssemblyAI; the separately selected live checkpoint below
records those later observations. Release signing and installer qualification
remain unselected. These reports make no full-release or whole-delivery
certification claim.

## Windows live smoke checkpoint

The [November 2 live report](handoffs/windows-2026-11-02/live-smoke/README.md)
records one operator-approved 91.297415-second fictional recording through the
same tested package, with product bytes unchanged at `2640f29`. One native
AssemblyAI transcription published all four files with two-speaker diarization,
readable punctuation and confirmed remote deletion. Native playback/seeking
worked. One subscription summary each through actual Codex 0.163.0 and Claude
Code 2.1.280 saved with matching transcript/skill provenance; both distinguish
committed actions from the unapproved gallery. Codex retained a nonfatal provider
warning; Claude reported `claude-opus-5-5[1m]`, while Codex reported no model ID.
No live retry, retranscription or extra generation was made.

Normal restart restored the transcript, both summary history entries and the
Codex warning without changing saved bytes. Final normal Close removed owned
app/WebView/CLI processes, listener and private session. The report includes only
the approved fictional transcript/summaries, redacted results and two original
safe app captures. The AssemblyAI approval cap was $0.10, with an approximate
input-duration estimate of $0.00584; actual billing was not queried. This dated
live sample is separate from the synthetic failure/lifecycle proof and makes no
broader quality or release claim. Its approval was limited to that input and run.

## Windows qualification route

Use a Windows x64 machine with Python 3.13/uv, WebView2 Evergreen and installed
FFprobe. Follow the pinned environment and native build route already documented:

```text
uv sync --frozen --extra desktop --group desktop-build
uv run --frozen --extra desktop --group desktop-build python -m desktop.build
```

The build produces a native folder package under `.local-artifacts/desktop-build/dist`.
Move/copy it out of the source checkout for packaged QA. Begin with the existing
desktop/native tests in DEVELOPMENT and their documented native-runtime switch.
Some CLI/process test fixtures use macOS/Bash/POSIX executables: inspect them
before choosing a Windows command set. Do not treat a fixture platform failure
as app failure, or a skipped case as native proof.

The target-platform qualification covers:

1. Real Windows owner/DACL protection for credentials, copied audio, receipts,
   summaries, temporary files and atomic replacements; permissive inherited ACLs
   must not expose sensitive bytes. POSIX mode bits are not Windows proof.
2. Relocated WebView2 package: native file/tool pickers, key Save/readiness,
   import, explicit fixture transcription, responsive navigation, busy Close/Quit
   guard, restart discovery/warnings and qualified DELETE-only cleanup recovery.
3. Summary subprocess ownership: cancel/timeout/window close and surviving
   descendants through the Windows Job Object, plus bundled skill and native CLI
   discovery. Real model calls are separate from executable-fixture checks.
4. Resource cleanup: owned processes, listener and private webview session gone
   after normal shutdown. Preserve source audio, earlier successful runs and
   unrelated fixture/library entries.

Use fresh owned synthetic data/config before the first scan. For desktop
transcription QA, scope `MEETING_DATA_DIR`, `MEETING_CONFIG_DIR`, `HOME` and
Windows `LOCALAPPDATA` to that fixture **in the launched process environment**;
leave the operator's shell/global configuration alone. Clear inherited provider
keys, use a fake `.env`, and pin `MEETING_TRANSCRIBER_ASSEMBLYAI_API_BASE` to the
local fixture before pressing Transcribe. Existing HTTP scenarios are in
`tests/test_assemblyai_adapter.py`; shared Bridge/identity/recovery cases are in
`tests/test_transcription.py` and `tests/test_transcription_boundaries.py`.
Confirm the intended process and synthetic library before UI automation attaches.

Summary fixture tests and real subscription tests have different auth needs;
do not copy real CLI credentials into fixture homes. No live model or paid
AssemblyAI smoke is authorized by this handoff. The operator must select any
real call and, for metered/API usage, its input and spend bound. Keep credentials,
recordings, personal transcripts, raw private plans and machine paths out of Git.
Record the actual Windows revision, commands/results, native observations and
remaining gaps in a sanitized follow-up report. Preserve passing evidence and
return source corrections through their applicable reviews before publication.
