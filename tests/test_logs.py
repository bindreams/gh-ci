from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from ghci.gh import GhError
from ghci.ignore import IgnoreRule
from ghci.logs import run_logs
from ghci.target import JobTarget, PrTarget, RunTarget


# Helpers =====


class FakeDownload:
    """Drop-in for gh_api_download. Maps path → bytes-or-exception."""

    def __init__(self) -> None:
        self.responses: dict[str, Any] = {}
        self.calls: list[dict] = []

    def set_ok(self, path: str, data: bytes) -> None:
        self.responses[path] = data

    def set_error(self, path: str, *, returncode: int = 1, stderr: str = "",
                  bytes_written: int = 0, tmp_path: Path | None = None) -> None:
        self.responses[path] = GhError(
            returncode=returncode, stderr=stderr,
            bytes_written=bytes_written, tmp_path=tmp_path,
        )

    def __call__(self, path: str, dest: Path, *, host: str | None = None) -> int:
        self.calls.append({"path": path, "dest": str(dest), "host": host})
        if path not in self.responses:
            raise AssertionError(f"FakeDownload: no response for {path!r}")
        resp = self.responses[path]
        if isinstance(resp, GhError):
            # Simulate partial write if bytes_written>0
            tmp = dest.with_suffix(dest.suffix + ".tmp")
            if resp.bytes_written > 0:
                tmp.write_bytes(b"x" * resp.bytes_written)
                raise GhError(
                    returncode=resp.returncode, stderr=resp.stderr,
                    bytes_written=resp.bytes_written, tmp_path=tmp,
                )
            raise resp
        # Success: write file (skip .tmp dance since FakeDownload is the wrapper).
        dest.write_bytes(resp)
        return len(resp)


NOW = datetime(2026, 5, 25, 14, 30, 0)


# Single job target =====


def test_job_target_downloads_one_log(fake_gh, tmp_path):
    fake_gh.set_get(
        "/repos/o/r/actions/jobs/55",
        {"id": 55, "name": "Build", "status": "completed", "conclusion": "success",
         "started_at": "2026-05-25T14:00:00Z", "completed_at": "2026-05-25T14:25:00Z",
         "html_url": "u", "run_id": 1000},
    )
    fake_gh.set_get(
        "/repos/o/r/actions/runs/1000",
        {"id": 1000, "name": "CI", "status": "completed"},
    )
    dl = FakeDownload()
    dl.set_ok("/repos/o/r/actions/jobs/55/logs", b"log content")

    code = run_logs(
        target=JobTarget(owner="o", repo="r", host="github.com",
                         run_id=1000, job_id=55),
        failed_only=False,
        ignore_rules=[],
        output_dir=tmp_path,
        stderr=io.StringIO(),
        gh_get=fake_gh.gh_get,
        gh_download_fn=dl,
        now=NOW,
    )
    assert code == 0
    subdirs = list(tmp_path.iterdir())
    assert len(subdirs) == 1
    subdir = subdirs[0]
    assert subdir.name == "gh-ci-1000-2026-05-25T14-30-00Z"
    files = sorted(p.name for p in subdir.iterdir())
    assert "55-Build.log" in files
    assert "manifest.json" in files
    assert (subdir / "55-Build.log").read_bytes() == b"log content"


# Manifest schema =====


def test_manifest_schema(fake_gh, tmp_path):
    fake_gh.set_get(
        "/repos/o/r/actions/jobs/55",
        {"id": 55, "name": "Build", "status": "completed", "conclusion": "success",
         "started_at": "S", "completed_at": "E", "html_url": "u", "run_id": 1000},
    )
    fake_gh.set_get(
        "/repos/o/r/actions/runs/1000",
        {"id": 1000, "name": "CI", "status": "completed"},
    )
    dl = FakeDownload()
    dl.set_ok("/repos/o/r/actions/jobs/55/logs", b"x" * 200)

    run_logs(
        target=JobTarget(owner="o", repo="r", host="github.com",
                         run_id=1000, job_id=55),
        failed_only=False,
        ignore_rules=[],
        output_dir=tmp_path,
        stderr=io.StringIO(),
        gh_get=fake_gh.gh_get,
        gh_download_fn=dl,
        now=NOW,
    )
    subdir = next(tmp_path.iterdir())
    manifest = json.loads((subdir / "manifest.json").read_text())
    assert manifest["schema_version"] == 1
    assert manifest["target_kind"] == "job"
    assert manifest["run_id"] == 1000
    # NOW is a naive datetime (2026-05-25 14:30:00). Per Option A semantics,
    # naive datetimes are assumed to be UTC, so the formatted value must
    # match exactly — not just "contain 'Z'".
    assert manifest["fetched_at_utc"] == "2026-05-25T14:30:00Z"
    j = manifest["jobs"][0]
    assert j["job_id"] == 55
    assert j["name"] == "Build"
    assert j["status"] == "completed"
    assert j["conclusion"] == "success"
    assert j["log_file"] == "55-Build.log"
    assert j["partial"] is False
    assert j["truncated"] is False
    assert j["bytes_written"] == 200
    assert j["error"] is None
    assert manifest["filter"] is None


