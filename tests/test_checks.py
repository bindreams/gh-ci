from __future__ import annotations

from ghci.checks import (
    CheckItem,
    Outcome,
    classify_conclusion,
    fetch_job,
    fetch_pr_checks,
    fetch_pr_meta,
    fetch_run_jobs,
    fetch_workflow_latest_run,
)


# classify_conclusion =====


def test_classify_passed():
    assert classify_conclusion("success") == Outcome.PASSED


def test_classify_failed_set():
    for c in ["failure", "cancelled", "timed_out", "action_required", "startup_failure"]:
        assert classify_conclusion(c) == Outcome.FAILED, c


def test_classify_skipped_set():
    for c in ["skipped", "neutral", "stale"]:
        assert classify_conclusion(c) == Outcome.SKIPPED, c


def test_classify_none_is_none():
    assert classify_conclusion(None) is None


def test_classify_unknown_string_is_none():
    # Future-proof: unknown values don't crash, just classify as None.
    assert classify_conclusion("weird") is None


# fetch_pr_checks — GraphQL =====


def _graphql_payload(nodes, *, has_next=False, end_cursor="X", pr_state="OPEN",
                     head_sha="abc1234"):
    return {
        "repository": {
            "pullRequest": {
                "state": pr_state,
                "mergeable": "MERGEABLE",
                "headRefOid": head_sha,
                "commits": {
                    "nodes": [
                        {
                            "commit": {
                                "statusCheckRollup": {
                                    "contexts": {
                                        "pageInfo": {
                                            "hasNextPage": has_next,
                                            "endCursor": end_cursor,
                                        },
                                        "nodes": nodes,
                                    }
                                }
                            }
                        }
                    ]
                },
            }
        }
    }


def _checkrun_actions_node(name="lint", status="COMPLETED", conclusion="SUCCESS",
                            workflow="CI", required=False, db_id=99, run_id=12345):
    return {
        "__typename": "CheckRun",
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "detailsUrl": "https://github.com/foo/bar/actions/runs/12345/job/99",
        "isRequired": required,
        "databaseId": db_id,
        "checkSuite": {
            "workflowRun": {
                "databaseId": run_id,
                "workflow": {"name": workflow},
                "url": f"https://github.com/foo/bar/actions/runs/{run_id}",
            }
        },
    }


def _checkrun_thirdparty_node(name="codecov", status="COMPLETED", conclusion="SUCCESS",
                              required=False, db_id=88):
    return {
        "__typename": "CheckRun",
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "detailsUrl": "https://example.com/codecov",
        "isRequired": required,
        "databaseId": db_id,
        "checkSuite": {"workflowRun": None},
    }


def _status_context_node(context="ci/external", state="EXPECTED",
                          required=True, target_url=None):
    return {
        "__typename": "StatusContext",
        "context": context,
        "state": state,
        "targetUrl": target_url,
        "isRequired": required,
    }


def _gh_graphql_returning(*payloads):
    """Returns a callable that yields each payload in sequence."""
    payloads = list(payloads)
    calls: list[dict] = []

    def fn(query, *, host=None, **variables):
        calls.append({"query": query, "host": host, "variables": variables})
        return payloads.pop(0)

    fn.calls = calls
    return fn


def test_fetch_pr_checks_actions_kind():
    fn = _gh_graphql_returning(
        _graphql_payload([_checkrun_actions_node(name="lint")])
    )
    items, meta = fetch_pr_checks("foo", "bar", 1, gh_graphql_fn=fn)
    assert len(items) == 1
    item = items[0]
    assert item.kind == "actions"
    assert item.name == "lint"
    assert item.workflow_name == "CI"
    assert item.status == "completed"
    assert item.conclusion == "success"
    assert item.run_id == 12345
    assert item.check_run_id == 99
    assert meta["headRefOid"] == "abc1234"
    assert meta["state"] == "OPEN"


def test_fetch_pr_checks_third_party_check_run_kind():
    fn = _gh_graphql_returning(
        _graphql_payload([_checkrun_thirdparty_node()])
    )
    items, _ = fetch_pr_checks("foo", "bar", 1, gh_graphql_fn=fn)
    assert items[0].kind == "check_run"
    assert items[0].workflow_name is None
    assert items[0].run_id is None


def test_fetch_pr_checks_status_context_kind():
    fn = _gh_graphql_returning(
        _graphql_payload([_status_context_node(state="EXPECTED")])
    )
    items, _ = fetch_pr_checks("foo", "bar", 1, gh_graphql_fn=fn)
    assert items[0].kind == "status_context"
    # EXPECTED maps to our "expected" status, no conclusion
    assert items[0].status == "expected"
    assert items[0].conclusion is None
    assert items[0].required is True


def test_fetch_pr_checks_status_context_failure():
    fn = _gh_graphql_returning(
        _graphql_payload([_status_context_node(state="FAILURE")])
    )
    items, _ = fetch_pr_checks("foo", "bar", 1, gh_graphql_fn=fn)
    assert items[0].status == "completed"
    assert items[0].conclusion == "failure"


