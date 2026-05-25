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
        sub.append("--paginate")
    sub += ["-X", "GET", path]
    args = _api_args(host, sub)
    proc = subprocess.run(args, capture_output=True, text=True)
    if proc.returncode != 0:
        raise GhError(returncode=proc.returncode, stderr=proc.stderr)
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise GhError(
            returncode=0, stderr=f"invalid JSON from gh: {e}: {proc.stdout[:200]}"
        ) from e


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
