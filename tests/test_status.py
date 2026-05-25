from __future__ import annotations

from ghci.checks import CheckItem
from ghci.ignore import IgnoreRule
from ghci.status import evaluate_snapshot, format_summary


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


def test_s3_unknown_pr_summary_matches_plan_wording():
    # Plan §status step 4: "Mergeability still computing; retry with watch."
    # The PR-# prefix must be dropped from the user-facing message.
    pr = {"state": "open", "mergeable": None, "mergeable_state": "unknown"}
    code, summary = evaluate_snapshot([], pr_meta=pr, pr_number=42, ignore_rules=[])
    assert code == 7
    assert "Mergeability still computing; retry with watch." in summary
    assert "PR #42" not in summary


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


# format_summary unit tests for stalled/in-flight dedup (S5 follow-up) =====


def test_format_summary_dedupes_stalled_from_in_flight_by_full_key():
    # Two checks share the name "validate" but differ by (kind, workflow_name):
    # - one is a stalled required status_context (state==expected),
    # - the other is an in-flight Actions job.
    # The stalled one must appear ONLY under "Not reported (stalled)"; the
    # in-flight Actions job must still appear in the "In progress" group.
    # Deduping by name alone would incorrectly omit the in-flight item.
    stalled = CheckItem(
        kind="status_context", name="validate", workflow_name=None,
        status="expected", conclusion=None, url=None, required=True,
        check_run_id=None, run_id=None, workflow_run_url=None,
    )
    in_flight_actions = CheckItem(
        kind="actions", name="validate", workflow_name="CI",
        status="in_progress", conclusion=None, url=None, required=False,
        check_run_id=1, run_id=1, workflow_run_url=None,
    )
    summary = format_summary(
        [stalled, in_flight_actions], [],
        result_line="Result: required check stalled",
        in_flight_label="In progress",
        stalled_items=[stalled],
    )
    assert "Not reported (stalled): validate" in summary
    assert "In progress: validate" in summary


def test_format_summary_unknown_group_catches_unrecognized_conclusion():
    # Defensive: if GitHub adds a new conclusion string our group map
    # doesn't know about, the item must still appear under Unknown rather
    # than silently disappearing.
    weird = CheckItem(
        kind="actions", name="future-thing", workflow_name="CI",
        status="completed", conclusion="newly_added_conclusion",
        url=None, required=False,
        check_run_id=None, run_id=None, workflow_run_url=None,
    )
    summary = format_summary([weird], [], result_line="Result: green")
    assert "Unknown: future-thing" in summary


def test_format_summary_unknown_ignored_group_appears():
    # Coverage for the S6 ignored variant: an ignored item with status=
    # completed and conclusion=None should appear under
    # "Unknown (ignored by --ignore)".
    ignored = CheckItem(
        kind="actions", name="weird", workflow_name="CI",
        status="completed", conclusion=None, url=None, required=False,
        check_run_id=None, run_id=None, workflow_run_url=None,
    )
    summary = format_summary([ignored], [ignored], result_line="Result: green")
    assert "Unknown (ignored by --ignore): weird" in summary
