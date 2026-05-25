from __future__ import annotations

from collections import OrderedDict
from typing import Iterable

from ghci.checks import CheckItem, Outcome, classify_conclusion
from ghci.conflicts import ConflictOutcome, assess_pr, message_for
from ghci.ignore import IgnoreRule, matches as ignore_matches


def evaluate_snapshot(
    items: list[CheckItem],
    *,
    pr_meta: dict | None,
    pr_number: int | None,
    ignore_rules: list[IgnoreRule],
) -> tuple[int, str]:
    """Single-snapshot evaluation.

    Returns (exit_code, summary_text). Summary is intended for stdout.
    """
    # PR-state conflict pre-check.
    if pr_meta is not None and pr_number is not None:
        outcome = assess_pr(pr_meta)
        if outcome == ConflictOutcome.CLOSED:
            return 4, _conflict_summary(outcome, pr_number)
        if outcome == ConflictOutcome.DIRTY:
            return 4, _conflict_summary(outcome, pr_number)
        if outcome == ConflictOutcome.BEHIND:
            return 4, _conflict_summary(outcome, pr_number)
        if outcome == ConflictOutcome.UNKNOWN:
            return 7, _conflict_summary(outcome, pr_number)
        # OK → fall through

    # Partition items.
    ignored = [it for it in items if ignore_matches(it, ignore_rules)]
    active = [it for it in items if not ignore_matches(it, ignore_rules)]

    # Any non-ignored failure?
    failed_items = [it for it in active if classify_conclusion(it.conclusion) == Outcome.FAILED]
    if failed_items:
        first_failed = failed_items[0]
        result_line = f'Result: red CI (job "{first_failed.name}" {first_failed.conclusion})'
        return 3, format_summary(items, ignored, result_line=result_line)

    # Any in-flight?
    in_flight = [it for it in active if it.status != "completed" and classify_conclusion(it.conclusion) is None]
    if in_flight:
        result_line = "Result: still in progress"
        return 7, format_summary(
            items, ignored, result_line=result_line, in_flight_label="In progress"
        )

    # All non-ignored terminal. Count successes.
    passes = [it for it in active if classify_conclusion(it.conclusion) == Outcome.PASSED]
    if passes:
        result_line = "Result: green"
        return 0, format_summary(items, ignored, result_line=result_line)

    # Zero passes and zero failures → no productive CI.
    result_line = "Result: no productive CI ran"
    return 4, format_summary(items, ignored, result_line=result_line)


def _conflict_summary(outcome: ConflictOutcome, pr_number: int) -> str:
    line = message_for(outcome, pr_number=pr_number)
    return f"Result: {outcome.value}\n{line}"


# Summary formatting (shared with watch) =====


# Group order in the summary block. Conclusions displayed verbatim.
_GROUP_ORDER: tuple[tuple[str, str | None], ...] = (
    ("Passed", "success"),
    ("Failed", "failure"),
    ("Cancelled", "cancelled"),
    ("Timed out", "timed_out"),
    ("Action required", "action_required"),
    ("Startup failure", "startup_failure"),
    ("Skipped", "skipped"),
    ("Neutral", "neutral"),
    ("Stale", "stale"),
)

# Conclusion strings already covered by a dedicated group. Anything else
# (including None and any future GitHub-added conclusion) falls into the
# Unknown bucket so it can't silently vanish from summaries.
_KNOWN_CONCLUSIONS: frozenset[str] = frozenset(
    c for _, c in _GROUP_ORDER if c is not None
)


def format_summary(
    all_items: list[CheckItem],
    ignored_items: list[CheckItem],
    *,
    result_line: str,
    in_flight_label: str | None = None,
    stalled_items: Iterable[CheckItem] | None = None,
) -> str:
    active = [it for it in all_items if it not in ignored_items]

    groups: OrderedDict[str, list[str]] = OrderedDict()
    for label, conclusion in _GROUP_ORDER:
        names = [it.name for it in active if (it.conclusion or "") == (conclusion or "")]
        if names:
            groups[label] = names

    # Materialize stalled_items once (it may be an Iterable / generator).
    # Stalled items appear in their own "Not reported (stalled)" group; do
    # not also surface them as In progress. Dedupe by (kind, name,
    # workflow_name) — name alone is insufficient because two checks may
    # share a name across different kinds or workflows (matrix builds, a
    # status context and an Actions job with the same display name, etc.).
    stalled_items_list: list[CheckItem] = list(stalled_items) if stalled_items else []
    stalled_keys: set[tuple[str, str, str | None]] = {
        (it.kind, it.name, it.workflow_name) for it in stalled_items_list
    }

    if in_flight_label:
        in_flight_names = [
            it.name
            for it in active
            if it.status != "completed"
            and classify_conclusion(it.conclusion) is None
            and (it.kind, it.name, it.workflow_name) not in stalled_keys
        ]
        if in_flight_names:
            groups[in_flight_label] = in_flight_names

    # S6: terminal items whose conclusion isn't None and isn't in the known
    # group set would otherwise vanish from the summary. This covers both
    # the original pathological completed/None case AND any future-or-rare
    # conclusion strings GitHub might introduce. Surface them under
    # "Unknown".
    unknown_active = [
        it.name
        for it in active
        if it.status == "completed" and it.conclusion not in _KNOWN_CONCLUSIONS
    ]
    if unknown_active:
        groups["Unknown"] = unknown_active

    if stalled_items_list:
        groups["Not reported (stalled)"] = [it.name for it in stalled_items_list]

    for label, conclusion in _GROUP_ORDER:
        names = [it.name for it in ignored_items if (it.conclusion or "") == (conclusion or "")]
        if names:
            groups[f"{label} (ignored by --ignore)"] = names

    # In-progress ignored, treated separately.
    ig_in_flight = [
        it.name
        for it in ignored_items
        if it.status != "completed" and classify_conclusion(it.conclusion) is None
    ]
    if ig_in_flight:
        groups["In progress (ignored by --ignore)"] = ig_in_flight

    # S6 (ignored variant): mirror the Unknown group for ignored items.
    ig_unknown = [
        it.name
        for it in ignored_items
        if it.status == "completed" and it.conclusion not in _KNOWN_CONCLUSIONS
    ]
    if ig_unknown:
        groups["Unknown (ignored by --ignore)"] = ig_unknown

    lines = [result_line]
    for label, names in groups.items():
        lines.append(f"{label}: {', '.join(names)}")
    return "\n".join(lines)
