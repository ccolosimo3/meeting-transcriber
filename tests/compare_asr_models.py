#!/usr/bin/env python3
"""Compare the current CPU ASR path with Apple-MLX Whisper models."""

from __future__ import annotations

import argparse
import difflib
import json
import os
from pathlib import Path
import platform
import shlex
import statistics
import subprocess
import time
import wave

from faster_whisper.utils import download_model
from huggingface_hub import snapshot_download
import mlx.core as mx


CPU_MODELS = {
    "cpu-small": "small",
    "cpu-turbo": "turbo",
    "cpu-distil-large-v3": "distil-large-v3",
}


MLX_MODELS = {
    "mlx-small": "mlx-community/whisper-small-mlx",
    "mlx-turbo": "mlx-community/whisper-large-v3-turbo",
    "mlx-distil-large-v3": "mlx-community/distil-whisper-large-v3",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a repeatable local CPU-vs-MLX Whisper comparison."
    )
    parser.add_argument("--input", required=True, type=Path, help="WAV recording")
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--repetitions", type=int, default=2)
    return parser.parse_args()


def audio_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as recording:
        return recording.getnframes() / recording.getframerate()


def private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=False, mode=0o700)


def peak_rss_kb(pid: int) -> int:
    result = subprocess.run(
        ["ps", "-o", "rss=", "-p", str(pid)],
        check=False,
        capture_output=True,
        text=True,
    )
    try:
        return int(result.stdout.strip())
    except ValueError:
        return 0


def run_process(
    command: list[str], run_dir: Path, environment: dict[str, str]
) -> tuple[float, float]:
    log_path = run_dir / "run.log"
    started = time.perf_counter()
    peak_kb = 0
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            command,
            stdout=log,
            stderr=subprocess.STDOUT,
            env=environment,
            text=True,
        )
        while process.poll() is None:
            peak_kb = max(peak_kb, peak_rss_kb(process.pid))
            time.sleep(0.1)
        peak_kb = max(peak_kb, peak_rss_kb(process.pid))
    elapsed = time.perf_counter() - started
    if process.returncode != 0:
        raise RuntimeError(f"command failed ({process.returncode}); see {log_path}")
    return elapsed, peak_kb / 1024


def locate_json(run_dir: Path, source_stem: str) -> Path:
    for candidate in (run_dir / "transcript.json", run_dir / f"{source_stem}.json"):
        if candidate.is_file():
            return candidate
    candidates = list(run_dir.glob("*.json"))
    if len(candidates) == 1:
        return candidates[0]
    raise RuntimeError(f"could not identify transcript JSON under {run_dir}")


def transcript_text(path: Path) -> str:
    payload = json.loads(path.read_text(encoding="utf-8"))
    segments = payload.get("segments")
    if not isinstance(segments, list):
        raise RuntimeError(f"transcript has no segments: {path}")
    return " ".join(
        str(segment.get("text", "")).strip()
        for segment in segments
        if isinstance(segment, dict) and str(segment.get("text", "")).strip()
    ).strip()