def test_fetch_pr_checks_first_page_sends_cursor_as_null():
    # Regression: the first page must NOT pass `cursor=""` — GraphQL's
    # `String` is nullable and `after:""` is ill-defined. Correct semantics
    # is to either omit the variable (GitHub treats it as null) or pass
    # explicit None. Either is acceptable here; passing `""` is not.
    fn = _gh_graphql_returning(
        _graphql_payload([_checkrun_actions_node(name="lint")])
    )
    fetch_pr_checks("foo", "bar", 1, gh_graphql_fn=fn)
    assert len(fn.calls) >= 1
    first_vars = fn.calls[0]["variables"]
    # `cursor` must be absent OR explicitly None — never the empty string.
    assert first_vars.get("cursor", None) is None
    assert first_vars.get("cursor") != ""


def test_fetch_pr_checks_paginates():
    fn = _gh_graphql_returning(
        _graphql_payload(
            [_checkrun_actions_node(name="lint", db_id=1)], has_next=True, end_cursor="P2"
        ),
        _graphql_payload(
            [_checkrun_actions_node(name="build", db_id=2)], has_next=False
        ),
    )
    items, _ = fetch_pr_checks("foo", "bar", 1, gh_graphql_fn=fn)
    assert [i.name for i in items] == ["lint", "build"]
    assert len(fn.calls) == 2
    assert fn.calls[1]["variables"]["cursor"] == "P2"


def test_fetch_pr_checks_rollup_null_yields_empty():
    payload = {
        "repository": {
            "pullRequest": {
                "state": "OPEN",
                "mergeable": "UNKNOWN",
                "headRefOid": "deadbeef",
                "commits": {"nodes": [{"commit": {"statusCheckRollup": None}}]},
            }
        }
    }
    fn = _gh_graphql_returning(payload)
    items, meta = fetch_pr_checks("foo", "bar", 1, gh_graphql_fn=fn)
    assert items == []
    assert meta["headRefOid"] == "deadbeef"


def test_fetch_pr_checks_empty_contexts():
    fn = _gh_graphql_returning(_graphql_payload([]))
    items, _ = fetch_pr_checks("foo", "bar", 1, gh_graphql_fn=fn)
    assert items == []


def test_fetch_pr_checks_null_details_url():
    node = _checkrun_actions_node()
    node["detailsUrl"] = None
    fn = _gh_graphql_returning(_graphql_payload([node]))
    items, _ = fetch_pr_checks("foo", "bar", 1, gh_graphql_fn=fn)
    assert items[0].url is None


# fetch_pr_meta — REST =====


def test_fetch_pr_meta_returns_rest_dict():
    captured = {}

    def gh_get(path, *, host=None, paginate=False):
        captured["path"] = path
        captured["host"] = host
        return {
            "state": "open",
            "mergeable": True,
            "mergeable_state": "clean",
            "head": {"sha": "abc"},
        }

    meta = fetch_pr_meta("foo", "bar", 7, gh_get=gh_get)
    assert meta["state"] == "open"
    assert meta["mergeable_state"] == "clean"
    assert captured["path"] == "/repos/foo/bar/pulls/7"


# fetch_run_jobs — REST =====


def test_fetch_run_jobs_paginated():
    captured = {}

    def gh_get(path, *, host=None, paginate=False):
        captured["path"] = path
        captured["paginate"] = paginate
        return {
            "jobs": [
                {
                    "id": 11,
                    "name": "lint",
                    "status": "completed",
                    "conclusion": "success",
                    "html_url": "https://example.com/job/11",
                    "run_id": 1234,
                },
                {
                    "id": 12,
                    "name": "build",
                    "status": "in_progress",
                    "conclusion": None,
                    "html_url": "https://example.com/job/12",
                    "run_id": 1234,
                },
            ]
        }

    items = fetch_run_jobs("foo", "bar", 1234, workflow_name="CI", gh_get=gh_get)
    assert captured["path"] == "/repos/foo/bar/actions/runs/1234/jobs"
    assert captured["paginate"] is True
    assert len(items) == 2
    assert items[0].kind == "actions"
    assert items[0].workflow_name == "CI"
    assert items[0].run_id == 1234
    assert items[0].required is False  # non-PR target


# fetch_job — REST =====


def test_fetch_job_single():
    def gh_get(path, *, host=None, paginate=False):
        assert path == "/repos/foo/bar/actions/jobs/55"
        return {
            "id": 55,
            "name": "deploy",
            "status": "completed",
            "conclusion": "failure",
            "html_url": "u",
            "run_id": 9,
        }

    item = fetch_job("foo", "bar", 55, workflow_name=None, gh_get=gh_get)
    assert item.name == "deploy"
    assert item.conclusion == "failure"
    assert item.kind == "actions"


# fetch_workflow_latest_run =====


def test_fetch_workflow_latest_run_uses_branch_filter():
    captured = {}

    def gh_get(path, *, host=None, paginate=False):
        captured["path"] = path
        return {
            "workflow_runs": [
                {"id": 9001, "name": "CI Build", "head_branch": "main"},
            ]
        }

    run = fetch_workflow_latest_run(
        "foo", "bar", "ci.yml", branch="main", gh_get=gh_get
    )
    assert run["id"] == 9001
    assert "/repos/foo/bar/actions/workflows/ci.yml/runs" in captured["path"]
    assert "branch=main" in captured["path"]


def test_fetch_workflow_latest_run_empty_returns_none():
    def gh_get(path, *, host=None, paginate=False):
        return {"workflow_runs": []}

    run = fetch_workflow_latest_run(
        "foo", "bar", "ci.yml", branch="main", gh_get=gh_get
    )
    assert run is None
