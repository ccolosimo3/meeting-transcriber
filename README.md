# Meeting Transcriber

Private, local meeting transcription for macOS using MLX Whisper, WhisperX,
and pyannote.
Record a meeting, produce a speaker-labeled transcript, assign names, and hand
the result to Codex without using a paid transcription API.

## Platform support

V0 supports Apple-silicon macOS only. It relies on Apple MLX acceleration,
FFmpeg AVFoundation recording, Homebrew, macOS Keychain, Finder, and shell
commands that do not have drop-in Windows equivalents. WhisperX and pyannote
can form the core of a future Windows CPU/CUDA adapter, but Windows is not
currently a supported or seamless installation target.

## Quick start

From a fresh checkout:

```bash
./install.sh
meeting
```

The installer explains its changes before running. It installs the pinned local
toolchain, configures the microphone and Hugging Face Keychain credential, links
the `meeting` command, and installs the bundled Codex skill. If `~/.local/bin`
is not on `PATH`, setup stops with the exact shell-profile line required.

The normal workflow is available from the interactive `meeting` menu or as two
commands:

```bash
meeting record "weekly sync"
# Press q when the meeting ends.

meeting process --speakers 4
```

While recording in an interactive terminal, the command shows the selected
microphone, elapsed time, and a rolling waveform driven by the live input level.
The display is visual only and does not change the captured 48 kHz mono audio.

`meeting process` transcribes the newest completed meeting bundle, optionally asks
for speaker names, creates the human and agent transcript views, opens the HTML
transcript, copies a `$meeting` Codex prompt, and posts a completion
notification. Omit `--speakers` to let diarization estimate the count.

## Commands

```text
meeting                         Open the guided menu
meeting record [LABEL]          Record until q is pressed
meeting process [INPUT]         Run the complete post-meeting workflow
meeting open                    Open the newest color-coded transcript
meeting folder                  Open recordings and transcripts in Finder
meeting prepare                 Prepare the newest transcript for Codex
meeting setup                   Configure this Mac
meeting doctor                  Check the installation without model work
```

Useful process options:

```text
--speakers N    Set the known speaker count
--backend ENGINE Use mlx (default) or whisperx (CPU fallback)
--model MODEL    Select a Whisper model (default: turbo)
--hotwords TEXT Improve recognition of names and domain terms
--no-diarize    Transcribe without speaker labels
--skip-names    Keep SPEAKER_XX labels
--no-open       Do not open the HTML transcript
--no-copy       Print rather than copy the Codex prompt
```

Run `meeting help` or `meeting <command> --help` for the complete interface.
The earlier `meeting-*` executables remain available as compatibility and
advanced commands.

## Codex follow-up

The repository includes `skills/meeting/`. The installer places it under the
personal Codex skills directory. After processing a meeting, paste the prompt
already copied to the clipboard:

```text
Use $meeting to digest the local transcript at: /path/to/transcript.agent.md
```

The skill creates an evidence-grounded digest first, then offers only the
follow-up work supported by that meeting, such as focused research, repository
grounding, a draft message, or a project charter. It does not automatically
publish notes, contact people, create tasks, or modify a repository.

## Files and privacy

Code can be checked out anywhere. Private runtime data defaults to:

```text
~/Transcriptions/20260825-174918-weekly-sync/recording.wav
~/Transcriptions/20260825-174918-weekly-sync/transcripts/20260825-175142/
```

Configuration defaults to:

```text
~/.config/meeting-transcriber/config
```

Override the meeting-bundle root with `MEETING_DATA_DIR` and the configuration
root with `MEETING_CONFIG_DIR`.

Run `meeting folder` to open `~/Transcriptions` in Finder. Keeping this private
data outside `~/meeting-transcriber` prevents recordings and generated
transcripts from entering the source repository.

- Recording, transcription, alignment, and Community-1 diarization execute
  locally after their model files are available.
- MLX Turbo runs on the Apple GPU by default. WhisperX CPU Turbo remains an
  explicit fallback through `--backend whisperx`.
- Model files may be downloaded or resolved from their upstream hosts.
- The Hugging Face token is stored in macOS Keychain under service
  `meeting-transcriber-hf-token`; it is not stored in the checkout.
- Pyannote telemetry is disabled for transcription runs.
- Giving a transcript to Codex is a separate, deliberate disclosure governed by
  that Codex workspace's data controls.
- Record only with participant consent. Recordings and transcripts are retained
  until the user removes them.
- Runtime directories use mode `0700` and generated private files use `0600`.
  Rerunning `meeting setup` repairs older recording and transcript permissions.

## Meeting bundles

Each recording creates one dated meeting bundle containing `recording.wav`.
Each transcription creates an immutable dated run beneath that bundle, so
rerunning with another model or speaker count never duplicates the recording or
silently replaces earlier evidence:

```text
~/Transcriptions/
└── 20260825-174918-weekly-sync/
    ├── recording.wav
    └── transcripts/
        └── 20260825-175142/
            ├── transcript.json
            ├── transcript.txt
            ├── transcript.srt
            ├── transcript.vtt
            ├── transcript.tsv
            ├── transcript.html
            ├── transcript.speakers.json
            └── transcript.agent.md
```

Explicit external audio passed to `meeting transcribe` is copied into a new
meeting bundle; the original file is not changed. The selected transcription
engine supplies structured JSON, text, subtitle, and table formats. This
project adds:

- `.html` — local color-coded transcript for reading;
- `.speakers.json` — local mapping from anonymous labels to display names; and
- `.agent.md` — compact named transcript with timestamps for an agent.

When diarization is enabled, the `.txt`, `.srt`, and `.vtt` views retain each
phrase segment's `SPEAKER_XX` label. JSON also keeps word-level timing and
speaker metadata for downstream tools; TSV stays a plain timing/text table.

The bundled recording is never overwritten and existing transcript runs are
not silently replaced.

## Troubleshooting

Start with:

```bash
meeting doctor
```

It checks Homebrew, uv, FFmpeg 7, the locked Python environment, PyAV/TorchCodec
library alignment, the configured microphone, the Keychain credential, data
directories, and the Codex skill without transcribing audio or loading a model.

Known limitations:

- Room acoustics and microphone placement set the quality ceiling.
- Overlapping speech and distant speakers can reduce diarization accuracy.
- Speaker labels are anonymous until a person maps them to names.
- MLX acceleration requires an Apple-silicon Mac. If the MLX path fails, rerun
  the same input with `meeting process --backend whisperx` to use CPU Turbo.

Development architecture, dependency rationale, and verification details live
in [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md).
