#!/usr/bin/env python3
"""Capture a gh API response as a sanitized JSON fixture.

Usage:
    python scripts/capture_fixture.py <fixture-name> -- gh <args...>

Examples:
    python scripts/capture_fixture.py pr_meta_dirty -- \\
        gh api -X GET /repos/owner/repo/pulls/123

    python scripts/capture_fixture.py rollup_one_failure -- \\
        gh api graphql -F query=@my_query.graphql -F number=123

The script runs the gh command, sanitizes the stdout (removing tokens, emails,
and token-bearing URLs), then writes the result to
`tests/fixtures/<fixture-name>.json`.

If you don't sanitize a value you care about, edit the file by hand afterward.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path


SANITIZERS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"ghs_[A-Za-z0-9]{20,}"), "ghs_REDACTED"),
    (re.compile(r"ghp_[A-Za-z0-9]{20,}"), "ghp_REDACTED"),
    (re.compile(r"gho_[A-Za-z0-9]{20,}"), "gho_REDACTED"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), "github_pat_REDACTED"),
    # Token-bearing query strings (e.g. log download URLs)
    (re.compile(r"token=[A-Za-z0-9%_-]{20,}"), "token=REDACTED"),
    # Email addresses
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "user@example.com"),
]


def sanitize(text: str) -> str:
    for pat, repl in SANITIZERS:
        text = pat.sub(repl, text)
    return text


def main(argv: list[str]) -> int:
    if "--" not in argv:
        print(
            "Usage: capture_fixture.py <name> -- gh <args...>",
            file=sys.stderr,
        )
        return 2
    sep = argv.index("--")
    leading = argv[:sep]
    gh_args = argv[sep + 1 :]
    if len(leading) != 1 or not gh_args or gh_args[0] != "gh":
        print(
            "Usage: capture_fixture.py <name> -- gh <args...>",
            file=sys.stderr,
        )
        return 2
    name = leading[0]
    fixtures_dir = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
    fixtures_dir.mkdir(parents=True, exist_ok=True)
    out_path = fixtures_dir / f"{name}.json"

    proc = subprocess.run(gh_args, capture_output=True, text=True)
    if proc.returncode != 0:
        print(f"gh exited {proc.returncode}", file=sys.stderr)
        print(proc.stderr, file=sys.stderr)
        return proc.returncode

    sanitized = sanitize(proc.stdout)
    # Pretty-print if it's valid JSON; otherwise write raw.
    try:
        parsed = json.loads(sanitized)
        out_path.write_text(json.dumps(parsed, indent=2))
    except json.JSONDecodeError:
        out_path.write_text(sanitized)

    print(f"Wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
