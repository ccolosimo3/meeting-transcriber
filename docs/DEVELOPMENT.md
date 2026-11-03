# Development notes

## Architecture

### Desktop reader

`python -m desktop.main` launches the optional Windows/Mac reader.
It clears an inherited `ASSEMBLYAI_API_KEY` before importing native rendering,
and exposes library selection/refresh, run loading, saved-HTML opening and the
explicit summary job/history actions.
On Mac, an entry process that inherited the key re-executes with the sanitized
environment so OS process inspection cannot retain the original key copy.
`lib/transcription.py` owns local import, captured destination/source identity,
FFprobe/size/duration preflight, exclusive run allocation and qualified cleanup.
The Bash presentation and desktop both call these operations. The existing HTTP
adapter remains the provider state machine and emits phase callbacks. Only
explicit Transcribe/cleanup work reads credentials for provider delivery.
`desktop/transcription_job.py` owns one in-memory non-cancellable background
operation; its result retains the captured Library. Normal close/Quit uses the
same pywebview closing event and is vetoed while that operation runs. Summary
subprocess ownership stays separate. Unresolved failed receipts suppress a blind
desktop retranscription; published cleanup warnings survive Library reload.

`lib/private_files.py` contains the shared Windows owner/DACL primitives and
private atomic writer. New task directories get protected ACLs at creation;
private temporary files are verified before bytes are written and replacements
are verified again. A thread-local publication guard checks captured destination
identities during shared runs. Windows execution must independently qualify this
source; POSIX tests are not an ACL proof.

- `lib/meeting_library.py` projects existing bundles through the shared
  classifier, canonical parser and identity owner. IDs belong to the current
  library session. It rejects linked/reparse descendants and verifies actual
  opened-file paths and identities before reading. Media and saved HTML leave
  this owner as verified binary handles, never arbitrary renderer paths.
- `desktop/server.py` serves an explicit asset allowlist and capability-bound
  handles on one ephemeral loopback listener. Media supports ranges without
  loading whole recordings; no HTTP endpoint lists or reads arbitrary files.
  Saved HTML opens through a temporary handle-backed URL in the system browser,
  which has no Python bridge. URLs and document content are never logged.
- `desktop/native.py` adapts the pinned pywebview renderers before their first
  app navigation. It blocks other navigation, popups, drops and downloads,
  protects session storage, and rejects missing WebView2 rather than allowing
  MSHTML. Windows storage retains required engine AppContainer access within
  the protected per-user session. Mac uses a nonpersistent WK data store set
  before webview construction. The browser sandbox remains enabled.
  On Mac, application Quit uses the same guard and ordinary window teardown
  so Python returns through server/session cleanup. Pending JavaScript result
  waits end when the native window closes,
  so late bridge replies cannot keep the process alive.
  Native messages also enforce an exact action/arity/input allowlist before the
  framework dispatcher; hiding private Python attributes from API enumeration
  alone does not prevent raw dotted-attribute calls.
- `Bridge.load_run` derives a transient `reader_transcript` only after the full
  Library load and validation. It contains language, speaker names and ordered
  passages with start/end/text/speaker, or null for unavailable structured data.
  It replaces `canonical` only on this private desktop response: Library data,
  files, exports and summary source inputs retain complete canonical data.
- `desktop/assets/` renders escaped passage text with source-ordinal identity.
  The actual media element drives interval highlighting; filtering changes the
  text view, not the audio. Library/Find/Details/Listening and summary-generation
  disclosure is local presentation state and never replaces the media element.
  Show categories derive from the library snapshot's eligible runs and exact
  statuses; selected version labels and warnings derive from the current run. Summary Markdown uses
  a local token parser and a closed text-node/DOM renderer; it cannot render raw
  HTML, remote resources or generated actions.

  Completed-run retranscription and its paid-use disclosure live in More;
  audio-only meetings keep the same action inline. The existing button/handler
  moves between hosts. Summary generation is a compact anchored form using the
  existing panel/focus/Escape owner, with outside-pointer/focus dismissal and
  viewport-bounded scrolling; opening it does not invoke a job. Job status,
  Cancel, results, errors, revision warnings and cleanup recovery remain outside
  these optional controls. Summary history uses local date labels with collision
  disambiguation; exact timestamp/hash/model provenance stays in Details.

