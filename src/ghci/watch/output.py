from __future__ import annotations

from datetime import datetime

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


def _fmt_time(when: datetime) -> str:
    return when.strftime("%H:%M:%S")


def format_event_line(event: Event, *, when: datetime) -> str:
    ts = _fmt_time(when)
    if event.kind == "started":
        return f'{ts}  Job "{event.name}" started'
    if event.kind == "concluded":
        word = _CONCLUSION_WORDS.get(event.conclusion or "", event.conclusion or "?")
        return f'{ts}  Job "{event.name}" {word}'
    return f'{ts}  Job "{event.name}" {event.kind}'


def format_force_push_line(*, when: datetime, new_sha: str) -> str:
    short = new_sha[:7] if new_sha else "?"
    return f"{_fmt_time(when)}  Force-push detected — now watching SHA {short}"


def format_resolution_line(text: str) -> str:
    # No-op for now; provided as a seam in case future work wants to enrich it.
    return text
