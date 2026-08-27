from __future__ import annotations

import argparse
import html
from dataclasses import dataclass, field
from pathlib import Path

from meeting_identity import meeting_identity
from transcript_bundle import Segment, atomic_write_private, load_canonical


PALETTE = (
    ("#1d4ed8", "#60a5fa"),
    ("#c2410c", "#fb923c"),
    ("#047857", "#34d399"),
    ("#7e22ce", "#c084fc"),
    ("#be123c", "#fb7185"),
    ("#0e7490", "#22d3ee"),
    ("#a16207", "#facc15"),
    ("#4338ca", "#818cf8"),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render a canonical JSON transcript as color-coded HTML."
    )
    parser.add_argument("json_path", type=Path)
    parser.add_argument("html_path", nargs="?", type=Path)
    return parser.parse_args()


def timestamp(value: float) -> str:
    seconds = float(value or 0)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{int(hours):02d}:{int(minutes):02d}:{seconds:06.3f}"


def human_timestamp(value: float) -> str:
    total_seconds = max(0, int(float(value or 0)))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def duration_label(value: float) -> str:
    total_minutes = max(1, round(value / 60))
    hours, minutes = divmod(total_minutes, 60)
    if not hours:
        return f"{minutes} min"
    if not minutes:
        return f"{hours} hr"
    return f"{hours} hr {minutes} min"


@dataclass
class Turn:
    speaker: str
    start: float
    end: float
    texts: list[str] = field(default_factory=list)


def group_turns(segments: list[Segment]) -> list[Turn]:
    turns: list[Turn] = []
    for segment in segments:
        if turns and turns[-1].speaker == segment.speaker:
            turns[-1].end = segment.end
            turns[-1].texts.append(segment.text)
        else:
            turns.append(
                Turn(
                    speaker=segment.speaker,
                    start=segment.start,
                    end=segment.end,
                    texts=[segment.text],
                )
            )
    return turns


def speaker_label(speaker: str, names: dict[str, str]) -> str:
    name = names.get(speaker)
    if not name:
        return f'<span class="speaker-name">{html.escape(speaker)}</span>'
    return (
        f'<span class="speaker-name">{html.escape(name)}</span>'
        f'<span class="speaker-id">{html.escape(speaker)}</span>'
    )


def turn_markup(
    turns: list[Turn],
    names: dict[str, str],
    speaker_styles: dict[str, tuple[str, str]],
) -> str:
    rows: list[str] = []
    for turn in turns:
        light, dark = speaker_styles[turn.speaker]
        style = f"--speaker-light:{light};--speaker-dark:{dark}"
        label = speaker_label(turn.speaker, names)
        time = f"{human_timestamp(turn.start)}–{human_timestamp(turn.end)}"
        copy = html.escape(" ".join(turn.texts))

        rows.append(
            f'<article class="editorial-turn" style="{style}">'
            f'<header class="turn-header"><span class="speaker">{label}</span>'
            f"<time>{time}</time></header><p>{copy}</p></article>"
        )
    return "\n".join(rows)


