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


def _shape_error(detail: str) -> GhError:
    return GhError(
        returncode=0,
        stderr=f"inconsistent paginated response shape from gh: {detail}",
    )


def _merge_paginated_pages(pages: Any) -> Any:
    """Merge the list of per-page payloads emitted by `gh api --paginate --slurp`.

    Cases handled:
    - Empty list (no results at all): return `{}`. This is a dict so that
      object-endpoint callers doing `data.get("workflow_runs", [])` keep
      working without an explicit empty-check. Array-endpoint callers
      iterating `data` will get an empty iteration (dicts iterate keys),
      which is also harmless.
    - Pages are arrays (e.g. `/issues`): concatenate them.
    - Pages are objects (e.g. `{"total_count": N, "jobs": [...]}`):
      concatenate array-valued fields across pages; non-array fields are
      taken from the first page (GitHub reports collection-wide values like
      `total_count` on every page, so summing would double-count).
    - Any `null` page raises GhError — gh should never emit a null page
      under `--slurp`, and silently passing it through hides upstream bugs.
    - Mixed shapes (dict-then-list or vice versa) raise GhError.
    """
    if not isinstance(pages, list):
        # gh should always emit a JSON array under --slurp; if it didn't,
        # surface the value untouched so callers can decide what to do.
        return pages
    if not pages:
        # Return a dict (not []) so callers using `.get(...)` work uniformly
        # across "no results" and "some results" without conditional checks.
        return {}
    # Validate every page up front. `null` pages are never valid: they slip
    # through later isinstance() checks and produce wrong results.
    for i, page in enumerate(pages):
        if page is None:
            raise _shape_error(f"page {i} is null")
    first = pages[0]
    if isinstance(first, list):
        merged_list: list[Any] = []
        for page in pages:
            if not isinstance(page, list):
                raise _shape_error(
                    f"expected list, got {type(page).__name__}"
                )
            merged_list.extend(page)
        return merged_list
    if isinstance(first, dict):
        # Validate dict-shape across all pages before scanning for array keys.
        for page in pages:
            if not isinstance(page, dict):
                raise _shape_error(
                    f"expected dict, got {type(page).__name__}"
                )
        # Identify array-valued keys (e.g. "jobs", "workflow_runs",
        # "artifacts", "check_runs") across ALL pages, not just page 0:
        # the first page may report the key as `null` or omit it while
        # later pages contain the actual array. We merge those by
        # concatenation. Non-array keys (e.g. "total_count") are taken
        # from the first page; GitHub reports the collection-wide total
        # on every page, so summing would double-count.
        array_keys: list[str] = []
        seen: set[str] = set()
        for page in pages:
            for k, v in page.items():
                if isinstance(v, list) and k not in seen:
                    array_keys.append(k)
                    seen.add(k)
        merged: dict[str, Any] = {}
        for k, v in first.items():
            if k in seen:
                merged[k] = []
            else:
                merged[k] = v
        # Ensure array keys discovered only on later pages are still present
        # in the result (initialized to empty before extension).
        for k in array_keys:
            merged.setdefault(k, [])
        for page in pages:
            for k in array_keys:
                if k not in page:
                    continue
                page_val = page[k]
                # `None` (JSON null) is acceptable for array keys on a per-
                # page basis (treated as "no items contributed by this page")
                # since GitHub occasionally reports the key as null when the
                # page has nothing to contribute. A non-list, non-null value
                # is a real shape error.
                if page_val is None:
                    continue
                if not isinstance(page_val, list):
                    raise _shape_error(
                        f"key {k!r} expected list, got "
                        f"{type(page_val).__name__}"
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
        # Skip None values so callers can pass `key=None` to mean "omit the
        # variable". GraphQL treats a declared-but-unprovided nullable
        # variable as null, which is the correct semantics for e.g. first-
        # page pagination (`after:null` == "from the start"). Passing
        # `-F key=` would send the empty string verbatim, which is
        # ill-defined for `String` cursors.
        if v is None:
            continue
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


_ALLOW_ESCAPE_FLAG = "--allow-escape-sequences"


def gh_api_download(
    path: str,
    dest: Path,
    *,
    host: str | None = None,
    chunk_size: int = 64 * 1024,
) -> int:
    # Body goes to a file, never a terminal, so escape sequences are safe.
    flagged = _api_args(host, [_ALLOW_ESCAPE_FLAG, "-X", "GET", path])
    try:
        return _download(flagged, dest, chunk_size)
    except GhError as e:
        # gh < 2.97 does not know the flag; it fails before making a request.
        unknown_flag = e.stderr.partition("\n")[0] == f"unknown flag: {_ALLOW_ESCAPE_FLAG}"
        if not (unknown_flag and e.bytes_written == 0):
            raise
    return _download(_api_args(host, ["-X", "GET", path]), dest, chunk_size)


def _download(args: list[str], dest: Path, chunk_size: int) -> int:
    tmp_path = dest.with_suffix(dest.suffix + ".tmp")
    bytes_written = 0
    proc = subprocess.Popen(
        args,
        stdin=subprocess.DEVNULL,
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