# Manifest fetched_at_utc handles tz-aware input =====


def test_manifest_fetched_at_utc_converts_aware_datetime(fake_gh, tmp_path):
    """Regression for bug #8: a tz-aware `now` (e.g. Berlin UTC+2) must be
    converted to UTC before being formatted into `fetched_at_utc`. A naive
    `nowdt.strftime("%Y-%m-%dT%H:%M:%SZ")` would have silently labeled the
    local wall-clock time as UTC, which is incorrect."""
    fake_gh.set_get(
        "/repos/o/r/actions/jobs/55",
        {"id": 55, "name": "Build", "status": "completed", "conclusion": "success",
         "started_at": "S", "completed_at": "E", "html_url": "u", "run_id": 1000},
    )
    fake_gh.set_get(
        "/repos/o/r/actions/runs/1000",
        {"id": 1000, "name": "CI", "status": "completed"},
    )
    dl = FakeDownload()
    dl.set_ok("/repos/o/r/actions/jobs/55/logs", b"x")

    berlin = timezone(timedelta(hours=2))
    now_berlin = datetime(2026, 5, 25, 14, 30, 0, tzinfo=berlin)

    run_logs(
        target=JobTarget(owner="o", repo="r", host="github.com",
                         run_id=1000, job_id=55),
        failed_only=False,
        ignore_rules=[],
        output_dir=tmp_path,
        stderr=io.StringIO(),
        gh_get=fake_gh.gh_get,
        gh_download_fn=dl,
        now=now_berlin,
    )
    subdir = next(tmp_path.iterdir())
    manifest = json.loads((subdir / "manifest.json").read_text())
    # 14:30 Berlin (UTC+2) == 12:30 UTC.
    assert manifest["fetched_at_utc"] == "2026-05-25T12:30:00Z"
    # The subdir name uses the same UTC normalization.
    assert subdir.name == "gh-ci-1000-2026-05-25T12-30-00Z"


# Run target with multiple jobs + --failed =====


def test_run_target_failed_only(fake_gh, tmp_path):
    fake_gh.set_get(
        "/repos/o/r/actions/runs/1000",
        {"id": 1000, "name": "CI", "status": "completed"},
    )
    fake_gh.set_get(
        "/repos/o/r/actions/runs/1000/jobs",
        {"jobs": [
            {"id": 1, "name": "lint", "status": "completed", "conclusion": "success",
             "started_at": "S", "completed_at": "E", "html_url": "u", "run_id": 1000},
            {"id": 2, "name": "build", "status": "completed", "conclusion": "failure",
             "started_at": "S", "completed_at": "E", "html_url": "u", "run_id": 1000},
        ]},
    )
    dl = FakeDownload()
    dl.set_ok("/repos/o/r/actions/jobs/2/logs", b"build log")

    code = run_logs(
        target=RunTarget(owner="o", repo="r", host="github.com", run_id=1000),
        failed_only=True,
        ignore_rules=[],
        output_dir=tmp_path,
        stderr=io.StringIO(),
        gh_get=fake_gh.gh_get,
        gh_download_fn=dl,
        now=NOW,
    )
    assert code == 0
    subdir = next(tmp_path.iterdir())
    files = {p.name for p in subdir.iterdir()}
    assert "2-build.log" in files
    assert "1-lint.log" not in files
    manifest = json.loads((subdir / "manifest.json").read_text())
    assert manifest["filter"] == "failed"
    assert len(manifest["jobs"]) == 1


# In-progress → .partial.log =====


