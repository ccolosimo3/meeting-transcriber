"""Canonical transcript type, validation boundary, and atomic publication."""

from __future__ import annotations

import json
import math
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class CanonicalTranscriptError(ValueError):
    """Raised when a payload is not a valid canonical transcript."""


@dataclass
class TimedWord:
    word: str
    start: float
    end: float
    probability: float
    speaker: str


@dataclass
class Segment:
    id: int
    start: float
    end: float
    text: str
    speaker: str
    words: list[TimedWord] = field(default_factory=list)


@dataclass
class CanonicalTranscript:
    language: str
    text: str
    segments: list[Segment]
    speaker_names: dict[str, str] = field(default_factory=dict)

    def speaker_labels(self) -> list[str]:
        """Segment speaker labels in first-appearance order."""
        ordered: list[str] = []
        for segment in self.segments:
            if segment.speaker not in ordered:
                ordered.append(segment.speaker)
        return ordered

    def all_speaker_labels(self) -> set[str]:
        labels = set(self.speaker_labels())
        for segment in self.segments:
            for word in segment.words:
                labels.add(word.speaker)
        return labels


def _require_nonempty_string(value: Any, description: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CanonicalTranscriptError(f"transcript has invalid {description}")
    return value


def _require_number(value: Any, description: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CanonicalTranscriptError(f"transcript has invalid {description}")
    number = float(value)
    if not math.isfinite(number):
        raise CanonicalTranscriptError(f"transcript has invalid {description}")
    return number


def _parse_word(value: Any) -> TimedWord:
    if not isinstance(value, dict):
        raise CanonicalTranscriptError("transcript has an invalid timed word")
    word = value.get("word")
    if not isinstance(word, str) or not word:
        raise CanonicalTranscriptError("transcript has a timed word without text")
    start = _require_number(value.get("start"), "word start")
    end = _require_number(value.get("end"), "word end")
    probability = _require_number(value.get("probability"), "word probability")
    if start < 0 or end < start:
        raise CanonicalTranscriptError("transcript has invalid word timestamps")
    if not 0 <= probability <= 1:
        raise CanonicalTranscriptError("transcript has an out-of-range word probability")
    speaker = value.get("speaker")
    if not isinstance(speaker, str) or not speaker:
        raise CanonicalTranscriptError("transcript has a timed word without a speaker")
    return TimedWord(
        word=word, start=start, end=end, probability=probability, speaker=speaker
    )


def _parse_segment(value: Any) -> Segment:
    if not isinstance(value, dict):
        raise CanonicalTranscriptError("transcript has an invalid segment")
    identifier = value.get("id")
    if isinstance(identifier, bool) or not isinstance(identifier, int):
        raise CanonicalTranscriptError("transcript has an invalid segment id")
    start = _require_number(value.get("start"), "segment start")
    end = _require_number(value.get("end"), "segment end")
    if start < 0 or end < start:
        raise CanonicalTranscriptError("transcript has invalid segment timestamps")
    text = _require_nonempty_string(value.get("text"), "segment text")
    speaker = _require_nonempty_string(value.get("speaker"), "segment speaker")
    words_value = value.get("words")
    if not isinstance(words_value, list):
        raise CanonicalTranscriptError("transcript segment has no word list")
    return Segment(
        id=identifier,
        start=start,
        end=end,
        text=text,
        speaker=speaker,
        words=[_parse_word(word) for word in words_value],
    )


def parse_canonical(
    payload: object, *, require_speaker_names: bool = True
) -> CanonicalTranscript:
    """Narrow an unknown JSON payload into the named canonical transcript type."""
    if not isinstance(payload, dict):
        raise CanonicalTranscriptError("transcript JSON must contain an object")
    language = _require_nonempty_string(payload.get("language"), "language")
    text = _require_nonempty_string(payload.get("text"), "text")
    segments_value = payload.get("segments")
    if not isinstance(segments_value, list) or not segments_value:
        raise CanonicalTranscriptError("transcript has no segments")
    segments = [_parse_segment(segment) for segment in segments_value]
    if not any(segment.words for segment in segments):
        raise CanonicalTranscriptError("transcript has no timed words")

    names_value = payload.get("speaker_names")
    if names_value is None:
        if require_speaker_names:
            raise CanonicalTranscriptError("transcript has no speaker_names object")
        names_value = {}
    if not isinstance(names_value, dict):
        raise CanonicalTranscriptError("transcript speaker_names must be an object")
    transcript = CanonicalTranscript(
        language=language, text=text, segments=segments, speaker_names={}
    )
    known_labels = transcript.all_speaker_labels()
    speaker_names: dict[str, str] = {}
    for label, name in names_value.items():
        if not isinstance(label, str) or label not in known_labels:
            raise CanonicalTranscriptError(
                f"speaker_names has a label that does not occur in the transcript: {label!r}"
            )
        if not isinstance(name, str) or not name.strip() or name != name.strip():
            raise CanonicalTranscriptError(
                f"speaker_names has an invalid display name for {label}"
            )
        speaker_names[label] = name
    transcript.speaker_names = speaker_names
    return transcript


def canonical_to_json(transcript: CanonicalTranscript) -> dict[str, Any]:
    return {
        "language": transcript.language,
        "text": transcript.text,
        "segments": [
            {
                "id": segment.id,
                "start": segment.start,
                "end": segment.end,
                "text": segment.text,
                "speaker": segment.speaker,
                "words": [
                    {
                        "word": word.word,
                        "start": word.start,
                        "end": word.end,
                        "probability": word.probability,
                        "speaker": word.speaker,
                    }
                    for word in segment.words
                ],
            }
            for segment in transcript.segments
        ],
        "speaker_names": dict(transcript.speaker_names),
    }


def atomic_write_private(path: Path, content: str) -> None:
    """Atomically replace path with private 0600 content."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        path.chmod(0o600)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def load_canonical(path: Path, *, require_speaker_names: bool = True) -> CanonicalTranscript:
    with path.open(encoding="utf-8") as stream:
        payload: object = json.load(stream)
    return parse_canonical(payload, require_speaker_names=require_speaker_names)


def save_canonical(path: Path, transcript: CanonicalTranscript) -> CanonicalTranscript:
    """Atomically publish canonical JSON, then reload and verify the round trip."""
    atomic_write_private(
        path, json.dumps(canonical_to_json(transcript), ensure_ascii=False)
    )
    reloaded = load_canonical(path)
    if canonical_to_json(reloaded) != canonical_to_json(transcript):
        raise CanonicalTranscriptError(
            f"canonical transcript did not reload identically: {path}"
        )
    return reloaded
