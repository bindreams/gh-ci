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


def test_force_push_anchors_quiescence_baseline_at_push_instant():
    # The force-push tick anchors the run-quiescence baseline at the push
    # instant, so a check still `expected` on the new SHA stalls at
    # push_time + timeout — without needing a further poll to seed the
    # baseline. (Pins the force-push `last_active` reset.)
    item = _item("ci", kind="status_context", status="expected", required=True,
                 workflow_name=None)
    state = initial_state([item], head_sha="sha1")
    state, _, _ = update_state(state, [item], now=0.0)
    state, _, force_push = update_state(state, [item], now=100.0, new_head_sha="sha2")
    assert force_push is True
    assert state.last_active == 100.0
    # At the boundary: not yet stalled.
    assert not stalled_required_keys(state, now=160.0, stalled_timeout=60.0,
                                     ignore_rules=[])
    # Just past it: stalled, with no intervening poll needed to seed the baseline.
    assert stalled_required_keys(state, now=161.0, stalled_timeout=60.0,
                                 ignore_rules=[])


def test_no_force_push_when_head_sha_unchanged():
    state = initial_state([_item("a", status="queued")], head_sha="sha1")
    _, _, force_push = update_state(state, [_item("a", status="queued")],
                                     now=10.0, new_head_sha="sha1")
    assert force_push is False


def test_force_push_detected_when_prior_head_sha_was_none():
    # Regression: if the initial GraphQL fetch returned headRefOid: None,
    # state.head_sha is None. The first tick that reveals a real SHA must
    # still be detected as a force-push so timers get reset against the
    # correct baseline.
    state = initial_state([_item("a", status="queued")], head_sha=None)
    new_state, events, force_push = update_state(
        state, [_item("a", status="queued")], now=10.0, new_head_sha="sha1",
    )
    assert force_push is True
    assert events == []
    assert new_state.head_sha == "sha1"


def test_no_force_push_when_both_head_shas_are_none():
    # Regression: when we still don't have a SHA at all, we can't claim a
    # force-push — there's nothing to compare against yet.
    state = initial_state([_item("a", status="queued")], head_sha=None)
    new_state, _, force_push = update_state(
        state, [_item("a", status="queued")], now=10.0, new_head_sha=None,
    )
    assert force_push is False
    assert new_state.head_sha is None


def test_transient_null_head_sha_does_not_clobber_known_head_sha():
    # Regression: a tick where new_head_sha is None (e.g. a flaky/empty
    # GraphQL response) must not erase the known state.head_sha or be
    # treated as a force-push.
    state = initial_state([_item("a", status="queued")], head_sha="sha1")
    new_state, _, force_push = update_state(
        state, [_item("a", status="queued")], now=10.0, new_head_sha=None,
    )
    assert force_push is False
    assert new_state.head_sha == "sha1"


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


def test_required_expected_not_stalled_while_another_job_in_progress():
    # The reported false positive at the unit level: a needs:-gated required
    # check sits in `expected` while an unrelated build runs. The run is making
    # progress (build in_progress), so the expected check must NOT be flagged,
    # however far past the per-check timeout we poll.
    def snapshot():
        return [
            _item("Test (darwin)", kind="status_context", status="expected",
                  required=True, workflow_name=None),
            _item("Build (darwin)", status="in_progress", required=False),
        ]

    state = initial_state(snapshot())
    for now in (10.0, 30.0, 60.0, 120.0, 600.0):
        state, _, _ = update_state(state, snapshot(), now=now)
        assert not stalled_required_keys(
            state, now=now, stalled_timeout=60.0, ignore_rules=[]
        )


def test_run_quiescence_gate_releases_after_activity_stops():
    # Once the run goes quiescent (build concluded, nothing in_progress and no
    # events), the expected required check stalls — but only after the *run*
    # has been quiescent for the timeout, measured from the last activity, not
    # from when the check first appeared.
    def snapshot(build_status, build_concl=None):
        return [
            _item("Test", kind="status_context", status="expected",
                  required=True, workflow_name=None),
            _item("Build", status=build_status, conclusion=build_concl,
                  required=False),
        ]

    state = initial_state(snapshot("in_progress"))
    state, _, _ = update_state(state, snapshot("in_progress"), now=10.0)  # active: level
    state, events, _ = update_state(
        state, snapshot("completed", "success"), now=20.0
    )  # active: edge (conclude event)
    assert any(e.kind == "concluded" for e in events)

    # t=78: the check's own timer (started ~t=10) has exceeded 60s, but the run
    # has only been quiescent for 58s (since the t=20 conclude). NOT stalled.
    assert not stalled_required_keys(
        state, now=78.0, stalled_timeout=60.0, ignore_rules=[]
    )

    # Keep polling a quiescent run — no activity resets the clock.
    state, _, _ = update_state(state, snapshot("completed", "success"), now=78.0)
    # t=85: quiescent for 65s (> 60). Now it stalls.
    assert stalled_required_keys(
        state, now=85.0, stalled_timeout=60.0, ignore_rules=[]
    )


def test_ignored_in_progress_job_does_not_keep_run_alive():
    # --ignore removes a job from ALL consideration, liveness included: an
    # ignored job's in_progress state must not suppress stall detection for an
    # unrelated required check. A genuinely orphaned required check still
    # stalls even while an ignored job runs.
    def snapshot():
        return [
            _item("orphan", kind="status_context", status="expected",
                  required=True, workflow_name=None),
            _item("flaky", status="in_progress", required=False),
        ]

    rules = [IgnoreRule("job", "flaky")]
    state = initial_state(snapshot())
    for now in (10.0, 30.0, 60.0, 120.0):
        state, _, _ = update_state(
            state, snapshot(), now=now, ignore_rules=rules
        )
    assert stalled_required_keys(
        state, now=120.0, stalled_timeout=60.0, ignore_rules=rules
    )


def test_queued_only_upstream_still_stalls_downstream_known_boundary():
    # ACCEPTED BOUNDARY (no needs-graph): the liveness signal is "a job is
    # in_progress". A downstream required check whose only upstream is still
    # QUEUED (waiting for a runner), with nothing else in the run active, is
    # treated as quiescent and flagged. Distinguishing this from a genuine
    # orphan needs the workflow needs-graph; see README/SKILL "known
    # limitations". Pinned here so the behavior can't change silently.
    def snapshot():
        return [
            _item("downstream", kind="status_context", status="expected",
                  required=True, workflow_name=None),
            _item("upstream", status="queued", required=False),
        ]

    state = initial_state(snapshot())
    for now in (10.0, 30.0, 60.0, 120.0):
        state, _, _ = update_state(state, snapshot(), now=now)
    assert stalled_required_keys(
        state, now=120.0, stalled_timeout=60.0, ignore_rules=[]
    )


def test_not_started_statuses_contains_expected():
    assert "expected" in NOT_STARTED_STATUSES
    assert "queued" in NOT_STARTED_STATUSES
    assert "pending" in NOT_STARTED_STATUSES
    assert "waiting" in NOT_STARTED_STATUSES
    assert "requested" in NOT_STARTED_STATUSES
    assert "completed" not in NOT_STARTED_STATUSES
    assert "in_progress" not in NOT_STARTED_STATUSES