def test_in_progress_job_writes_partial(fake_gh, tmp_path):
    fake_gh.set_get(
        "/repos/o/r/actions/runs/1000",
        {"id": 1000, "name": "CI", "status": "in_progress"},
    )
    fake_gh.set_get(
        "/repos/o/r/actions/runs/1000/jobs",
        {"jobs": [
            {"id": 99, "name": "build", "status": "in_progress", "conclusion": None,
             "started_at": "S", "completed_at": None, "html_url": "u", "run_id": 1000},
        ]},
    )
    dl = FakeDownload()
    dl.set_ok("/repos/o/r/actions/jobs/99/logs", b"partial log so far")

    run_logs(
        target=RunTarget(owner="o", repo="r", host="github.com", run_id=1000),
        failed_only=False, ignore_rules=[], output_dir=tmp_path,
        stderr=io.StringIO(), gh_get=fake_gh.gh_get, gh_download_fn=dl, now=NOW,
    )
    subdir = next(tmp_path.iterdir())
    files = {p.name for p in subdir.iterdir()}
    assert "99-build.partial.log" in files
    manifest = json.loads((subdir / "manifest.json").read_text())
    assert manifest["jobs"][0]["partial"] is True


# 404 → no file, manifest entry =====


def test_404_records_no_file(fake_gh, tmp_path):
    fake_gh.set_get(
        "/repos/o/r/actions/runs/1000",
        {"id": 1000, "name": "CI", "status": "in_progress"},
    )
    fake_gh.set_get(
        "/repos/o/r/actions/runs/1000/jobs",
        {"jobs": [
            {"id": 99, "name": "queued-job", "status": "queued", "conclusion": None,
             "started_at": None, "completed_at": None, "html_url": "u", "run_id": 1000},
        ]},
    )
    dl = FakeDownload()
    dl.set_error("/repos/o/r/actions/jobs/99/logs",
                 returncode=1, stderr="gh: HTTP 404: Not Found")

    code = run_logs(
        target=RunTarget(owner="o", repo="r", host="github.com", run_id=1000),
        failed_only=False, ignore_rules=[], output_dir=tmp_path,
        stderr=io.StringIO(), gh_get=fake_gh.gh_get, gh_download_fn=dl, now=NOW,
    )
    # 404 is expected behavior, not an error
    assert code == 0
    subdir = next(tmp_path.iterdir())
    files = {p.name for p in subdir.iterdir()}
    assert "manifest.json" in files
    assert not any(name.endswith(".log") for name in files if name != "manifest.json")
    manifest = json.loads((subdir / "manifest.json").read_text())
    assert manifest["jobs"][0]["log_file"] is None
    assert "404" in manifest["jobs"][0]["error"]


# 404 with body bytes → orphan .tmp cleaned up =====


def test_404_with_body_cleans_up_tmp(fake_gh, tmp_path):
    """Regression: a 404 response can still carry a non-empty body (e.g. an
    HTML error page) which gh_api_download writes to <dest>.tmp before gh
    exits non-zero. In the 404 branch we record log_file=null, so the .tmp
    would otherwise be left orphaned in run_dir. Verify it is cleaned up."""
    fake_gh.set_get(
        "/repos/o/r/actions/runs/1000",
        {"id": 1000, "name": "CI", "status": "in_progress"},
    )
    fake_gh.set_get(
        "/repos/o/r/actions/runs/1000/jobs",
        {"jobs": [
            {"id": 99, "name": "queued-job", "status": "queued", "conclusion": None,
             "started_at": None, "completed_at": None, "html_url": "u", "run_id": 1000},
        ]},
    )
    dl = FakeDownload()
    # 404 with a non-empty error body — FakeDownload will create the .tmp
    # with 512 bytes (matching gh_api_download's real behavior).
    dl.set_error("/repos/o/r/actions/jobs/99/logs",
                 returncode=1, stderr="gh: HTTP 404: Not Found",
                 bytes_written=512)

    code = run_logs(
        target=RunTarget(owner="o", repo="r", host="github.com", run_id=1000),
        failed_only=False, ignore_rules=[], output_dir=tmp_path,
        stderr=io.StringIO(), gh_get=fake_gh.gh_get, gh_download_fn=dl, now=NOW,
    )
    # 404 is expected behavior, not an error
    assert code == 0
    subdir = next(tmp_path.iterdir())
    files = {p.name for p in subdir.iterdir()}
    # No orphan .tmp file should remain
    assert not any(name.endswith(".tmp") for name in files), (
        f"orphan .tmp left behind: {files}"
    )
    # Manifest still records the 404 and log_file: null, just like the
    # zero-body 404 case.
    manifest = json.loads((subdir / "manifest.json").read_text())
    j = manifest["jobs"][0]
    assert j["log_file"] is None
    assert "404" in j["error"]


