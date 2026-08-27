# Development notes

## Architecture

The public interface is `bin/meeting`. It owns the interactive menu, the
canonical command surface, one-release migration errors for removed commands,
and dispatch to one owner per command:

- `bin/record-meeting` (`meeting record`) captures lossless 48 kHz mono WAV
  through FFmpeg AVFoundation into a dated meeting bundle and finalizes
  `recording.partial.wav` to `recording.wav` only after FFprobe validates it.
  Interactive terminals also render an elapsed-time and level display from
  FFmpeg `astats` metadata; the analysis filter passes audio through unchanged.
  When the menu invokes it with `MEETING_TRANSCRIBER_RECORD_RESULT_FILE`, it
  atomically writes `{"recording": ..., "outcome": "stopped"|"interrupted"}`
  after a valid container finalizes, so the menu offers transcription only
  after a normal `q` stop.
- `bin/meeting-transcribe` (`meeting transcribe`) is the complete public
  orchestration: latest-recording selection (with a TTY confirmation), external
  import into a private bundle, the pre-upload service/privacy block, invoking
  the run owner, speaker-naming offer, HTML opening, notification, and the
  single-stdout-line contract. A successful run prints the absolute
  `transcript.json` path as its only stdout line; exit 3 means the transcript
  is usable locally but remote deletion is unconfirmed.
- `bin/transcribe-meeting` is the narrow internal run owner: credential
  containment, size/duration preflight, run-directory creation, and launching
  the provider adapter. It passes the adapter's 0/3 exit codes through and
  writes the run directory to `MEETING_TRANSCRIBER_RESULT_FILE`.
- `lib/transcribe_assemblyai.py` is the standard-library REST adapter and the
  provider lifecycle owner. It streams the upload, submits exactly one
  Universal-3.5 Pro job, polls with bounded transient retries, converts the
  completion into the typed canonical transcript, publishes canonical JSON and
  both views, compacts the receipt, and then deletes the remote transcript.
  Its `--cleanup` mode is the recovery-only remote deletion used by
  `meeting cleanup`; it can only issue DELETE requests.
- `lib/transcript_bundle.py` owns the named `CanonicalTranscript` type, the
  validation boundary that narrows unknown JSON, and atomic private
  publication with reload verification.
- `lib/meeting_identity.py` derives the meeting title, recorded date, and run
  id from the bundle path for both renderers; there is no second path parser.
- `lib/render_transcript_markdown.py` and `lib/render_transcript_html.py`
  render `transcript.md` and `transcript.html` from the saved canonical JSON.
- `lib/set_speaker_names.py` (`meeting speakers`, TTY only) collects display
  names, atomically replaces only the `speaker_names` field, reloads, and
  regenerates both views.
- `lib/transcript_run.py` is the single run-classification predicate behind
  latest-run discovery in `lib/config.sh`: it validates new-schema runs
  (exactly `transcript.json`, `transcript.md`, `transcript.html`, and
  `.assemblyai.json` as regular non-symlink files, canonical JSON valid, views
  nonempty, receipt `state: "published"`) and recognizes historical runs by
  `transcript.json` plus `transcript.html`.
- `bin/meeting-open` opens the latest usable run's saved HTML without
  rewriting anything and surfaces the cleanup-required warning.
- `bin/meeting-cleanup` resolves the recovery target and contains the
  credential before invoking the adapter's cleanup mode.
- `bin/meeting-setup` and `bin/meeting-doctor` own configuration and the
  Recording/Transcription readiness report.
- `lib/format.sh` is the shared terminal formatting helper; human status goes
  to stderr on every public command. Two deliberate exemptions: help/usage text
  prints on stdout everywhere (it is the requested result of `--help`), and
  `meeting doctor` keeps its compact `✓`/`!`/`✗` readiness rows (on stderr)
  instead of label/value rows.

## Toolchain

The supported environment is macOS with:

- Homebrew `uv` and keg-only `ffmpeg@7`;
- a uv-managed pinned Python 3.13 and a small project virtual environment; and
- the Python standard library only for the installed application.

There are no third-party production runtime dependencies. The single
development-only dependency is `aiohttp==3.14.3` in the default `dev`
dependency group, used by the Chromium DevTools browser test.

- Production install (`install.sh`): `uv sync --frozen --no-dev` — the dev
  group is absent from the user environment.
- Development bootstrap: `uv sync --frozen` — the default dev group installs
  `aiohttp` for the browser gate.

`pyproject.toml` and `uv.lock` own dependency versions. Do not commit `.venv`,
recordings, transcripts, local configuration, or Keychain material.

## Provider lifecycle and credential boundary

The managed path uses `https://api.assemblyai.com`, places the raw API key only
in the adapter child's `Authorization` header, and pins
`speech_models: ["universal-3-5-pro"]`. The shell layers read
`ASSEMBLYAI_API_KEY` or the `meeting-transcriber-assemblyai-key` Keychain item,
unset the inherited value, and export it only to the provider child; speaker,
renderer, notification, and open children never see it, and it never appears in
argv or output artifacts.

Each run atomically persists the hidden mode-`0600` receipt
`.assemblyai.json` in the run directory:

- Before publication it holds the lifecycle state machine
  (`unknown_after_upload`, `upload_rejected`, `upload_succeeded`,
  `unknown_after_submit`, `submitted`, `completed`, `provider_error`,
  `publication_failed`, `poll_failed`, `interrupted`) with bounded error
  strings — never full provider responses, transcript text, upload URLs, or
  credentials.
