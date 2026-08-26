from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

import mlx.core as mx
import mlx_whisper
from mlx_whisper.writers import format_timestamp


MODEL_ALIASES = {
    "small": "mlx-community/whisper-small-mlx",
    "turbo": "mlx-community/whisper-large-v3-turbo",
    "distil-large-v3": "mlx-community/distil-whisper-large-v3",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Transcribe locally with MLX Whisper and optional pyannote diarization."
    )
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--output-name", required=True)
    parser.add_argument("--model", default="turbo")
    parser.add_argument("--initial-prompt")
    parser.add_argument("--diarize", action="store_true")
    parser.add_argument("--speakers", type=int)
    return parser.parse_args()


def finalize_transcript(result: dict[str, Any]) -> dict[str, Any]:
    """Keep ASR phrase segments intact after speaker assignment."""
    raw_segments = result.get("segments")
    if not isinstance(raw_segments, list):
        raise ValueError("MLX transcript has no segment list")
    segments = [
        segment
        for segment in raw_segments
        if isinstance(segment, dict) and str(segment.get("text", "")).strip()
    ]
    if not segments:
        raise ValueError("MLX transcript has no nonempty segments")
    result["segments"] = segments
    result["text"] = "".join(str(segment["text"]) for segment in segments).strip()
    return result


def assign_speakers(
    audio: Path, result: dict[str, Any], token: str, speakers: int | None
) -> dict[str, Any]:
    # Importing WhisperX loads the torch stack, so keep it out of no-diarization runs.
    from whisperx.diarize import DiarizationPipeline, assign_word_speakers

    pipeline = DiarizationPipeline(token=token, device="cpu")
    if speakers is None:
        diarization = pipeline(str(audio))
    else:
        diarization = pipeline(str(audio), num_speakers=speakers)
    assigned = assign_word_speakers(diarization, result, fill_nearest=True)
    if not isinstance(assigned, dict):
        raise ValueError("pyannote returned an invalid speaker assignment")
    # assign_word_speakers gives each phrase segment its overlap-majority speaker and
    # also retains word-level speaker metadata. The phrase boundary is the stable
    # rendering boundary; splitting on word labels creates noisy one-word turns.
    return finalize_transcript(assigned)


def labeled_text(segment: dict[str, Any], *, subtitle: bool = False) -> str:
    text = str(segment.get("text", "")).strip()
    if subtitle:
        text = text.replace("-->", "->")
    speaker = str(segment.get("speaker", "")).strip()
    return f"[{speaker}]: {text}" if speaker else text


def write_outputs(
    result: dict[str, Any], output_dir: Path, output_name: str
) -> None:
    """Write the established bundle without treating dots as suffixes."""
    segments = result["segments"]
    destinations = {
        extension: output_dir / f"{output_name}.{extension}"
        for extension in ("json", "txt", "srt", "vtt", "tsv")
    }
    destinations["json"].write_text(
        json.dumps(result, ensure_ascii=False), encoding="utf-8"
    )
    destinations["txt"].write_text(
        "".join(f"{labeled_text(segment)}\n" for segment in segments),
        encoding="utf-8",
    )

    srt_lines: list[str] = []
    vtt_lines = ["WEBVTT", ""]
    tsv_lines = ["start\tend\ttext"]
    for index, segment in enumerate(segments, start=1):
        start = float(segment.get("start", 0))
        end = float(segment.get("end", start))
        text = labeled_text(segment, subtitle=True)
        srt_start = format_timestamp(start, always_include_hours=True, decimal_marker=",")
        srt_end = format_timestamp(end, always_include_hours=True, decimal_marker=",")
        vtt_start = format_timestamp(start, always_include_hours=False, decimal_marker=".")
        vtt_end = format_timestamp(end, always_include_hours=False, decimal_marker=".")
        srt_lines.extend((str(index), f"{srt_start} --> {srt_end}", text, ""))
        vtt_lines.extend((f"{vtt_start} --> {vtt_end}", text, ""))
        plain_text = str(segment.get("text", "")).strip().replace("\t", " ")
        tsv_lines.append(f"{round(1000 * start)}\t{round(1000 * end)}\t{plain_text}")

    destinations["srt"].write_text("\n".join(srt_lines), encoding="utf-8")
    destinations["vtt"].write_text("\n".join(vtt_lines), encoding="utf-8")
    destinations["tsv"].write_text("\n".join(tsv_lines) + "\n", encoding="utf-8")


def main() -> int:
    args = parse_args()
    os.umask(0o077)
    audio = args.input.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()
    if not audio.is_file() or not os.access(audio, os.R_OK):
        raise SystemExit(f"input is not a readable file: {audio}")
    if not output_dir.is_dir() or not os.access(output_dir, os.W_OK):
        raise SystemExit(f"output directory is not writable: {output_dir}")
    if args.speakers is not None and args.speakers < 1:
        raise SystemExit("--speakers must be positive")
    if args.speakers is not None and not args.diarize:
        raise SystemExit("--speakers may be used only with --diarize")
    if "/" in args.output_name or args.output_name in {"", ".", ".."}:
        raise SystemExit("--output-name must be a plain filename")

    device = str(mx.default_device())
    if "gpu" not in device.lower():
        raise SystemExit(f"MLX is not using the Apple GPU: {device}")

    model = MODEL_ALIASES.get(args.model, args.model)
    result = mlx_whisper.transcribe(
        str(audio),
        path_or_hf_repo=model,
        verbose=False,
        condition_on_previous_text=False,
        initial_prompt=args.initial_prompt,
        word_timestamps=True,
        task="transcribe",
    )
    if not isinstance(result, dict):
        raise ValueError("MLX returned an invalid transcript")
    result = finalize_transcript(result)

    if args.diarize:
        token = os.environ.get("HF_TOKEN")
        if not token:
            raise SystemExit("HF_TOKEN is required for Community-1 diarization")
        result = assign_speakers(audio, result, token, args.speakers)

    write_outputs(result, output_dir, args.output_name)
    print(output_dir / f"{args.output_name}.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
