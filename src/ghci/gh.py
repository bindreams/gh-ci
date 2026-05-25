from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class GhError(Exception):
    returncode: int
    stderr: str
    bytes_written: int = 0
    tmp_path: Path | None = None

    def __str__(self) -> str:
        return f"gh exited {self.returncode}: {self.stderr.strip()}"


_HTTP_STATUS_RE = re.compile(r"HTTP\s+(\d{3})")


def parse_http_status(stderr: str) -> int | None:
    m = _HTTP_STATUS_RE.search(stderr or "")
    return int(m.group(1)) if m else None


def _api_args(host: str | None, sub: list[str]) -> list[str]:
    args = ["gh", "api"]
    if host:
        args += ["--hostname", host]
    args += sub
    return args


def gh_api_get(
    path: str,
    *,
    host: str | None = None,
    paginate: bool = False,
) -> Any:
    sub: list[str] = []
    if paginate:
        # `gh api --paginate` concatenates per-page JSON without wrapping it,
        # which produces invalid JSON for object-returning endpoints (e.g.
        # `/repos/<o>/<r>/actions/runs/<id>/jobs` returns
        # `{"total_count": N, "jobs": [...]}`). Adding `--slurp` makes gh
        # wrap each page in a JSON array, which we then merge below.
        sub += ["--paginate", "--slurp"]
    sub += ["-X", "GET", path]
    args = _api_args(host, sub)
    proc = subprocess.run(args, capture_output=True, text=True)
    if proc.returncode != 0:
        raise GhError(returncode=proc.returncode, stderr=proc.stderr)
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise GhError(
            returncode=0, stderr=f"invalid JSON from gh: {e}: {proc.stdout[:200]}"
        ) from e
    if paginate:
        return _merge_paginated_pages(data)
    return data


def _merge_paginated_pages(pages: Any) -> Any:
    """Merge the list of per-page payloads emitted by `gh api --paginate --slurp`.

    Cases handled:
    - Empty list: return [] (no results at all).
    - Pages are arrays (e.g. `/issues`): concatenate them.
    - Pages are objects (e.g. `{"total_count": N, "jobs": [...]}`):
      concatenate array-valued fields across pages; non-array fields are
      taken from the first page (GitHub reports collection-wide values like
      `total_count` on every page, so summing would double-count).
    - Single page: return it as-is (preserves shape for callers that don't
      need merging).
    """
    if not isinstance(pages, list):
        # gh should always emit a JSON array under --slurp; if it didn't,
        # surface the value untouched so callers can decide what to do.
        return pages
    if not pages:
        return []
    if len(pages) == 1:
        return pages[0]
    first = pages[0]
    if isinstance(first, list):
        merged_list: list[Any] = []
        for page in pages:
            if not isinstance(page, list):
                raise GhError(
                    returncode=0,
                    stderr=(
                        "inconsistent paginated response shape from gh: "
                        f"expected list, got {type(page).__name__}"
                    ),
                )
            merged_list.extend(page)
        return merged_list
    if isinstance(first, dict):
        # Identify the array-valued keys (e.g. "jobs", "workflow_runs",
        # "artifacts", "check_runs"). We merge those by concatenation.
        # Non-array keys (e.g. "total_count") are taken from the first page;
        # GitHub reports the collection-wide total on every page, so summing
        # would double-count.
        array_keys = [k for k, v in first.items() if isinstance(v, list)]
        merged: dict[str, Any] = {}
        for k, v in first.items():
            if k in array_keys:
                merged[k] = []
            else:
                merged[k] = v
        for page in pages:
            if not isinstance(page, dict):
                raise GhError(
                    returncode=0,
                    stderr=(
                        "inconsistent paginated response shape from gh: "
                        f"expected dict, got {type(page).__name__}"
                    ),
                )
            for k in array_keys:
                page_val = page.get(k, [])
                if not isinstance(page_val, list):
                    raise GhError(
                        returncode=0,
                        stderr=(
                            f"inconsistent paginated response shape from gh: "
                            f"key {k!r} expected list, got "
                            f"{type(page_val).__name__}"
                        ),
                    )
                merged[k].extend(page_val)
        return merged
    # Scalar pages (unlikely from GitHub APIs): return the list as-is.
    return pages


def gh_api_graphql(
    query: str,
    *,
    host: str | None = None,
    **variables: Any,
) -> dict:
    sub: list[str] = ["graphql", "-F", "query=@-"]
    for k, v in variables.items():
        sub += ["-F", f"{k}={v}"]
    args = _api_args(host, sub)
    proc = subprocess.run(args, input=query, capture_output=True, text=True)
    if proc.returncode != 0:
        raise GhError(returncode=proc.returncode, stderr=proc.stderr)
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise GhError(
            returncode=0, stderr=f"invalid JSON from gh graphql: {e}"
        ) from e
    if payload.get("errors"):
        msg = "; ".join(str(e.get("message", e)) for e in payload["errors"])
        raise GhError(returncode=0, stderr=f"GraphQL errors: {msg}")
    return payload.get("data") or {}


def gh_api_download(
    path: str,
    dest: Path,
    *,
    host: str | None = None,
    chunk_size: int = 64 * 1024,
) -> int:
    sub = ["-X", "GET", path]
    args = _api_args(host, sub)
    tmp_path = dest.with_suffix(dest.suffix + ".tmp")
    bytes_written = 0
    proc = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert proc.stdout is not None
    try:
        with tmp_path.open("wb") as fh:
            while True:
                chunk = proc.stdout.read(chunk_size)
                if not chunk:
                    break
                fh.write(chunk)
                bytes_written += len(chunk)
        proc.wait()
    except BaseException:
        proc.kill()
        proc.wait()
        raise
    stderr_bytes = proc.stderr.read() if proc.stderr is not None else b""
    stderr = stderr_bytes.decode(errors="replace")
    if proc.returncode != 0:
        # Clean up zero-byte tmp files (e.g. immediate 404 with no body)
        actual_tmp: Path | None = tmp_path
        if bytes_written == 0 and tmp_path.exists():
            try:
                tmp_path.unlink()
                actual_tmp = None
            except OSError:
                pass
        raise GhError(
            returncode=proc.returncode,
            stderr=stderr,
            bytes_written=bytes_written,
            tmp_path=actual_tmp,
        )
    os.replace(tmp_path, dest)
    return bytes_written
