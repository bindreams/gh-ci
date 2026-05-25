from __future__ import annotations

import io
from pathlib import Path

import pytest

from ghci.cli import ExitCode, main


# Help & no subcommand =====


def test_help_exits_0(fake_gh, capsys):
    code = main(["--help"], gh_get=fake_gh.gh_get, gh_graphql_fn=fake_gh.gh_graphql)
    assert code == 0


def test_no_subcommand_exits_0(fake_gh):
    err = io.StringIO()
    code = main([], gh_get=fake_gh.gh_get, gh_graphql_fn=fake_gh.gh_graphql, stderr=err)
    assert code == 0
    assert "status" in err.getvalue() and "watch" in err.getvalue() and "logs" in err.getvalue()


# Bad args / target =====


def test_bad_target_exits_2(fake_gh):
    err = io.StringIO()
    code = main(["status", "not a url"],
                 gh_get=fake_gh.gh_get, gh_graphql_fn=fake_gh.gh_graphql,
                 stderr=err)
    assert code == 2
    assert "not a url" in err.getvalue() or "supported" in err.getvalue().lower()


def test_bad_ignore_exits_2(fake_gh):
    err = io.StringIO()
    code = main(["status", "https://github.com/o/r/pull/1", "--ignore", "foo"],
                 gh_get=fake_gh.gh_get, gh_graphql_fn=fake_gh.gh_graphql,
                 stderr=err)
    assert code == 2
    assert "ignore" in err.getvalue().lower()


def test_bad_timeout_exits_2(fake_gh):
    err = io.StringIO()
    code = main(["watch", "https://github.com/o/r/pull/1", "--timeout", "garbage"],
                 gh_get=fake_gh.gh_get, gh_graphql_fn=fake_gh.gh_graphql,
                 stderr=err)
    assert code == 2
    assert "garbage" in err.getvalue() or "duration" in err.getvalue().lower()


def test_invalid_subcommand_exits_2(fake_gh):
    err = io.StringIO()
    code = main(["bogus"],
                 gh_get=fake_gh.gh_get, gh_graphql_fn=fake_gh.gh_graphql,
                 stderr=err)
    assert code == 2


# status — happy path =====


def test_status_green_pr_exits_0(fake_gh):
    fake_gh.set_get("/repos/o/r/pulls/1",
                    {"state": "open", "mergeable": True, "mergeable_state": "clean",
                     "head": {"sha": "abc"}})
    fake_gh.queue_graphql({
        "repository": {"pullRequest": {
            "state": "OPEN", "mergeable": "MERGEABLE", "headRefOid": "abc",
            "commits": {"nodes": [{"commit": {"statusCheckRollup": {
                "contexts": {
                    "pageInfo": {"hasNextPage": False, "endCursor": "X"},
                    "nodes": [{
                        "__typename": "CheckRun",
                        "name": "lint",
                        "status": "COMPLETED",
                        "conclusion": "SUCCESS",
                        "detailsUrl": "u",
                        "isRequired": False,
                        "databaseId": 1,
                        "checkSuite": {"workflowRun": {
                            "databaseId": 100, "url": "u",
                            "workflow": {"name": "CI"},
                        }},
                    }],
                }
            }}}]},
        }}
    })
    out = io.StringIO()
    code = main(["status", "https://github.com/o/r/pull/1"],
                 gh_get=fake_gh.gh_get, gh_graphql_fn=fake_gh.gh_graphql,
                 stdout=out)
    assert code == 0
    assert "Passed: lint" in out.getvalue()


def test_status_dirty_pr_exits_4(fake_gh):
    fake_gh.set_get("/repos/o/r/pulls/1",
                    {"state": "open", "mergeable": False,
                     "mergeable_state": "dirty", "head": {"sha": "abc"}})
    fake_gh.queue_graphql({
        "repository": {"pullRequest": {
            "state": "OPEN", "mergeable": "CONFLICTING", "headRefOid": "abc",
            "commits": {"nodes": [{"commit": {"statusCheckRollup": None}}]},
        }}
    })
    out = io.StringIO()
    code = main(["status", "https://github.com/o/r/pull/1"],
                 gh_get=fake_gh.gh_get, gh_graphql_fn=fake_gh.gh_graphql,
                 stdout=out)
    assert code == 4
    assert "conflict" in out.getvalue().lower()


# logs — happy path =====


def test_logs_writes_to_output_dir(fake_gh, tmp_path):
    fake_gh.set_get(
        "/repos/o/r/actions/jobs/1",
        {"id": 1, "name": "Build", "status": "completed", "conclusion": "success",
         "started_at": "S", "completed_at": "E", "html_url": "u", "run_id": 100},
    )
    fake_gh.set_get(
        "/repos/o/r/actions/runs/100",
        {"id": 100, "name": "CI", "status": "completed", "html_url": "u"},
    )

    def fake_download(path, dest, *, host=None):
        dest.write_bytes(b"log content")
        return 11

    code = main(
        ["logs", "https://github.com/o/r/actions/runs/100/job/1",
         "--output-dir", str(tmp_path)],
        gh_get=fake_gh.gh_get,
        gh_graphql_fn=fake_gh.gh_graphql,
        gh_download_fn=fake_download,
    )
    assert code == 0
    subdirs = list(tmp_path.iterdir())
    assert len(subdirs) == 1


