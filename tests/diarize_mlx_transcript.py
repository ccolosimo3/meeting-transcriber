#!/usr/bin/env python3
"""Apply local pyannote diarization to an MLX Whisper JSON transcript."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from whisperx.diarize import DiarizationPipeline, assign_word_speakers


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audio", required=True, type=Path)
    parser.add_argument("--transcript", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--speakers", required=True, type=int)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.speakers < 1:
        raise SystemExit("--speakers must be positive")
    audio = args.audio.expanduser().resolve()
    transcript = args.transcript.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()
    if not audio.is_file() or not transcript.is_file():
        raise SystemExit("audio and transcript must be readable files")
    token = os.environ.get("HF_TOKEN")
    if not token:
        raise SystemExit("HF_TOKEN is required for Community-1 model access")

    os.umask(0o077)
    output_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    payload = json.loads(transcript.read_text(encoding="utf-8"))
    if not isinstance(payload.get("segments"), list):
        raise SystemExit("transcript JSON has no segments")

    pipeline = DiarizationPipeline(token=token, device="cpu")
    diarization = pipeline(str(audio), num_speakers=args.speakers)
    assigned = assign_word_speakers(diarization, payload, fill_nearest=True)
    segments = [
        segment
        for segment in assigned["segments"]
        if isinstance(segment, dict)
        and segment.get("speaker")
        and str(segment.get("text", "")).strip()
    ]
    result = {
        "segments": segments,
        "language": assigned.get("language", "unknown"),
        "text": " ".join(str(segment["text"]).strip() for segment in segments),
    }

    (output_dir / "transcript.json").write_text(
        json.dumps(result, indent=2) + "\n", encoding="utf-8"
    )
    intervals = [
        {
            "start": float(row["start"]),
            "end": float(row["end"]),
            "speaker": str(row["speaker"]),
        }
        for _, row in diarization.iterrows()
    ]
    (output_dir / "diarization.json").write_text(
        json.dumps(intervals, indent=2) + "\n", encoding="utf-8"
    )
    print(output_dir / "transcript.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
