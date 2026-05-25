from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime
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
    assert "fetched_at_utc" in manifest
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