def render(
    source: Path,
    language: str,
    segments: list[Segment],
    names: dict[str, str],
) -> str:
    turns = group_turns(segments)
    speakers = list(dict.fromkeys(turn.speaker for turn in turns))
    speaker_styles = {
        speaker: PALETTE[index % len(PALETTE)]
        for index, speaker in enumerate(speakers)
    }
    title, recorded_at, run_id = meeting_identity(source)
    duration = max(segment.end for segment in segments)

    legend = "\n".join(
        (
            f'<span class="legend-item" style="--speaker-light:{light};--speaker-dark:{dark}">'
            f'<span class="dot"></span>{speaker_label(speaker, names)}</span>'
        )
        for speaker, (light, dark) in speaker_styles.items()
    )
    editorial = turn_markup(turns, names, speaker_styles)

    return f"""<!doctype html>
<html lang="{html.escape(language)}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)} · Transcript</title>
  <script>
    (() => {{
      try {{
        const saved = localStorage.getItem("meeting-transcriber-theme");
        if (saved === "light" || saved === "dark") document.documentElement.dataset.theme = saved;
      }} catch (_) {{}}
    }})();
  </script>
  <style>
    :root {{
      color-scheme: light;
      --canvas: #f6f7f9;
      --surface: #ffffff;
      --surface-subtle: #f1f3f6;
      --text: #1b2433;
      --muted: #687386;
      --faint: #566273;
      --line: #dfe3e9;
      --line-strong: #cbd1da;
      --focus: #2563eb;
      --shadow: 0 16px 40px rgb(15 23 42 / 7%);
      font-family: Inter, ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    :root[data-theme="dark"] {{
      color-scheme: dark;
      --canvas: #101216;
      --surface: #171a20;
      --surface-subtle: #1d2129;
      --text: #eef1f5;
      --muted: #a8b0bd;
      --faint: #a6afbd;
      --line: #2b3039;
      --line-strong: #3a414d;
      --focus: #60a5fa;
      --shadow: none;
    }}
    @media (prefers-color-scheme: dark) {{
      :root:not([data-theme]) {{
        color-scheme: dark;
        --canvas: #101216;
        --surface: #171a20;
        --surface-subtle: #1d2129;
        --text: #eef1f5;
        --muted: #a8b0bd;
        --faint: #a6afbd;
        --line: #2b3039;
        --line-strong: #3a414d;
        --focus: #60a5fa;
        --shadow: none;
      }}
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; background: var(--canvas); color: var(--text); font-size: 16px; line-height: 1.75; }}
    button {{ font: inherit; }}
    main {{ width: min(960px, calc(100% - 32px)); margin: 0 auto; padding: 48px 0 80px; }}
    .document {{ overflow: hidden; border: 1px solid var(--line); border-radius: 18px; background: var(--surface); box-shadow: var(--shadow); }}
    .document-header {{ padding: clamp(24px, 5vw, 48px); border-bottom: 1px solid var(--line); }}
    .header-row {{ display: flex; align-items: flex-start; justify-content: space-between; gap: 24px; }}
    .eyebrow {{ margin: 0 0 8px; color: var(--muted); font-size: .75rem; font-weight: 750; letter-spacing: .12em; line-height: 1.4; text-transform: uppercase; }}
    h1 {{ max-width: 18ch; margin: 0; font-size: clamp(2rem, 7vw, 3.75rem); font-weight: 720; letter-spacing: -.04em; line-height: 1.03; text-wrap: balance; }}
    .recorded-at {{ margin: 14px 0 0; color: var(--muted); }}
    .theme-toggle {{ min-height: 48px; flex: 0 0 auto; border: 1px solid var(--line-strong); border-radius: 999px; background: transparent; color: var(--text); padding: 9px 15px; font-size: .86rem; font-weight: 700; cursor: pointer; }}
    .theme-toggle:hover {{ background: var(--surface-subtle); }}
    .theme-toggle:focus-visible {{ outline: 3px solid var(--focus); outline-offset: 3px; }}
    .summary {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); margin: 32px 0 0; padding-top: 24px; border-top: 1px solid var(--line); }}
    .summary div {{ min-width: 0; padding-right: 18px; }}
    .summary div + div {{ padding-left: 18px; border-left: 1px solid var(--line); }}
    .summary dt {{ color: var(--muted); font-size: .72rem; font-weight: 700; letter-spacing: .08em; line-height: 1.4; text-transform: uppercase; }}
    .summary dd {{ overflow: hidden; margin: 5px 0 0; font-size: .92rem; font-weight: 670; line-height: 1.4; text-overflow: ellipsis; white-space: nowrap; }}
    .legend {{ display: flex; flex-wrap: wrap; gap: 10px 16px; margin-top: 28px; }}
    .legend-item {{ --speaker: var(--speaker-light); display: inline-flex; align-items: center; gap: 7px; color: var(--muted); font-size: .8rem; font-weight: 680; line-height: 1.4; }}
    .dot {{ width: 8px; height: 8px; flex: 0 0 auto; border-radius: 50%; background: var(--speaker); }}
    .speaker {{ --speaker: var(--speaker-light); color: var(--speaker); font-size: .82rem; font-weight: 780; line-height: 1.4; }}
    .speaker-name {{ color: inherit; }}
    .speaker-id {{ margin-left: 6px; color: var(--faint); font-size: .67rem; font-weight: 650; letter-spacing: .02em; }}
    time {{ color: var(--faint); font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: .72rem; font-variant-numeric: tabular-nums; line-height: 1.4; }}
    .transcript {{ padding: clamp(8px, 2vw, 16px) clamp(24px, 5vw, 48px) clamp(32px, 6vw, 56px); }}
    .turn-header {{ display: flex; flex-wrap: wrap; align-items: baseline; justify-content: space-between; gap: 8px 16px; }}
    .transcript p {{ margin: 7px 0 0; white-space: pre-wrap; }}
    .visually-hidden {{ position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); clip-path: inset(50%); white-space: nowrap; }}

    .editorial-turn {{ --speaker: var(--speaker-light); position: relative; padding: 18px 0 19px 18px; border-bottom: 1px solid var(--line); }}
    .editorial-turn:last-child {{ border-bottom: 0; }}
    .editorial-turn::before {{ position: absolute; top: 21px; bottom: 22px; left: 0; width: 3px; border-radius: 3px; background: var(--speaker); content: ""; }}
    .editorial-turn p {{ max-width: 72ch; font-size: 1.02rem; }}

    .document-footer {{ display: flex; flex-wrap: wrap; justify-content: space-between; gap: 8px 24px; border-top: 1px solid var(--line); padding: 18px clamp(24px, 5vw, 48px); color: var(--faint); font-size: .72rem; line-height: 1.5; }}
    @media (prefers-color-scheme: dark) {{
      :root:not([data-theme]) .speaker, :root:not([data-theme]) .legend-item,
      :root:not([data-theme]) .editorial-turn {{ --speaker: var(--speaker-dark); }}
    }}
    :root[data-theme="dark"] .speaker, :root[data-theme="dark"] .legend-item,
    :root[data-theme="dark"] .editorial-turn {{ --speaker: var(--speaker-dark); }}

    @media (max-width: 680px) {{
      main {{ width: 100%; padding: 0; }}
      .document {{ border: 0; border-radius: 0; box-shadow: none; }}
      .document-header {{ padding: 28px 20px 24px; }}
      .header-row {{ flex-direction: column-reverse; gap: 22px; }}
      .theme-toggle {{ align-self: flex-end; min-height: 44px; }}
      h1 {{ font-size: clamp(2rem, 12vw, 3rem); }}
      .summary {{ grid-template-columns: repeat(2, minmax(0, 1fr)); row-gap: 18px; }}
      .summary div:nth-child(3) {{ padding-left: 0; border-left: 0; }}
      .summary div:nth-child(n + 3) {{ padding-top: 18px; border-top: 1px solid var(--line); }}
      .transcript {{ padding: 6px 20px 36px; }}
      .editorial-turn {{ padding: 17px 0 18px 14px; }}
      .speaker-id {{ display: block; margin: 2px 0 0; }}
      .document-footer {{ padding: 16px 20px; }}
    }}
    @media print {{
      :root, :root[data-theme="dark"] {{ color-scheme: light; --canvas: #fff; --surface: #fff; --surface-subtle: #fff; --text: #111827; --muted: #4b5563; --faint: #6b7280; --line: #d1d5db; --line-strong: #9ca3af; --shadow: none; }}
      body {{ background: #fff; }}
      main {{ width: 100%; margin: 0; padding: 0; }}
      .document {{ border: 0; box-shadow: none; }}
      .theme-toggle {{ display: none; }}
      :root[data-theme="dark"] .speaker, :root[data-theme="dark"] .legend-item,
      :root[data-theme="dark"] .editorial-turn, :root:not([data-theme]) .speaker,
      :root:not([data-theme]) .legend-item, :root:not([data-theme]) .editorial-turn {{ --speaker: var(--speaker-light); }}
      .document-header, .transcript {{ padding-right: 0; padding-left: 0; }}
      article {{ break-inside: avoid; }}
    }}
  </style>
</head>
<body>
  <main>
    <article class="document">
      <header class="document-header">
        <div class="header-row">
          <div>
            <p class="eyebrow">Meeting transcript</p>
            <h1>{html.escape(title)}</h1>
            <p class="recorded-at">Recorded {html.escape(recorded_at)}</p>
          </div>
          <button class="theme-toggle" id="theme-toggle" type="button" aria-label="Switch to dark mode" aria-pressed="false">Dark mode</button>
        </div>
        <dl class="summary">
          <div><dt>Duration</dt><dd>{duration_label(duration)}</dd></div>
          <div><dt>Speakers</dt><dd>{len(speakers)}</dd></div>
          <div><dt>Turns</dt><dd>{len(turns)}</dd></div>
          <div><dt>Language</dt><dd>{html.escape(language.upper())}</dd></div>
        </dl>
        <div class="legend" role="group" aria-label="Speaker color legend">{legend}</div>
      </header>
      <div class="transcript editorial">
        <h2 class="visually-hidden">Transcript</h2>
        {editorial}
      </div>
      <footer class="document-footer">
        <span>Generated by Meeting Transcriber</span>
        <span>Transcript run: {html.escape(run_id)}</span>
      </footer>
    </article>
  </main>
  <script>
    (() => {{
      const root = document.documentElement;
      const button = document.getElementById("theme-toggle");
      const systemDark = () => window.matchMedia("(prefers-color-scheme: dark)").matches;
      const current = () => root.dataset.theme || (systemDark() ? "dark" : "light");
      const updateLabel = () => {{
        const dark = current() === "dark";
        button.textContent = dark ? "Light mode" : "Dark mode";
        button.setAttribute("aria-label", dark ? "Switch to light mode" : "Switch to dark mode");
        button.setAttribute("aria-pressed", String(dark));
      }};
      button.addEventListener("click", () => {{
        const next = current() === "dark" ? "light" : "dark";
        root.dataset.theme = next;
        try {{ localStorage.setItem("meeting-transcriber-theme", next); }} catch (_) {{}}
        updateLabel();
      }});
      window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", updateLabel);
      updateLabel();
    }})();
  </script>
</body>
</html>
"""


def render_html(source: Path, destination: Path | None = None) -> Path:
    """Render the saved canonical transcript to HTML atomically."""
    transcript = load_canonical(source)
    target = destination if destination is not None else source.with_suffix(".html")
    rendered = render(
        source, transcript.language, transcript.segments, transcript.speaker_names
    )
    atomic_write_private(target, rendered)
    return target


def main() -> None:
    args = parse_args()
    source = args.json_path.expanduser().resolve()
    destination = args.html_path.expanduser().resolve() if args.html_path else None
    print(render_html(source, destination))


if __name__ == "__main__":
    main()
