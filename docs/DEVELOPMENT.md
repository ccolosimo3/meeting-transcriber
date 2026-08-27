# Development notes

## Architecture

The public interface is `bin/meeting`. It dispatches to small, independently
usable commands:

- `meeting-record` captures lossless 48 kHz mono WAV through FFmpeg AVFoundation
  into a dated meeting bundle and finalizes `recording.partial.wav` to
  `recording.wav` only after FFprobe validates it. Interactive terminals also
  render a two-line elapsed-time and rolling-peak display from FFmpeg `astats`
  metadata; the analysis filter passes the captured audio through unchanged.
- `meeting-transcribe` creates a new dated output directory and selects the
  transcription provider. Runs live under the source bundle's `transcripts/`
  directory; explicit external audio is first copied into a new bundle.
  AssemblyAI is the default provider. `--local` selects the existing local
  stack, where MLX is the normal engine and locked WhisperX CPU `int8` remains
  the engine fallback. Legacy local-only flags imply `--local`; provider
  failures never cross this boundary automatically.
- `lib/transcribe_assemblyai.py` is the standard-library asynchronous REST
  adapter. It streams upload bytes, submits exactly one Universal-3.5 Pro job,
  polls with bounded transient retries, validates and converts utterances, and
  reconciles remote deletion. It has no import-time dependency on MLX,
  WhisperX, Torch, or pyannote.
- `lib/transcribe_mlx.py` remains the narrow local MLX adapter and optionally
  applies Community-1 diarization. It retains the established segment-text join
  and phrase-majority speaker behavior.
- `lib/transcript_bundle.py` is the provider-neutral canonical writer shared by
  the AssemblyAI and MLX producers. It owns segment filtering and
  dependency-free timestamp formatting while accepting an already canonical
  top-level `text`; this preserves local output bytes and AssemblyAI's returned
  top-level text.
- `meeting-process` composes transcription, interactive speaker naming, HTML and
  Markdown rendering, browser opening, prompt copying, and notification.
- `meeting-setup` owns user-local configuration, command linking, Keychain
  guidance, and bundled-skill installation.
- `meeting-doctor` performs quick local diagnostics without model inference.

The older single-purpose executable names remain thin compatibility layers.
Keep orchestration in the friendly layer and core transcription behavior in the
single-purpose scripts so an interrupted workflow can be resumed manually.

## Toolchain

The supported V0 environment is macOS on Apple silicon with:

- Homebrew `uv`, `pkgconf`, and keg-only `ffmpeg@7`;
- uv-managed Python 3.13;
- MLX Whisper 0.4.3;
- WhisperX 3.8.6;
- pyannote.audio 4.0.7 / Community-1;
- TorchCodec 0.7; and
- PyAV 14.4.0 built against Homebrew FFmpeg 7.

PyAV is built from source during installation so it and TorchCodec load the same
FFmpeg library family. Loading a newer bundled PyAV FFmpeg beside TorchCodec's
FFmpeg 7 libraries produced duplicate-library warnings during the original
qualification.

`pyproject.toml` and `uv.lock` own Python dependency versions. Do not commit the
`.venv`, model caches, recordings, transcripts, local configuration, or
Keychain material.

## Provider lifecycle and credential boundary

The managed path uses `https://api.assemblyai.com`, places the raw API key only
in the adapter child's `Authorization` header, and pins
`speech_models: ["universal-3-5-pro"]`. The wrapper retrieves
`ASSEMBLYAI_API_KEY` only after provider selection or reads the
`meeting-transcriber-assemblyai-key` Keychain item, unsets the inherited value,
and does not expose it to renderers or preparation children.

Each managed run atomically persists mode-`0600`
`transcript.assemblyai.json`. The receipt begins after upload, gains the known
transcript ID immediately after submission, retains the terminal provider
response, and records deletion only after a successful DELETE response. Unknown
upload/submission outcomes retain explicit residual-risk states and are never
retried. Every known-ID failure attempts bounded deletion. Successful deletion
occurs only after the completed receipt and canonical outputs reload; an
unconfirmed deletion preserves local evidence but prevents downstream success
actions.

