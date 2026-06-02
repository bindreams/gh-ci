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
from ghci.colors import RESULT_STYLE_COLOR, Palette, _ResultStyle, ensure_palette
from ghci.conflicts import ConflictOutcome, assess_pr, message_for, result_style_for
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
    palette: Palette | None = None,
) -> tuple[int, str]:
    """Run the watch poll loop. Returns (exit_code, summary_text).

    Events stream to `stderr`. The summary should be printed to stdout by the caller.
    """
    clk = clock or RealClock()
    err = stderr or sys.stderr
    p = ensure_palette(palette)
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
            return 4, _conflict_summary(outcome, target.pr_number, palette=p)
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
    resolution = _resolution_line(target, head_sha, items, ignore_rules)
    print(p.style(resolution, dim=True), file=err, flush=True)

    state = initial_state(items, head_sha=head_sha)

    # Initial-snapshot exit conditions (no stalled detection on first snapshot —
    # there's no prior tick to compare against).
    decision = _evaluate_exit(
        state, ignore_rules=ignore_rules, now=clk.now(),
        stalled_timeout=None,
        timed_out=False,
        palette=p,
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
                return 4, _conflict_summary(outcome, target.pr_number, palette=p)

        # Refetch.
        items, meta = _fetch_items(target, gh_get=gh_get, gh_graphql_fn=gh_graphql_fn, host=host)
        new_head_sha = meta.get("headRefOid") if meta else None

        now = clk.now()
        state, events, force_pushed = update_state(
            state, items, now=now, new_head_sha=new_head_sha,
            ignore_rules=ignore_rules,
        )

        when = datetime.now()
        if force_pushed:
            print(
                format_force_push_line(when=when, new_sha=new_head_sha or "?", palette=p),
                file=err, flush=True,
            )
            # Skip exit-condition eval on force-push tick. Just check timeout.
            if timeout > 0 and (clk.now() - start_time) > timeout:
                return _timeout_summary(state, ignore_rules, palette=p)
            continue

        for evt in events:
            print(format_event_line(evt, when=when, palette=p), file=err, flush=True)

        # Timeout takes priority over normal exit conditions.
        if timeout > 0 and (clk.now() - start_time) > timeout:
            return _timeout_summary(state, ignore_rules, palette=p)

        decision = _evaluate_exit(
            state, ignore_rules=ignore_rules, now=clk.now(),
            stalled_timeout=stalled_timeout, timed_out=False,
            palette=p,
        )
        if decision is not None:
            return decision


# Helpers =====


def _conflict_summary(
    outcome: ConflictOutcome,
    pr_number: int,
    *,
    palette: Palette | None = None,
) -> str:
    p = ensure_palette(palette)
    result_line = f"Result: {outcome.value}"
    style = result_style_for(outcome)
    if style is not None:
        color = RESULT_STYLE_COLOR[style]
        result_line = p.style(result_line, color=color, bold=True)
    return f"{result_line}\n{message_for(outcome, pr_number=pr_number)}"


def _resolution_line(
    target: ResolvedTarget,
    head_sha: str | None,
    items: list[CheckItem] | None = None,
    ignore_rules: list[IgnoreRule] | None = None,
) -> str:
    if isinstance(target, PrTarget):
        # Prefer the plan-spec format: include a workflow run id and its
        # status if any non-ignored Actions checks have been surfaced.
        run_info = _first_actions_run_info(items or [], ignore_rules or [])
        if run_info is not None:
            run_id, status_text = run_info
            return (
                f"Watching run {run_id} ({status_text}) on PR #{target.pr_number} "
                f"in {target.owner}/{target.repo}"
            )
        # Fallback: no Actions runs surfaced yet — show PR + head SHA.
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


def _first_actions_run_info(
    items: list[CheckItem], ignore_rules: list[IgnoreRule]
) -> tuple[int, str] | None:
    """Return (run_id, human-status) for the first non-ignored Actions run, or None.

    Used to compose the PR-target resolution line per plan §watch step 1.

    The status is aggregated across *all* non-ignored Actions items that share
    the chosen run_id, so a multi-job run with mixed statuses correctly
    reports as still in progress rather than picking off the first item's
    status (which is GraphQL-order-dependent and may be misleading).
    """
    chosen_run_id: int | None = None
    for it in items:
        if it.kind != "actions" or it.run_id is None:
            continue
        if ignore_matches(it, ignore_rules):
            continue
        chosen_run_id = it.run_id
        break
    if chosen_run_id is None:
        return None

    statuses = [
        (it.status or "").lower()
        for it in items
        if it.kind == "actions"
        and it.run_id == chosen_run_id
        and not ignore_matches(it, ignore_rules)
    ]
    return chosen_run_id, _aggregate_run_status(statuses)


# Order of preference when aggregating per-job statuses up to a "run-level"
# status. More-active states win over less-active ones so a single
# in-progress job dominates a run that's otherwise queued or completed.
_STATUS_PRIORITY = (
    "in_progress",
    "queued",
    "waiting",
    "requested",
    "pending",
    "expected",
    "completed",
)


def _aggregate_run_status(statuses: list[str]) -> str:
    """Pick the most-active status from a per-job status list and humanize it."""
    if not statuses:
        return "unknown"
    chosen = next(
        (s for s in _STATUS_PRIORITY if s in statuses),
        # Fall back to whatever the items reported if none match the known set.
        statuses[0],
    )
    return _humanize_status(chosen)


def _humanize_status(status: str) -> str:
    """Translate a status string to a short, human phrase.

    Per the plan example ("Watching run 1234567 (in progress) ..."), the
    parenthesized value is the workflow run's *status* (queued / in_progress
    / completed), not its terminal conclusion. Underscores → spaces.
    """
    s = (status or "").lower()
    return s.replace("_", " ") if s else "unknown"


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
    palette: Palette | None = None,
) -> tuple[int, str] | None:
    """Returns (code, summary) if an exit condition fires; else None."""
    p = ensure_palette(palette)
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
        # S5: surface still-in-flight non-ignored jobs when bailing on red so
        # the agent knows what was running when we exited.
        return 3, format_summary(
            items, ignored, result_line=result_line,
            in_flight_label="In progress",
            result_style=_ResultStyle.RED, palette=p,
        )

    # 2. Stalled.
    if stalled_timeout is not None:
        stalled = stalled_required_keys(
            state, now=now, stalled_timeout=stalled_timeout,
            ignore_rules=ignore_rules,
        )
        if stalled:
            # S2: use the plan's exact wording — quote the name and end with
            # a period — and split it onto its own line.
            result_line = (
                f'Result: required check stalled\n'
                f'Required check "{stalled[0].name}" still unreported after '
                f'{stalled_timeout:.0f}s with no CI progress — likely misconfigured.'
            )
            # S5: include still-in-flight non-ignored jobs.
            return 5, format_summary(
                items, ignored, result_line=result_line,
                stalled_items=stalled, in_flight_label="In progress",
                result_style=_ResultStyle.RED, palette=p,
            )

    # 3. All done?
    in_flight = [it for it in active if it.status != "completed"]
    if not in_flight:
        passes = [it for it in active if classify_conclusion(it.conclusion) == Outcome.PASSED]
        if passes:
            return 0, format_summary(
                items, ignored, result_line="Result: green",
                result_style=_ResultStyle.GREEN, palette=p,
            )
        return 4, format_summary(
            items, ignored, result_line="Result: no productive CI ran",
            result_style=_ResultStyle.RED, palette=p,
        )
    return None


def _timeout_summary(
    state: WatchState,
    ignore_rules: list[IgnoreRule],
    *,
    palette: Palette | None = None,
) -> tuple[int, str]:
    p = ensure_palette(palette)
    items = list(state.items_by_key.values())
    ignored = [it for it in items if ignore_matches(it, ignore_rules)]
    result_line = "Result: timeout reached while watching"
    return 7, format_summary(
        items, ignored, result_line=result_line,
        in_flight_label="In progress (timed out)",
        result_style=_ResultStyle.YELLOW, palette=p,
    )
