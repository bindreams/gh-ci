from __future__ import annotations

import io
from typing import Any

import pytest

from ghci.clock import FakeClock
from ghci.ignore import IgnoreRule
from ghci.target import PrTarget, RunTarget
from ghci.watch.loop import run_watch


# Helpers =====


def _pr_meta(state="open", mergeable=True, mergeable_state="clean"):
    return {"state": state, "mergeable": mergeable, "mergeable_state": mergeable_state}


def _graphql_payload(nodes, *, has_next=False, end_cursor="X", head_sha="sha1",
                     pr_state="OPEN"):
    return {
        "repository": {
            "pullRequest": {
                "state": pr_state,
                "mergeable": "MERGEABLE",
                "headRefOid": head_sha,
                "commits": {
                    "nodes": [
                        {"commit": {"statusCheckRollup": {
                            "contexts": {
                                "pageInfo": {"hasNextPage": has_next, "endCursor": end_cursor},
                                "nodes": nodes,
                            }}}}
                    ]
                },
            }
        }
    }


def _node(name, *, status="COMPLETED", conclusion="SUCCESS", required=False, db_id=1):
    return {
        "__typename": "CheckRun",
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "detailsUrl": f"https://example.com/{name}",
        "isRequired": required,
        "databaseId": db_id,
        "checkSuite": {
            "workflowRun": {
                "databaseId": 100,
                "url": "https://example.com/runs/100",
                "workflow": {"name": "CI"},
            }
        },
    }


def _status_context(name, *, state="EXPECTED", required=True):
    return {
        "__typename": "StatusContext",
        "context": name,
        "state": state,
        "targetUrl": None,
        "isRequired": required,
    }


def _run_watch_pr(fake_gh, *, interval=10.0, timeout=0.0, stalled_timeout=60.0,
                   ignore_rules=None, clock=None):
    clock = clock or FakeClock()
    stderr = io.StringIO()
    code, summary = run_watch(
        target=PrTarget(owner="o", repo="r", host="github.com", pr_number=1),
        ignore_rules=ignore_rules or [],
        interval=interval,
        timeout=timeout,
        stalled_timeout=stalled_timeout,
        clock=clock,
        stderr=stderr,
        gh_get=fake_gh.gh_get,
        gh_graphql_fn=fake_gh.gh_graphql,
    )
    return code, summary, stderr.getvalue(), clock


# Initial snapshot exits =====


def test_initial_snapshot_already_green(fake_gh):
    fake_gh.set_get("/repos/o/r/pulls/1", _pr_meta())
    fake_gh.queue_graphql(_graphql_payload([_node("lint", conclusion="SUCCESS")]))
    code, summary, _, _ = _run_watch_pr(fake_gh)
    assert code == 0
    assert "Passed: lint" in summary


def test_initial_snapshot_already_red(fake_gh):
    fake_gh.set_get("/repos/o/r/pulls/1", _pr_meta())
    fake_gh.queue_graphql(_graphql_payload([
        _node("ok", conclusion="SUCCESS", db_id=1),
        _node("bad", conclusion="FAILURE", db_id=2),
    ]))
    code, summary, _, _ = _run_watch_pr(fake_gh)
    assert code == 3
    assert "Failed: bad" in summary


def test_initial_snapshot_all_skipped_exits_4(fake_gh):
    fake_gh.set_get("/repos/o/r/pulls/1", _pr_meta())
    fake_gh.queue_graphql(_graphql_payload([
        _node("a", conclusion="SKIPPED"),
        _node("b", conclusion="NEUTRAL"),
    ]))
    code, summary, _, _ = _run_watch_pr(fake_gh)
    assert code == 4
    assert "no productive ci" in summary.lower()


def test_pr_closed_exits_4_immediately(fake_gh):
    fake_gh.set_get("/repos/o/r/pulls/1",
                     _pr_meta(state="closed"))
    # No GraphQL call expected.
    code, summary, _, _ = _run_watch_pr(fake_gh)
    assert code == 4
    assert "closed" in summary.lower()


def test_pr_dirty_exits_4_immediately(fake_gh):
    fake_gh.set_get("/repos/o/r/pulls/1",
                     _pr_meta(mergeable=False, mergeable_state="dirty"))
    code, summary, _, _ = _run_watch_pr(fake_gh)
    assert code == 4
    assert "conflict" in summary.lower()


def test_pr_behind_exits_4_immediately(fake_gh):
    fake_gh.set_get("/repos/o/r/pulls/1",
                     _pr_meta(mergeable_state="behind"))
    code, summary, _, _ = _run_watch_pr(fake_gh)
    assert code == 4
    assert "behind" in summary.lower()


# Looping =====


def test_loop_red_after_one_tick(fake_gh):
    fake_gh.set_get("/repos/o/r/pulls/1", _pr_meta())
    fake_gh.queue_graphql(
        # initial: in progress
        _graphql_payload([_node("build", status="IN_PROGRESS", conclusion=None)]),
        # tick 1: failed
        _graphql_payload([_node("build", status="COMPLETED", conclusion="FAILURE")]),
    )
    clock = FakeClock()
    code, summary, stderr, _ = _run_watch_pr(fake_gh, interval=5.0, clock=clock)
    assert code == 3
    assert "Failed: build" in summary
    assert 'Job "build" failed' in stderr


