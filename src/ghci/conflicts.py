from __future__ import annotations

from enum import Enum


class ConflictOutcome(Enum):
    OK = "ok"
    UNKNOWN = "unknown"     # mergeability still computing — keep watching
    CLOSED = "closed"       # PR is closed (merged or not)
    DIRTY = "dirty"         # merge conflicts
    BEHIND = "behind"       # base branch ahead; "needs update" gate


def assess_pr(pr: dict) -> ConflictOutcome:
    state = (pr.get("state") or "").lower()
    if state == "closed":
        return ConflictOutcome.CLOSED

    mergeable = pr.get("mergeable")
    mergeable_state = (pr.get("mergeable_state") or "").lower()

    if mergeable_state == "dirty":
        return ConflictOutcome.DIRTY
    if mergeable_state == "behind":
        return ConflictOutcome.BEHIND
    if mergeable_state == "unknown" or mergeable is None:
        return ConflictOutcome.UNKNOWN
    return ConflictOutcome.OK


def message_for(outcome: ConflictOutcome, *, pr_number: int) -> str:
    match outcome:
        case ConflictOutcome.OK:
            return ""
        case ConflictOutcome.CLOSED:
            return f"PR #{pr_number} is closed; nothing to watch."
        case ConflictOutcome.DIRTY:
            return (
                f"PR #{pr_number} has merge conflicts — CI cannot run. "
                f"Resolve conflicts and rerun."
            )
        case ConflictOutcome.BEHIND:
            return (
                f'PR #{pr_number} is behind its base branch — required '
                f'"needs update" gate. Update the branch.'
            )
        case ConflictOutcome.UNKNOWN:
            return (
                f"PR #{pr_number} mergeability is still computing; "
                f"retry with watch."
            )
