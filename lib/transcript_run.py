"""Classify one transcript run directory for latest-run discovery.

Usage: transcript_run.py RUN_DIR

Prints one tab-separated line on success and exits 0:
  clean\t<transcript.json>
  cleanup-required\t<transcript.json>\t<transcript_id>
  historical\t<transcript.json>
  historical-unconfirmed\t<transcript.json>\t<transcript_id>

Exits 1 when the directory is not a usable run (partial, malformed, symlinked,
or pre-publication). This is the single validation boundary behind shell
discovery; it never mutates anything.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from transcript_bundle import CanonicalTranscriptError, load_canonical


RECEIPT_NAME = ".assemblyai.json"
HISTORICAL_RECEIPT_NAME = "transcript.assemblyai.json"


def _regular_file(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()


def _load_receipt(path: Path) -> dict[str, object] | None:
    try:
        with path.open(encoding="utf-8") as stream:
            payload: object = json.load(stream)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _deletion_confirmed(receipt: dict[str, object]) -> bool:
    deletion = receipt.get("deletion")
    return isinstance(deletion, dict) and deletion.get("confirmed") is True


def classify(run_dir: Path) -> tuple[str, Path, str | None] | None:
    if not run_dir.is_dir() or run_dir.is_symlink():
        return None
    json_path = run_dir / "transcript.json"
    receipt_path = run_dir / RECEIPT_NAME

    if receipt_path.exists() or receipt_path.is_symlink():
        markdown_path = run_dir / "transcript.md"
        html_path = run_dir / "transcript.html"
        required = (json_path, markdown_path, html_path, receipt_path)
        if not all(_regular_file(path) for path in required):
            return None
        if markdown_path.stat().st_size == 0 or html_path.stat().st_size == 0:
            return None
        try:
            load_canonical(json_path)
        except (CanonicalTranscriptError, OSError, json.JSONDecodeError, UnicodeDecodeError):
            return None
        receipt = _load_receipt(receipt_path)
        if receipt is None or receipt.get("state") != "published":
            return None
        if _deletion_confirmed(receipt):
            return ("clean", json_path, None)
        transcript_id = receipt.get("transcript_id")
        identifier = transcript_id if isinstance(transcript_id, str) else None
        return ("cleanup-required", json_path, identifier)

    html_path = run_dir / "transcript.html"
    if not _regular_file(json_path) or not _regular_file(html_path):
        return None
    historical_receipt_path = run_dir / HISTORICAL_RECEIPT_NAME
    if _regular_file(historical_receipt_path):
        receipt = _load_receipt(historical_receipt_path)
        if receipt is not None and not _deletion_confirmed(receipt):
            transcript_id = receipt.get("transcript_id")
            identifier = transcript_id if isinstance(transcript_id, str) else None
            return ("historical-unconfirmed", json_path, identifier)
    return ("historical", json_path, None)


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: transcript_run.py RUN_DIR", file=sys.stderr)
        return 2
    result = classify(Path(sys.argv[1]))
    if result is None:
        return 1
    state, json_path, transcript_id = result
    fields = [state, str(json_path)]
    if transcript_id:
        fields.append(transcript_id)
    print("\t".join(fields))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