# GhError → exit 6 =====


def test_gh_error_exits_6(fake_gh):
    from ghci.gh import GhError

    def gh_get(path, *, host=None, paginate=False):
        raise GhError(returncode=1, stderr="gh: HTTP 401: Bad credentials")

    err = io.StringIO()
    code = main(["status", "https://github.com/o/r/pull/1"],
                 gh_get=gh_get, gh_graphql_fn=fake_gh.gh_graphql,
                 stderr=err)
    assert code == 6
    assert "401" in err.getvalue()


# KeyboardInterrupt → exit 130 =====


def test_sigint_exits_130(fake_gh):
    def gh_get(path, *, host=None, paginate=False):
        raise KeyboardInterrupt()

    err = io.StringIO()
    code = main(["status", "https://github.com/o/r/pull/1"],
                 gh_get=gh_get, gh_graphql_fn=fake_gh.gh_graphql,
                 stderr=err)
    assert code == 130
    assert "Interrupted" in err.getvalue()


# gh CLI missing → exit 6 =====


def test_gh_missing_exits_6(fake_gh):
    def gh_get(path, *, host=None, paginate=False):
        raise FileNotFoundError("[Errno 2] No such file or directory: 'gh'")

    err = io.StringIO()
    code = main(["status", "https://github.com/o/r/pull/1"],
                 gh_get=gh_get, gh_graphql_fn=fake_gh.gh_graphql,
                 stderr=err)
    assert code == 6
    assert "gh" in err.getvalue()


# Empty WorkflowTarget (recognized URL but no runs on branch) → exit 2 =====


def test_status_empty_workflow_exits_2(fake_gh):
    """Workflow URL parses fine, but the workflow has 0 runs on the
    requested branch. status must exit 2 with a clear "recognized-but-empty"
    message — not exit 4 ("no productive CI ran")."""
    # Default branch lookup (no ?branch= in URL).
    fake_gh.set_get("/repos/o/r", {"default_branch": "main"})
    # Workflow runs query returns zero runs.
    fake_gh.set_get(
        "/repos/o/r/actions/workflows/ci.yml/runs?branch=main&per_page=1",
        {"workflow_runs": []},
    )
    err = io.StringIO()
    out = io.StringIO()
    code = main(
        ["status", "https://github.com/o/r/actions/workflows/ci.yml"],
        gh_get=fake_gh.gh_get, gh_graphql_fn=fake_gh.gh_graphql,
        stdout=out, stderr=err,
    )
    assert code == 2
    msg = err.getvalue().lower()
    assert "ci.yml" in err.getvalue()
    assert "no runs" in msg or "empty" in msg
    assert "main" in err.getvalue()


def test_watch_empty_workflow_exits_2(fake_gh):
    """Same recognized-but-empty case for watch — exit 2, not exit 4."""
    fake_gh.set_get("/repos/o/r", {"default_branch": "main"})
    fake_gh.set_get(
        "/repos/o/r/actions/workflows/ci.yml/runs?branch=main&per_page=1",
        {"workflow_runs": []},
    )
    err = io.StringIO()
    out = io.StringIO()
    code = main(
        ["watch", "https://github.com/o/r/actions/workflows/ci.yml",
         "--interval", "1s", "--timeout", "1s"],
        gh_get=fake_gh.gh_get, gh_graphql_fn=fake_gh.gh_graphql,
        stdout=out, stderr=err,
    )
    assert code == 2
    msg = err.getvalue().lower()
    assert "ci.yml" in err.getvalue()
    assert "no runs" in msg or "empty" in msg
    assert "main" in err.getvalue()


def test_logs_empty_workflow_exits_2(fake_gh, tmp_path):
    """Same recognized-but-empty case for logs — exit 2 with clear message,
    not silent exit 0."""
    fake_gh.set_get("/repos/o/r", {"default_branch": "main"})
    fake_gh.set_get(
        "/repos/o/r/actions/workflows/ci.yml/runs?branch=main&per_page=1",
        {"workflow_runs": []},
    )
    err = io.StringIO()

    def fake_download(path, dest, *, host=None):
        raise AssertionError("should not download anything for empty workflow")

    code = main(
        ["logs", "https://github.com/o/r/actions/workflows/ci.yml",
         "--output-dir", str(tmp_path)],
        gh_get=fake_gh.gh_get, gh_graphql_fn=fake_gh.gh_graphql,
        gh_download_fn=fake_download, stderr=err,
    )
    assert code == 2
    msg = err.getvalue().lower()
    assert "ci.yml" in err.getvalue()
    assert "no runs" in msg or "empty" in msg
    assert "main" in err.getvalue()