def test_loop_green_after_one_tick(fake_gh):
    fake_gh.set_get("/repos/o/r/pulls/1", _pr_meta())
    fake_gh.queue_graphql(
        _graphql_payload([_node("build", status="IN_PROGRESS", conclusion=None)]),
        _graphql_payload([_node("build", status="COMPLETED", conclusion="SUCCESS")]),
    )
    code, summary, stderr, _ = _run_watch_pr(fake_gh, interval=5.0)
    assert code == 0
    assert 'Job "build" green' in stderr


def test_timeout_exits_7(fake_gh):
    fake_gh.set_get("/repos/o/r/pulls/1", _pr_meta())
    # Always returns in-progress so we never exit naturally.
    fake_gh.queue_graphql(*[
        _graphql_payload([_node("build", status="IN_PROGRESS", conclusion=None)])
        for _ in range(100)
    ])
    clock = FakeClock()
    code, summary, _, _ = _run_watch_pr(fake_gh, interval=5.0, timeout=12.0, clock=clock)
    assert code == 7
    assert "In progress" in summary or "still" in summary.lower()


def test_required_check_stalls(fake_gh):
    fake_gh.set_get("/repos/o/r/pulls/1", _pr_meta())
    # Always returns the same expected required check.
    fake_gh.queue_graphql(*[
        _graphql_payload([_status_context("ci/external", state="EXPECTED", required=True)])
        for _ in range(100)
    ])
    clock = FakeClock()
    code, summary, _, _ = _run_watch_pr(
        fake_gh, interval=5.0, stalled_timeout=30.0, clock=clock,
    )
    assert code == 5
    assert "stalled" in summary.lower() or "not reported" in summary.lower()


def test_ignored_failure_does_not_exit_3(fake_gh):
    fake_gh.set_get("/repos/o/r/pulls/1", _pr_meta())
    fake_gh.queue_graphql(_graphql_payload([
        _node("ok", conclusion="SUCCESS", db_id=1),
        _node("flaky", conclusion="FAILURE", db_id=2),
    ]))
    code, summary, _, _ = _run_watch_pr(
        fake_gh, ignore_rules=[IgnoreRule("job", "flaky")],
    )
    assert code == 0
    assert "Failed (ignored by --ignore): flaky" in summary


def test_initial_head_sha_falls_back_to_rest_when_graphql_null(fake_gh):
    # Regression: if the GraphQL rollup returns headRefOid: None on the
    # initial fetch, the loop must seed state.head_sha from the REST
    # head.sha (always present for an open PR). Otherwise a subsequent real
    # tick with the same SHA would either be missed as a force-push or
    # falsely flagged as one.
    rest_sha = "restsha1234567"
    fake_gh.set_get(
        "/repos/o/r/pulls/1",
        {
            "state": "open",
            "mergeable": True,
            "mergeable_state": "clean",
            "head": {"sha": rest_sha},
        },
    )
    fake_gh.queue_graphql(
        # Initial: GraphQL returns headRefOid: None (and an in-progress job
        # so we proceed into the loop).
        _graphql_payload(
            [_node("build", status="IN_PROGRESS", conclusion=None)],
            head_sha=None,
        ),
        # Tick 1: GraphQL surfaces the real SHA (matches REST). Job completes
        # green. This must NOT be reported as a force-push, because state
        # was seeded from REST at init.
        _graphql_payload(
            [_node("build", status="COMPLETED", conclusion="SUCCESS")],
            head_sha=rest_sha,
        ),
    )
    code, summary, stderr, _ = _run_watch_pr(fake_gh, interval=5.0)
    assert code == 0
    # The resolution line includes the head SHA prefix when present — this
    # is the user-observable proof that state.head_sha was seeded from REST.
    assert rest_sha[:7] in stderr
    # And no spurious force-push line was emitted on tick 1.
    assert "Force-push detected" not in stderr


def test_force_push_event_emitted(fake_gh):
    fake_gh.set_get("/repos/o/r/pulls/1", _pr_meta())
    fake_gh.queue_graphql(
        _graphql_payload(
            [_node("build", status="IN_PROGRESS", conclusion=None)],
            head_sha="oldsha1",
        ),
        # tick 1: same job, different head_sha → force-push
        _graphql_payload(
            [_node("build", status="IN_PROGRESS", conclusion=None)],
            head_sha="newsha2",
        ),
        # tick 2: now green
        _graphql_payload(
            [_node("build", status="COMPLETED", conclusion="SUCCESS")],
            head_sha="newsha2",
        ),
    )
    code, summary, stderr, _ = _run_watch_pr(fake_gh, interval=5.0)
    assert code == 0
    assert "Force-push detected" in stderr
    assert "newsha2"[:7] in stderr


# Non-PR target — runs without PR meta check =====


def test_run_target_does_not_call_pr_meta(fake_gh):
    fake_gh.set_get(
        "/repos/o/r/actions/runs/1234/jobs",
        {"jobs": [
            {"id": 1, "name": "lint", "status": "completed", "conclusion": "success",
             "html_url": "u", "run_id": 1234},
        ]},
    )
    fake_gh.set_get(
        "/repos/o/r/actions/runs/1234",
        {"id": 1234, "name": "CI Build"},
    )
    stderr = io.StringIO()
    code, summary = run_watch(
        target=RunTarget(owner="o", repo="r", host="github.com", run_id=1234),
        ignore_rules=[],
        interval=10.0,
        timeout=0.0,
        stalled_timeout=60.0,
        clock=FakeClock(),
        stderr=stderr,
        gh_get=fake_gh.gh_get,
        gh_graphql_fn=fake_gh.gh_graphql,
    )
    assert code == 0
    assert "Passed: lint" in summary
    # No /pulls/ call.
    assert not any("/pulls/" in c["path"] for c in fake_gh.get_calls)