- Publication requires canonical JSON and both views to be saved and reloaded.
  The receipt is then compacted to exactly: provider, requested/used model,
  transcript ID, `state: "published"`, terminal provider status, and the
  created/submitted/completed/published timestamps. These retained fields are
  the independent evidence for the cleanup 404 rule below.
- Remote deletion runs only after publication (or on a failure with a known
  ID). A confirmed DELETE records `deletion.confirmed: true`; a failure records
  `confirmed: false` with bounded attempt/error metadata. Exit code 3 from the
  adapter and both shell layers means "usable transcript; cleanup required".
- `meeting cleanup` is recovery-only: it validates the published receipt,
  issues bounded DELETE attempts, and performs the terminal false-to-true
  transition with re-compaction and reload. A DELETE 404 confirms cleanup only
  when the receipt independently proves the same transcript ID was returned by
  submission and reached a provider terminal state; that confirmation records
  `confirmed_via: "absent_after_delete"`. An unqualified 404 stays unconfirmed.
  Already-clean targets are an idempotent success; malformed, pre-publication,
  and historical targets exit 2 without mutation.
- A managed-model mismatch produces no canonical transcript, reconciles remote
  deletion when an ID is known, and reports a support action — never an
  automatic or recommended paid retry.

Ctrl-C uses the interruption path: the receipt records `interrupted` and a
bounded deletion attempt runs for a known ID. Polling of normal
`queued`/`processing` states continues until a terminal state or interruption;
only transient GET and DELETE retries are bounded.

## Output contract and discovery

A successful run contains exactly `transcript.json`, `transcript.md`,
`transcript.html`, and hidden `.assemblyai.json`. Bundle and run directories
are mode `0700`; recordings, artifacts, and atomic replacements are `0600`.

Canonical JSON is minimal and provider-neutral: `language`, `text`,
`segments` (id, start, end, text, speaker, timed words with probability), and
`speaker_names` (always present, `{}` at publication). Provider identity and
lifecycle evidence live only in the receipt. Transcript content and timing are
immutable after publication; the only supported mutation is the atomic
`speaker_names` replacement, which regenerates both views.

Latest-run discovery selects the greatest tuple
`(run directory basename, meeting-bundle basename, canonical JSON path)` in
`LC_ALL=C` byte order, skipping partial, malformed, symlinked, and
pre-publication candidates. A published deletion-unconfirmed run is selected
and flagged cleanup-required rather than hidden behind an older clean run.

Historical read compatibility is isolated in the discovery/open boundary: a run
without `.assemblyai.json` is selectable when `transcript.json` and
`transcript.html` exist, `meeting open` opens its saved HTML without rewriting
anything, and `meeting speakers` refuses it with an explanation. Runs from the
earlier AssemblyAI release keep their `transcript.assemblyai.json`; that old
receipt is read only to surface a deletion-unconfirmed warning with a
dashboard/support action and is never migrated or rewritten.

## Verification

The full implementation gate:

```bash
uv sync --frozen
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
test -x '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' || test -x '/Applications/Chromium.app/Contents/MacOS/Chromium'
MEETING_REQUIRE_BROWSER=1 .venv/bin/python tests/test_html_browser.py
tests/interface-smoke.sh
bash -n install.sh bin/* lib/*.sh tests/interface-smoke.sh tests/fixtures/*
.venv/bin/python -m compileall -q lib tests
shellcheck install.sh bin/* lib/*.sh tests/interface-smoke.sh tests/fixtures/*
```

- `tests/test_assemblyai_adapter.py` runs a loopback HTTP server that crosses
  the real request boundary for streamed upload framing, submission, polling,
  publication-before-delete, receipts, cleanup, and the single-action failure
  blocks — without provider traffic.
- `tests/test_meeting_cli.py` covers public orchestration, the migration-error
  tables for every removed command and flag, retained-option routing,
  discovery through `meeting open`/`meeting speakers`, and the menu's
  recorder-result contract, including the macOS Bash 3.2 and
  credential-containment regressions.
- `tests/interface-smoke.sh` exercises the real front door with a provider
  stub: four-file inventory, source immutability, paths with spaces, import
  copy, exit-3 selection, name regeneration, and the permission regression.
- The recorder PTY test requires macOS and Homebrew `ffmpeg@7`. The rendered
  HTML browser test requires the dev-group `aiohttp` and a local Google Chrome
  or Chromium at one of the two supported paths; with
  `MEETING_REQUIRE_BROWSER=1` it fails (rather than skips) when a prerequisite
  is missing, so the dedicated browser gate cannot silently pass.

Set `MEETING_NOTIFICATIONS=0` during automated verification.
`MEETING_DATA_DIR`, `MEETING_CONFIG_DIR`, the executable overrides
(`MEETING_TRANSCRIBER_ASSEMBLYAI_BIN`, `MEETING_TRANSCRIBER_FFPROBE_BIN`,
`MEETING_TRANSCRIBER_OPEN_BIN`), and the result-file channels are undocumented
test and embedding seams, not public flags.

Deterministic tests and `meeting doctor` never make a provider request. A live
AssemblyAI job is a separately approved operator action with a named input and
cost cap.

## Packaging

Source and personal data are intentionally separate. A checkout may live in any
directory while runtime data defaults to `~/Transcriptions`; each immediate
child is one dated meeting bundle with one recording and zero or more immutable
transcript runs. Development verification artifacts stay under the ignored
`tests/artifacts/`. The bundled skill at `skills/meeting/` is the distributable
source; setup installs it under `~/.codex/skills/meeting` without overwriting a
different existing copy.
