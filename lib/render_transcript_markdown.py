from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path

from render_transcript_html import (
    group_turns,
    load_segments,
    load_speaker_names,
    timestamp,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render a WhisperX JSON transcript as agent-friendly Markdown."
    )
    parser.add_argument("json_path", type=Path)
    parser.add_argument("markdown_path", nargs="?", type=Path)
    return parser.parse_args()


def speaker_label(speaker: str, names: dict[str, str]) -> str:
    name = names.get(speaker)
    return f"{name} ({speaker})" if name else speaker


def render(source: Path) -> str:
    language, segments = load_segments(source)
    names = load_speaker_names(source)
    turns = group_turns(segments)
    lines = [
        f"# {source.stem}",
        "",
        f"- Language: {language}",
        f"- Speaker turns: {len(turns)}",
        f"- Structured source: `{source.name}`",
        "",
        "## Transcript",
        "",
    ]
    for turn in turns:
        label = speaker_label(str(turn["speaker"]), names)
        time_range = f'{timestamp(turn["start"])}–{timestamp(turn["end"])}'
        text = " ".join(str(part).strip() for part in turn["texts"] if str(part).strip())
        lines.extend((f"### {label} — {time_range}", "", text, ""))
    return "\n".join(lines)


def write_atomic(destination: Path, content: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(content)
        os.replace(temporary_name, destination)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def main() -> None:
    args = parse_args()
    source = args.json_path.expanduser().resolve()
    destination = (
        args.markdown_path.expanduser().resolve()
        if args.markdown_path
        else source.with_suffix(".agent.md")
    )
    write_atomic(destination, render(source))
    print(destination)


if __name__ == "__main__":
    main()
