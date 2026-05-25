from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Literal
from urllib.parse import quote

from ghci.gh import gh_api_get, gh_api_graphql


CheckKind = Literal["actions", "check_run", "status_context"]


@dataclass(frozen=True)
class CheckItem:
    kind: CheckKind
    name: str
    workflow_name: str | None
    status: str
    conclusion: str | None
    url: str | None
    required: bool
    check_run_id: int | None
    run_id: int | None
    workflow_run_url: str | None


class Outcome(Enum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"


_FAILED_CONCLUSIONS = frozenset(
    {"failure", "cancelled", "timed_out", "action_required", "startup_failure"}
)
_SKIPPED_CONCLUSIONS = frozenset({"skipped", "neutral", "stale"})


def classify_conclusion(conclusion: str | None) -> Outcome | None:
    if conclusion is None:
        return None
    c = conclusion.lower()
    if c == "success":
        return Outcome.PASSED
    if c in _FAILED_CONCLUSIONS:
        return Outcome.FAILED
    if c in _SKIPPED_CONCLUSIONS:
        return Outcome.SKIPPED
    return None


# GraphQL =====

_PR_QUERY = """
query($owner:String!, $repo:String!, $number:Int!, $cursor:String) {
  repository(owner:$owner, name:$repo) {
    pullRequest(number:$number) {
      state
      mergeable
      headRefOid
      commits(last:1) {
        nodes {
          commit {
            statusCheckRollup {
              contexts(first:100, after:$cursor) {
                pageInfo { hasNextPage endCursor }
                nodes {
                  __typename
                  ... on CheckRun {
                    name conclusion status detailsUrl
                    isRequired(pullRequestNumber:$number)
                    databaseId
                    checkSuite {
                      workflowRun {
                        databaseId
                        url
                        workflow { name }
                      }
                    }
                  }
                  ... on StatusContext {
                    context state targetUrl
                    isRequired(pullRequestNumber:$number)
                  }
                }
              }
            }
          }
        }
      }
    }
  }
}
""".strip()


def fetch_pr_checks(
    owner: str,
    repo: str,
    pr_number: int,
    *,
    host: str | None = None,
    gh_graphql_fn: Callable[..., dict] = gh_api_graphql,
) -> tuple[list[CheckItem], dict[str, Any]]:
    """Fetch all CheckItems for a PR via paginated GraphQL.

    Returns (items, pr_meta) where pr_meta has {state, mergeable, headRefOid}.
    """
    items: list[CheckItem] = []
    cursor: str | None = None
    meta: dict[str, Any] = {}
    while True:
        data = gh_graphql_fn(
            _PR_QUERY,
            host=host,
            owner=owner,
            repo=repo,
            number=pr_number,
            cursor=cursor or "",
        )
        pr = data["repository"]["pullRequest"]
        if not meta:
            meta = {
                "state": pr.get("state"),
                "mergeable": pr.get("mergeable"),
                "headRefOid": pr.get("headRefOid"),
            }
        commit_nodes = pr.get("commits", {}).get("nodes", [])
        if not commit_nodes:
            break
        rollup = commit_nodes[0]["commit"].get("statusCheckRollup")
        if rollup is None:
            break
        contexts = rollup.get("contexts", {})
        for node in contexts.get("nodes", []) or []:
            items.append(_node_to_check_item(node))
        page = contexts.get("pageInfo", {})
        if not page.get("hasNextPage"):
            break
        cursor = page.get("endCursor")
    return items, meta


def _node_to_check_item(node: dict) -> CheckItem:
    typename = node.get("__typename")
    if typename == "CheckRun":
        suite = node.get("checkSuite") or {}
        wf_run = suite.get("workflowRun")
        if wf_run is not None:
            workflow = wf_run.get("workflow") or {}
            return CheckItem(
                kind="actions",
                name=node["name"],
                workflow_name=workflow.get("name"),
                status=str(node.get("status", "")).lower(),
                conclusion=_lower_or_none(node.get("conclusion")),
                url=node.get("detailsUrl"),
                required=bool(node.get("isRequired")),
                check_run_id=node.get("databaseId"),
                run_id=wf_run.get("databaseId"),
                workflow_run_url=wf_run.get("url"),
            )
        # CheckRun without a workflowRun — a third-party check creator.
        return CheckItem(
            kind="check_run",
            name=node["name"],
            workflow_name=None,
            status=str(node.get("status", "")).lower(),
            conclusion=_lower_or_none(node.get("conclusion")),
            url=node.get("detailsUrl"),
            required=bool(node.get("isRequired")),
            check_run_id=node.get("databaseId"),
            run_id=None,
            workflow_run_url=None,
        )
    if typename == "StatusContext":
        state = str(node.get("state", "")).upper()
        status, conclusion = _status_context_to_status_conclusion(state)
        return CheckItem(
            kind="status_context",
            name=node["context"],
            workflow_name=None,
            status=status,
            conclusion=conclusion,
            url=node.get("targetUrl"),
            required=bool(node.get("isRequired")),
            check_run_id=None,
            run_id=None,
            workflow_run_url=None,
        )
    # Unknown __typename — degrade gracefully.
    return CheckItem(
        kind="check_run",
        name=str(node.get("name") or node.get("context") or "?"),
        workflow_name=None,
        status="completed",
        conclusion=None,
        url=None,
        required=False,
        check_run_id=None,
        run_id=None,
        workflow_run_url=None,
    )


def _lower_or_none(v: Any) -> str | None:
    if v is None:
        return None
    return str(v).lower()


def _status_context_to_status_conclusion(
    state: str,
) -> tuple[str, str | None]:
    """Translate StatusState enum to our (status, conclusion) tuple."""
    if state == "SUCCESS":
        return "completed", "success"
    if state in ("FAILURE", "ERROR"):
        return "completed", "failure"
    if state == "PENDING":
        return "pending", None
    if state == "EXPECTED":
        return "expected", None
    return "completed", None


# REST builders for non-PR targets =====


def fetch_pr_meta(
    owner: str,
    repo: str,
    pr_number: int,
    *,
    host: str | None = None,
    gh_get: Callable[..., Any] = gh_api_get,
) -> dict:
    """Fetch /pulls/N via REST. Returns the dict (state/mergeable/mergeable_state/head)."""
    return gh_get(f"/repos/{owner}/{repo}/pulls/{pr_number}", host=host)


def fetch_run(
    owner: str,
    repo: str,
    run_id: int,
    *,
    host: str | None = None,
    gh_get: Callable[..., Any] = gh_api_get,
) -> dict:
    return gh_get(f"/repos/{owner}/{repo}/actions/runs/{run_id}", host=host)


def fetch_run_jobs(
    owner: str,
    repo: str,
    run_id: int,
    *,
    workflow_name: str | None,
    host: str | None = None,
    gh_get: Callable[..., Any] = gh_api_get,
) -> list[CheckItem]:
    data = gh_get(
        f"/repos/{owner}/{repo}/actions/runs/{run_id}/jobs",
        host=host,
        paginate=True,
    )
    items: list[CheckItem] = []
    for j in data.get("jobs", []):
        items.append(_job_dict_to_check_item(j, workflow_name=workflow_name))
    return items


def fetch_job(
    owner: str,
    repo: str,
    job_id: int,
    *,
    workflow_name: str | None,
    host: str | None = None,
    gh_get: Callable[..., Any] = gh_api_get,
) -> CheckItem:
    data = gh_get(f"/repos/{owner}/{repo}/actions/jobs/{job_id}", host=host)
    return _job_dict_to_check_item(data, workflow_name=workflow_name)


def fetch_workflow_latest_run(
    owner: str,
    repo: str,
    workflow_path: str,
    *,
    branch: str,
    host: str | None = None,
    gh_get: Callable[..., Any] = gh_api_get,
) -> dict | None:
    path = (
        f"/repos/{owner}/{repo}/actions/workflows/{quote(workflow_path)}/runs"
        f"?branch={quote(branch)}&per_page=1"
    )
    data = gh_get(path, host=host)
    runs = data.get("workflow_runs", [])
    return runs[0] if runs else None


def fetch_workflow_latest_run_or_raise(
    owner: str,
    repo: str,
    workflow_path: str,
    *,
    branch: str,
    host: str | None = None,
    gh_get: Callable[..., Any] = gh_api_get,
) -> dict:
    """Like fetch_workflow_latest_run but raises EmptyTargetError if no runs.

    Used to convert the "recognized-but-empty" workflow target case into a
    distinct error path (CLI exit 2) instead of silently returning empty
    item lists that downstream code misclassifies as "no productive CI ran".
    """
    # Local import to avoid a module-level cycle: target.py imports gh, and
    # checks.py is widely imported.
    from ghci.target import EmptyTargetError

    run = fetch_workflow_latest_run(
        owner, repo, workflow_path, branch=branch, host=host, gh_get=gh_get,
    )
    if run is None:
        raise EmptyTargetError(
            f"Workflow {workflow_path!r} in {owner}/{repo} has no runs on "
            f"branch {branch!r} (recognized target, but empty). "
            f"Check the workflow filename and branch, or trigger a run."
        )
    return run


def _job_dict_to_check_item(j: dict, *, workflow_name: str | None) -> CheckItem:
    return CheckItem(
        kind="actions",
        name=j["name"],
        workflow_name=workflow_name,
        status=str(j.get("status") or "").lower(),
        conclusion=_lower_or_none(j.get("conclusion")),
        url=j.get("html_url"),
        required=False,
        check_run_id=None,
        run_id=j.get("run_id"),
        workflow_run_url=None,
    )
