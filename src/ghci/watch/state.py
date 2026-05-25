from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from ghci.checks import CheckItem
from ghci.ignore import IgnoreRule, matches as ignore_matches


NOT_STARTED_STATUSES = frozenset(
    {"queued", "pending", "waiting", "requested", "expected"}
)


CheckKey = tuple[str, str, str | None]  # (kind, name, workflow_name)


def _key(item: CheckItem) -> CheckKey:
    return (item.kind, item.name, item.workflow_name)


@dataclass
class Event:
    kind: str  # "started" | "concluded"
    name: str
    item: CheckItem
    conclusion: str | None = None  # only set for "concluded"


@dataclass
class WatchState:
    items_by_key: dict[CheckKey, CheckItem] = field(default_factory=dict)
    timer_starts: dict[CheckKey, float] = field(default_factory=dict)
    head_sha: str | None = None


def initial_state(items: Iterable[CheckItem], *, head_sha: str | None = None) -> WatchState:
    state = WatchState(head_sha=head_sha)
    for it in items:
        state.items_by_key[_key(it)] = it
    return state


def update_state(
    state: WatchState,
    new_items: list[CheckItem],
    *,
    now: float,
    new_head_sha: str | None = None,
) -> tuple[WatchState, list[Event], bool]:
    """Diff state against new_items and return (new_state, events, force_pushed).

    On force-push: rebuild state from new_items, reset all timers, emit no events.
    Otherwise: emit Events for transitions and update timers.
    """
    force_pushed = (
        new_head_sha is not None
        and state.head_sha is not None
        and new_head_sha != state.head_sha
    )
    next_state = WatchState(head_sha=new_head_sha if new_head_sha is not None else state.head_sha)

    if force_pushed:
        for it in new_items:
            next_state.items_by_key[_key(it)] = it
        # Re-seed timers based on the new SHA's snapshot.
        for it in new_items:
            if _is_not_started(it):
                next_state.timer_starts[_key(it)] = now
        return next_state, [], True

    events: list[Event] = []
    new_keys = {_key(it) for it in new_items}

    for it in new_items:
        key = _key(it)
        prev = state.items_by_key.get(key)
        # Emit transitions.
        if prev is None:
            # New check. Emit "started" if it appears already in progress,
            # or "concluded" if it appears already terminal.
            if it.status == "completed" and it.conclusion is not None:
                events.append(Event("concluded", it.name, it, conclusion=it.conclusion))
            elif it.status == "in_progress":
                events.append(Event("started", it.name, it))
        else:
            if it.status == "in_progress" and prev.status != "in_progress":
                events.append(Event("started", it.name, it))
            if (
                it.status == "completed"
                and it.conclusion is not None
                and (prev.status != "completed" or prev.conclusion != it.conclusion)
            ):
                events.append(Event("concluded", it.name, it, conclusion=it.conclusion))
        next_state.items_by_key[key] = it

    # Update timers.
    for it in new_items:
        key = _key(it)
        prev = state.items_by_key.get(key)
        if _is_not_started(it):
            # Continue or start the timer.
            if prev is not None and key in state.timer_starts and prev.status == it.status:
                # Same not-started status — carry timer forward.
                next_state.timer_starts[key] = state.timer_starts[key]
            else:
                # First poll in this not-started state, or status changed.
                next_state.timer_starts[key] = now
        # If item is no longer not-started, the timer simply doesn't carry forward
        # (omitted from next_state.timer_starts).

    # Drop timers for checks that disappeared entirely.
    for key in list(state.timer_starts):
        if key not in new_keys:
            next_state.timer_starts.pop(key, None)

    return next_state, events, False


def _is_not_started(item: CheckItem) -> bool:
    return item.status in NOT_STARTED_STATUSES


def stalled_required_keys(
    state: WatchState,
    *,
    now: float,
    stalled_timeout: float,
    ignore_rules: list[IgnoreRule],
) -> list[CheckItem]:
    """Return required checks whose stalled timer has exceeded stalled_timeout."""
    stalled: list[CheckItem] = []
    for key, start in state.timer_starts.items():
        item = state.items_by_key.get(key)
        if item is None or not item.required:
            continue
        if ignore_matches(item, ignore_rules):
            continue
        if now - start > stalled_timeout:
            stalled.append(item)
    return stalled
