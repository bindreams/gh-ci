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
