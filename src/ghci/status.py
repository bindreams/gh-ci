from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from typing import Iterable

from ghci.checks import CheckItem, Outcome, classify_conclusion
from ghci.colors import (
    GROUP_STYLE_COLOR,
    Palette,
    RESULT_STYLE_COLOR,
    _GroupStyle,
    _ResultStyle,
    ensure_palette,
)
from ghci.conflicts import ConflictOutcome, assess_pr, message_for, result_style_for
from ghci.ignore import IgnoreRule, matches as ignore_matches


# evaluate_snapshot =====


def evaluate_snapshot(
    items: list[CheckItem],
    *,
    pr_meta: dict | None,
    pr_number: int | None,
    ignore_rules: list[IgnoreRule],
    palette: Palette | None = None,
) -> tuple[int, str]:
    """Single-snapshot evaluation.

    Returns (exit_code, summary_text). Summary is intended for stdout.
    """
    p = ensure_palette(palette)

    # PR-state conflict pre-check.
    if pr_meta is not None and pr_number is not None:
        outcome = assess_pr(pr_meta)
        if outcome == ConflictOutcome.CLOSED:
            return 4, _conflict_summary(outcome, pr_number, palette=p)
        if outcome == ConflictOutcome.DIRTY:
            return 4, _conflict_summary(outcome, pr_number, palette=p)
        if outcome == ConflictOutcome.BEHIND:
            return 4, _conflict_summary(outcome, pr_number, palette=p)
        if outcome == ConflictOutcome.UNKNOWN:
            return 7, _conflict_summary(outcome, pr_number, palette=p)
        # OK → fall through

    # Partition items.
    ignored = [it for it in items if ignore_matches(it, ignore_rules)]
    active = [it for it in items if not ignore_matches(it, ignore_rules)]

    # Any non-ignored failure?
    failed_items = [it for it in active if classify_conclusion(it.conclusion) == Outcome.FAILED]
    if failed_items:
        first_failed = failed_items[0]
        result_line = f'Result: red CI (job "{first_failed.name}" {first_failed.conclusion})'
        return 3, format_summary(
            items, ignored, result_line=result_line,
            result_style=_ResultStyle.RED, palette=p,
        )

    # Any in-flight?
    in_flight = [it for it in active if it.status != "completed" and classify_conclusion(it.conclusion) is None]
    if in_flight:
        result_line = "Result: still in progress"
        return 7, format_summary(
            items, ignored, result_line=result_line, in_flight_label="In progress",
            result_style=_ResultStyle.YELLOW, palette=p,
        )

    # All non-ignored terminal. Count successes.
    passes = [it for it in active if classify_conclusion(it.conclusion) == Outcome.PASSED]
    if passes:
        result_line = "Result: green"
        return 0, format_summary(
            items, ignored, result_line=result_line,
            result_style=_ResultStyle.GREEN, palette=p,
        )

    # Zero passes and zero failures → no productive CI.
    result_line = "Result: no productive CI ran"
    return 4, format_summary(
        items, ignored, result_line=result_line,
        result_style=_ResultStyle.RED, palette=p,
    )


def _conflict_summary(
    outcome: ConflictOutcome,
    pr_number: int,
    *,
    palette: Palette | None = None,
) -> str:
    p = ensure_palette(palette)
    line = message_for(outcome, pr_number=pr_number)
    result_line = f"Result: {outcome.value}"
    style = result_style_for(outcome)
    if style is not None:
        color = RESULT_STYLE_COLOR[style]
        result_line = p.style(result_line, color=color, bold=True)
    return f"{result_line}\n{line}"


# Summary formatting (shared with watch) =====


# Group order in the summary block. Conclusions displayed verbatim; the
# third tuple element is the _GroupStyle that drives label coloring.
_GROUP_ORDER: tuple[tuple[str, str | None, _GroupStyle], ...] = (
    ("Passed", "success", _GroupStyle.PASSED),
    ("Failed", "failure", _GroupStyle.FAILED),
    ("Cancelled", "cancelled", _GroupStyle.FAILED),
    ("Timed out", "timed_out", _GroupStyle.FAILED),
    ("Action required", "action_required", _GroupStyle.FAILED),
    ("Startup failure", "startup_failure", _GroupStyle.FAILED),
    ("Skipped", "skipped", _GroupStyle.NEUTRAL),
    ("Neutral", "neutral", _GroupStyle.NEUTRAL),
    ("Stale", "stale", _GroupStyle.NEUTRAL),
)

# Conclusion strings already covered by a dedicated group. Anything else
# (including None and any future GitHub-added conclusion) falls into the
# Unknown bucket so it can't silently vanish from summaries.
_KNOWN_CONCLUSIONS: frozenset[str] = frozenset(
    c for _, c, _ in _GROUP_ORDER if c is not None
)


