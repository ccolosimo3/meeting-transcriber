#!/usr/bin/env python3
"""Render a small terminal status display from FFmpeg audio metadata."""

from __future__ import annotations

import argparse
from collections import deque
import math
import os
import re
import sys
from typing import Iterable, TextIO


BLOCKS = "▁▂▃▄▅▆▇█"
WAVEFORM_WIDTH = 24
TIME_PATTERN = re.compile(r"\bpts_time:([-+0-9.eE]+)")
LEVEL_PREFIX = "lavfi.astats.Overall.Peak_level="


def format_elapsed(seconds: float) -> str:
    total_seconds = max(0, int(seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def level_to_block(level: float) -> str:
    if not math.isfinite(level):
        return BLOCKS[0]
    normalized = (min(max(level, -60.0), -12.0) + 60.0) / 48.0
    index = min(len(BLOCKS) - 1, int(normalized * len(BLOCKS)))
    return BLOCKS[index]


def level_label(level: float) -> str:
    if not math.isfinite(level):
        return "-∞ dBFS"
    return f"{level:>4.0f} dBFS"


def render_lines(
    microphone: str,
    elapsed: float,
    levels: Iterable[float],
    *,
    color: bool,
) -> tuple[str, str]:
    level_values = list(levels)[-WAVEFORM_WIDTH:]
    padded_levels = [float("-inf")] * (WAVEFORM_WIDTH - len(level_values)) + level_values
    waveform = "".join(level_to_block(level) for level in padded_levels)
    latest_level = level_values[-1] if level_values else float("-inf")

    if color:
        recording_mark = "\033[31m●\033[0m"
        waveform_color = "\033[33m" if latest_level >= -6.0 else "\033[32m"
        waveform = f"{waveform_color}{waveform}\033[0m"
    else:
        recording_mark = "●"

    first_line = (
        f"{recording_mark} RECORDING · {format_elapsed(elapsed)} · {microphone}"
    )
    second_line = f"  {waveform}   {level_label(latest_level)}   q stop"
    return first_line, second_line


class TerminalDisplay:
    def __init__(self, output: TextIO) -> None:
        self.output = output
        self.rendered = False

    def draw(self, first_line: str, second_line: str) -> None:
        if self.rendered:
            self.output.write("\r\033[2K\033[1A\r\033[2K")
        self.output.write(f"{first_line}\n{second_line}")
        self.output.flush()
        self.rendered = True

    def close(self) -> None:
        if self.rendered:
            self.output.write("\n")
            self.output.flush()


def run(metadata: Iterable[str], microphone: str, output: TextIO) -> None:
    color = output.isatty() and os.environ.get("NO_COLOR") is None
    display = TerminalDisplay(output)
    levels: deque[float] = deque(maxlen=WAVEFORM_WIDTH)
    elapsed = 0.0
    last_sample_time = -1.0
    pending_time = 0.0

    display.draw(*render_lines(microphone, elapsed, levels, color=color))
    try:
        for line in metadata:
            time_match = TIME_PATTERN.search(line)
            if time_match:
                pending_time = float(time_match.group(1))
                elapsed = max(elapsed, pending_time)
                continue
            if not line.startswith(LEVEL_PREFIX):
                continue
            try:
                level = float(line.removeprefix(LEVEL_PREFIX).strip())
            except ValueError:
                continue
            if last_sample_time >= 0 and pending_time - last_sample_time < 0.125:
                continue
            levels.append(level)
            last_sample_time = pending_time
            display.draw(*render_lines(microphone, elapsed, levels, color=color))
    finally:
        display.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--microphone", required=True)
    args = parser.parse_args()
    try:
        run(sys.stdin, args.microphone, sys.stdout)
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