The explicit local provider reads `HF_TOKEN` for diarization when inherited or retrieves the
`meeting-transcriber-hf-token` item for the current account from macOS Keychain.
The token is exported only to the transcription child environment. It is not
passed through a command-line argument, printed, or written into an output
bundle.

`PYANNOTE_METRICS_ENABLED=0` disables pyannote telemetry for transcription
runs. Community-1 inference is local after model download; premium pyannoteAI
services are not used.

Recording and transcript writers set `umask 077`. `meeting setup` also repairs
every existing meeting-bundle directory to `0700` and file to `0600`; keep the
permissions regression in the lightweight interface smoke whenever output
creation changes.

## Verification

Use the smallest proof appropriate to the change:

```bash
bash -n bin/* lib/config.sh install.sh
.venv/bin/python -m py_compile lib/*.py tests/*.py
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
meeting doctor
meeting record --dry-run "verification"
meeting prepare --no-copy /path/to/transcript.json
tests/interface-smoke.sh
```

The discover-based suite includes two environment-qualified real-boundary
tests. Recorder PTY coverage requires macOS and Homebrew `ffmpeg@7`; rendered
HTML browser coverage requires `aiohttp` from the locked environment and a
local Google Chrome or Chromium executable. Those tests skip when their
prerequisites are unavailable, so recorder and transcript-UI changes should be
verified in the supported V0 environment before release.

`bin/verify-install` retains the heavier synthetic-audio smoke and real
diarization checks used to qualify dependency behavior. These checks may load
models or perform local transcription; they always pass `--local`, cannot invoke
AssemblyAI, and are not part of ordinary setup or command help.

`tests/test_assemblyai_adapter.py` runs a standard-library loopback HTTP server
that crosses the real request boundary for streamed upload framing, raw
authorization, submit/poll/delete behavior, canonical publication, and lifecycle
receipts without provider traffic. Narrow injected failures cover transport
ambiguity and interruption. Shell coverage uses
`MEETING_TRANSCRIBER_ASSEMBLYAI_BIN`, `MEETING_TRANSCRIBER_MLX_BIN`, and
`MEETING_TRANSCRIBER_WHISPERX_BIN` to prove routing and credential containment.
Set `MEETING_NOTIFICATIONS=0` during automated verification.

After deterministic implementation and both review gates, the remaining Tier 4
proof is exactly one separately approved, short production-path AssemblyAI job
with a named consented/synthetic input and cost cap. It must confirm the reported
model, complete rendering, unchanged source hash, credential-free receipt,
confirmed DELETE, and follow-up provider deletion. Deterministic tests and
`meeting doctor` never make a provider request.

## ASR model comparison

The model comparison is intentionally separate from the normal `meeting`
interface. It holds one recording and the English decoding settings constant,
then compares WhisperX CPU Small, Turbo, and Distil Large V3 with the equivalent
MLX GPU candidates. Public model downloads are completed before timing.

```bash
.venv/bin/python tests/compare_asr_models.py \
  --input /absolute/path/to/recording.wav \
  --output-root tests/artifacts/model-comparison-YYYYMMDD \
  --repetitions 2
```

Review the generated `README.md`, first-run transcripts, and pairwise diffs.
Timing alone does not select the winner: prefer the fastest model that preserves
names, technical terms, meaning, and freedom from hallucinated speech.

The August 2026 M5 comparison selected MLX Turbo as the normal local engine. Across
two runs of the fixed 114.5-second sample it averaged 6.96 seconds, produced
byte-identical transcripts, and retained more of the quiet opening and final
exchange than the CPU candidates. WhisperX CPU Turbo remains available as a
manual local-engine fallback so an MLX failure is visible rather than silently
retried.

## Packaging

Source and personal data are intentionally separate. A checkout may live in any
directory while runtime data defaults to `~/Transcriptions`. Each immediate
child is one dated meeting bundle with one recording and zero or more immutable
transcript runs. Development verification artifacts stay under the ignored
source-local `.local-artifacts/`, not beside personal meetings.
The bundled skill at `skills/meeting/` is the distributable source; the setup
command installs it under `~/.codex/skills/meeting` without overwriting a
different existing copy.