@dataclass
class _GroupSpec:
    """Internal record for a rendered group line.

    `style` decides the color; `ignored` overrides to dim regardless of style.
    """
    label: str
    names: list[str]
    style: _GroupStyle
    ignored: bool = False


def format_summary(
    all_items: list[CheckItem],
    ignored_items: list[CheckItem],
    *,
    result_line: str,
    in_flight_label: str | None = None,
    stalled_items: Iterable[CheckItem] | None = None,
    result_style: _ResultStyle | None = None,
    palette: Palette | None = None,
) -> str:
    p = ensure_palette(palette)
    active = [it for it in all_items if it not in ignored_items]

    groups: OrderedDict[str, _GroupSpec] = OrderedDict()
    for label, conclusion, style in _GROUP_ORDER:
        names = [it.name for it in active if (it.conclusion or "") == (conclusion or "")]
        if names:
            groups[label] = _GroupSpec(label=label, names=names, style=style)

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
            and not it.suite_placeholder
            and (it.kind, it.name, it.workflow_name) not in stalled_keys
        ]
        if in_flight_names:
            groups[in_flight_label] = _GroupSpec(
                label=in_flight_label, names=in_flight_names,
                style=_GroupStyle.IN_FLIGHT,
            )

    # Actions workflow runs whose check suite exists but has not reported any
    # jobs yet (queued runs). Surfaced as a distinct group so the agent sees
    # which workflow is still pending and why the PR is not green. Failed suite
    # placeholders (status "completed") are excluded — they ride the normal
    # conclusion groups (e.g. "Startup failure").
    suite_pending_names = [
        it.name for it in active
        if it.suite_placeholder and it.status != "completed"
    ]
    if suite_pending_names:
        groups["In progress (no jobs reported yet)"] = _GroupSpec(
            label="In progress (no jobs reported yet)",
            names=suite_pending_names,
            style=_GroupStyle.IN_FLIGHT,
        )

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
        groups["Unknown"] = _GroupSpec(
            label="Unknown", names=unknown_active, style=_GroupStyle.UNKNOWN,
        )

    if stalled_items_list:
        groups["Not reported (stalled)"] = _GroupSpec(
            label="Not reported (stalled)",
            names=[it.name for it in stalled_items_list],
            style=_GroupStyle.STALLED,
        )

    for label, conclusion, style in _GROUP_ORDER:
        names = [it.name for it in ignored_items if (it.conclusion or "") == (conclusion or "")]
        if names:
            ig_label = f"{label} (ignored by --ignore)"
            groups[ig_label] = _GroupSpec(
                label=ig_label, names=names, style=style, ignored=True,
            )

    # In-progress ignored, treated separately.
    ig_in_flight = [
        it.name
        for it in ignored_items
        if it.status != "completed" and classify_conclusion(it.conclusion) is None
    ]
    if ig_in_flight:
        ig_in_flight_label = "In progress (ignored by --ignore)"
        groups[ig_in_flight_label] = _GroupSpec(
            label=ig_in_flight_label, names=ig_in_flight,
            style=_GroupStyle.IN_FLIGHT, ignored=True,
        )

    # S6 (ignored variant): mirror the Unknown group for ignored items.
    ig_unknown = [
        it.name
        for it in ignored_items
        if it.status == "completed" and it.conclusion not in _KNOWN_CONCLUSIONS
    ]
    if ig_unknown:
        ig_unknown_label = "Unknown (ignored by --ignore)"
        groups[ig_unknown_label] = _GroupSpec(
            label=ig_unknown_label, names=ig_unknown,
            style=_GroupStyle.UNKNOWN, ignored=True,
        )

    rendered_result = _render_result_line(result_line, result_style, p)
    lines = [rendered_result]
    for spec in groups.values():
        styled_label = _render_group_label(spec, p)
        lines.append(f"{styled_label}: {', '.join(spec.names)}")
    return "\n".join(lines)


def _render_result_line(
    result_line: str,
    result_style: _ResultStyle | None,
    palette: Palette,
) -> str:
    """Color only the first line of a (possibly multi-line) result block."""
    if result_style is None:
        return result_line
    first, sep, rest = result_line.partition("\n")
    color = RESULT_STYLE_COLOR[result_style]
    colored_first = palette.style(first, color=color, bold=True)
    return colored_first if not sep else f"{colored_first}{sep}{rest}"


def _render_group_label(spec: _GroupSpec, palette: Palette) -> str:
    """Color a group label per its style key. Ignored groups are always dim."""
    if spec.ignored:
        return palette.style(spec.label, dim=True)
    if spec.style == _GroupStyle.NEUTRAL:
        return palette.style(spec.label, dim=True)
    color = GROUP_STYLE_COLOR[spec.style]
    return palette.style(spec.label, color=color)