# Unexpected exception → exit 1 with clean message =====


def test_unexpected_oserror_exits_1_no_traceback(fake_gh):
    def gh_get(path, *, host=None, paginate=False):
        raise OSError("disk full")

    err = io.StringIO()
    code = main(["status", "https://github.com/o/r/pull/1"],
                 gh_get=gh_get, gh_graphql_fn=fake_gh.gh_graphql,
                 stderr=err)
    assert code == int(ExitCode.UNEXPECTED) == 1
    stderr_value = err.getvalue()
    assert "unexpected" in stderr_value.lower()
    assert "disk full" in stderr_value
    assert "Traceback (most recent call last)" not in stderr_value


# Color tests =====


class _TtyStub(io.StringIO):
    def isatty(self) -> bool:  # type: ignore[override]
        return True


def test_color_always_renders_red_error(fake_gh):
    err = io.StringIO()
    code = main(["--color=always", "status", "not a url"],
                gh_get=fake_gh.gh_get, gh_graphql_fn=fake_gh.gh_graphql,
                stderr=err)
    assert code == 2
    assert "\033[31m" in err.getvalue()


def test_color_never_strips_color_even_with_tty_stderr(fake_gh):
    err = _TtyStub()
    code = main(["--color=never", "status", "not a url"],
                gh_get=fake_gh.gh_get, gh_graphql_fn=fake_gh.gh_graphql,
                stderr=err)
    assert code == 2
    assert "\033[" not in err.getvalue()


def test_flag_omitted_no_color_env_disables(fake_gh, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    err = _TtyStub()
    code = main(["status", "not a url"],
                gh_get=fake_gh.gh_get, gh_graphql_fn=fake_gh.gh_graphql,
                stderr=err)
    assert code == 2
    assert "\033[" not in err.getvalue()


def test_flag_omitted_force_color_enables_non_tty(fake_gh, monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("FORCE_COLOR", "1")
    err = io.StringIO()  # non-TTY
    code = main(["status", "not a url"],
                gh_get=fake_gh.gh_get, gh_graphql_fn=fake_gh.gh_graphql,
                stderr=err)
    assert code == 2
    assert "\033[31m" in err.getvalue()


def test_flag_omitted_force_color_zero_enables_per_literal_spec(fake_gh, monkeypatch):
    """FORCE_COLOR=0 enables (literal spec, matches Rich, diverges from Node)."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("FORCE_COLOR", "0")
    err = io.StringIO()
    code = main(["status", "not a url"],
                gh_get=fake_gh.gh_get, gh_graphql_fn=fake_gh.gh_graphql,
                stderr=err)
    assert code == 2
    assert "\033[31m" in err.getvalue()


def test_color_auto_with_no_color_env_disables_via_tty_check(fake_gh, monkeypatch):
    """--color=auto bypasses NO_COLOR but still falls to TTY check (non-TTY → off)."""
    monkeypatch.setenv("NO_COLOR", "1")
    err = io.StringIO()  # non-TTY
    code = main(["--color=auto", "status", "not a url"],
                gh_get=fake_gh.gh_get, gh_graphql_fn=fake_gh.gh_graphql,
                stderr=err)
    assert code == 2
    assert "\033[" not in err.getvalue()


def test_color_auto_with_force_color_env_disables_via_tty_check(fake_gh, monkeypatch):
    """--color=auto bypasses FORCE_COLOR; non-TTY stderr → no color."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("FORCE_COLOR", "1")
    err = io.StringIO()  # non-TTY
    code = main(["--color=auto", "status", "not a url"],
                gh_get=fake_gh.gh_get, gh_graphql_fn=fake_gh.gh_graphql,
                stderr=err)
    assert code == 2
    assert "\033[" not in err.getvalue()


def test_color_auto_with_tty_enables(fake_gh, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")  # bypassed by auto
    err = _TtyStub()
    code = main(["--color=auto", "status", "not a url"],
                gh_get=fake_gh.gh_get, gh_graphql_fn=fake_gh.gh_graphql,
                stderr=err)
    assert code == 2
    assert "\033[31m" in err.getvalue()


def test_flag_omitted_no_color_wins_over_force_color(fake_gh, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("FORCE_COLOR", "1")
    err = _TtyStub()
    code = main(["status", "not a url"],
                gh_get=fake_gh.gh_get, gh_graphql_fn=fake_gh.gh_graphql,
                stderr=err)
    assert code == 2
    assert "\033[" not in err.getvalue()


def test_argparse_error_not_colored(fake_gh):
    """Documented limitation: SystemExit fires before palette is built."""
    err = io.StringIO()
    code = main(["--color=always", "no-such-subcommand", "x"],
                gh_get=fake_gh.gh_get, gh_graphql_fn=fake_gh.gh_graphql,
                stderr=err)
    assert code != 0
    # The argparse-generated error in err is not colored.
    assert "\033[" not in err.getvalue()
