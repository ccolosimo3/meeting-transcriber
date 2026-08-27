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
import re
import sys
from pathlib import Path

from transcript_bundle import CanonicalTranscriptError, load_canonical


RECEIPT_NAME = ".assemblyai.json"
HISTORICAL_RECEIPT_NAME = "transcript.assemblyai.json"
MANAGED_INVENTORY = {"transcript.json", "transcript.md", "transcript.html", RECEIPT_NAME}
MANAGED_MODEL = "universal-3-5-pro"
TRANSCRIPT_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*\Z")


class ManagedRunError(ValueError):
    """A run does not satisfy the current app-managed publication contract."""


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


def validate_managed_run(run_dir: Path) -> tuple[Path, Path, dict[str, object], str]:
    """Validate and return one current-schema published AssemblyAI run."""
    if not run_dir.is_dir() or run_dir.is_symlink():
        raise ManagedRunError("the transcript run directory is not a regular directory")
    try:
        inventory = {path.name for path in run_dir.iterdir()}
    except OSError as error:
        raise ManagedRunError(f"the transcript run is unreadable: {error}") from error
    if inventory != MANAGED_INVENTORY:
        raise ManagedRunError("the transcript run does not have the complete published inventory")
    json_path = run_dir / "transcript.json"
    markdown_path = run_dir / "transcript.md"
    html_path = run_dir / "transcript.html"
    receipt_path = run_dir / RECEIPT_NAME
    if not all(_regular_file(path) for path in (json_path, markdown_path, html_path, receipt_path)):
        raise ManagedRunError("the published run files must be regular non-symlink files")
    if markdown_path.stat().st_size == 0 or html_path.stat().st_size == 0:
        raise ManagedRunError("the published transcript views must be non-empty")
    try:
        load_canonical(json_path)
    except (CanonicalTranscriptError, OSError, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ManagedRunError(f"the canonical transcript is invalid: {error}") from error
    receipt = _load_receipt(receipt_path)
    if receipt is None:
        raise ManagedRunError("the AssemblyAI receipt is unreadable or malformed")
    transcript_id = receipt.get("transcript_id")
    timestamps = receipt.get("timestamps")
    deletion = receipt.get("deletion")
    if receipt.get("provider") != "assemblyai":
        raise ManagedRunError("the receipt provider is not AssemblyAI")
    if receipt.get("state") != "published":
        raise ManagedRunError("this run was never published locally")
    if receipt.get("requested_model") != MANAGED_MODEL or receipt.get("used_model") != MANAGED_MODEL:
        raise ManagedRunError("the receipt model does not match the managed model")
    if receipt.get("provider_status") != "completed":
        raise ManagedRunError("the receipt provider lifecycle is not complete")
    if not isinstance(transcript_id, str) or not TRANSCRIPT_ID_PATTERN.fullmatch(transcript_id):
        raise ManagedRunError("the AssemblyAI receipt has no transcript ID")
    if not isinstance(timestamps, dict) or not all(
        isinstance(timestamps.get(key), str) and bool(timestamps.get(key))
        for key in ("created_at", "submitted_at", "completed_at", "published_at")
    ):
        raise ManagedRunError("the receipt lifecycle timestamps are incomplete")
    if deletion is not None and (
        not isinstance(deletion, dict) or not isinstance(deletion.get("confirmed"), bool)
    ):
        raise ManagedRunError("the receipt deletion state is malformed")
    return json_path, receipt_path, receipt, transcript_id


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
