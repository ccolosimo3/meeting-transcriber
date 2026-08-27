"""Shared meeting identity derived from the canonical meeting-bundle path."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path


BUNDLE_PATTERN = re.compile(r"^(\d{8})-(\d{6})(?:-(.+))?$")


def meeting_identity(source: Path) -> tuple[str, str, str]:
    """Derive a human title, local recorded date, and run id for a transcript.

    The source is the canonical transcript JSON path. Production paths look like
    <root>/<YYYYMMDD-HHMMSS>-<label>/transcripts/<run>/transcript.json.
    """
    fallback_label = source.stem.replace("-", " ").replace("_", " ").strip()
    fallback = fallback_label[:1].upper() + fallback_label[1:]
    fallback_identity = (
        fallback or "Meeting transcript",
        "Date unavailable",
        source.stem,
    )
    bundle_name = source.parents[2].name if len(source.parents) >= 3 else ""
    match = BUNDLE_PATTERN.fullmatch(bundle_name)
    if not match:
        return fallback_identity

    try:
        recorded_at = datetime.strptime("".join(match.group(1, 2)), "%Y%m%d%H%M%S")
    except ValueError:
        return fallback_identity
    slug = match.group(3) or "meeting"
    label = slug.replace("-", " ").replace("_", " ").strip()
    title = label[:1].upper() + label[1:]
    hour = recorded_at.strftime("%I").lstrip("0") or "12"
    date = (
        f"{recorded_at.strftime('%B')} {recorded_at.day}, {recorded_at.year} "
        f"at {hour}:{recorded_at.strftime('%M %p')}"
    )
    return title, date, source.parent.name
