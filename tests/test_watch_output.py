from __future__ import annotations

from datetime import datetime

from ghci.checks import CheckItem
from ghci.watch.output import (
    format_event_line,
    format_force_push_line,
    format_resolution_line,
)
from ghci.watch.state import Event


def _item(name="lint", conclusion=None):
    return CheckItem(
        kind="actions", name=name, workflow_name="CI",
        status="completed" if conclusion else "in_progress",
        conclusion=conclusion, url=None, required=False,
        check_run_id=None, run_id=None, workflow_run_url=None,
    )


WHEN = datetime(2026, 5, 25, 13, 55, 2)


def test_started_event():
    evt = Event("started", "Run tests", _item("Run tests"))
    assert format_event_line(evt, when=WHEN) == '13:55:02  Job "Run tests" started'


def test_concluded_success_says_green():
    item = _item("Lint", conclusion="success")
    evt = Event("concluded", "Lint", item, conclusion="success")
    assert format_event_line(evt, when=WHEN) == '13:55:02  Job "Lint" green'


def test_concluded_failure_says_failed():
    item = _item("Build", conclusion="failure")
    evt = Event("concluded", "Build", item, conclusion="failure")
    assert format_event_line(evt, when=WHEN) == '13:55:02  Job "Build" failed'


def test_concluded_cancelled_is_verbatim():
    item = _item("Deploy", conclusion="cancelled")
    evt = Event("concluded", "Deploy", item, conclusion="cancelled")
    assert format_event_line(evt, when=WHEN) == '13:55:02  Job "Deploy" cancelled'


def test_concluded_timed_out_human_words():
    item = _item("Slow", conclusion="timed_out")
    evt = Event("concluded", "Slow", item, conclusion="timed_out")
    assert format_event_line(evt, when=WHEN) == '13:55:02  Job "Slow" timed out'


def test_concluded_action_required_human_words():
    item = _item("Manual", conclusion="action_required")
    evt = Event("concluded", "Manual", item, conclusion="action_required")
    assert format_event_line(evt, when=WHEN) == '13:55:02  Job "Manual" action required'


def test_concluded_skipped_verbatim():
    item = _item("Skip", conclusion="skipped")
    evt = Event("concluded", "Skip", item, conclusion="skipped")
    assert format_event_line(evt, when=WHEN) == '13:55:02  Job "Skip" skipped'


def test_force_push_line():
    line = format_force_push_line(when=WHEN, new_sha="abc1234deadbeef")
    # truncated SHA in the output for readability
    assert "13:55:02" in line
    assert "Force-push detected" in line
    assert "abc1234" in line


def test_resolution_line_run_in_pr():
    line = format_resolution_line("Watching run 1234567 (in progress) on PR #123 in owner/repo")
    assert line == "Watching run 1234567 (in progress) on PR #123 in owner/repo"