# Truncated → .tmp left + manifest entry =====


def test_truncated_download_leaves_tmp(fake_gh, tmp_path):
    fake_gh.set_get(
        "/repos/o/r/actions/runs/1000",
        {"id": 1000, "name": "CI", "status": "completed"},
    )
    fake_gh.set_get(
        "/repos/o/r/actions/runs/1000/jobs",
        {"jobs": [
            {"id": 7, "name": "broken", "status": "completed", "conclusion": "success",
             "started_at": "S", "completed_at": "E", "html_url": "u", "run_id": 1000},
        ]},
    )
    dl = FakeDownload()
    dl.set_error("/repos/o/r/actions/jobs/7/logs",
                 returncode=1, stderr="gh: HTTP 500",
                 bytes_written=42)

    code = run_logs(
        target=RunTarget(owner="o", repo="r", host="github.com", run_id=1000),
        failed_only=False, ignore_rules=[], output_dir=tmp_path,
        stderr=io.StringIO(), gh_get=fake_gh.gh_get, gh_download_fn=dl, now=NOW,
    )
    # Non-404 gh error → exit 6
    assert code == 6
    subdir = next(tmp_path.iterdir())
    files = {p.name for p in subdir.iterdir()}
    assert "7-broken.log.tmp" in files
    manifest = json.loads((subdir / "manifest.json").read_text())
    j = manifest["jobs"][0]
    assert j["truncated"] is True
    assert j["partial"] is False
    assert j["log_file"] == "7-broken.log.tmp"
    assert j["bytes_written"] == 42


# Ignore =====


def test_ignore_workflow_filters_jobs(fake_gh, tmp_path):
    fake_gh.set_get(
        "/repos/o/r/actions/runs/1000",
        {"id": 1000, "name": "Lint", "status": "completed"},
    )
    fake_gh.set_get(
        "/repos/o/r/actions/runs/1000/jobs",
        {"jobs": [
            {"id": 1, "name": "lint", "status": "completed", "conclusion": "success",
             "started_at": "S", "completed_at": "E", "html_url": "u", "run_id": 1000},
        ]},
    )
    dl = FakeDownload()

    code = run_logs(
        target=RunTarget(owner="o", repo="r", host="github.com", run_id=1000),
        failed_only=False,
        ignore_rules=[IgnoreRule("workflow", "Lint")],
        output_dir=tmp_path,
        stderr=io.StringIO(),
        gh_get=fake_gh.gh_get,
        gh_download_fn=dl,
        now=NOW,
    )
    assert code == 0
    subdir = next(tmp_path.iterdir())
    files = {p.name for p in subdir.iterdir() if not p.name.endswith(".json")}
    assert not files  # everything filtered out


def test_ignore_job_filters_one(fake_gh, tmp_path):
    fake_gh.set_get(
        "/repos/o/r/actions/runs/1000",
        {"id": 1000, "name": "CI", "status": "completed"},
    )
    fake_gh.set_get(
        "/repos/o/r/actions/runs/1000/jobs",
        {"jobs": [
            {"id": 1, "name": "lint", "status": "completed", "conclusion": "success",
             "started_at": "S", "completed_at": "E", "html_url": "u", "run_id": 1000},
            {"id": 2, "name": "build", "status": "completed", "conclusion": "success",
             "started_at": "S", "completed_at": "E", "html_url": "u", "run_id": 1000},
        ]},
    )
    dl = FakeDownload()
    dl.set_ok("/repos/o/r/actions/jobs/2/logs", b"build log")

    run_logs(
        target=RunTarget(owner="o", repo="r", host="github.com", run_id=1000),
        failed_only=False,
        ignore_rules=[IgnoreRule("job", "lint")],
        output_dir=tmp_path,
        stderr=io.StringIO(),
        gh_get=fake_gh.gh_get,
        gh_download_fn=dl,
        now=NOW,
    )
    subdir = next(tmp_path.iterdir())
    files = {p.name for p in subdir.iterdir()}
    assert "2-build.log" in files
    assert "1-lint.log" not in files


# Filename sanitization =====


