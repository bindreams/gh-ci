from __future__ import annotations

import json
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TextIO

import platformdirs

from ghci.checks import Outcome, classify_conclusion, fetch_pr_checks
from ghci.colors import Palette, ensure_palette
from ghci.gh import GhError, gh_api_download, gh_api_get, gh_api_graphql, parse_http_status
from ghci.ignore import IgnoreRule
from ghci.target import (
    EmptyTargetError,
    JobTarget,
    PrTarget,
    ResolvedTarget,
    RunTarget,
    WorkflowTarget,
    host_for_gh,
)


_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]")


def run_logs(
    target: ResolvedTarget,
    *,
    failed_only: bool,
    ignore_rules: list[IgnoreRule],
    output_dir: Path | None,
    stderr: TextIO | None = None,
    gh_get: Callable[..., Any] = gh_api_get,
    gh_graphql_fn: Callable[..., dict] = gh_api_graphql,
    gh_download_fn: Callable[..., int] = gh_api_download,
    now: datetime | None = None,
    palette: Palette | None = None,
) -> int:
    err = stderr or sys.stderr
    p = ensure_palette(palette)
    host = host_for_gh(target)
    nowdt = now or datetime.now(timezone.utc)

    out_dir = _resolve_output_dir(output_dir, err=err, palette=p)
    if out_dir is None:
        return 2

    # Determine list of runs to process.
    try:
        runs = _resolve_runs_for_target(
            target, gh_get=gh_get, gh_graphql_fn=gh_graphql_fn, host=host,
        )
    except GhError as e:
        print(p.style(f"gh error: {e}", color="red"), file=err)
        return 6

    if not runs:
        print(p.style("No matching runs to download logs for.", color="yellow"), file=err)
        return 0

    # Print resolution line (dim).
    total_jobs = sum(len(r["jobs"]) for r in runs)
    resolved = (
        f"Resolved target to "
        f"{'run' if len(runs) == 1 else 'runs'} "
        f"{', '.join(str(r['run_id']) for r in runs)} "
        f"({total_jobs} job{'s' if total_jobs != 1 else ''}) "
        f"in {target.owner}/{target.repo}"
    )
    print(p.style(resolved, dim=True), file=err)

    any_gh_error = False
    summary_counts = {"written": 0, "partial": 0, "truncated": 0, "skipped": 0}

    for run in runs:
        # Apply filters.
        filtered = []
        for job in run["jobs"]:
            if failed_only and classify_conclusion(job.get("conclusion")) != Outcome.FAILED:
                continue
            if _job_matches_ignore(job, run["workflow_name"], ignore_rules):
                continue
            filtered.append(job)

        run_dir = out_dir / _run_subdir_name(run["run_id"], nowdt)
        run_dir.mkdir(parents=True, exist_ok=True)

        manifest_jobs: list[dict] = []
        for job in filtered:
            entry, gh_err = _download_one_job(
                target.owner, target.repo, host, job, run_dir,
                gh_download_fn=gh_download_fn,
            )
            manifest_jobs.append(entry)
            if entry["log_file"]:
                # Any log file counts toward 'written'; 'partial' is a subset
                # of 'written' (partial files are still files we wrote).
                summary_counts["written"] += 1
                if entry["partial"]:
                    summary_counts["partial"] += 1
                if entry["truncated"]:
                    summary_counts["truncated"] += 1
                abs_path = (run_dir / entry["log_file"]).resolve()
                print(str(abs_path), file=err)
            else:
                summary_counts["skipped"] += 1
            if gh_err:
                any_gh_error = True

        manifest = {
            "schema_version": 1,
            "target": _target_to_str(target),
            "target_kind": _target_kind(target),
            "filter": "failed" if failed_only else None,
            "run_id": run["run_id"],
            "run_url": run["run_url"],
            "fetched_at_utc": _format_utc(nowdt),
            "jobs": manifest_jobs,
        }
        manifest_path = run_dir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2))
        print(p.style(f"manifest: {manifest_path.resolve()}", dim=True), file=err)

    written = summary_counts["written"]
    leading = f"Wrote {written} logs"
    if any_gh_error:
        leading = p.style(leading, color="red")
    elif written > 0:
        leading = p.style(leading, color="green")
    else:
        leading = p.style(leading, color="yellow")
    summary_line = (
        f"{leading} "
        f"({summary_counts['partial']} partial, "
        f"{summary_counts['truncated']} truncated, "
        f"{summary_counts['skipped']} with no logs available)"
    )
    print(summary_line, file=err)

    return 6 if any_gh_error else 0


# Run resolution =====