The shipped CSP prohibits inline/remote scripts. On Mac only, `unsafe-eval` is
needed for pinned pywebview's `new Function` bridge registration; document,
frame/origin, action allowlist and escaped-text controls remain enforced.

#### Summary formatting

`desktop/assets/summary-markdown.js` owns presentation only. Initial load,
successful-result projection and history selection converge at `renderSummaries`
in `app.js`; stored Markdown, source hashes and provenance are unchanged. The
renderer consumes parser tokens and builds a detached fragment from a closed
element set. It never uses an HTML sink or copies arbitrary token attributes.
Only fixed app table semantics/classes and a validated ordered-list start are
set. HTML, link, image, autolink and reference rules are disabled, with `html`,
`linkify` and `typographer` off. URLs and reference definitions remain visible
source text. Fence information adds no execution, highlighting or attributes.

Before parsing, UTF-8 input is limited to the existing 128 KiB saved-record
contract. Normalized physical-line count times (largest literal pipe count per
line + 1) must be at most 4,096 when any pipes occur. This conservative admission
bounds table padding before token allocation; it intentionally overcounts code
and escaped pipes. After parsing, a finite traversal admits at most 10,000 flat
and inline-child tokens and rejects any level at or above 63 with `maxNesting:64`.
The latter also catches the parser's deep-block tail omission. No partial tree
is attached. Any limit, missing parser, error or unsupported structure leaves the
entire original string in `textContent`/`pre-wrap`, with a separate formatting
notice alongside existing warnings. A later successful render clears that notice.
These bounds are not a universal parser-complexity or timing guarantee.

The exact local scripts are allowlisted by `ReaderServer`; the vendor directory
is not a public directory root. CSP/native restrictions and the packaging route
are unchanged. Scoped typography uses existing theme tokens, with local table
and code overflow. No worker, cache, service or asynchronous rendering state is
introduced.

