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


# Color-aware tests =====

from ghci.colors import Palette
from ghci.watch.output import _CONCLUSION_STYLE, _CONCLUSION_WORDS


def test_conclusion_words_and_style_have_same_keys():
    # Source-of-truth parity: the two dicts must not drift.
    assert set(_CONCLUSION_WORDS) == set(_CONCLUSION_STYLE)


def test_concluded_failed_colors_word_red():
    evt = Event(kind="concluded", name="Build", item=_item("Build", "failure"), conclusion="failure")
    line = format_event_line(evt, when=WHEN, palette=Palette(True))
    assert "\033[31mfailed\033[0m" in line


def test_concluded_success_colors_word_green():
    evt = Event(kind="concluded", name="Lint", item=_item("Lint", "success"), conclusion="success")
    line = format_event_line(evt, when=WHEN, palette=Palette(True))
    assert "\033[32mgreen\033[0m" in line


def test_concluded_skipped_dims_word():
    evt = Event(kind="concluded", name="S", item=_item("S", "skipped"), conclusion="skipped")
    line = format_event_line(evt, when=WHEN, palette=Palette(True))
    assert "\033[2mskipped\033[0m" in line


def test_started_event_uncolored_word_even_with_palette():
    evt = Event(kind="started", name="Build", item=_item("Build"))
    line = format_event_line(evt, when=WHEN, palette=Palette(True))
    # The word "started" itself has no ANSI wrapping.
    assert "started\033[" not in line
    assert line.endswith('Job "Build" started')


def test_timestamp_dimmed_when_palette_enabled():
    evt = Event(kind="started", name="Build", item=_item("Build"))
    line = format_event_line(evt, when=WHEN, palette=Palette(True))
    assert line.startswith("\033[2m13:55:02\033[0m")


def test_force_push_line_phrase_yellow():
    line = format_force_push_line(when=WHEN, new_sha="abc1234deadbeef", palette=Palette(True))
    assert "\033[33mForce-push detected\033[0m" in line


def test_event_line_byte_identical_when_palette_disabled():
    evt = Event(kind="concluded", name="Build", item=_item("Build", "failure"), conclusion="failure")
    assert format_event_line(evt, when=WHEN) == '13:55:02  Job "Build" failed'
    assert format_event_line(evt, when=WHEN, palette=Palette(False)) == '13:55:02  Job "Build" failed'


def test_force_push_line_byte_identical_when_palette_disabled():
    expected = "13:55:02  Force-push detected — now watching SHA abc1234"
    assert format_force_push_line(when=WHEN, new_sha="abc1234deadbeef") == expected
    assert format_force_push_line(when=WHEN, new_sha="abc1234deadbeef", palette=Palette(False)) == expected
