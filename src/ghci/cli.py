from __future__ import annotations

import argparse
import sys
from enum import IntEnum
from pathlib import Path
from typing import Any, Callable, TextIO

import pytimeparse2

from ghci.clock import Clock, RealClock
from ghci.gh import GhError, gh_api_download, gh_api_get, gh_api_graphql
from ghci.ignore import IgnoreParseError, IgnoreRule, parse_ignore
from ghci.logs import run_logs
from ghci.status import evaluate_snapshot
from ghci.target import (
    EmptyTargetError,
    TargetParseError,
    host_for_gh,
    resolve_target,
)


class ExitCode(IntEnum):
    OK = 0
    UNEXPECTED = 1
    BAD_ARGS = 2
    RED_CI = 3
    NO_PRODUCTIVE = 4
    STALLED = 5
    GH_ERROR = 6
    TIMEOUT_OR_IN_PROGRESS = 7
    SIGINT = 130


_EXIT_CODE_HELP = (
    "Exit codes:\n"
    "  0   success\n"
    "  1   unexpected error\n"
    "  2   bad CLI args / unrecognized target\n"
    "  3   CI failed — one or more non-ignored checks are red\n"
    "  4   PR conflict, closed PR, behind base, or no productive CI ran\n"
    "  5   required check stalled (watch only)\n"
    "  6   gh subprocess error (non-zero exit, missing binary, auth, network)\n"
    "  7   watch: --timeout reached; status: still in progress\n"
    "  130 Ctrl-C / SIGINT\n"
)


def main(
    argv: list[str] | None = None,
    *,
    gh_get: Callable[..., Any] | None = None,
    gh_graphql_fn: Callable[..., dict] | None = None,
    gh_download_fn: Callable[..., int] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    clock: Clock | None = None,
) -> int:
    gh_get = gh_get or gh_api_get
    gh_graphql_fn = gh_graphql_fn or gh_api_graphql
    gh_download_fn = gh_download_fn or gh_api_download
    out = stdout or sys.stdout
    err = stderr or sys.stderr
    clk = clock or RealClock()

    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        # argparse exits 2 on bad args, 0 on --help. Preserve.
        return int(e.code) if e.code is not None else int(ExitCode.OK)

    if args.subcommand is None:
        parser.print_help(file=err)
        return int(ExitCode.OK)

    try:
        if args.subcommand == "status":
            return _do_status(args, gh_get=gh_get, gh_graphql_fn=gh_graphql_fn,
                              out=out, err=err)
        if args.subcommand == "watch":
            # Late import to avoid pulling watch deps when not needed.
            from ghci.watch.loop import run_watch
            return _do_watch(args, gh_get=gh_get, gh_graphql_fn=gh_graphql_fn,
                             out=out, err=err, clock=clk, run_watch_fn=run_watch)
        if args.subcommand == "logs":
            return _do_logs(args, gh_get=gh_get, gh_download_fn=gh_download_fn,
                            err=err)
    except KeyboardInterrupt:
        print("Interrupted", file=err)
        return int(ExitCode.SIGINT)
    except EmptyTargetError as e:
        print(str(e), file=err)
        return int(ExitCode.BAD_ARGS)
    except FileNotFoundError as e:
        print(f"gh: not found ({e})", file=err)
        return int(ExitCode.GH_ERROR)
    except GhError as e:
        print(f"gh: {e.stderr.strip()}", file=err)
        return int(ExitCode.GH_ERROR)
    except Exception as e:
        # Catch-all for unexpected errors (OSError, etc.). Print a clean,
        # one-line message — no traceback — and exit 1. KeyboardInterrupt
        # inherits from BaseException (not Exception), so it stays handled
        # above and won't be swallowed here.
        print(f"gh-ci: unexpected error: {e}", file=err)
        return int(ExitCode.UNEXPECTED)

    return int(ExitCode.UNEXPECTED)


# Subcommand handlers =====


