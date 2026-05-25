from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ghci.gh import (
    GhError,
    gh_api_download,
    gh_api_get,
    gh_api_graphql,
    parse_http_status,
)


# parse_http_status =====


def test_parse_http_status_finds_404():
    stderr = "gh: HTTP 404: Not Found (https://api.github.com/repos/foo/bar)"
    assert parse_http_status(stderr) == 404


def test_parse_http_status_finds_403():
    stderr = "gh: HTTP 403: Forbidden"
    assert parse_http_status(stderr) == 403


def test_parse_http_status_returns_none_when_absent():
    assert parse_http_status("connection refused") is None


def test_parse_http_status_handles_empty():
    assert parse_http_status("") is None


# gh_api_get =====


def test_gh_api_get_passes_explicit_method(fp):
    fp.register(["gh", "api", "-X", "GET", "/repos/foo/bar"], stdout='{"x":1}')
    assert gh_api_get("/repos/foo/bar") == {"x": 1}


def test_gh_api_get_with_host_passes_hostname_flag(fp):
    fp.register(
        ["gh", "api", "--hostname", "ghes.example.com", "-X", "GET", "/foo"],
        stdout='{"y":2}',
    )
    assert gh_api_get("/foo", host="ghes.example.com") == {"y": 2}


def test_gh_api_get_omits_hostname_when_host_is_none(fp):
    fp.register(["gh", "api", "-X", "GET", "/foo"], stdout="[]")
    assert gh_api_get("/foo") == []


def test_gh_api_get_with_paginate(fp):
    fp.register(
        ["gh", "api", "--paginate", "--slurp", "-X", "GET", "/foo"],
        stdout="[[1,2,3]]",
    )
    assert gh_api_get("/foo", paginate=True) == [1, 2, 3]


def test_gh_api_get_paginate_merges_multi_page_array(fp):
    # gh api --paginate --slurp wraps each page in a JSON array.
    # For an array endpoint (e.g. /issues), pages are arrays themselves.
    fp.register(
        ["gh", "api", "--paginate", "--slurp", "-X", "GET", "/issues"],
        stdout="[[1,2,3],[4,5]]",
    )
    assert gh_api_get("/issues", paginate=True) == [1, 2, 3, 4, 5]


def test_gh_api_get_paginate_merges_multi_page_object_with_array(fp):
    # Regression: /repos/<o>/<r>/actions/runs/<id>/jobs returns
    # {"total_count": N, "jobs": [...]}. With --paginate --slurp, gh wraps
    # each page in a JSON array. We must merge the `jobs` arrays and take
    # the first page's `total_count` (GitHub reports collection-wide totals
    # on every page, so "first-page wins" — summing would double-count).
    # Use disagreeing totals so first-page-wins is distinguishable from sum.
    page1 = '{"total_count": 3, "jobs": [{"id": 1}, {"id": 2}]}'
    page2 = '{"total_count": 5, "jobs": [{"id": 3}]}'
    fp.register(
        [
            "gh",
            "api",
            "--paginate",
            "--slurp",
            "-X",
            "GET",
            "/repos/o/r/actions/runs/1/jobs",
        ],
        stdout=f"[{page1},{page2}]",
    )
    result = gh_api_get("/repos/o/r/actions/runs/1/jobs", paginate=True)
    assert result == {
        "total_count": 3,
        "jobs": [{"id": 1}, {"id": 2}, {"id": 3}],
    }


def test_gh_api_get_paginate_merges_three_pages_object_with_array(fp):
    # Three-page merge: guards against "merge only last page" / "merge only
    # adjacent pages" bugs. Each page contributes to `jobs`; first page's
    # `total_count` wins.
    page1 = '{"total_count": 6, "jobs": [{"id": 1}]}'
    page2 = '{"total_count": 6, "jobs": [{"id": 2}, {"id": 3}]}'
    page3 = '{"total_count": 6, "jobs": [{"id": 4}, {"id": 5}, {"id": 6}]}'
    fp.register(
        ["gh", "api", "--paginate", "--slurp", "-X", "GET", "/jobs"],
        stdout=f"[{page1},{page2},{page3}]",
    )
    result = gh_api_get("/jobs", paginate=True)
    assert result == {
        "total_count": 6,
        "jobs": [{"id": 1}, {"id": 2}, {"id": 3}, {"id": 4}, {"id": 5}, {"id": 6}],
    }


def test_gh_api_get_paginate_first_page_array_key_is_null(fp):
    # Bug B regression: when the first page has an array-valued key that's
    # `null` (e.g. `{"jobs": null}`), subsequent pages' arrays must still be
    # merged. The fix detects array keys across all pages, not just page 0.
    page1 = '{"total_count": 2, "jobs": null}'
    page2 = '{"total_count": 2, "jobs": [{"id": 7}, {"id": 8}]}'
    fp.register(
        ["gh", "api", "--paginate", "--slurp", "-X", "GET", "/jobs"],
        stdout=f"[{page1},{page2}]",
    )
    result = gh_api_get("/jobs", paginate=True)
    assert result == {
        "total_count": 2,
        "jobs": [{"id": 7}, {"id": 8}],
    }


