from __future__ import annotations

import argparse
from pathlib import Path

from meeting_identity import meeting_identity
from render_transcript_html import group_turns, timestamp
from transcript_bundle import atomic_write_private, load_canonical


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render a canonical JSON transcript as agent-friendly Markdown."
    )
    parser.add_argument("json_path", type=Path)
    parser.add_argument("markdown_path", nargs="?", type=Path)
    return parser.parse_args()


def speaker_label(speaker: str, names: dict[str, str]) -> str:
    name = names.get(speaker)
    return f"{name} ({speaker})" if name else speaker


def render(source: Path) -> str:
    transcript = load_canonical(source)
    title, recorded_at, run_id = meeting_identity(source)
    turns = group_turns(transcript.segments)
    lines = [
        f"# {title}",
        "",
        f"- Recorded: {recorded_at}",
        f"- Transcript run: {run_id}",
        f"- Structured source: `{source.name}`",
        f"- Language: {transcript.language}",
        f"- Speaker turns: {len(turns)}",
        "",
        "## Transcript",
        "",
    ]
    for turn in turns:
        label = speaker_label(turn.speaker, transcript.speaker_names)
        time_range = f"{timestamp(turn.start)}–{timestamp(turn.end)}"
        text = " ".join(part.strip() for part in turn.texts if part.strip())
        lines.extend((f"### {label} — {time_range}", "", text, ""))
    return "\n".join(lines)


def render_markdown(source: Path, destination: Path | None = None) -> Path:
    """Render the saved canonical transcript to Markdown atomically."""
    target = destination if destination is not None else source.with_suffix(".md")
    atomic_write_private(target, render(source))
    return target


def main() -> None:
    args = parse_args()
    source = args.json_path.expanduser().resolve()
    destination = args.markdown_path.expanduser().resolve() if args.markdown_path else None
    print(render_markdown(source, destination))


if __name__ == "__main__":
    main()
