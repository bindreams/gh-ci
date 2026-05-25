from __future__ import annotations

import sys
from datetime import datetime
from typing import Any, Callable, TextIO

from ghci.checks import (
    CheckItem,
    Outcome,
    classify_conclusion,
    fetch_job,
    fetch_pr_checks,
    fetch_pr_meta,
    fetch_run,
    fetch_run_jobs,
    fetch_workflow_latest_run_or_raise,
)
from ghci.clock import Clock, RealClock
from ghci.conflicts import ConflictOutcome, assess_pr, message_for
from ghci.gh import gh_api_get, gh_api_graphql
from ghci.ignore import IgnoreRule, matches as ignore_matches
from ghci.status import format_summary
from ghci.target import (
    JobTarget,
    PrTarget,
    ResolvedTarget,
    RunTarget,
    WorkflowTarget,
    host_for_gh,
)
from ghci.watch.output import format_event_line, format_force_push_line
from ghci.watch.state import (
    WatchState,
    initial_state,
    stalled_required_keys,
    update_state,
)


def run_watch(
    target: ResolvedTarget,
    *,
    ignore_rules: list[IgnoreRule],
    interval: float,
    timeout: float,
    stalled_timeout: float,
    clock: Clock | None = None,
    stderr: TextIO | None = None,
    gh_get: Callable[..., Any] = gh_api_get,
    gh_graphql_fn: Callable[..., dict] = gh_api_graphql,
) -> tuple[int, str]:
    """Run the watch poll loop. Returns (exit_code, summary_text).

    Events stream to `stderr`. The summary should be printed to stdout by the caller.
    """
    clk = clock or RealClock()
    err = stderr or sys.stderr
    host = host_for_gh(target)

    # Pre-loop conflict check.
    pr_meta: dict | None = None
    if isinstance(target, PrTarget):
        pr_meta = fetch_pr_meta(
            target.owner, target.repo, target.pr_number,
            host=host, gh_get=gh_get,
        )
        outcome = assess_pr(pr_meta)
        if outcome in (ConflictOutcome.CLOSED, ConflictOutcome.DIRTY, ConflictOutcome.BEHIND):
            return 4, _conflict_summary(outcome, target.pr_number)
        # OK or UNKNOWN — fall through.

    # Initial item fetch + resolution line.
    items, meta = _fetch_items(target, gh_get=gh_get, gh_graphql_fn=gh_graphql_fn, host=host)
    head_sha = meta.get("headRefOid") if meta else None
    # If the GraphQL rollup didn't surface a head SHA (e.g. the PR has no
    # commits visible via that query, or a fixture inconsistency), fall back
    # to the REST head.sha — it's always present for an open PR. Seeding
    # state.head_sha from REST avoids emitting a spurious force-push event
    # on the first tick where GraphQL recovers a non-null headRefOid.
    if head_sha is None and pr_meta is not None:
        rest_head = pr_meta.get("head") or {}
        rest_sha = rest_head.get("sha") if isinstance(rest_head, dict) else None
        if rest_sha:
            head_sha = rest_sha
    print(_resolution_line(target, head_sha), file=err, flush=True)

    state = initial_state(items, head_sha=head_sha)

    # Initial-snapshot exit conditions (no stalled detection on first snapshot —
    # there's no prior tick to compare against).
    decision = _evaluate_exit(
        state, ignore_rules=ignore_rules, now=clk.now(),
        stalled_timeout=None,
        timed_out=False,
    )
    if decision is not None:
        return decision

    start_time = clk.now()

    while True:
        clk.sleep(interval)

        # Re-check PR conflicts.
        if isinstance(target, PrTarget):
            pr_meta = fetch_pr_meta(
                target.owner, target.repo, target.pr_number,
                host=host, gh_get=gh_get,
            )
            outcome = assess_pr(pr_meta)
            if outcome in (ConflictOutcome.CLOSED, ConflictOutcome.DIRTY, ConflictOutcome.BEHIND):
                return 4, _conflict_summary(outcome, target.pr_number)

        # Refetch.
        items, meta = _fetch_items(target, gh_get=gh_get, gh_graphql_fn=gh_graphql_fn, host=host)
        new_head_sha = meta.get("headRefOid") if meta else None

        now = clk.now()
        state, events, force_pushed = update_state(
            state, items, now=now, new_head_sha=new_head_sha,
        )

        when = datetime.now()
        if force_pushed:
            print(
                format_force_push_line(when=when, new_sha=new_head_sha or "?"),
                file=err, flush=True,
            )
            # Skip exit-condition eval on force-push tick. Just check timeout.
            if timeout > 0 and (clk.now() - start_time) > timeout:
                return _timeout_summary(state, ignore_rules)
            continue

        for evt in events:
            print(format_event_line(evt, when=when), file=err, flush=True)

        # Timeout takes priority over normal exit conditions.
        if timeout > 0 and (clk.now() - start_time) > timeout:
            return _timeout_summary(state, ignore_rules)

        decision = _evaluate_exit(
            state, ignore_rules=ignore_rules, now=clk.now(),
            stalled_timeout=stalled_timeout, timed_out=False,
        )
        if decision is not None:
            return decision