Vendored dependency: official [markdown-it 15.0.3 registry metadata](https://registry.npmjs.org/markdown-it/15.0.3)
and [tarball](https://registry.npmjs.org/markdown-it/-/markdown-it-15.0.3.tgz),
member `package/dist/browser/markdown-it.umd.min.js`, 100,786 bytes, SHA-256
`391f2aa638e75e6c7859d0a5c5650ef0956722a45998f17e05b83bc655778e1e`.
The archive integrity is
`sha512-+Az7Ojz3iRGu8KYMvJ6xVhvrxP3eE+vEtJX75Ow88XBpcQNV/0lGX5ZynIIBftREo4DC2jsveAMDaeltmm8QvQ==`.
The upstream browser source map and [versioned lockfile](https://raw.githubusercontent.com/markdown-it/markdown-it/15.0.3/package-lock.json)
identify the five embedded packages. `desktop/assets/vendor/markdown-it.LICENSES.txt`
preserves the original markdown-it MIT notice plus entities 8.1.0 (BSD-2-Clause),
linkify-it 6.1.0, mdurl 2.1.0, punycode.js 2.3.1 and uc.micro 3.0.0 (MIT) notices
from their exact official registry archives. The combined notice SHA-256 is
`0b2ece9c2df0b3ae40b46d17811bc57caca9c1803200b1dc8cd2cde214dc06c9`.
Scoped Git attributes prevent checkout line-ending translation of both vendor
files, preserving these exact bytes on Windows and Mac.
No package manager, dependency resolution, source map or upstream build is part
of app setup/build; both scripts and all notices ship through the existing assets
copy. Node is used only for development syntax checks, not by the app/build.

Summary generation is a separate deliberate disclosure, never a transcription
callback. `desktop/digest_job.py` owns one in-memory job and invokes a native,
already signed-in first-party subscription CLI with a sanitized environment,
neutral private working directory, stdin input, bounded independent pipe drains
and restrictive tool/hook/integration controls. Summary generation reads no
AssemblyAI credential. Claude uses safe/restricted mode, no tools, strict empty
MCP, denied
prompts and no session persistence. Its structured output stream is admitted only
after validation; unexpected actions, provider
errors, malformed output and incomplete results cannot publish a summary.
Claude admission requires its initialization event with an explicit empty tool
list before its successful final result.
Codex uses ordinary configuration consistently for `mcp list`, subscription auth
status and `exec`; it does not use exec-only `--ignore-user-config`, which would
change auth/cloud bootstrap. One shared set of per-run controls selects the
built-in first-party provider and official ChatGPT routing, disables action/
hook/app/plugin features and web search, clears legacy `notify`, disables OTEL
log/prompt/trace/metrics exporters and analytics, and selects read-only execution
with no approvals or transcript history. Auth-store/workspace selection remains
CLI-owned. The built-in OpenAI provider is not replaced by a user-defined provider
of the same name; the per-run empty `openai_base_url` restores its auth-dependent
default. See the matching [provider implementation](https://github.com/openai/codex/blob/rust-v0.164.0/codex-rs/model-provider-info/src/lib.rs)
and [configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference).

Before prompt assembly, the job lists configured MCP names, adds one
`-c mcp_servers.NAME.enabled=false` override per name, then rechecks the exact
name set and explicit disabled states. Invalid/duplicate/unsafe names, unknown
states, changed inventories and failed controls stop before source submission.
Only validated names enter arguments; transport/config values are never copied.
Both checks and generation share the captured environment, neutral private cwd,
auth selection and common controls. Nothing edits global config or creates a
profile/auth copy. Listing is metadata discovery and can perform ordinary OAuth
discovery/auth refresh; it does not initialize MCP tools. See [MCP controls](https://learn.chatgpt.com/docs/extend/mcp)
and [configuration layers](https://learn.chatgpt.com/docs/config-file/config-basic).
CLI/environment configuration must remain stable during the owned job; this is
not isolation from trusted administrative changes. Structured Codex output still
rejects actions/fatal errors/incomplete turns; documented nonfatal item warnings
retain only a generic indicator. The reader keeps Claude as its initial choice
and permits an explicit Codex choice, with no automatic fallback.
The app does not claim isolation from trusted admin policy or ordinary CLI
authentication/cache behavior. Raw prompts/events/stderr never become app logs.

`lib/meeting_digests.py` owns the small app-serialized record and consumes the
bundled repository skill plus a quoted transcript snapshot. `Library` captures
the exact selected canonical bytes through verified handles and publishes only
under that captured meeting's private `summaries/<UTC>-<random>/summary.json`.
Content and app-owned provenance share one atomic record, followed by reload.
History derives from records for the existing selected transcript run; there is
no index, latest pointer, background queue or durable recovery service. Source
hashes expose a later speaker-name/content revision without rewriting history.

Cancellation/timeout/window-close owns the entire subprocess tree. POSIX uses a
new session/process group; Windows assigns a trusted waiting bootstrap to a
kill-on-close Job Object before giving it the provider launch header. The app
retains the Job handle after the root exits so surviving children remain owned.
Windows private publication checks actual owner/DACL rather than treating POSIX
mode bits as a Windows privacy proof. Target-OS process/privacy and native package
tests remain necessary; a Mac run cannot qualify Windows behavior.

The existing embedding/test `MEETING_DATA_DIR` seam also selects the desktop
library before its first scan. Use a synthetic data root with normal CLI auth
for native tests; never copy credentials into a synthetic HOME. Deterministic
digest tests use real executable fixtures, not provider requests. Live synthetic
subscription checks require the bounded operator authorization for that run.

Install/build separately from the CLI-only route:

```text
uv sync --frozen --extra desktop --group desktop-build
uv run --frozen --extra desktop python -m desktop.main
uv run --frozen --extra desktop --group desktop-build python -m desktop.build
```

Builds must run on the target OS. Folder packages and the Apple Silicon `.app`
land under `.local-artifacts/desktop-build/dist`; they are local unsigned/ad hoc
builds, not public installers. Library choice and theme are session-only.

Focused desktop checks:

```text
uv run --frozen --extra desktop python -m unittest tests.test_meeting_library tests.test_meeting_digests tests.test_desktop_server tests.test_desktop_bridge tests.test_desktop_dispatch tests.test_desktop_native
```

Set `MEETING_REQUIRE_NATIVE=1` for the actual Windows native-engine navigation
probe in `tests.test_desktop_native`. Its missing-runtime test forces only the
runtime-availability decision and never uninstalls the host WebView2 runtime.
Packaged launch, native picker/permissions and integrated playback still need
the Windows/WebView2 and Mac/WKWebView operation boundary; an ordinary browser
test cannot establish both. Use synthetic libraries and a sentinel key, and
retain the tested revision and captures outside product commits.

For reader layout changes, also interact with the actual served assets in the
native reader using synthetic bundles. Check wide/light and narrow/dark layouts,
Library search/Show and folder actions, Find → close → filter chips/clear,
Details → select a saved version → close, and live playback while collapsing
Library and Listening. Exercise Escape/focus return, warnings, duplicate speaker
display names, source passage numbers, no matches, missing audio and saved HTML.
These native UI checks complement the focused Python checks above.

### Existing CLI

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
- `bin/transcribe-meeting` is a thin private shell entry to the shared Python
  preflight/run operation through `lib/transcription_cli.py`. It passes the
  adapter's 0/3 exit codes through and
  writes the run directory to `MEETING_TRANSCRIBER_RESULT_FILE`.
- `lib/transcribe_assemblyai.py` is the standard-library REST adapter and the
  provider lifecycle owner. It streams the upload, submits exactly one
  Universal-3.5 Pro job, polls with bounded transient retries, converts the
  completion into the typed canonical transcript, publishes canonical JSON and
  both views, compacts the receipt, and then deletes the remote transcript.
  Its cleanup operation is shared by `meeting cleanup` and the desktop; it can
  only issue DELETE requests.
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
  credential before invoking the shared qualified-cleanup operation. Cleanup
  target proof
  reuses `lib/transcript_run.py`'s current-schema run validator before the
  adapter can issue DELETE.
- `bin/meeting-setup` and `bin/meeting-doctor` own configuration and the
  Recording/Transcription readiness report.
- `lib/format.sh` is the shared terminal formatting helper; human status goes
  to stderr on every public command. Deliberate exemptions: help/usage text
  prints on stdout everywhere (it is the requested result of `--help`), and
  `meeting doctor` and `meeting setup` keep their compact step-report styles
  (`✓`/`!`/`✗` readiness rows and the stepwise configuration report, both on
  stderr) instead of label/value rows.

## Toolchain

The supported environment is macOS with:

- Homebrew `uv` and keg-only `ffmpeg@7`;
- a uv-managed pinned Python 3.13 and a small project virtual environment; and
- the Python standard library only for the installed CLI.

The CLI has no third-party production runtime dependencies. The optional
desktop extra adds pinned pywebview/Bottle and their native renderer bindings;
the separate desktop-build group pins PyInstaller. The single CLI
development-only dependency is `aiohttp==3.14.3` in the default `dev`
dependency group, used by the Chromium DevTools browser test.

- Production install (`install.sh`): `uv sync --frozen --no-dev` — the dev
  group is absent from the user environment.
- Development bootstrap: `uv sync --frozen` — the default dev group installs
  `aiohttp` for the browser gate.

`pyproject.toml` and `uv.lock` own dependency versions. Do not commit `.venv`,
recordings, transcripts or local configuration.

## Provider lifecycle and credential boundary

The managed path uses `https://api.assemblyai.com`, places the raw API key only
in the adapter's `Authorization` header, and pins
`speech_models: ["universal-3-5-pro"]`. The shared per-user `.env` is the only
persistent credential store (`~/Library/Application Support/MeetingTranscriber/.env`
on Mac; `%LOCALAPPDATA%/MeetingTranscriber/.env` on Windows) and must contain
exactly one literal
`ASSEMBLYAI_API_KEY=<value>` assignment in a regular non-symlink mode-`0600`
file. The shared `transcription_config.py` parser treats it as data; shell wrappers
never source or export the file. Existing users manually move the one-assignment
checkout file with private permissions; no old-file fallback or migration runs. A
process-level override has priority. Every executable captures and unsets an
inherited value immediately, and only the shared provider operation receives
the selected key. Recorder, FFmpeg/FFprobe, status, speaker, renderer, browser/Finder,
doctor/setup, notification, and menu-helper children never inherit it. The
no-argument menu retains its captured override only as a private shell variable
until a later transcription selection. The key never appears in argv or output
artifacts.

Each run atomically persists the hidden mode-`0600` receipt
`.assemblyai.json` in the run directory. The existing `unknown_after_upload`
uncertainty is saved before the first upload byte; failure to save it prevents
upload, and a crash or later receipt-write failure retains the support-before-retry
warning on desktop reload:

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
- `meeting cleanup` is recovery-only: it validates the regular non-symlink
  canonical transcript and adjacent receipt through the current-schema run
  validator, including the complete set of managed run files (extra unrelated
  files are tolerated), canonical schema, provider, model/status/lifecycle
  evidence, transcript ID, and deletion state,
  issues bounded DELETE attempts, and performs the terminal false-to-true
  transition with re-compaction and reload. A DELETE 404 confirms cleanup only
  when the receipt independently proves the same transcript ID was returned by
  submission and reached a provider terminal state; that confirmation records
  `confirmed_via: "absent_after_delete"`. An unqualified 404 stays unconfirmed.
  Already-clean targets are an idempotent success; malformed, pre-publication,
  and historical targets exit 2 without mutation.
- A managed-model mismatch produces no canonical transcript, reconciles remote
  deletion when an ID is known, and reports a support action — never an
  automatic or recommended paid retry. Persisted `used_model` evidence keeps
  that support requirement after desktop reload, even when deletion succeeded.

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
  HTML and desktop summary browser tests require the dev-group `aiohttp` and
  an installed supported Chrome/Chromium on Mac, or Chrome/Edge on Windows; with
  `MEETING_REQUIRE_BROWSER=1` it fails (rather than skips) when a prerequisite
  is missing, so the dedicated browser gate cannot silently pass.

For summary presentation changes, the focused gate is:

```bash
MEETING_REQUIRE_BROWSER=1 .venv/bin/python -m unittest tests.test_desktop_summary_browser -v
.venv/bin/python -m unittest tests.test_desktop_server tests.test_meeting_digests tests.test_desktop_bridge -q
node --check desktop/assets/app.js
node --check desktop/assets/summary-markdown.js
node --check desktop/assets/vendor/markdown-it.umd.min.js
git diff --check
```

On Windows PowerShell, set `$env:MEETING_REQUIRE_BROWSER='1'` for the browser
command and use `.venv\Scripts\python.exe`; restore the setting afterward.
The existing transcription Bridge case also needs installed FFprobe; when it is
outside PATH, select that existing executable with the documented test override.
The browser fixture creates the ordinary `AppData/Local` and `AppData/Roaming`
structure inside its synthetic USERPROFILE so Windows known-folder resolution
can establish that its explicit Chromium profile is non-default. No browser
profile safety check is disabled. The three reader behavior families cover real
Bridge/Library projections, generated-content request/action inertness, and
full-source fallback/recovery with selected response bounds. New HTTP coverage
checks exact script routes and unchanged security headers. Existing saved-history
and provider/lifecycle tests remain at their original owners. Native package QA
uses synthetic homes/libraries and already saved fictional summaries; it needs
no provider call. Qualify packaged loading, wide/light and narrow/dark layout,
affected navigation/fallback and normal cleanup on each target OS separately.

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


### Import/transcription verification

Run the affected Python suites plus `tests.test_transcription` for the shared
local import → loopback HTTP adapter → Library save/reload seam. Existing
`tests.test_assemblyai_adapter` remains the provider failure/ordering matrix;
caller tests cover only their distinct status, explicit-send, navigation and
lifecycle behavior. Use synthetic HOME, `MEETING_DATA_DIR` and
`MEETING_CONFIG_DIR` before the first scan in native QA, a fake key, localhost
`MEETING_TRANSCRIBER_ASSEMBLYAI_API_BASE`, and installed/located FFprobe. Build and
relocate the actual native package and launch without development Python or
checkout paths on PATH. Native title-bar close and application Quit must both
stay open while the fixture holds work, then shutdown must release owned server
and session resources. Retain inspected wide/light and narrow/dark captures in
ignored task artifacts. Windows WebView2/package/ACL execution remains a separate
target-OS proof; no actual provider transaction is implied by the fixture.