def _do_status(args, *, gh_get, gh_graphql_fn, out, err) -> int:
    target_result = _resolve_or_exit_2(args.target, gh_get=gh_get, err=err)
    if isinstance(target_result, int):
        return target_result
    target = target_result

    rules = _parse_rules_or_exit_2(args.ignore, err=err)
    if isinstance(rules, int):
        return rules

    host = host_for_gh(target)
    from ghci.checks import fetch_pr_meta, fetch_pr_checks
    from ghci.target import PrTarget

    items, pr_meta_rest = _fetch_for_status(target, host, gh_get, gh_graphql_fn)
    pr_number = target.pr_number if isinstance(target, PrTarget) else None
    code, summary = evaluate_snapshot(
        items, pr_meta=pr_meta_rest, pr_number=pr_number, ignore_rules=rules,
    )
    print(summary, file=out)
    return code


def _do_watch(args, *, gh_get, gh_graphql_fn, out, err, clock, run_watch_fn) -> int:
    target_result = _resolve_or_exit_2(args.target, gh_get=gh_get, err=err)
    if isinstance(target_result, int):
        return target_result
    target = target_result

    rules = _parse_rules_or_exit_2(args.ignore, err=err)
    if isinstance(rules, int):
        return rules

    durations = _parse_durations_or_exit_2(args, err=err)
    if isinstance(durations, int):
        return durations
    interval, timeout, stalled_timeout = durations

    code, summary = run_watch_fn(
        target=target,
        ignore_rules=rules,
        interval=interval,
        timeout=timeout,
        stalled_timeout=stalled_timeout,
        clock=clock,
        stderr=err,
        gh_get=gh_get,
        gh_graphql_fn=gh_graphql_fn,
    )
    print(summary, file=out)
    return code


def _do_logs(args, *, gh_get, gh_download_fn, err) -> int:
    target_result = _resolve_or_exit_2(args.target, gh_get=gh_get, err=err)
    if isinstance(target_result, int):
        return target_result
    target = target_result

    rules = _parse_rules_or_exit_2(args.ignore, err=err)
    if isinstance(rules, int):
        return rules

    output_dir = Path(args.output_dir) if args.output_dir else None
    return run_logs(
        target=target,
        failed_only=args.failed,
        ignore_rules=rules,
        output_dir=output_dir,
        stderr=err,
        gh_get=gh_get,
        gh_download_fn=gh_download_fn,
    )


# Helpers =====


def _fetch_for_status(target, host, gh_get, gh_graphql_fn):
    from ghci.checks import (
        fetch_job,
        fetch_pr_checks,
        fetch_pr_meta,
        fetch_run,
        fetch_run_jobs,
        fetch_workflow_latest_run_or_raise,
    )
    from ghci.target import JobTarget, PrTarget, RunTarget, WorkflowTarget

    if isinstance(target, PrTarget):
        pr_meta = fetch_pr_meta(target.owner, target.repo, target.pr_number,
                                 host=host, gh_get=gh_get)
        items, _ = fetch_pr_checks(target.owner, target.repo, target.pr_number,
                                    host=host, gh_graphql_fn=gh_graphql_fn)
        return items, pr_meta
    if isinstance(target, RunTarget):
        run = fetch_run(target.owner, target.repo, target.run_id,
                         host=host, gh_get=gh_get)
        items = fetch_run_jobs(target.owner, target.repo, target.run_id,
                                workflow_name=run.get("name"),
                                host=host, gh_get=gh_get)
        return items, None
    if isinstance(target, JobTarget):
        item = fetch_job(target.owner, target.repo, target.job_id,
                          workflow_name=None, host=host, gh_get=gh_get)
        return [item], None
    assert isinstance(target, WorkflowTarget)
    # Raises EmptyTargetError if the workflow has no runs on the branch —
    # caught at the main() entry to produce exit 2 with a clear message.
    run = fetch_workflow_latest_run_or_raise(
        target.owner, target.repo, target.workflow_path,
        branch=target.branch, host=host, gh_get=gh_get,
    )
    items = fetch_run_jobs(target.owner, target.repo, run["id"],
                            workflow_name=run.get("name"),
                            host=host, gh_get=gh_get)
    return items, None


