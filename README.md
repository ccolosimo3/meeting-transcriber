# Meeting Transcriber

A small macOS utility with one job: record a room meeting to a private local
file, transcribe it with AssemblyAI speaker diarization, and save one canonical
transcript with a clean browser view and an agent-friendly Markdown view.

Recording always stays on your Mac and works offline. Transcription uploads the
completed recording to AssemblyAI, then deletes the remote transcript and audio
after the local files are saved. There is one supported transcription service
and one complete transcription command.

## Platform support

macOS only. The app relies on FFmpeg AVFoundation recording, Homebrew, macOS
Keychain, notifications, and Finder. Transcription requires an internet
connection, an AssemblyAI API key, and a paid AssemblyAI account.

## Quick start

From a fresh checkout:

```bash
./install.sh
meeting
```

The installer explains its changes before running. It installs Homebrew `uv`
and `ffmpeg@7`, creates a small locked Python environment
(`uv sync --frozen --no-dev`), links the `meeting` command under
`~/.local/bin`, and walks through microphone, AssemblyAI Keychain key, and
`$meeting` skill setup. If `~/.local/bin` is not on `PATH`, setup stops with
the exact shell-profile line required.

The normal workflow is two commands (or the interactive `meeting` menu):

```bash
meeting record "weekly sync"
# Press q when the meeting ends.

meeting transcribe
```

While recording in an interactive terminal, the command shows the selected
microphone, elapsed time, and a rolling level meter. The display is visual only
and does not change the captured 48 kHz mono audio.

`meeting transcribe` states the service and privacy boundary before uploading,
lets AssemblyAI estimate the speaker count unless you pass `--speakers N`,
saves the transcript, offers speaker naming, opens the HTML view, and posts a
completion notification. It finishes by printing the three useful paths and the
agent handoff line `Use $meeting to digest: <transcript.md>`.

## Commands

```text
meeting                         Open the guided menu
meeting record [LABEL]          Record until q is pressed
meeting transcribe [INPUT]      Transcribe a recording with AssemblyAI
meeting open                    Open the latest transcript
meeting speakers                Name or correct speakers
meeting folder                  Open the meeting folder in Finder
meeting setup                   Configure this Mac
meeting doctor                  Check recording and transcription readiness
meeting help                    Show the command summary
```

One recovery-only command exists for the rare case where remote deletion could
not be confirmed:

```text
meeting cleanup [TRANSCRIPT_JSON]   Confirm remote deletion for a saved transcript
```

Retained options:

```text
record      --device NAME_OR_INDEX   Use this microphone for this capture only
transcribe  --speakers N             Expected speaker count (omit to estimate)
transcribe  --skip-names             Keep anonymous speaker labels
transcribe  --no-open                Do not open the finished HTML transcript
speakers    --no-open                Do not open the regenerated HTML transcript
setup       --device NAME_OR_INDEX   Set the default microphone
```

Run `meeting help` or `meeting <command> --help` for details.

## Privacy and cost

- Recording is always local. A recording is never uploaded automatically; only
  `meeting transcribe` sends audio to AssemblyAI.
- Transcription uploads the recording to AssemblyAI's US REST service and uses
  the Universal-3.5 Pro model with speaker diarization, formatting, and
  automatic language detection. This is a managed, paid service: provider
  billing, training opt-out, and retention settings are owned by your
  AssemblyAI account. There is no offline transcription mode.
- After the local transcript is saved, the app deletes the remote transcript
  and uploaded audio and records the confirmed or unconfirmed outcome in a
  private receipt. An unconfirmed deletion is reported prominently with a
  single `meeting cleanup` recovery command; the local transcript remains
  usable either way.
- The AssemblyAI API key is stored in macOS Keychain under service
  `meeting-transcriber-assemblyai-key` and reaches only the provider request —
  never renderers, notifications, command lines, or saved files. A one-run
  `ASSEMBLYAI_API_KEY` environment override is also supported.
- Giving a transcript to an agent is a separate, deliberate disclosure governed
  by that workspace's data controls.
- Record only with participant consent. Recordings and transcripts are retained
  until you remove them.
- Meeting directories use mode `0700` and generated files use `0600`.
  Rerunning `meeting setup` repairs older permissions.

## Files

Code can be checked out anywhere. Private runtime data defaults to
`~/Transcriptions`; configuration defaults to
`~/.config/meeting-transcriber/config`. Run `meeting folder` to open the data
root in Finder.

Each recording creates one dated meeting bundle. Each transcription creates one
immutable dated run beneath it:

```text
~/Transcriptions/
└── 20260826-100000-weekly-sync/
    ├── recording.wav
    └── transcripts/
        └── 20260826-105401/
            ├── transcript.json
            ├── transcript.md
            ├── transcript.html
            └── .assemblyai.json    (hidden private provider receipt)
```

- `transcript.json` — the canonical structured transcript: language, text,
  speaker-labeled segments, word-level timings, and your saved speaker display
  names. Both views regenerate from it.
- `transcript.md` — the agent-friendly view with the meeting title, recorded
  date, timestamps, and display names.
- `transcript.html` — the self-contained reading view with speaker colors,
  search, print styling, and a light/dark preference.
- `.assemblyai.json` — a private operational receipt holding only the provider
  job evidence (model, transcript ID, lifecycle timestamps, and deletion
  state). It never contains transcript text or credentials.

The recording is never modified or deleted by transcription, and existing
transcript runs are never replaced. External audio passed to
`meeting transcribe` is first copied into a new private bundle; the original
file is not changed.

Transcript bundles created by earlier versions of this app (with `.txt`,
`.srt`, `.vtt`, `.tsv`, `.agent.md`, `.speakers.json`, or
`transcript.assemblyai.json` files) remain readable: `meeting open` opens their
saved HTML without rewriting anything. They are not migrated; transcribe the
recording again if you need editable embedded speaker names.

## The $meeting skill

The repository includes `skills/meeting/`; setup installs it under the personal
Codex skills directory. After transcribing, hand the printed line to an agent:

```text
Use $meeting to digest: /path/to/transcript.md
```

The skill creates an evidence-grounded digest first, then offers only the
follow-up work supported by that meeting.

## Troubleshooting

Start with:

```bash
meeting doctor
```

It reports separate Recording and Transcription readiness. A missing AssemblyAI
key blocks transcription but recording remains available.

Known limitations:

- Room acoustics and microphone placement set the quality ceiling; overlapping
  or distant speakers reduce diarization accuracy.
- Speaker labels are anonymous until you map them with `meeting speakers`.
- The service is pinned to Universal-3.5 Pro. Its documented language set is
  English, Spanish, French, German, Italian, Portuguese, Arabic, Danish, Dutch,
  Finnish, Hebrew, Hindi, Japanese, Mandarin, Norwegian, Swedish, Turkish, and
  Vietnamese. See AssemblyAI's
  [Universal-3.5 Pro overview](https://www.assemblyai.com/blog/universal-3-5-pro-code-switching-contextual-prompting).
- AssemblyAI accepts recordings up to 2.2 GB and 10 hours; larger or longer
  recordings are rejected before upload.
- Transcription cannot continue through an AssemblyAI outage. The recording is
  safe locally; transcribe it later.

Development architecture and verification details live in
[docs/DEVELOPMENT.md](docs/DEVELOPMENT.md).
