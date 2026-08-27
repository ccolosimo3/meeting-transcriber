from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def format_timestamp(
    seconds: float, always_include_hours: bool = False, decimal_marker: str = "."
) -> str:
    """Match the timestamp formatting used by mlx-whisper 0.4.3."""
    if seconds < 0:
        raise ValueError("non-negative timestamp expected")
    milliseconds = round(seconds * 1000.0)
    hours = milliseconds // 3_600_000
    milliseconds -= hours * 3_600_000
    minutes = milliseconds // 60_000
    milliseconds -= minutes * 60_000
    whole_seconds = milliseconds // 1_000
    milliseconds -= whole_seconds * 1_000
    hours_marker = f"{hours:02d}:" if always_include_hours or hours > 0 else ""
    return (
        f"{hours_marker}{minutes:02d}:{whole_seconds:02d}"
        f"{decimal_marker}{milliseconds:03d}"
    )


def nonempty_segments(result: dict[str, Any]) -> list[dict[str, Any]]:
    raw_segments = result.get("segments")
    if not isinstance(raw_segments, list):
        raise ValueError("transcript has no segment list")
    segments = [
        segment
        for segment in raw_segments
        if isinstance(segment, dict) and str(segment.get("text", "")).strip()
    ]
    if not segments:
        raise ValueError("transcript has no nonempty segments")
    return segments


def labeled_text(segment: dict[str, Any], *, subtitle: bool = False) -> str:
    text = str(segment.get("text", "")).strip()
    if subtitle:
        text = text.replace("-->", "->")
    speaker = str(segment.get("speaker", "")).strip()
    return f"[{speaker}]: {text}" if speaker else text


def write_outputs(
    result: dict[str, Any], output_dir: Path, output_name: str
) -> dict[str, Path]:
    """Write the provider-neutral canonical bundle without rewriting top-level text."""
    if "/" in output_name or output_name in {"", ".", ".."}:
        raise ValueError("output name must be a plain filename")
    segments = nonempty_segments(result)
    result["segments"] = segments
    if not isinstance(result.get("text"), str):
        raise ValueError("transcript text must be a string")

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
    return destinations