def _resolve_or_exit_2(url, *, gh_get, err):
    try:
        return resolve_target(url, gh_get=gh_get)
    except TargetParseError as e:
        print(str(e), file=err)
        return int(ExitCode.BAD_ARGS)


def _parse_rules_or_exit_2(values, *, err):
    rules: list[IgnoreRule] = []
    for v in values or []:
        try:
            rules.append(parse_ignore(v))
        except IgnoreParseError as e:
            print(str(e), file=err)
            return int(ExitCode.BAD_ARGS)
    return rules


def _parse_durations_or_exit_2(args, *, err):
    try:
        interval = _parse_duration("--interval", args.interval)
        timeout = _parse_duration("--timeout", args.timeout)
        stalled = _parse_duration("--stalled-timeout", args.stalled_timeout)
    except _BadDuration as e:
        print(str(e), file=err)
        return int(ExitCode.BAD_ARGS)
    return interval, timeout, stalled


class _BadDuration(ValueError):
    pass


def _parse_duration(flag: str, value: str) -> float:
    """Parse a duration like '30s', '5m', '1.5h'. Returns seconds (float)."""
    if value in (None, ""):
        raise _BadDuration(f"{flag}: empty value")
    seconds = pytimeparse2.parse(value)
    if seconds is None:
        # Try numeric value (bare number = seconds)
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            raise _BadDuration(
                f"{flag}: invalid duration {value!r}. "
                f"Expected forms: 30s, 5m, 1.5h, 2h30m."
            )
    return float(seconds)


# Arg parser =====


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gh-ci",
        description=(
            "A CLI tool to make agents (and humans) better at watching "
            "GitHub CI. Wraps `gh` with sensible defaults: detects PR "
            "conflicts up front, exits on first red, fetches in-progress "
            "logs, and accepts any GitHub URL as a target."
        ),
        epilog=_EXIT_CODE_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="subcommand")

    # status
    p_status = sub.add_parser(
        "status",
        help="Single-snapshot inspection; never blocks.",
        epilog=_EXIT_CODE_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_status.add_argument("target", help="GitHub PR/run/job/workflow URL")
    p_status.add_argument(
        "--ignore", action="append", default=[],
        metavar="PREFIX:NAME",
        help="Skip a check. Prefix is one of workflow/job/check. Repeatable.",
    )

    # watch
    p_watch = sub.add_parser(
        "watch",
        help="Block until CI is decisive; exits on first red.",
        epilog=_EXIT_CODE_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_watch.add_argument("target", help="GitHub PR/run/job/workflow URL")
    p_watch.add_argument(
        "--ignore", action="append", default=[],
        metavar="PREFIX:NAME",
        help="Skip a check. Prefix is one of workflow/job/check. Repeatable.",
    )
    p_watch.add_argument(
        "--interval", default="10s",
        help="Poll interval (default 10s). Accepts 30s, 5m, 1.5h, etc.",
    )
    p_watch.add_argument(
        "--timeout", default="30m",
        help="Maximum wall-clock time to watch (default 30m; 0 disables).",
    )
    p_watch.add_argument(
        "--stalled-timeout", default="60s",
        help=(
            "How long a required check can be in queued/expected/pending state "
            "before exit 5 fires (default 60s)."
        ),
    )

    # logs
    p_logs = sub.add_parser(
        "logs",
        help="Download job logs (including in-progress) to disk.",
        epilog=_EXIT_CODE_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_logs.add_argument("target", help="GitHub PR/run/job/workflow URL")
    p_logs.add_argument(
        "--failed", action="store_true",
        help="Download only failed jobs' logs.",
    )
    p_logs.add_argument(
        "--ignore", action="append", default=[],
        metavar="PREFIX:NAME",
        help="Skip a check. Prefix is one of workflow/job/check. Repeatable.",
    )
    p_logs.add_argument(
        "--output-dir",
        help="Where to write logs (default: platform Downloads dir).",
    )

    return parser