def main() -> int:
    args = parse_args()
    if args.repetitions < 1:
        raise SystemExit("--repetitions must be at least 1")
    input_path = args.input.expanduser().resolve()
    if not input_path.is_file():
        raise SystemExit(f"input does not exist: {input_path}")

    os.umask(0o077)
    project_root = Path(__file__).resolve().parents[1]
    whisperx = project_root / ".venv/bin/whisperx"
    mlx_whisper = project_root / ".venv/bin/mlx_whisper"
    for executable in (whisperx, mlx_whisper):
        if not executable.is_file():
            raise SystemExit(f"missing executable: {executable}")

    output_root = args.output_root.expanduser().resolve()
    private_directory(output_root)
    duration = audio_duration(input_path)
    mlx_device = str(mx.default_device())
    if "gpu" not in mlx_device.lower():
        raise SystemExit(f"MLX is not using the Apple GPU: {mlx_device}")

    ffmpeg_prefix = subprocess.check_output(
        ["brew", "--prefix", "ffmpeg@7"], text=True
    ).strip()
    environment = os.environ.copy()
    environment["PATH"] = f"{ffmpeg_prefix}/bin:{environment['PATH']}"
    existing_dyld = environment.get("DYLD_LIBRARY_PATH")
    environment["DYLD_LIBRARY_PATH"] = (
        f"{ffmpeg_prefix}/lib:{existing_dyld}"
        if existing_dyld
        else f"{ffmpeg_prefix}/lib"
    )
    environment["PYANNOTE_METRICS_ENABLED"] = "0"

    print(
        "Prefetching public CPU model weights (download time is not benchmarked):",
        flush=True,
    )
    for label, model in CPU_MODELS.items():
        print(f"  {label}: {model}", flush=True)
        download_model(model)

    print(
        "Prefetching public MLX model weights (download time is not benchmarked):",
        flush=True,
    )
    for label, repository in MLX_MODELS.items():
        print(f"  {label}: {repository}", flush=True)
        snapshot_download(repo_id=repository)

    candidates: list[tuple[str, list[str]]] = []
    for label, model in CPU_MODELS.items():
        candidates.append(
            (
                label,
                [
                    str(whisperx),
                    str(input_path),
                    "--model",
                    model,
                    "--language",
                    "en",
                    "--device",
                    "cpu",
                    "--compute_type",
                    "int8",
                    "--batch_size",
                    "8",
                    "--condition_on_previous_text",
                    "False",
                    "--output_format",
                    "json",
                ],
            )
        )
    for label, repository in MLX_MODELS.items():
        candidates.append(
            (
                label,
                [
                    str(mlx_whisper),
                    str(input_path),
                    "--model",
                    repository,
                    "--language",
                    "en",
                    "--condition-on-previous-text",
                    "False",
                    "--word-timestamps",
                    "True",
                    "--verbose",
                    "False",
                    "--output-format",
                    "json",
                    "--output-name",
                    "transcript",
                ],
            )
        )

    source_stem = input_path.stem
    results: list[dict[str, object]] = []
    representative_text: dict[str, str] = {}
    for label, base_command in candidates:
        print(f"\n{label}", flush=True)
        for repetition in range(1, args.repetitions + 1):
            run_dir = output_root / f"{label}-run-{repetition}"
            private_directory(run_dir)
            command = list(base_command)
            if label.startswith("cpu-"):
                command.extend(["--output_dir", str(run_dir)])
            else:
                command.extend(["--output-dir", str(run_dir)])
            (run_dir / "command.txt").write_text(
                shlex.join(command) + "\n", encoding="utf-8"
            )
            elapsed, peak_mb = run_process(command, run_dir, environment)
            json_path = locate_json(run_dir, source_stem)
            text = transcript_text(json_path)
            (run_dir / "transcript.txt").write_text(text + "\n", encoding="utf-8")
            if repetition == 1:
                representative_text[label] = text
            result = {
                "candidate": label,
                "repetition": repetition,
                "elapsed_seconds": round(elapsed, 3),
                "real_time_factor": round(elapsed / duration, 4),
                "peak_rss_mb": round(peak_mb, 1),
                "word_count": len(text.split()),
                "json": str(json_path),
            }
            results.append(result)
            print(
                f"  run {repetition}: {elapsed:.2f}s, "
                f"RTF {elapsed / duration:.3f}, peak RSS {peak_mb:.0f} MB, "
                f"{len(text.split())} words",
                flush=True,
            )

    baseline = representative_text["cpu-small"].split()
    for label, text in representative_text.items():
        if label == "cpu-small":
            continue
        diff = difflib.unified_diff(
            baseline,
            text.split(),
            fromfile="cpu-small",
            tofile=label,
            lineterm="",
        )
        (output_root / f"cpu-small-vs-{label}.diff").write_text(
            "\n".join(diff) + "\n", encoding="utf-8"
        )

    receipt = {
        "input": str(input_path),
        "audio_duration_seconds": round(duration, 3),
        "machine": {
            "platform": platform.platform(),
            "architecture": platform.machine(),
            "mlx_device": mlx_device,
        },
        "repetitions": args.repetitions,
        "results": results,
    }
    (output_root / "receipt.json").write_text(
        json.dumps(receipt, indent=2) + "\n", encoding="utf-8"
    )

    lines = [
        "# ASR model comparison",
        "",
        f"Audio: `{input_path}` ({duration:.1f}s)",
        "",
        f"MLX device: `{mlx_device}`",
        "",
        "Model download time is excluded. RTF is elapsed time divided by audio duration; lower is faster.",
        "",
        "| Candidate | Mean seconds | Mean RTF | Max peak RSS | Words |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for label, _ in candidates:
        selected = [item for item in results if item["candidate"] == label]
        lines.append(
            f"| {label} | "
            f"{statistics.mean(float(item['elapsed_seconds']) for item in selected):.2f} | "
            f"{statistics.mean(float(item['real_time_factor']) for item in selected):.3f} | "
            f"{max(float(item['peak_rss_mb']) for item in selected):.0f} MB | "
            f"{selected[0]['word_count']} |"
        )
    lines.extend(
        [
            "",
            "Use each candidate's first-run `transcript.txt` and the generated diffs for human quality review.",
            "",
        ]
    )
    (output_root / "README.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"\nReceipt: {output_root / 'README.md'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