def test_filename_sanitization(fake_gh, tmp_path):
    fake_gh.set_get(
        "/repos/o/r/actions/runs/1000",
        {"id": 1000, "name": "CI", "status": "completed"},
    )
    fake_gh.set_get(
        "/repos/o/r/actions/runs/1000/jobs",
        {"jobs": [
            {"id": 1, "name": "test (3.11, ubuntu-latest)",
             "status": "completed", "conclusion": "success",
             "started_at": "S", "completed_at": "E", "html_url": "u", "run_id": 1000},
        ]},
    )
    dl = FakeDownload()
    dl.set_ok("/repos/o/r/actions/jobs/1/logs", b"x")

    run_logs(
        target=RunTarget(owner="o", repo="r", host="github.com", run_id=1000),
        failed_only=False, ignore_rules=[], output_dir=tmp_path,
        stderr=io.StringIO(), gh_get=fake_gh.gh_get, gh_download_fn=dl, now=NOW,
    )
    subdir = next(tmp_path.iterdir())
    files = {p.name for p in subdir.iterdir()}
    # spaces/commas/parens → _
    assert any(name.startswith("1-test") and name.endswith(".log") for name in files)


# PR target (fork PR) =====


def test_fork_pr_target_uses_check_runs_rollup(fake_gh, tmp_path):
    """Regression: for a fork PR, /actions/runs?head_sha=<sha> against the
    base repo returns zero workflow runs (the head SHA lives in the fork,
    not the base repo). We must derive run_ids from the PR's check-runs
    rollup (GraphQL) instead, which is keyed by PR and works for forks.
    Once we have a run_id, REST endpoints on the base repo do work, because
    workflow runs for a PR are associated with the base repo."""
    # GraphQL rollup with one Actions CheckRun pointing to run 999.
    fake_gh.queue_graphql({
        "repository": {
            "pullRequest": {
                "state": "OPEN",
                "mergeable": "MERGEABLE",
                "headRefOid": "forksha",
                "commits": {
                    "nodes": [{
                        "commit": {
                            "statusCheckRollup": {
                                "contexts": {
                                    "pageInfo": {
                                        "hasNextPage": False,
                                        "endCursor": None,
                                    },
                                    "nodes": [{
                                        "__typename": "CheckRun",
                                        "name": "build",
                                        "status": "COMPLETED",
                                        "conclusion": "SUCCESS",
                                        "detailsUrl": (
                                            "https://github.com/o/r/runs/1"
                                        ),
                                        "isRequired": False,
                                        "databaseId": 1,
                                        "checkSuite": {
                                            "workflowRun": {
                                                "databaseId": 999,
                                                "url": (
                                                    "https://github.com/"
                                                    "o/r/actions/runs/999"
                                                ),
                                                "workflow": {"name": "CI"},
                                            },
                                        },
                                    }],
                                },
                            },
                        },
                    }],
                },
            },
        },
    })
    # REST run metadata + jobs on the BASE repo for run 999. These DO work
    # for fork-PR runs because the workflow run is associated with the base
    # repo via the PR.
    fake_gh.set_get(
        "/repos/o/r/actions/runs/999",
        {"id": 999, "name": "CI", "status": "completed",
         "html_url": "https://github.com/o/r/actions/runs/999"},
    )
    fake_gh.set_get(
        "/repos/o/r/actions/runs/999/jobs",
        {"jobs": [
            {"id": 77, "name": "build", "status": "completed",
             "conclusion": "success", "started_at": "S",
             "completed_at": "E", "html_url": "u", "run_id": 999},
        ]},
    )

    dl = FakeDownload()
    dl.set_ok("/repos/o/r/actions/jobs/77/logs", b"build log content")

    # Wrap gh_get so the old (buggy) REST query path would loudly fail if
    # invoked — the new code must not call /actions/runs?head_sha=...
    def gh_get_guard(path, *, host=None, paginate=False):
        if path.startswith("/repos/o/r/actions/runs?head_sha="):
            raise AssertionError(
                "old buggy path used: /actions/runs?head_sha=... — must "
                "use check-runs rollup for fork PRs"
            )
        return fake_gh.gh_get(path, host=host, paginate=paginate)

    code = run_logs(
        target=PrTarget(owner="o", repo="r", host="github.com", pr_number=42),
        failed_only=False,
        ignore_rules=[],
        output_dir=tmp_path,
        stderr=io.StringIO(),
        gh_get=gh_get_guard,
        gh_graphql_fn=fake_gh.gh_graphql,
        gh_download_fn=dl,
        now=NOW,
    )
    assert code == 0
    subdirs = list(tmp_path.iterdir())
    assert len(subdirs) == 1
    subdir = subdirs[0]
    assert subdir.name == "gh-ci-999-2026-05-25T14-30-00Z"
    files = {p.name for p in subdir.iterdir()}
    assert "77-build.log" in files
    assert (subdir / "77-build.log").read_bytes() == b"build log content"
    manifest = json.loads((subdir / "manifest.json").read_text())
    assert manifest["run_id"] == 999
    assert manifest["target_kind"] == "pr"


