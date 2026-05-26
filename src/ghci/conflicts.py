from __future__ import annotations

from enum import Enum

from ghci.colors import _ResultStyle


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
    mergeable_state = pr.get("mergeable_state")
    if mergeable_state is None:
        return ConflictOutcome.UNKNOWN
    mergeable_state = mergeable_state.lower()

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
            # Plan §status step 4 specifies this exact wording (no PR-#
            # prefix). The caller is responsible for printing any target
            # context (resolution line, etc.) if needed.
            return "Mergeability still computing; retry with watch."


def result_style_for(outcome: ConflictOutcome) -> _ResultStyle | None:
    """Return the `Result:` line style for a conflict outcome.

    RED for hard stops (closed / dirty / behind), YELLOW for the still-
    computing UNKNOWN state, None for OK (no summary line is printed).
    Shared by `status._conflict_summary` and `watch.loop._conflict_summary`.
    """
    match outcome:
        case ConflictOutcome.OK:
            return None
        case ConflictOutcome.UNKNOWN:
            return _ResultStyle.YELLOW
        case ConflictOutcome.CLOSED | ConflictOutcome.DIRTY | ConflictOutcome.BEHIND:
            return _ResultStyle.RED
