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
    # Wall-clock of the last poll at which the *run* showed forward progress —
    # any non-ignored item in_progress (level) or any non-ignored started/
    # concluded event (edge). Used to gate the stall verdict on whole-run
    # quiescence rather than on a single check's not-started timer. None until
    # the first update_state call.
    last_active: float | None = None


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
    ignore_rules: list[IgnoreRule] | None = None,
) -> tuple[WatchState, list[Event], bool]:
    """Diff state against new_items and return (new_state, events, force_pushed).

    On force-push: rebuild state from new_items, reset all timers, emit no events.
    Otherwise: emit Events for transitions and update timers.
    """
    # Force-push detection: any time we observe a new, real SHA that differs
    # from what we previously had (including the None we had before we knew
    # any SHA), treat it as a baseline reset. We require new_head_sha to be
    # non-None so we don't flag a transient null tick as a force-push.
    force_pushed = (
        new_head_sha is not None
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
        # Anchor the quiescence baseline at the force-push instant, so the
        # post-push idle window is measured from here, not from the next poll.
        # (Instant staleness is already prevented by the timer reseed above and
        # by the loop skipping exit-evaluation on the force-push tick.)
        next_state.last_active = now
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

    # Run-level liveness: the run is "active" this poll if any non-ignored item
    # is currently in_progress (level signal — a slow build emits no events for
    # its whole duration, so the level read is what keeps it alive) OR any
    # non-ignored transition fired this poll (edge signal — an upstream
    # concluding is progress even though nothing is in_progress for that
    # instant). --ignore removes a job from all consideration, liveness
    # included, so the same ignore-filtered view drives this gate and the
    # stalled set. Carry the timestamp forward across quiescent polls; seed it
    # on the first poll so quiescence is measured from when we started watching.
    #
    # Note: a check that flaps out of and back into the rollup reappears as a
    # fresh item and re-emits a started/concluded event, counted as activity
    # here. That can only delay a stall verdict, never manufacture a false one,
    # so it is safe.
    rules = ignore_rules or []
    live_items = [it for it in new_items if not ignore_matches(it, rules)]
    live_events = [e for e in events if not ignore_matches(e.item, rules)]
    run_active = (
        any(it.status == "in_progress" for it in live_items) or bool(live_events)
    )
    if run_active:
        next_state.last_active = now
    else:
        next_state.last_active = (
            state.last_active if state.last_active is not None else now
        )

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
    """Return required checks that are stalled: not-started past the timer AND
    the whole run quiescent for the timeout.

    Gated on whole-run quiescence: while the run is making progress (any job
    in_progress, or a state change within the timeout window) a not-started
    required check is legitimately waiting (e.g. blocked on a needs: upstream),
    not stalled. Only once the run has been quiescent for the full timeout do
    we escalate.
    """
    if state.last_active is None or now - state.last_active <= stalled_timeout:
        return []
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
