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
  transcription engine. Runs live under the source bundle's `transcripts/`
  directory; explicit external audio is first copied into a new bundle. MLX
  Whisper Turbo on the Apple GPU is the default; locked WhisperX Turbo with CPU
  `int8` is the explicit fallback.
- `lib/transcribe_mlx.py` is the narrow MLX adapter. It writes the same JSON,
  text, subtitle, and table bundle as the CPU path and optionally applies local
  pyannote diarization. Diarization labels MLX phrase segments by overlap
  majority; word-level labels remain metadata and never create one-word turns.
  Its writer preserves dotted source stems verbatim and includes phrase speaker
  labels in TXT, SRT, and VTT while keeping TSV as plain timing/text data.
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

## Credential boundary

Diarization reads `HF_TOKEN` when explicitly inherited or retrieves the
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
.venv/bin/python -m py_compile lib/*.py
.venv/bin/python -m unittest tests/test_mlx_adapter.py
meeting doctor
meeting record --dry-run "verification"
meeting prepare --no-copy /path/to/transcript.json
tests/interface-smoke.sh
```

`bin/verify-install` retains the heavier synthetic-audio smoke and real
diarization checks used to qualify dependency behavior. These checks may load
models or perform transcription; they are not part of ordinary setup or command
help.

For orchestration changes, select deterministic engine stubs through
`MEETING_TRANSCRIBER_MLX_BIN` and `MEETING_TRANSCRIBER_WHISPERX_BIN`. Set
`MEETING_NOTIFICATIONS=0` during automated verification. Confirm a child sees
the inherited token when diarization is requested while neither argv nor output
contains the credential.

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

The August 2026 M5 comparison selected MLX Turbo as the normal default. Across
two runs of the fixed 114.5-second sample it averaged 6.96 seconds, produced
byte-identical transcripts, and retained more of the quiet opening and final
exchange than the CPU candidates. WhisperX CPU Turbo remains available as a
manual fallback so an MLX failure is visible rather than silently retried.

## Packaging

Source and personal data are intentionally separate. A checkout may live in any
directory while runtime data defaults to `~/Transcriptions`. Each immediate
child is one dated meeting bundle with one recording and zero or more immutable
transcript runs. Development verification artifacts stay under the ignored
source-local `.local-artifacts/`, not beside personal meetings.
The bundled skill at `skills/meeting/` is the distributable source; the setup
command installs it under `~/.codex/skills/meeting` without overwriting a
different existing copy.