# Final summary line counts partials =====


def test_final_summary_counts_partials_in_written(fake_gh, tmp_path):
    """Regression for bug #9: the final 'Wrote N logs (...)' line should count
    every log file we wrote — including .partial.log files — toward N. The
    partial bucket is a subset breakdown, not a separate disjoint group.

    Setup: 1 completed job (writes .log) + 2 in-progress jobs (each writes
    .partial.log) + 1 queued job (404 → no file). Expected final line:
    'Wrote 3 logs (2 partial, 0 truncated, 1 with no logs available)'."""
    fake_gh.set_get(
        "/repos/o/r/actions/runs/1000",
        {"id": 1000, "name": "CI", "status": "in_progress"},
    )
    fake_gh.set_get(
        "/repos/o/r/actions/runs/1000/jobs",
        {"jobs": [
            {"id": 1, "name": "complete-job", "status": "completed",
             "conclusion": "success", "started_at": "S", "completed_at": "E",
             "html_url": "u", "run_id": 1000},
            {"id": 2, "name": "in-progress-a", "status": "in_progress",
             "conclusion": None, "started_at": "S", "completed_at": None,
             "html_url": "u", "run_id": 1000},
            {"id": 3, "name": "in-progress-b", "status": "in_progress",
             "conclusion": None, "started_at": "S", "completed_at": None,
             "html_url": "u", "run_id": 1000},
            {"id": 4, "name": "queued-job", "status": "queued",
             "conclusion": None, "started_at": None, "completed_at": None,
             "html_url": "u", "run_id": 1000},
        ]},
    )
    dl = FakeDownload()
    dl.set_ok("/repos/o/r/actions/jobs/1/logs", b"complete log")
    dl.set_ok("/repos/o/r/actions/jobs/2/logs", b"partial a")
    dl.set_ok("/repos/o/r/actions/jobs/3/logs", b"partial b")
    dl.set_error("/repos/o/r/actions/jobs/4/logs",
                 returncode=1, stderr="gh: HTTP 404: Not Found")

    err = io.StringIO()
    code = run_logs(
        target=RunTarget(owner="o", repo="r", host="github.com", run_id=1000),
        failed_only=False, ignore_rules=[], output_dir=tmp_path,
        stderr=err, gh_get=fake_gh.gh_get, gh_download_fn=dl, now=NOW,
    )
    assert code == 0
    # Final summary line should count all 3 log files (1 complete + 2 partial)
    # toward 'Wrote N', with partial as a subset breakdown.
    lines = err.getvalue().splitlines()
    summary_lines = [ln for ln in lines if ln.startswith("Wrote ")]
    assert len(summary_lines) == 1, f"expected 1 summary line, got: {summary_lines}"
    assert summary_lines[0] == (
        "Wrote 3 logs (2 partial, 0 truncated, 1 with no logs available)"
    ), f"unexpected summary line: {summary_lines[0]!r}"


# Output dir mkdir =====


def test_output_dir_mkdir_p(fake_gh, tmp_path):
    target_dir = tmp_path / "deeply" / "nested" / "dir"
    fake_gh.set_get(
        "/repos/o/r/actions/jobs/1",
        {"id": 1, "name": "Build", "status": "completed", "conclusion": "success",
         "started_at": "S", "completed_at": "E", "html_url": "u", "run_id": 1000},
    )
    fake_gh.set_get(
        "/repos/o/r/actions/runs/1000",
        {"id": 1000, "name": "CI", "status": "completed"},
    )
    dl = FakeDownload()
    dl.set_ok("/repos/o/r/actions/jobs/1/logs", b"log")

    code = run_logs(
        target=JobTarget(owner="o", repo="r", host="github.com",
                         run_id=1000, job_id=1),
        failed_only=False, ignore_rules=[], output_dir=target_dir,
        stderr=io.StringIO(), gh_get=fake_gh.gh_get, gh_download_fn=dl, now=NOW,
    )
    assert code == 0
    assert target_dir.exists()
    assert next(target_dir.iterdir()).is_dir()
