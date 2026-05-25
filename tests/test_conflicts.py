from __future__ import annotations

from ghci.conflicts import ConflictOutcome, assess_pr, message_for


def test_open_clean_ok():
    pr = {"state": "open", "mergeable": True, "mergeable_state": "clean"}
    assert assess_pr(pr) == ConflictOutcome.OK


def test_open_blocked_ok():
    # blocked = required checks pending → informational, keep watching
    pr = {"state": "open", "mergeable": True, "mergeable_state": "blocked"}
    assert assess_pr(pr) == ConflictOutcome.OK


def test_open_unstable_ok():
    pr = {"state": "open", "mergeable": True, "mergeable_state": "unstable"}
    assert assess_pr(pr) == ConflictOutcome.OK


def test_open_dirty_is_conflict():
    pr = {"state": "open", "mergeable": False, "mergeable_state": "dirty"}
    assert assess_pr(pr) == ConflictOutcome.DIRTY


def test_open_behind_is_behind():
    pr = {"state": "open", "mergeable": True, "mergeable_state": "behind"}
    assert assess_pr(pr) == ConflictOutcome.BEHIND


def test_open_unknown_is_unknown():
    pr = {"state": "open", "mergeable": None, "mergeable_state": "unknown"}
    assert assess_pr(pr) == ConflictOutcome.UNKNOWN


def test_open_mergeable_null_is_unknown():
    # mergeable null on an explicit state still implies pending
    pr = {"state": "open", "mergeable": None, "mergeable_state": "clean"}
    assert assess_pr(pr) == ConflictOutcome.UNKNOWN


def test_open_missing_mergeable_state_is_unknown():
    # Regression: partial gh response / mocked fixture without mergeable_state.
    pr = {"state": "open", "mergeable": True}
    assert assess_pr(pr) == ConflictOutcome.UNKNOWN


def test_open_missing_both_keys_is_unknown():
    # Regression: neither mergeable nor mergeable_state present.
    pr = {"state": "open"}
    assert assess_pr(pr) == ConflictOutcome.UNKNOWN


def test_closed_pr_is_closed():
    pr = {"state": "closed", "mergeable": False, "mergeable_state": "dirty"}
    assert assess_pr(pr) == ConflictOutcome.CLOSED


def test_merged_pr_state_closed_is_closed():
    # GitHub reports state="closed" for merged PRs too
    pr = {"state": "closed", "mergeable": True, "mergeable_state": "clean", "merged": True}
    assert assess_pr(pr) == ConflictOutcome.CLOSED


def test_message_for_closed():
    assert "closed" in message_for(ConflictOutcome.CLOSED, pr_number=42).lower()
    assert "42" in message_for(ConflictOutcome.CLOSED, pr_number=42)


def test_message_for_dirty():
    msg = message_for(ConflictOutcome.DIRTY, pr_number=99)
    assert "99" in msg
    assert "conflict" in msg.lower()


def test_message_for_behind():
    msg = message_for(ConflictOutcome.BEHIND, pr_number=7)
    assert "7" in msg
    assert "behind" in msg.lower()


def test_message_for_unknown():
    msg = message_for(ConflictOutcome.UNKNOWN, pr_number=5)
    assert "computing" in msg.lower() or "unknown" in msg.lower()


def test_message_for_ok_is_empty():
    assert message_for(ConflictOutcome.OK, pr_number=1) == ""