def test_gh_api_get_paginate_first_page_empty_array_key(fp):
    # First page has `"jobs": []`, second page has `"jobs": [...]`. Both
    # pages should be merged; verifies the array-key detection across pages.
    page1 = '{"total_count": 2, "jobs": []}'
    page2 = '{"total_count": 2, "jobs": [{"id": 9}, {"id": 10}]}'
    fp.register(
        ["gh", "api", "--paginate", "--slurp", "-X", "GET", "/jobs"],
        stdout=f"[{page1},{page2}]",
    )
    result = gh_api_get("/jobs", paginate=True)
    assert result == {
        "total_count": 2,
        "jobs": [{"id": 9}, {"id": 10}],
    }


def test_gh_api_get_paginate_mixed_shape_dict_then_list_raises(fp):
    # Inconsistent paginated response shape (dict at page 0, list at page 1)
    # must raise GhError — silently dropping or coercing one page is a bug.
    page1 = '{"jobs": [{"id": 1}]}'
    page2 = "[2, 3]"
    fp.register(
        ["gh", "api", "--paginate", "--slurp", "-X", "GET", "/jobs"],
        stdout=f"[{page1},{page2}]",
    )
    with pytest.raises(GhError) as exc:
        gh_api_get("/jobs", paginate=True)
    assert "inconsistent paginated response shape" in exc.value.stderr


def test_gh_api_get_paginate_null_page_raises(fp):
    # Bug A regression: a JSON `null` page (whether at index 0 or later)
    # must raise GhError — not be silently returned as-is or merged.
    fp.register(
        ["gh", "api", "--paginate", "--slurp", "-X", "GET", "/foo"],
        stdout="[null]",
    )
    with pytest.raises(GhError) as exc:
        gh_api_get("/foo", paginate=True)
    assert "inconsistent paginated response shape" in exc.value.stderr


def test_gh_api_get_paginate_null_page_at_index_one_raises(fp):
    # Symmetry with the null-at-index-0 case: null at any index raises.
    page1 = '{"jobs": [{"id": 1}]}'
    fp.register(
        ["gh", "api", "--paginate", "--slurp", "-X", "GET", "/jobs"],
        stdout=f"[{page1},null]",
    )
    with pytest.raises(GhError) as exc:
        gh_api_get("/jobs", paginate=True)
    assert "inconsistent paginated response shape" in exc.value.stderr


def test_gh_api_get_paginate_single_page_object(fp):
    # Single page returns a one-element list after --slurp; merging should
    # still produce a single object with the same shape.
    page = '{"total_count": 2, "jobs": [{"id": 11}, {"id": 12}]}'
    fp.register(
        ["gh", "api", "--paginate", "--slurp", "-X", "GET", "/jobs"],
        stdout=f"[{page}]",
    )
    result = gh_api_get("/jobs", paginate=True)
    assert result == {
        "total_count": 2,
        "jobs": [{"id": 11}, {"id": 12}],
    }


def test_gh_api_get_paginate_empty(fp):
    # Bug C regression: no results at all (`--slurp` emits `[]`). The merger
    # must return a dict-like value so object-endpoint callers doing
    # `data.get("workflow_runs", [])` don't crash with AttributeError.
    fp.register(
        ["gh", "api", "--paginate", "--slurp", "-X", "GET", "/foo"],
        stdout="[]",
    )
    result = gh_api_get("/foo", paginate=True)
    # Must support .get(...) so object-endpoint callers work without checks.
    assert result.get("workflow_runs", []) == []
    assert result == {}


def test_gh_api_get_raises_GhError_on_non_zero(fp):
    fp.register(
        ["gh", "api", "-X", "GET", "/foo"],
        stdout="",
        stderr="gh: HTTP 404",
        returncode=1,
    )
    with pytest.raises(GhError) as exc:
        gh_api_get("/foo")
    assert exc.value.returncode == 1
    assert "404" in exc.value.stderr


def test_gh_api_get_raises_GhError_on_bad_json(fp):
    fp.register(["gh", "api", "-X", "GET", "/foo"], stdout="not json", returncode=0)
    with pytest.raises(GhError):
        gh_api_get("/foo")


# gh_api_graphql =====


def test_gh_api_graphql_passes_query_via_stdin_and_variables_as_F(fp):
    query = "query($n:Int!){ x(n:$n) }"
    captured: dict[str, object] = {}

    def callback(stdin_input):
        captured["stdin"] = stdin_input

    fp.register(
        ["gh", "api", "graphql", "-F", "query=@-", "-F", "n=42"],
        stdout='{"data":{"x":"hello"}}',
        stdin_callable=callback,
    )
    result = gh_api_graphql(query, n=42)
    assert result == {"x": "hello"}
    assert captured["stdin"] == query


