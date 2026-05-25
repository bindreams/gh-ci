from __future__ import annotations

from datetime import datetime

from ghci.colors import GROUP_STYLE_COLOR, Palette, _GroupStyle, ensure_palette
from ghci.watch.state import Event


_CONCLUSION_WORDS = {
    "success": "green",
    "failure": "failed",
    "cancelled": "cancelled",
    "timed_out": "timed out",
    "action_required": "action required",
    "startup_failure": "startup failure",
    "skipped": "skipped",
    "neutral": "neutral",
    "stale": "stale",
}

# Parallel to _CONCLUSION_WORDS — drives the color of the trailing conclusion
# word on event lines. Same _GroupStyle the summary uses, so the two surfaces
# never drift. A parity test asserts the key sets are equal.
_CONCLUSION_STYLE: dict[str, _GroupStyle] = {
    "success": _GroupStyle.PASSED,
    "failure": _GroupStyle.FAILED,
    "cancelled": _GroupStyle.FAILED,
    "timed_out": _GroupStyle.FAILED,
    "action_required": _GroupStyle.FAILED,
    "startup_failure": _GroupStyle.FAILED,
    "skipped": _GroupStyle.NEUTRAL,
    "neutral": _GroupStyle.NEUTRAL,
    "stale": _GroupStyle.NEUTRAL,
}


def _fmt_time(when: datetime) -> str:
    return when.strftime("%H:%M:%S")


def format_event_line(
    event: Event,
    *,
    when: datetime,
    palette: Palette | None = None,
) -> str:
    p = ensure_palette(palette)
    ts = p.style(_fmt_time(when), dim=True)
    if event.kind == "started":
        return f'{ts}  Job "{event.name}" started'
    if event.kind == "concluded":
        raw = event.conclusion or ""
        word = _CONCLUSION_WORDS.get(raw, raw or "?")
        # Unknown conclusion strings fall back to the same yellow we use
        # for the "Unknown" group in summaries — keeps the two surfaces
        # visually consistent.
        style = _CONCLUSION_STYLE.get(raw, _GroupStyle.UNKNOWN)
        if style == _GroupStyle.NEUTRAL:
            colored_word = p.style(word, dim=True)
        else:
            color = GROUP_STYLE_COLOR[style]
            colored_word = p.style(word, color=color)
        return f'{ts}  Job "{event.name}" {colored_word}'
    return f'{ts}  Job "{event.name}" {event.kind}'


def format_force_push_line(
    *,
    when: datetime,
    new_sha: str,
    palette: Palette | None = None,
) -> str:
    p = ensure_palette(palette)
    ts = p.style(_fmt_time(when), dim=True)
    short = new_sha[:7] if new_sha else "?"
    phrase = p.style("Force-push detected", color="yellow")
    return f"{ts}  {phrase} — now watching SHA {short}"


def format_resolution_line(text: str) -> str:
    # No-op for now; provided as a seam in case future work wants to enrich it.
    return text
