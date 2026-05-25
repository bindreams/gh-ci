from __future__ import annotations

from ghci.checks import CheckItem
from ghci.ignore import IgnoreRule
from ghci.watch.state import (
    NOT_STARTED_STATUSES,
    Event,
    WatchState,
    initial_state,
    stalled_required_keys,
    update_state,
)


def _item(name, *, status="queued", conclusion=None, kind="actions",
          workflow_name="CI", required=False):
    return CheckItem(
        kind=kind, name=name, workflow_name=workflow_name,
        status=status, conclusion=conclusion, url=None, required=required,
        check_run_id=None, run_id=None, workflow_run_url=None,
    )


# Event diffing =====


def test_initial_state_emits_no_events():
    state = initial_state([_item("a"), _item("b")], head_sha="sha1")
    assert isinstance(state, WatchState)
    assert state.head_sha == "sha1"


def test_update_emits_started_when_transitioning_to_in_progress():
    state = initial_state([_item("a", status="queued")])
    new_items = [_item("a", status="in_progress")]
    state2, events, force_push = update_state(state, new_items, now=10.0)
    assert len(events) == 1
    assert events[0].kind == "started"
    assert events[0].name == "a"
    assert force_push is False


def test_update_emits_concluded_with_verbatim_conclusion():
    state = initial_state([_item("a", status="in_progress")])
    new = [_item("a", status="completed", conclusion="success")]
    _, events, _ = update_state(state, new, now=20.0)
    assert events[0].kind == "concluded"
    assert events[0].name == "a"
    assert events[0].conclusion == "success"


def test_update_emits_concluded_cancelled_verbatim():
    state = initial_state([_item("a", status="in_progress")])
    new = [_item("a", status="completed", conclusion="cancelled")]
    _, events, _ = update_state(state, new, now=20.0)
    assert events[0].conclusion == "cancelled"


def test_update_does_not_emit_for_unchanged_items():
    state = initial_state([_item("a", status="in_progress")])
    new = [_item("a", status="in_progress")]
    _, events, _ = update_state(state, new, now=20.0)
    assert events == []


def test_update_emits_started_for_newly_appearing_in_progress_item():
    state = initial_state([])
    new = [_item("a", status="in_progress")]
    _, events, _ = update_state(state, new, now=20.0)
    assert len(events) == 1
    assert events[0].kind == "started"


def test_update_emits_concluded_for_item_that_appears_already_completed():
    state = initial_state([])
    new = [_item("a", status="completed", conclusion="failure")]
    _, events, _ = update_state(state, new, now=20.0)
    assert len(events) == 1
    assert events[0].kind == "concluded"
    assert events[0].conclusion == "failure"


# Force-push handling =====


def test_force_push_detected_when_head_sha_changes():
    state = initial_state([_item("a", status="in_progress")], head_sha="sha1")
    new = [_item("a", status="completed", conclusion="success")]
    new_state, events, force_push = update_state(state, new, now=10.0, new_head_sha="sha2")
    assert force_push is True
    # No state-change events emitted on the force-push tick — baseline is reset.
    assert events == []
    assert new_state.head_sha == "sha2"


def test_force_push_resets_stalled_timers():
    items = [_item("a", status="expected", required=True, kind="status_context")]
    state = initial_state(items, head_sha="sha1")
    state, _, _ = update_state(state, items, now=0.0)
    # Confirm timer started.
    assert stalled_required_keys(state, now=70.0, stalled_timeout=60.0,
                                 ignore_rules=[])
    # Force-push: timers reset; check appears in same not-started state.
    state, _, force_push = update_state(state, items, now=70.0, new_head_sha="sha2")
    assert force_push is True
    # Immediately after force-push, no stall yet.
    assert not stalled_required_keys(state, now=70.0, stalled_timeout=60.0,
                                     ignore_rules=[])


def test_no_force_push_when_head_sha_unchanged():
    state = initial_state([_item("a", status="queued")], head_sha="sha1")
    _, _, force_push = update_state(state, [_item("a", status="queued")],
                                     now=10.0, new_head_sha="sha1")
    assert force_push is False


# Stalled-required timers =====


def test_required_check_in_expected_state_stalls_after_timeout():
    item = _item("ci/external", kind="status_context", status="expected",
                 required=True, workflow_name=None)
    state = initial_state([item])
    # Timer starts at first update tick.
    state, _, _ = update_state(state, [item], now=0.0)
    # Below the timeout — not stalled.
    assert not stalled_required_keys(state, now=30.0, stalled_timeout=60.0,
                                     ignore_rules=[])
    # Past the timeout — stalled.
    stalled = stalled_required_keys(state, now=61.0, stalled_timeout=60.0,
                                    ignore_rules=[])
    assert len(stalled) == 1


def test_required_check_timer_resets_on_status_change():
    item_pending = _item("x", kind="status_context", status="pending",
                          required=True, workflow_name=None)
    state = initial_state([item_pending])
    state, _, _ = update_state(state, [item_pending], now=0.0)

    # Status moves from pending to in_progress (still a CheckRun-like change)
    item_active = _item("x", kind="status_context", status="completed",
                         conclusion="success", required=True, workflow_name=None)
    state, _, _ = update_state(state, [item_active], now=50.0)
    # Timer cleared, can't be stalled
    assert not stalled_required_keys(state, now=200.0, stalled_timeout=60.0,
                                     ignore_rules=[])


def test_non_required_check_never_stalls():
    item = _item("x", kind="status_context", status="expected",
                 required=False, workflow_name=None)
    state = initial_state([item])
    state, _, _ = update_state(state, [item], now=0.0)
    assert not stalled_required_keys(state, now=1000.0, stalled_timeout=60.0,
                                     ignore_rules=[])


def test_ignored_check_does_not_stall():
    item = _item("flaky", kind="status_context", status="expected",
                 required=True, workflow_name=None)
    state = initial_state([item])
    state, _, _ = update_state(state, [item], now=0.0)
    rules = [IgnoreRule("check", "flaky")]
    assert not stalled_required_keys(state, now=1000.0, stalled_timeout=60.0,
                                     ignore_rules=rules)


def test_timer_persists_across_ticks_if_check_still_not_started():
    item = _item("ci", kind="status_context", status="expected", required=True,
                 workflow_name=None)
    state = initial_state([item])
    state, _, _ = update_state(state, [item], now=0.0)
    # Re-fetch the same state at a later time
    state, _, _ = update_state(state, [item], now=30.0)
    assert not stalled_required_keys(state, now=30.0, stalled_timeout=60.0,
                                     ignore_rules=[])
    state, _, _ = update_state(state, [item], now=61.0)
    assert stalled_required_keys(state, now=61.0, stalled_timeout=60.0,
                                 ignore_rules=[])


def test_not_started_statuses_contains_expected():
    assert "expected" in NOT_STARTED_STATUSES
    assert "queued" in NOT_STARTED_STATUSES
    assert "pending" in NOT_STARTED_STATUSES
    assert "waiting" in NOT_STARTED_STATUSES
    assert "requested" in NOT_STARTED_STATUSES
    assert "completed" not in NOT_STARTED_STATUSES
    assert "in_progress" not in NOT_STARTED_STATUSES
