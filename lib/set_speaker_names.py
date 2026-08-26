from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Persist local display names for anonymous WhisperX speakers."
    )
    parser.add_argument("json_path", type=Path)
    parser.add_argument("--interactive", action="store_true")
    parser.add_argument("assignments", nargs="*", metavar="SPEAKER_XX=NAME")
    return parser.parse_args()


def transcript_speakers(path: Path) -> tuple[list[str], dict[str, str]]:
    with path.open(encoding="utf-8") as source:
        payload: Any = json.load(source)
    if not isinstance(payload, dict) or not isinstance(payload.get("segments"), list):
        raise ValueError("transcript JSON has no segments")
    ordered: list[str] = []
    excerpts: dict[str, str] = {}
    for segment in payload["segments"]:
        if not isinstance(segment, dict) or not segment.get("speaker"):
            continue
        speaker = str(segment["speaker"])
        if speaker in excerpts:
            continue
        text = " ".join(str(segment.get("text") or "").split())
        ordered.append(speaker)
        excerpts[speaker] = text[:120]
    return ordered, excerpts


def parse_assignments(values: list[str], allowed: set[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        speaker, separator, name = value.partition("=")
        speaker = speaker.strip()
        name = name.strip()
        if not separator or not speaker or not name:
            raise ValueError(f"invalid assignment: {value!r}; use SPEAKER_XX=Name")
        if speaker not in allowed:
            raise ValueError(
                f"unknown speaker {speaker!r}; available labels: {', '.join(sorted(allowed))}"
            )
        result[speaker] = name
    return result


def prompt_assignments(
    ordered: list[str], excerpts: dict[str, str], existing: dict[str, str]
) -> dict[str, str]:
    print("Speakers in first-appearance order:", file=sys.stderr)
    for index, speaker in enumerate(ordered, start=1):
        current = f" (currently {existing[speaker]})" if speaker in existing else ""
        excerpt = excerpts.get(speaker) or "no text excerpt"
        print(f'  {index}. {speaker}{current}: “{excerpt}”', file=sys.stderr)
    print(
        f"Enter {len(ordered)} names in that order, separated by commas: ",
        end="",
        file=sys.stderr,
        flush=True,
    )
    raw = sys.stdin.readline()
    if not raw:
        raise ValueError("no names were entered")
    names = [name.strip() for name in next(csv.reader([raw], skipinitialspace=True))]
    if len(names) != len(ordered) or any(not name for name in names):
        raise ValueError(
            f"expected exactly {len(ordered)} nonempty comma-separated names"
        )
    return dict(zip(ordered, names, strict=True))


def write_atomic(path: Path, payload: dict[str, str]) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(payload, output, ensure_ascii=False, indent=2, sort_keys=True)
            output.write("\n")
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def main() -> None:
    args = parse_args()
    transcript = args.json_path.expanduser().resolve()
    ordered, excerpts = transcript_speakers(transcript)
    if not ordered:
        raise ValueError("transcript contains no speaker labels")
    available = set(ordered)
    mapping_path = transcript.with_suffix(".speakers.json")
    existing: dict[str, str] = {}
    if mapping_path.exists():
        with mapping_path.open(encoding="utf-8") as source:
            payload = json.load(source)
        if isinstance(payload, dict):
            existing = {str(key): str(value) for key, value in payload.items()}
    if args.interactive:
        if args.assignments:
            raise ValueError("interactive mode does not accept explicit assignments")
        updates = prompt_assignments(ordered, excerpts, existing)
    else:
        if not args.assignments:
            raise ValueError("provide assignments or use --interactive")
        updates = parse_assignments(args.assignments, available)
    existing.update(updates)
    write_atomic(mapping_path, existing)
    print(mapping_path)


if __name__ == "__main__":
    try:
        main()
    except (FileNotFoundError, json.JSONDecodeError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(2) from None
