from __future__ import annotations

import argparse
import sys
from pathlib import Path

from render_transcript_html import render_html
from render_transcript_markdown import render_markdown
from transcript_bundle import (
    CanonicalTranscript,
    CanonicalTranscriptError,
    load_canonical,
    save_canonical,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Interactively embed speaker display names in a canonical transcript "
            "and regenerate its Markdown and HTML views."
        )
    )
    parser.add_argument("json_path", type=Path)
    return parser.parse_args()


def speaker_excerpts(transcript: CanonicalTranscript) -> dict[str, str]:
    excerpts: dict[str, str] = {}
    for segment in transcript.segments:
        if segment.speaker not in excerpts:
            excerpts[segment.speaker] = " ".join(segment.text.split())[:120]
    return excerpts


def prompt_names(transcript: CanonicalTranscript) -> dict[str, str]:
    """Collect optional display names, one prompt per anonymous speaker."""
    ordered = transcript.speaker_labels()
    excerpts = speaker_excerpts(transcript)
    updates = dict(transcript.speaker_names)
    print("Speakers in first-appearance order:", file=sys.stderr)
    for speaker in ordered:
        excerpt = excerpts.get(speaker) or "no text excerpt"
        print(f"\n{speaker}  “{excerpt}”", file=sys.stderr)
        current = updates.get(speaker)
        default = f"keep {current}" if current else f"keep {speaker}"
        print(f"  Name [Enter to {default}]: ", end="", file=sys.stderr, flush=True)
        raw = sys.stdin.readline()
        if not raw:
            break
        name = raw.strip()
        if name:
            updates[speaker] = name
    return updates


def main() -> int:
    args = parse_args()
    transcript_path = args.json_path.expanduser().resolve()
    try:
        transcript = load_canonical(transcript_path)
    except CanonicalTranscriptError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2
    updates = prompt_names(transcript)
    transcript.speaker_names = updates
    try:
        reloaded = save_canonical(transcript_path, transcript)
    except CanonicalTranscriptError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2
    markdown_path = render_markdown(transcript_path)
    html_path = render_html(transcript_path)
    named = len(reloaded.speaker_names)
    print(
        f"\nSaved {named} speaker name(s) and regenerated both views.",
        file=sys.stderr,
    )
    print(f"  Read        {html_path}", file=sys.stderr)
    print(f"  Agent       {markdown_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nCancelled; no names were saved.", file=sys.stderr)
        raise SystemExit(130) from None
    except (OSError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(2) from None
