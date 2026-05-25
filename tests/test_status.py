from __future__ import annotations

from ghci.checks import CheckItem
from ghci.ignore import IgnoreRule
from ghci.status import evaluate_snapshot


def _item(name, *, status="completed", conclusion="success", workflow_name="CI",
          kind="actions", required=False):
    return CheckItem(
        kind=kind,
        name=name,
        workflow_name=workflow_name,
        status=status,
        conclusion=conclusion,
        url=None,
        required=required,
        check_run_id=None,
        run_id=None,
        workflow_run_url=None,
    )


# PR-state conflict checks =====


def test_closed_pr_exits_4():
    pr = {"state": "closed", "mergeable_state": "clean"}
    code, summary = evaluate_snapshot([], pr_meta=pr, pr_number=42, ignore_rules=[])
    assert code == 4
    assert "closed" in summary.lower()
    assert "42" in summary


def test_dirty_pr_exits_4():
    pr = {"state": "open", "mergeable_state": "dirty"}
    code, summary = evaluate_snapshot([], pr_meta=pr, pr_number=1, ignore_rules=[])
    assert code == 4
    assert "conflict" in summary.lower()


def test_behind_pr_exits_4():
    pr = {"state": "open", "mergeable_state": "behind"}
    code, summary = evaluate_snapshot([], pr_meta=pr, pr_number=1, ignore_rules=[])
    assert code == 4
    assert "behind" in summary.lower()


def test_unknown_pr_exits_7():
    pr = {"state": "open", "mergeable": None, "mergeable_state": "unknown"}
    code, summary = evaluate_snapshot([], pr_meta=pr, pr_number=1, ignore_rules=[])
    assert code == 7
    assert "computing" in summary.lower() or "still" in summary.lower()


# Item evaluation =====


def test_all_passed_exits_0():
    items = [_item("a"), _item("b")]
    pr = {"state": "open", "mergeable": True, "mergeable_state": "clean"}
    code, summary = evaluate_snapshot(items, pr_meta=pr, pr_number=1, ignore_rules=[])
    assert code == 0
    assert "Passed:" in summary
    assert "a" in summary and "b" in summary


def test_first_failure_exits_3():
    items = [_item("a"), _item("b", conclusion="failure")]
    pr = {"state": "open", "mergeable": True, "mergeable_state": "clean"}
    code, summary = evaluate_snapshot(items, pr_meta=pr, pr_number=1, ignore_rules=[])
    assert code == 3
    assert "Failed:" in summary and "b" in summary


def test_cancelled_displayed_as_cancelled_but_counts_as_failure():
    items = [_item("a", conclusion="cancelled")]
    pr = {"state": "open", "mergeable": True, "mergeable_state": "clean"}
    code, summary = evaluate_snapshot(items, pr_meta=pr, pr_number=1, ignore_rules=[])
    assert code == 3
    assert "Cancelled:" in summary
    assert "Failed:" not in summary  # not masked


def test_in_progress_exits_7_with_in_progress_group():
    items = [_item("a", status="in_progress", conclusion=None), _item("b")]
    pr = {"state": "open", "mergeable": True, "mergeable_state": "clean"}
    code, summary = evaluate_snapshot(items, pr_meta=pr, pr_number=1, ignore_rules=[])
    assert code == 7
    assert "In progress:" in summary
    assert "a" in summary
    assert "still in progress" in summary.lower()


def test_only_skipped_exits_4():
    items = [_item("a", conclusion="skipped"), _item("b", conclusion="neutral")]
    pr = {"state": "open", "mergeable": True, "mergeable_state": "clean"}
    code, summary = evaluate_snapshot(items, pr_meta=pr, pr_number=1, ignore_rules=[])
    assert code == 4
    assert "no productive ci" in summary.lower()
    assert "Skipped:" in summary


def test_ignored_failure_does_not_count():
    items = [
        _item("a"),
        _item("flaky", conclusion="failure"),
    ]
    pr = {"state": "open", "mergeable": True, "mergeable_state": "clean"}
    code, summary = evaluate_snapshot(
        items, pr_meta=pr, pr_number=1, ignore_rules=[IgnoreRule("job", "flaky")]
    )
    assert code == 0
    assert "Failed (ignored by --ignore):" in summary
    assert "flaky" in summary


def test_all_ignored_with_no_passes_exits_4():
    items = [_item("flaky", conclusion="failure")]
    pr = {"state": "open", "mergeable": True, "mergeable_state": "clean"}
    code, summary = evaluate_snapshot(
        items, pr_meta=pr, pr_number=1, ignore_rules=[IgnoreRule("job", "flaky")]
    )
    assert code == 4
    assert "no productive ci" in summary.lower()


def test_blocked_pr_proceeds_to_evaluate_items():
    # blocked = required checks pending → informational
    items = [_item("a")]
    pr = {"state": "open", "mergeable": True, "mergeable_state": "blocked"}
    code, _ = evaluate_snapshot(items, pr_meta=pr, pr_number=1, ignore_rules=[])
    assert code == 0


def test_unstable_pr_proceeds_to_evaluate_items():
    items = [_item("a")]
    pr = {"state": "open", "mergeable": True, "mergeable_state": "unstable"}
    code, _ = evaluate_snapshot(items, pr_meta=pr, pr_number=1, ignore_rules=[])
    assert code == 0


def test_non_pr_target_skips_pr_state_checks():
    # pr_meta=None → no closed/dirty/behind checks
    items = [_item("a")]
    code, _ = evaluate_snapshot(items, pr_meta=None, pr_number=None, ignore_rules=[])
    assert code == 0


def test_summary_lists_all_conclusions_verbatim():
    items = [
        _item("a", conclusion="success"),
        _item("b", conclusion="failure"),
        _item("c", conclusion="cancelled"),
        _item("d", conclusion="timed_out"),
        _item("e", conclusion="skipped"),
    ]
    pr = {"state": "open", "mergeable": True, "mergeable_state": "clean"}
    code, summary = evaluate_snapshot(items, pr_meta=pr, pr_number=1, ignore_rules=[])
    # Failed, so check for groups
    assert code == 3
    for label in ["Passed:", "Failed:", "Cancelled:", "Timed out:", "Skipped:"]:
        assert label in summary


def test_empty_groups_are_omitted():
    items = [_item("a")]
    pr = {"state": "open", "mergeable": True, "mergeable_state": "clean"}
    _, summary = evaluate_snapshot(items, pr_meta=pr, pr_number=1, ignore_rules=[])
    assert "Failed:" not in summary
    assert "Skipped:" not in summary
    assert "Cancelled:" not in summary
