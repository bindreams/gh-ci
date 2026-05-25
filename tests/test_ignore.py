from __future__ import annotations

from dataclasses import dataclass

import pytest

from ghci.ignore import IgnoreParseError, IgnoreRule, matches, parse_ignore


@dataclass
class _Item:
    kind: str
    name: str
    workflow_name: str | None = None


# parse_ignore =====


def test_parse_workflow():
    assert parse_ignore("workflow:CI") == IgnoreRule(prefix="workflow", name="CI")


def test_parse_job():
    assert parse_ignore("job:Run tests") == IgnoreRule(prefix="job", name="Run tests")


def test_parse_check():
    assert parse_ignore("check:codecov") == IgnoreRule(prefix="check", name="codecov")


def test_parse_preserves_name_case_sensitive():
    assert parse_ignore("job:RunTests") == IgnoreRule(prefix="job", name="RunTests")


def test_parse_name_with_colons_inside():
    # Job names can include colons (e.g. matrix "test (3.11, ubuntu-latest)")
    # The split is on the *first* colon only.
    assert parse_ignore("job:test (3.11): foo") == IgnoreRule(
        prefix="job", name="test (3.11): foo"
    )


def test_parse_no_prefix_raises():
    with pytest.raises(IgnoreParseError) as exc:
        parse_ignore("foo")
    msg = str(exc.value)
    assert "workflow:" in msg and "job:" in msg and "check:" in msg
    assert "foo" in msg


def test_parse_unknown_prefix_raises():
    with pytest.raises(IgnoreParseError) as exc:
        parse_ignore("unknown:thing")
    assert "unknown:thing" in str(exc.value)


def test_parse_missing_id_raises():
    with pytest.raises(IgnoreParseError):
        parse_ignore("job:")


# matches =====


def test_matches_workflow_against_actions_item():
    rules = [IgnoreRule("workflow", "CI")]
    assert matches(_Item(kind="actions", name="lint", workflow_name="CI"), rules)
    assert not matches(_Item(kind="actions", name="lint", workflow_name="Build"), rules)


def test_matches_workflow_does_not_match_non_actions():
    rules = [IgnoreRule("workflow", "CI")]
    # A status_context has no workflow_name; shouldn't match
    assert not matches(_Item(kind="status_context", name="CI"), rules)
    assert not matches(_Item(kind="check_run", name="CI"), rules)


def test_matches_job_against_actions_item():
    rules = [IgnoreRule("job", "lint")]
    assert matches(_Item(kind="actions", name="lint", workflow_name="CI"), rules)
    assert not matches(_Item(kind="actions", name="build", workflow_name="CI"), rules)


def test_matches_job_does_not_match_status_or_check_run():
    rules = [IgnoreRule("job", "lint")]
    assert not matches(_Item(kind="status_context", name="lint"), rules)
    assert not matches(_Item(kind="check_run", name="lint"), rules)


def test_matches_check_matches_status_context_and_check_run():
    rules = [IgnoreRule("check", "codecov")]
    assert matches(_Item(kind="status_context", name="codecov"), rules)
    assert matches(_Item(kind="check_run", name="codecov"), rules)
    # Actions items are NOT matched by `check:` prefix
    assert not matches(
        _Item(kind="actions", name="codecov", workflow_name="X"), rules
    )


def test_matches_is_case_sensitive():
    rules = [IgnoreRule("job", "Lint")]
    assert matches(_Item(kind="actions", name="Lint", workflow_name="CI"), rules)
    assert not matches(_Item(kind="actions", name="lint", workflow_name="CI"), rules)


def test_matches_multiple_rules_any():
    rules = [IgnoreRule("job", "lint"), IgnoreRule("job", "build")]
    assert matches(_Item(kind="actions", name="lint", workflow_name="CI"), rules)
    assert matches(_Item(kind="actions", name="build", workflow_name="CI"), rules)
    assert not matches(_Item(kind="actions", name="test", workflow_name="CI"), rules)


def test_matches_empty_rules_never_matches():
    assert not matches(_Item(kind="actions", name="x", workflow_name="CI"), [])