def test_gh_api_graphql_with_host(fp):
    query = "query{ ok }"
    fp.register(
        ["gh", "api", "--hostname", "ghes.example.com", "graphql", "-F", "query=@-"],
        stdout='{"data":{"ok":true}}',
    )
    assert gh_api_graphql(query, host="ghes.example.com") == {"ok": True}


def test_gh_api_graphql_omits_None_variables(fp):
    # Regression: None-valued variables must NOT be passed as `-F key=`
    # (which sends the empty string verbatim, ill-defined for nullable
    # GraphQL variables like `String` cursors). GitHub treats a
    # declared-but-unprovided variable as null, so omitting the flag is
    # the correct way to mean "use null / from the start".
    query = "query($n:Int, $cursor:String){ x(n:$n, after:$cursor) }"
    fp.register(
        ["gh", "api", "graphql", "-F", "query=@-", "-F", "n=42"],
        stdout='{"data":{"x":"ok"}}',
    )
    result = gh_api_graphql(query, n=42, cursor=None)
    assert result == {"x": "ok"}
    # Inspect the actual args list to assert cursor flag is absent while n is present.
    call = fp.calls[0]
    args = list(call)
    assert "-F" in args
    # The presence of `n=42` proves non-None variables still go through.
    assert "n=42" in args
    # The absence of any `cursor=...` flag proves None was omitted.
    assert not any(a.startswith("cursor=") for a in args)


def test_gh_api_graphql_raises_GhError_when_errors_present(fp):
    fp.register(
        ["gh", "api", "graphql", "-F", "query=@-"],
        stdout='{"data":null,"errors":[{"message":"bad"}]}',
    )
    with pytest.raises(GhError) as exc:
        gh_api_graphql("query{ ok }")
    assert "bad" in exc.value.stderr


# gh_api_download =====


def test_gh_api_download_streams_to_tmp_then_renames(fp, tmp_path: Path):
    dest = tmp_path / "out.log"
    fp.register(["gh", "api", "-X", "GET", "/jobs/1/logs"], stdout=b"log bytes")
    bytes_written = gh_api_download("/jobs/1/logs", dest)
    assert bytes_written == len(b"log bytes")
    assert dest.exists()
    assert dest.read_bytes() == b"log bytes"
    # tmp must not exist after success
    assert not (tmp_path / "out.log.tmp").exists()


def test_gh_api_download_with_host_passes_hostname(fp, tmp_path: Path):
    dest = tmp_path / "out.log"
    fp.register(
        ["gh", "api", "--hostname", "ghes.example.com", "-X", "GET", "/x/logs"],
        stdout=b"a",
    )
    assert gh_api_download("/x/logs", dest, host="ghes.example.com") == 1


def test_gh_api_download_leaves_tmp_on_failure(fp, tmp_path: Path):
    dest = tmp_path / "out.log"
    fp.register(
        ["gh", "api", "-X", "GET", "/jobs/1/logs"],
        stdout=b"partial",
        stderr="gh: HTTP 500",
        returncode=1,
    )
    with pytest.raises(GhError) as exc:
        gh_api_download("/jobs/1/logs", dest)
    assert exc.value.returncode == 1
    assert exc.value.bytes_written == len(b"partial")
    tmp_path_actual = tmp_path / "out.log.tmp"
    assert exc.value.tmp_path == tmp_path_actual
    assert tmp_path_actual.exists()
    assert tmp_path_actual.read_bytes() == b"partial"
    # final dest should NOT exist
    assert not dest.exists()


def test_gh_api_download_404_carries_http_status(fp, tmp_path: Path):
    dest = tmp_path / "out.log"
    fp.register(
        ["gh", "api", "-X", "GET", "/jobs/1/logs"],
        stdout=b"",
        stderr="gh: HTTP 404: Not Found",
        returncode=1,
    )
    with pytest.raises(GhError) as exc:
        gh_api_download("/jobs/1/logs", dest)
    assert parse_http_status(exc.value.stderr) == 404
    # No tmp file when zero bytes were written before failure
    assert exc.value.bytes_written == 0


def test_gh_api_download_passes_stdin_devnull(monkeypatch, tmp_path: Path):
    # Regression: gh_api_download must pass stdin=subprocess.DEVNULL to
    # subprocess.Popen so the child process does not inherit the parent's
    # stdin. If `gh` prompts for interactive auth (or anything else), it
    # would otherwise hang waiting for input the agent will never provide.
    captured: dict[str, object] = {}
    real_popen = subprocess.Popen

    class _FakeProc:
        def __init__(self, args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            self.stdout = _Empty()
            self.stderr = _Empty()
            self.returncode = 0

        def wait(self):
            return 0

        def kill(self):
            pass

    class _Empty:
        def read(self, *_a, **_k):
            return b""

    def fake_popen(args, **kwargs):
        return _FakeProc(args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    try:
        gh_api_download("/jobs/1/logs", tmp_path / "out.log")
    finally:
        monkeypatch.setattr(subprocess, "Popen", real_popen)

    assert captured["kwargs"].get("stdin") is subprocess.DEVNULL
