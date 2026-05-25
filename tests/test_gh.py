from __future__ import annotations

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
    # each page in a JSON array. We must merge the `jobs` arrays and sum
    # `total_count` across pages.
    page1 = '{"total_count": 3, "jobs": [{"id": 1}, {"id": 2}]}'
    page2 = '{"total_count": 3, "jobs": [{"id": 3}]}'
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
    # No results at all: --slurp emits []. Caller should get a sensible empty
    # value (we choose [] to match the array-endpoint shape).
    fp.register(
        ["gh", "api", "--paginate", "--slurp", "-X", "GET", "/foo"],
        stdout="[]",
    )
    assert gh_api_get("/foo", paginate=True) == []


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