def _resolve_runs_for_target(
    target: ResolvedTarget,
    *,
    gh_get: Callable[..., Any],
    gh_graphql_fn: Callable[..., dict],
    host: str | None,
) -> list[dict]:
    """Return list of {run_id, jobs, workflow_name, run_url}."""
    if isinstance(target, JobTarget):
        job = gh_get(
            f"/repos/{target.owner}/{target.repo}/actions/jobs/{target.job_id}",
            host=host,
        )
        run = gh_get(
            f"/repos/{target.owner}/{target.repo}/actions/runs/{target.run_id}",
            host=host,
        )
        return [{
            "run_id": target.run_id,
            "jobs": [job],
            "workflow_name": run.get("name"),
            "run_url": run.get("html_url") or f"https://github.com/{target.owner}/{target.repo}/actions/runs/{target.run_id}",
        }]
    if isinstance(target, RunTarget):
        run = gh_get(
            f"/repos/{target.owner}/{target.repo}/actions/runs/{target.run_id}",
            host=host,
        )
        jobs_data = gh_get(
            f"/repos/{target.owner}/{target.repo}/actions/runs/{target.run_id}/jobs",
            host=host, paginate=True,
        )
        return [{
            "run_id": target.run_id,
            "jobs": jobs_data.get("jobs", []),
            "workflow_name": run.get("name"),
            "run_url": run.get("html_url") or f"https://github.com/{target.owner}/{target.repo}/actions/runs/{target.run_id}",
        }]
    if isinstance(target, WorkflowTarget):
        from urllib.parse import quote
        data = gh_get(
            f"/repos/{target.owner}/{target.repo}/actions/workflows/"
            f"{quote(target.workflow_path)}/runs"
            f"?branch={quote(target.branch)}&per_page=1",
            host=host,
        )
        runs = data.get("workflow_runs", [])
        if not runs:
            # Recognized target but no runs: surface as EmptyTargetError so
            # cli.py returns exit 2 with a clear message instead of silently
            # treating this as "no logs to download" (exit 0).
            raise EmptyTargetError(
                f"Workflow {target.workflow_path!r} in "
                f"{target.owner}/{target.repo} has no runs on branch "
                f"{target.branch!r} (recognized target, but empty). "
                f"Check the workflow filename and branch, or trigger a run."
            )
        run = runs[0]
        jobs_data = gh_get(
            f"/repos/{target.owner}/{target.repo}/actions/runs/{run['id']}/jobs",
            host=host, paginate=True,
        )
        return [{
            "run_id": run["id"],
            "jobs": jobs_data.get("jobs", []),
            "workflow_name": run.get("name"),
            "run_url": run.get("html_url") or "",
        }]
    assert isinstance(target, PrTarget)
    # PR target: derive workflow run_ids from the PR's check-runs rollup
    # (GraphQL). This works for fork PRs too — the REST query
    # /actions/runs?head_sha=<sha> against the BASE repo returns nothing
    # when the head SHA lives in the fork. The check-runs rollup is keyed
    # by PR, not repo, so it lists all workflow runs regardless of where
    # the head ref lives. Once we have the run_ids, REST endpoints on the
    # base repo (/actions/runs/<id>, /actions/runs/<id>/jobs) do work for
    # fork-PR runs because those runs are associated with the base repo
    # via the PR.
    items, _ = fetch_pr_checks(
        target.owner, target.repo, target.pr_number,
        host=host, gh_graphql_fn=gh_graphql_fn,
    )
    seen: set[int] = set()
    run_ids: list[int] = []
    for item in items:
        if item.kind != "actions" or item.run_id is None:
            continue
        if item.run_id in seen:
            continue
        seen.add(item.run_id)
        run_ids.append(item.run_id)
    results = []
    for run_id in run_ids:
        run = gh_get(
            f"/repos/{target.owner}/{target.repo}/actions/runs/{run_id}",
            host=host,
        )
        jobs_data = gh_get(
            f"/repos/{target.owner}/{target.repo}/actions/runs/{run_id}/jobs",
            host=host, paginate=True,
        )
        results.append({
            "run_id": run_id,
            "jobs": jobs_data.get("jobs", []),
            "workflow_name": run.get("name"),
            "run_url": run.get("html_url") or "",
        })
    return results


# Per-job download =====


