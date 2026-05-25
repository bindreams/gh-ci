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


# Color-aware tests =====


from ghci.colors import Palette, _ResultStyle
from ghci.conflicts import ConflictOutcome
from ghci.status import _conflict_summary as _status_conflict_summary


def test_result_line_colored_green_when_palette_enabled():
    summary = format_summary(
        [_item("t1")], [], result_line="Result: green",
        result_style=_ResultStyle.GREEN, palette=Palette(True),
    )
    first_line = summary.splitlines()[0]
    assert first_line.startswith("\033[1m\033[32m")
    assert first_line.endswith("\033[0m")
    assert "Result: green" in first_line


def test_result_line_colored_red_for_no_productive_ci():
    summary = format_summary(
        [_item("t1", conclusion="skipped")], [],
        result_line="Result: no productive CI ran",
        result_style=_ResultStyle.RED, palette=Palette(True),
    )
    first_line = summary.splitlines()[0]
    assert "\033[1m\033[31m" in first_line
    assert "Result: no productive CI ran" in first_line


def test_result_line_colored_yellow_for_in_progress():
    summary = format_summary(
        [_item("t1", status="in_progress", conclusion=None)], [],
        result_line="Result: still in progress",
        in_flight_label="In progress",
        result_style=_ResultStyle.YELLOW, palette=Palette(True),
    )
    first_line = summary.splitlines()[0]
    assert "\033[1m\033[33m" in first_line
    assert "Result: still in progress" in first_line


def test_result_line_colored_yellow_for_timeout():
    summary = format_summary(
        [_item("t1", status="in_progress", conclusion=None)], [],
        result_line="Result: timeout reached while watching",
        in_flight_label="In progress (timed out)",
        result_style=_ResultStyle.YELLOW, palette=Palette(True),
    )
    first_line = summary.splitlines()[0]
    assert "\033[1m\033[33m" in first_line


def test_multi_line_result_colors_only_first_line():
    multi = (
        'Result: red CI (job "Build" failure)\n'
        'To continue watching despite this failure: '
        'gh-ci watch <target> --ignore job:"Build"'
    )
    summary = format_summary(
        [_item("Build", conclusion="failure")], [],
        result_line=multi,
        in_flight_label="In progress",
        result_style=_ResultStyle.RED, palette=Palette(True),
    )
    lines = summary.splitlines()
    assert "\033[0m" in lines[0]
    # Second line is byte-identical to the original input — no ANSI codes.
    assert lines[1] == "To continue watching despite this failure: gh-ci watch <target> --ignore job:\"Build\""
    assert "\033[" not in lines[1]


def test_group_label_passed_colored_green_names_plain():
    summary = format_summary(
        [_item("t1")], [], result_line="Result: green",
        result_style=_ResultStyle.GREEN, palette=Palette(True),
    )
    # Find the Passed line.
    passed = next(l for l in summary.splitlines() if "Passed" in l)
    # Label wrapped in green; the colon and names are not.
    assert passed.startswith("\033[32mPassed\033[0m: t1")


def test_group_label_failed_colored_red():
    summary = format_summary(
        [_item("Build", conclusion="failure")], [],
        result_line='Result: red CI (job "Build" failure)',
        in_flight_label="In progress",
        result_style=_ResultStyle.RED, palette=Palette(True),
    )
    failed_line = next(l for l in summary.splitlines() if "Failed" in l)
    assert failed_line.startswith("\033[31mFailed\033[0m: Build")


def test_ignored_group_dimmed_even_when_base_style_is_red():
    failed_item = _item("Lint", conclusion="failure")
    summary = format_summary(
        [failed_item], [failed_item], result_line="Result: green",
        result_style=_ResultStyle.GREEN, palette=Palette(True),
    )
    line = next(l for l in summary.splitlines() if "ignored by --ignore" in l)
    # Dim, NOT red — verifying ignored=True overrides base FAILED style.
    assert line.startswith("\033[2mFailed (ignored by --ignore)\033[0m:")
    assert "\033[31m" not in line.split(":")[0]


def test_neutral_groups_dimmed():
    summary = format_summary(
        [_item("Lint", conclusion="skipped")], [], result_line="Result: green",
        result_style=_ResultStyle.GREEN, palette=Palette(True),
    )
    skipped_line = next(l for l in summary.splitlines() if l.startswith("\033[2mSkipped"))
    assert "Lint" in skipped_line


def test_in_progress_group_yellow():
    summary = format_summary(
        [_item("Run", status="in_progress", conclusion=None)], [],
        result_line="Result: still in progress",
        in_flight_label="In progress",
        result_style=_ResultStyle.YELLOW, palette=Palette(True),
    )
    line = next(l for l in summary.splitlines() if "In progress:" in l or "In progress\033" in l)
    assert line.startswith("\033[33mIn progress\033[0m:")


def test_conflict_summary_dirty_colored_red():
    out = _status_conflict_summary(
        ConflictOutcome.DIRTY, 42, palette=Palette(True),
    )
    lines = out.splitlines()
    assert lines[0] == "\033[1m\033[31mResult: dirty\033[0m"
    # Advice line plain.
    assert "\033[" not in lines[1]


def test_conflict_summary_unknown_colored_yellow():
    out = _status_conflict_summary(
        ConflictOutcome.UNKNOWN, 42, palette=Palette(True),
    )
    lines = out.splitlines()
    assert lines[0] == "\033[1m\033[33mResult: unknown\033[0m"


def test_conflict_summary_closed_colored_red():
    out = _status_conflict_summary(
        ConflictOutcome.CLOSED, 42, palette=Palette(True),
    )
    lines = out.splitlines()
    assert lines[0].startswith("\033[1m\033[31m")
    assert "Result: closed" in lines[0]


def test_group_order_carries_valid_style():
    from ghci.status import _GROUP_ORDER, _GroupStyle
    for row in _GROUP_ORDER:
        assert len(row) == 3
        assert isinstance(row[2], _GroupStyle)


def test_format_summary_byte_identical_when_palette_disabled():
    # Backward-compat lock: the no-color path must be byte-identical to the
    # pre-change rendering. A representative mixed input.
    items = [
        _item("t1"),
        _item("t2", conclusion="failure"),
        _item("t3", status="in_progress", conclusion=None),
    ]
    out = format_summary(
        items, [], result_line='Result: red CI (job "t2" failure)',
        in_flight_label="In progress",
    )
    # Exact-string assertion — must match what the codebase produced
    # before this change.
    assert out == (
        'Result: red CI (job "t2" failure)\n'
        'Passed: t1\n'
        'Failed: t2\n'
        'In progress: t3'
    )


def test_format_summary_no_color_with_result_style_none_unchanged():
    # Even when palette is disabled but result_style is provided, the
    # output is unchanged (result_style is a coloring directive, not a
    # text transformation).
    out = format_summary(
        [_item("ok")], [], result_line="Result: green",
        result_style=_ResultStyle.GREEN,
    )
    assert out == "Result: green\nPassed: ok"