# Helpers =====


def _conflict_summary(outcome: ConflictOutcome, pr_number: int) -> str:
    return f"Result: {outcome.value}\n{message_for(outcome, pr_number=pr_number)}"


def _resolution_line(target: ResolvedTarget, head_sha: str | None) -> str:
    if isinstance(target, PrTarget):
        sha = f" (head {head_sha[:7]})" if head_sha else ""
        return f"Watching PR #{target.pr_number} in {target.owner}/{target.repo}{sha}"
    if isinstance(target, RunTarget):
        return f"Watching run {target.run_id} in {target.owner}/{target.repo}"
    if isinstance(target, JobTarget):
        return (
            f"Watching job {target.job_id} (run {target.run_id}) "
            f"in {target.owner}/{target.repo}"
        )
    assert isinstance(target, WorkflowTarget)
    return (
        f"Watching workflow {target.workflow_path} on branch {target.branch} "
        f"in {target.owner}/{target.repo}"
    )


def _fetch_items(
    target: ResolvedTarget,
    *,
    gh_get: Callable[..., Any],
    gh_graphql_fn: Callable[..., dict],
    host: str | None,
) -> tuple[list[CheckItem], dict | None]:
    if isinstance(target, PrTarget):
        items, meta = fetch_pr_checks(
            target.owner, target.repo, target.pr_number,
            host=host, gh_graphql_fn=gh_graphql_fn,
        )
        return items, meta
    if isinstance(target, RunTarget):
        run = fetch_run(target.owner, target.repo, target.run_id, host=host, gh_get=gh_get)
        items = fetch_run_jobs(
            target.owner, target.repo, target.run_id,
            workflow_name=run.get("name"), host=host, gh_get=gh_get,
        )
        return items, None
    if isinstance(target, JobTarget):
        item = fetch_job(
            target.owner, target.repo, target.job_id,
            workflow_name=None, host=host, gh_get=gh_get,
        )
        return [item], None
    assert isinstance(target, WorkflowTarget)
    # Raises EmptyTargetError if the workflow has no runs on the branch —
    # caught at the cli.py main() entry to produce exit 2 with a clear
    # message rather than silently returning [] (which downstream would
    # misclassify as "no productive CI ran").
    run = fetch_workflow_latest_run_or_raise(
        target.owner, target.repo, target.workflow_path,
        branch=target.branch, host=host, gh_get=gh_get,
    )
    items = fetch_run_jobs(
        target.owner, target.repo, run["id"],
        workflow_name=run.get("name"), host=host, gh_get=gh_get,
    )
    return items, None


def _evaluate_exit(
    state: WatchState,
    *,
    ignore_rules: list[IgnoreRule],
    now: float,
    stalled_timeout: float | None,
    timed_out: bool,
) -> tuple[int, str] | None:
    """Returns (code, summary) if an exit condition fires; else None."""
    items = list(state.items_by_key.values())
    ignored = [it for it in items if ignore_matches(it, ignore_rules)]
    active = [it for it in items if not ignore_matches(it, ignore_rules)]

    # 1. First red.
    failed = [it for it in active if classify_conclusion(it.conclusion) == Outcome.FAILED]
    if failed:
        first = failed[0]
        result_line = (
            f'Result: red CI (job "{first.name}" {first.conclusion})\n'
            f'To continue watching despite this failure: '
            f'gh-ci watch <target> --ignore job:"{first.name}"'
        )
        return 3, format_summary(items, ignored, result_line=result_line)

    # 2. Stalled.
    if stalled_timeout is not None:
        stalled = stalled_required_keys(
            state, now=now, stalled_timeout=stalled_timeout,
            ignore_rules=ignore_rules,
        )
        if stalled:
            names = [it.name for it in stalled]
            result_line = (
                f'Result: required check stalled ({names[0]} has not reported '
                f'in {stalled_timeout:.0f}s — likely misconfigured)'
            )
            return 5, format_summary(
                items, ignored, result_line=result_line, stalled_names=names,
            )

    # 3. All done?
    in_flight = [it for it in active if it.status != "completed"]
    if not in_flight:
        passes = [it for it in active if classify_conclusion(it.conclusion) == Outcome.PASSED]
        if passes:
            return 0, format_summary(items, ignored, result_line="Result: green")
        return 4, format_summary(
            items, ignored, result_line="Result: no productive CI ran",
        )
    return None


def _timeout_summary(state: WatchState, ignore_rules: list[IgnoreRule]) -> tuple[int, str]:
    items = list(state.items_by_key.values())
    ignored = [it for it in items if ignore_matches(it, ignore_rules)]
    result_line = "Result: timeout reached while watching"
    return 7, format_summary(
        items, ignored, result_line=result_line,
        in_flight_label="In progress (timed out)",
    )
