from __future__ import annotations

import argparse
import html
import json
import os
import tempfile
from pathlib import Path
from typing import Any


PALETTE = (
    ("#2563eb", "#eff6ff"),
    ("#c2410c", "#fff7ed"),
    ("#047857", "#ecfdf5"),
    ("#7c3aed", "#f5f3ff"),
    ("#be123c", "#fff1f2"),
    ("#0e7490", "#ecfeff"),
    ("#a16207", "#fefce8"),
    ("#4f46e5", "#eef2ff"),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render a local WhisperX JSON transcript as color-coded HTML."
    )
    parser.add_argument("json_path", type=Path)
    parser.add_argument("html_path", nargs="?", type=Path)
    return parser.parse_args()


def timestamp(value: Any) -> str:
    seconds = float(value or 0)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{int(hours):02d}:{int(minutes):02d}:{seconds:06.3f}"


def load_segments(path: Path) -> tuple[str, list[dict[str, Any]]]:
    with path.open(encoding="utf-8") as source:
        payload = json.load(source)
    if not isinstance(payload, dict):
        raise ValueError("transcript JSON must contain an object")
    language = str(payload.get("language") or "unknown")
    raw_segments = payload.get("segments")
    if not isinstance(raw_segments, list) or not raw_segments:
        raise ValueError("transcript JSON has no segments")

    segments: list[dict[str, Any]] = []
    for raw in raw_segments:
        if not isinstance(raw, dict):
            continue
        text = str(raw.get("text") or "").strip()
        if not text:
            continue
        segments.append(
            {
                "speaker": str(raw.get("speaker") or "UNASSIGNED"),
                "start": float(raw.get("start") or 0),
                "end": float(raw.get("end") or raw.get("start") or 0),
                "text": text,
            }
        )
    if not segments:
        raise ValueError("transcript JSON has no nonempty text segments")
    return language, segments


def group_turns(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    for segment in segments:
        if turns and turns[-1]["speaker"] == segment["speaker"]:
            turns[-1]["end"] = segment["end"]
            turns[-1]["texts"].append(segment["text"])
        else:
            turns.append(
                {
                    "speaker": segment["speaker"],
                    "start": segment["start"],
                    "end": segment["end"],
                    "texts": [segment["text"]],
                }
            )
    return turns


def load_speaker_names(source: Path) -> dict[str, str]:
    mapping_path = source.with_suffix(".speakers.json")
    if not mapping_path.exists():
        return {}
    with mapping_path.open(encoding="utf-8") as mapping_source:
        payload = json.load(mapping_source)
    if not isinstance(payload, dict):
        raise ValueError("speaker-name map must contain an object")
    return {
        str(speaker): str(name).strip()
        for speaker, name in payload.items()
        if str(name).strip()
    }


def speaker_label(speaker: str, names: dict[str, str]) -> str:
    name = names.get(speaker)
    if not name:
        return html.escape(speaker)
    return f'{html.escape(name)} <small>{html.escape(speaker)}</small>'


def render(
    source: Path,
    language: str,
    segments: list[dict[str, Any]],
    names: dict[str, str],
) -> str:
    turns = group_turns(segments)
    speakers = list(dict.fromkeys(turn["speaker"] for turn in turns))
    speaker_styles = {
        speaker: PALETTE[index % len(PALETTE)]
        for index, speaker in enumerate(speakers)
    }
    title = source.stem

    legend = "\n".join(
        f'<span class="legend-item" style="--accent:{accent};--surface:{surface}">'
        f'<span class="dot"></span>{speaker_label(speaker, names)}</span>'
        for speaker, (accent, surface) in speaker_styles.items()
    )
    turn_markup = "\n".join(
        (
            f'<article class="turn" style="--accent:{speaker_styles[turn["speaker"]][0]};'
            f'--surface:{speaker_styles[turn["speaker"]][1]}">'
            '<header>'
            f'<span class="speaker">{speaker_label(turn["speaker"], names)}</span>'
            f'<time>{timestamp(turn["start"])}–{timestamp(turn["end"])}</time>'
            '</header>'
            f'<p>{html.escape(" ".join(turn["texts"]))}</p>'
            '</article>'
        )
        for turn in turns
    )

    return f"""<!doctype html>
<html lang="{html.escape(language)}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)} transcript</title>
  <style>
    :root {{ color-scheme: light; font-family: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; background: #f8fafc; color: #172033; line-height: 1.55; }}
    main {{ width: min(900px, calc(100% - 32px)); margin: 40px auto 80px; }}
    h1 {{ margin: 0 0 4px; font-size: clamp(1.5rem, 4vw, 2.25rem); line-height: 1.2; }}
    .meta {{ margin: 0 0 20px; color: #64748b; }}
    .legend {{ display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 24px; }}
    .legend-item {{ display: inline-flex; align-items: center; gap: 7px; border: 1px solid #cbd5e1; border-radius: 999px; background: var(--surface); padding: 5px 10px; font-size: .82rem; font-weight: 700; }}
    .dot {{ width: 10px; height: 10px; border-radius: 50%; background: var(--accent); }}
    .turn {{ margin: 0 0 12px; border: 1px solid #dbe3ee; border-left: 6px solid var(--accent); border-radius: 10px; background: var(--surface); padding: 13px 16px 14px; box-shadow: 0 1px 2px rgb(15 23 42 / 5%); }}
    header {{ display: flex; flex-wrap: wrap; align-items: baseline; justify-content: space-between; gap: 8px; margin-bottom: 5px; }}
    .speaker {{ color: var(--accent); font-size: .82rem; font-weight: 800; letter-spacing: .03em; }}
    small {{ margin-left: 4px; color: #64748b; font-size: .68rem; font-weight: 600; letter-spacing: 0; }}
    time {{ color: #64748b; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .75rem; }}
    p {{ margin: 0; white-space: pre-wrap; }}
    @media print {{ body {{ background: white; }} main {{ width: 100%; margin: 0; }} .turn {{ break-inside: avoid; box-shadow: none; }} }}
  </style>
</head>
<body>
  <main>
    <h1>{html.escape(title)}</h1>
    <p class="meta">{len(turns)} speaker turns · language: {html.escape(language)}</p>
    <nav class="legend" aria-label="Speaker color legend">{legend}</nav>
    {turn_markup}
  </main>
</body>
</html>
"""


def main() -> None:
    args = parse_args()
    source = args.json_path.expanduser().resolve()
    destination = (
        args.html_path.expanduser().resolve()
        if args.html_path
        else source.with_suffix(".html")
    )
    language, segments = load_segments(source)
    names = load_speaker_names(source)
    rendered = render(source, language, segments, names)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(rendered)
        os.replace(temporary_name, destination)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise
    print(destination)


if __name__ == "__main__":
    main()
