from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Union
from urllib.parse import parse_qs, urlparse

from ghci.gh import gh_api_get


class TargetParseError(ValueError):
    """Raised when the target string cannot be resolved to a known target form."""


class EmptyTargetError(ValueError):
    """Raised when a target is recognized but has no runs/data to evaluate.

    Currently triggered when a WorkflowTarget resolves to a workflow URL but
    the workflow has no runs on the requested branch (e.g. brand-new workflow,
    branch typo). The CLI surfaces this as exit code 2 with a clear message.
    """


@dataclass(frozen=True)
class PrTarget:
    owner: str
    repo: str
    host: str
    pr_number: int


@dataclass(frozen=True)
class RunTarget:
    owner: str
    repo: str
    host: str
    run_id: int


@dataclass(frozen=True)
class JobTarget:
    owner: str
    repo: str
    host: str
    run_id: int
    job_id: int


@dataclass(frozen=True)
class WorkflowTarget:
    owner: str
    repo: str
    host: str
    workflow_path: str  # filename, e.g. "ci.yml"
    branch: str


ResolvedTarget = Union[PrTarget, RunTarget, JobTarget, WorkflowTarget]

GhGetFn = Callable[..., object]


_SUPPORTED_FORMS_MSG = (
    "Supported targets: PR URL (.../pull/<n>), Run URL (.../actions/runs/<id>), "
    "Job URL (.../actions/runs/<id>/job(s)/<id>), Workflow URL "
    "(.../actions/workflows/<name>.yml). Bare PR numbers, commit SHAs, and "
    "branch names are not supported in v1."
)


# NOTE: re.IGNORECASE matches the static route segments (pull, actions, runs,
# jobs, workflows) case-insensitively, while the (?P<owner>...) and (?P<repo>...)
# captures still preserve the original case of the matched substring. The
# workflow filename group also preserves case since `\.ya?ml` only affects the
# extension match, and GitHub treats workflow filenames case-sensitively in
# practice -- but accepting mixed-case `.YML` for matching purposes is harmless
# because the captured string is used as-is.
_PR_RE = re.compile(
    r"^/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<num>\d+)(?:/.*)?$",
    re.IGNORECASE,
)
_RUN_RE = re.compile(
    r"^/(?P<owner>[^/]+)/(?P<repo>[^/]+)/actions/runs/(?P<run>\d+)"
    r"(?:/attempts/\d+)?/?$",
    re.IGNORECASE,
)
_JOB_RE = re.compile(
    r"^/(?P<owner>[^/]+)/(?P<repo>[^/]+)/actions/runs/(?P<run>\d+)"
    r"/jobs?/(?P<job>\d+)/?$",
    re.IGNORECASE,
)
_WORKFLOW_RE = re.compile(
    r"^/(?P<owner>[^/]+)/(?P<repo>[^/]+)/actions/workflows/(?P<wf>[^/]+\.ya?ml)/?$",
    re.IGNORECASE,
)

_BRANCH_IN_QUERY_RE = re.compile(r"branch:([^\s+]+)")


def resolve_target(url: str, *, gh_get: GhGetFn = gh_api_get) -> ResolvedTarget:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        raise TargetParseError(
            f"Not a recognized target: {url!r}. {_SUPPORTED_FORMS_MSG}"
        )
    host = parsed.netloc.lower()
    path = parsed.path.rstrip("/")
    if not path:
        raise TargetParseError(
            f"Not a recognized target: {url!r}. {_SUPPORTED_FORMS_MSG}"
        )

    # PR URL — handle before run/workflow to be safe.
    m = _PR_RE.match(path)
    if m:
        return PrTarget(
            owner=m["owner"],
            repo=m["repo"],
            host=host,
            pr_number=int(m["num"]),
        )

    # Job URL (more specific than run URL — match first).
    m = _JOB_RE.match(path)
    if m:
        return JobTarget(
            owner=m["owner"],
            repo=m["repo"],
            host=host,
            run_id=int(m["run"]),
            job_id=int(m["job"]),
        )

    # Run URL.
    m = _RUN_RE.match(path)
    if m:
        return RunTarget(
            owner=m["owner"],
            repo=m["repo"],
            host=host,
            run_id=int(m["run"]),
        )

    # Workflow URL.
    m = _WORKFLOW_RE.match(path)
    if m:
        owner, repo, wf = m["owner"], m["repo"], m["wf"]
        branch = _extract_branch(parsed.query)
        if branch is None:
            data = gh_get(f"/repos/{owner}/{repo}", host=_host_for_gh(host))
            branch = data["default_branch"] if isinstance(data, dict) else "main"
        return WorkflowTarget(
            owner=owner,
            repo=repo,
            host=host,
            workflow_path=wf,
            branch=branch,
        )

    raise TargetParseError(
        f"Unrecognized GitHub URL: {url!r}. {_SUPPORTED_FORMS_MSG}"
    )


def _extract_branch(query: str) -> str | None:
    if not query:
        return None
    qs = parse_qs(query, keep_blank_values=False)
    if "branch" in qs and qs["branch"]:
        return qs["branch"][0]
    if "query" in qs and qs["query"]:
        m = _BRANCH_IN_QUERY_RE.search(qs["query"][0])
        if m:
            return m.group(1)
    return None


def _host_for_gh(host: str) -> str | None:
    # gh defaults to github.com; only pass --hostname for non-default.
    return None if host == "github.com" else host


def host_for_gh(target: ResolvedTarget) -> str | None:
    """Public helper: extract the host for gh API calls (None for github.com)."""
    return _host_for_gh(target.host)