def _download_one_job(
    owner: str,
    repo: str,
    host: str | None,
    job: dict,
    run_dir: Path,
    *,
    gh_download_fn: Callable[..., int],
) -> tuple[dict, bool]:
    """Download one job's log. Returns (manifest_entry, was_real_gh_error)."""
    job_id = job["id"]
    name = job["name"]
    sanitized = _SAFE_FILENAME_RE.sub("_", name)
    status = (job.get("status") or "").lower()
    is_partial = status != "completed"
    suffix = ".partial.log" if is_partial else ".log"
    dest = run_dir / f"{job_id}-{sanitized}{suffix}"
    log_path = f"/repos/{owner}/{repo}/actions/jobs/{job_id}/logs"

    entry = {
        "job_id": job_id,
        "name": name,
        "status": job.get("status"),
        "conclusion": job.get("conclusion"),
        "started_at": job.get("started_at"),
        "completed_at": job.get("completed_at"),
        "url": job.get("html_url"),
        "log_file": None,
        "partial": False,
        "truncated": False,
        "bytes_written": None,
        "error": None,
    }

    try:
        bytes_written = gh_download_fn(log_path, dest, host=host)
        entry["log_file"] = dest.name
        entry["partial"] = is_partial
        entry["bytes_written"] = bytes_written
        return entry, False
    except GhError as e:
        status_code = parse_http_status(e.stderr)
        if status_code == 404:
            entry["error"] = f"no logs available yet (HTTP 404)"
            # gh_api_download only deletes the .tmp file when bytes_written == 0.
            # A 404 response may still carry a non-empty error body (e.g. an
            # HTML page), which gets written to <dest>.tmp before gh exits
            # non-zero. We treat 404 as "no logs yet" (log_file stays null),
            # so any leftover .tmp would be an orphan — remove it.
            if e.tmp_path is not None:
                try:
                    e.tmp_path.unlink()
                except OSError:
                    pass
            return entry, False
        # Real gh error. Manifest reflects the truncated state.
        if e.tmp_path is not None and e.tmp_path.exists():
            entry["log_file"] = e.tmp_path.name
            entry["truncated"] = True
        entry["partial"] = is_partial and entry["log_file"] is not None
        entry["bytes_written"] = e.bytes_written
        entry["error"] = e.stderr.strip()
        return entry, True


# Filters =====


def _job_matches_ignore(
    job: dict,
    workflow_name: str | None,
    rules: list[IgnoreRule],
) -> bool:
    for rule in rules:
        if rule.prefix == "workflow" and workflow_name == rule.name:
            return True
        if rule.prefix == "job" and job.get("name") == rule.name:
            return True
        # check: is not applicable to Actions jobs.
    return False


# Output-dir resolution =====


def _resolve_output_dir(
    custom: Path | None,
    *,
    err: TextIO,
    palette: Palette | None = None,
) -> Path | None:
    p = ensure_palette(palette)
    if custom is not None:
        try:
            custom.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            print(
                p.style(f"--output-dir: cannot create {custom}: {e}", color="red"),
                file=err,
            )
            return None
        return custom
    default = Path(platformdirs.user_downloads_dir())
    if default.exists() and default.is_dir():
        return default
    fallback = Path(tempfile.gettempdir())
    print(
        p.style(
            f"Default Downloads dir not available; falling back to {fallback}",
            color="yellow",
        ),
        file=err,
    )
    return fallback


def _to_utc(when: datetime) -> datetime:
    """Normalize a datetime to tz-aware UTC.

    Naive datetimes are assumed to be UTC (matching how the rest of this
    module passes around `datetime.now(timezone.utc)`). Aware datetimes are
    converted to UTC.
    """
    when_utc = when if when.tzinfo else when.replace(tzinfo=timezone.utc)
    return when_utc.astimezone(timezone.utc)


def _format_utc(when: datetime) -> str:
    """Format `when` as an ISO-8601 UTC string with a trailing 'Z'."""
    return _to_utc(when).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run_subdir_name(run_id: int, when: datetime) -> str:
    ts = _to_utc(when).strftime("%Y-%m-%dT%H-%M-%SZ")
    return f"gh-ci-{run_id}-{ts}"


def _target_to_str(target: ResolvedTarget) -> str:
    if isinstance(target, PrTarget):
        return f"https://{target.host}/{target.owner}/{target.repo}/pull/{target.pr_number}"
    if isinstance(target, RunTarget):
        return f"https://{target.host}/{target.owner}/{target.repo}/actions/runs/{target.run_id}"
    if isinstance(target, JobTarget):
        return (
            f"https://{target.host}/{target.owner}/{target.repo}/"
            f"actions/runs/{target.run_id}/job/{target.job_id}"
        )
    assert isinstance(target, WorkflowTarget)
    return (
        f"https://{target.host}/{target.owner}/{target.repo}/"
        f"actions/workflows/{target.workflow_path}"
    )


def _target_kind(target: ResolvedTarget) -> str:
    if isinstance(target, PrTarget):
        return "pr"
    if isinstance(target, RunTarget):
        return "run"
    if isinstance(target, JobTarget):
        return "job"
    return "workflow"
