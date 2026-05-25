from __future__ import annotations

import pytest

from ghci.target import (
    JobTarget,
    PrTarget,
    RunTarget,
    TargetParseError,
    WorkflowTarget,
    resolve_target,
)


def _fake_gh_get(default_branch: str = "main"):
    """A fake gh_api_get that returns {default_branch: ...} for repo lookups."""

    def _get(path: str, *, host: str | None = None, paginate: bool = False):
        assert path.startswith("/repos/")
        # /repos/owner/repo
        return {"default_branch": default_branch}

    return _get


# PR URLs =====


def test_pr_url_plain():
    t = resolve_target("https://github.com/foo/bar/pull/123", gh_get=_fake_gh_get())
    assert t == PrTarget(owner="foo", repo="bar", host="github.com", pr_number=123)


def test_pr_url_with_files_segment():
    t = resolve_target(
        "https://github.com/foo/bar/pull/123/files", gh_get=_fake_gh_get()
    )
    assert isinstance(t, PrTarget) and t.pr_number == 123


def test_pr_url_with_commits_segment():
    t = resolve_target(
        "https://github.com/foo/bar/pull/123/commits", gh_get=_fake_gh_get()
    )
    assert isinstance(t, PrTarget) and t.pr_number == 123


def test_pr_url_with_hash_fragment():
    t = resolve_target(
        "https://github.com/foo/bar/pull/123#diff-abc", gh_get=_fake_gh_get()
    )
    assert isinstance(t, PrTarget) and t.pr_number == 123


def test_pr_url_with_trailing_slash():
    t = resolve_target("https://github.com/foo/bar/pull/123/", gh_get=_fake_gh_get())
    assert isinstance(t, PrTarget) and t.pr_number == 123


# Run URLs =====


def test_run_url_plain():
    t = resolve_target(
        "https://github.com/foo/bar/actions/runs/9876", gh_get=_fake_gh_get()
    )
    assert t == RunTarget(owner="foo", repo="bar", host="github.com", run_id=9876)


def test_run_url_with_attempts_ignored():
    t = resolve_target(
        "https://github.com/foo/bar/actions/runs/9876/attempts/3",
        gh_get=_fake_gh_get(),
    )
    assert t == RunTarget(owner="foo", repo="bar", host="github.com", run_id=9876)


# Job URLs =====


def test_job_url_singular_job_segment():
    t = resolve_target(
        "https://github.com/foo/bar/actions/runs/9876/job/42", gh_get=_fake_gh_get()
    )
    assert t == JobTarget(
        owner="foo", repo="bar", host="github.com", run_id=9876, job_id=42
    )


def test_job_url_plural_jobs_segment():
    t = resolve_target(
        "https://github.com/foo/bar/actions/runs/9876/jobs/42", gh_get=_fake_gh_get()
    )
    assert isinstance(t, JobTarget) and t.job_id == 42


# Workflow URLs =====


def test_workflow_url_no_branch_resolves_default():
    t = resolve_target(
        "https://github.com/foo/bar/actions/workflows/ci.yml",
        gh_get=_fake_gh_get(default_branch="main"),
    )
    assert t == WorkflowTarget(
        owner="foo",
        repo="bar",
        host="github.com",
        workflow_path="ci.yml",
        branch="main",
    )


def test_workflow_url_with_branch_query():
    t = resolve_target(
        "https://github.com/foo/bar/actions/workflows/ci.yml?branch=feature",
        gh_get=_fake_gh_get(),
    )
    assert isinstance(t, WorkflowTarget) and t.branch == "feature"


def test_workflow_url_with_query_param_branch():
    t = resolve_target(
        "https://github.com/foo/bar/actions/workflows/ci.yml?query=branch%3Afeature",
        gh_get=_fake_gh_get(),
    )
    assert isinstance(t, WorkflowTarget) and t.branch == "feature"


def test_workflow_url_with_complex_query():
    t = resolve_target(
        "https://github.com/foo/bar/actions/workflows/ci.yml?query=is%3Aopen+branch%3Afeat-x",
        gh_get=_fake_gh_get(),
    )
    assert isinstance(t, WorkflowTarget) and t.branch == "feat-x"


# GHES host =====


def test_ghes_host_is_preserved():
    t = resolve_target(
        "https://github.example.com/foo/bar/pull/123", gh_get=_fake_gh_get()
    )
    assert isinstance(t, PrTarget) and t.host == "github.example.com"


def test_ghes_default_branch_lookup_uses_host():
    captured = {}

    def gh_get(path, *, host=None, paginate=False):
        captured["host"] = host
        return {"default_branch": "main"}

    resolve_target(
        "https://github.example.com/foo/bar/actions/workflows/ci.yml",
        gh_get=gh_get,
    )
    assert captured["host"] == "github.example.com"


# Errors =====


def test_bare_pr_number_is_rejected():
    with pytest.raises(TargetParseError) as exc:
        resolve_target("123", gh_get=_fake_gh_get())
    assert "PR URL" in str(exc.value) or "supported" in str(exc.value).lower()


def test_bare_sha_is_rejected():
    with pytest.raises(TargetParseError):
        resolve_target("abc1234def5678", gh_get=_fake_gh_get())


def test_branch_name_is_rejected():
    with pytest.raises(TargetParseError):
        resolve_target("feature-foo", gh_get=_fake_gh_get())


def test_unknown_github_path_is_rejected():
    with pytest.raises(TargetParseError):
        resolve_target("https://github.com/foo/bar/issues/1", gh_get=_fake_gh_get())


def test_not_a_url_is_rejected():
    with pytest.raises(TargetParseError):
        resolve_target("not a url", gh_get=_fake_gh_get())
