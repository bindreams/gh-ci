from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Iterable, Protocol


class IgnoreParseError(ValueError):
    """Raised when --ignore <value> cannot be parsed."""


@dataclass(frozen=True)
class IgnoreRule:
    prefix: str  # "workflow" | "job" | "check"
    name: str


_VALID_PREFIXES = ("workflow", "job", "check")


def parse_ignore(value: str) -> IgnoreRule:
    sep = value.find(":")
    if sep == -1:
        raise IgnoreParseError(
            f"--ignore expects 'workflow:<name>', 'job:<name>', or "
            f"'check:<name>' (got: '{value}')"
        )
    prefix = value[:sep]
    name = value[sep + 1 :]
    if prefix not in _VALID_PREFIXES:
        raise IgnoreParseError(
            f"--ignore expects 'workflow:<name>', 'job:<name>', or "
            f"'check:<name>' (got: '{value}')"
        )
    if not name:
        raise IgnoreParseError(
            f"--ignore {prefix}: requires a non-empty name (got: '{value}')"
        )
    return IgnoreRule(prefix=prefix, name=name)


class _MatchableCheckItem(Protocol):
    kind: str
    name: str
    workflow_name: str | None
    suite_placeholder: bool


def ignore_prefix(item: _MatchableCheckItem) -> str:
    """The --ignore prefix whose rule skips item by its name: "check" for a
    non-Actions check or status, "workflow" for a no-jobs run placeholder,
    else "job"."""
    if item.kind != "actions":
        return "check"
    return "workflow" if item.suite_placeholder else "job"


def ignore_arg(item: _MatchableCheckItem) -> str:
    """The --ignore value that skips item, quoted for a POSIX shell: names come
    from workflow authors and may hold quotes or `$(...)`."""
    return shlex.quote(f"{ignore_prefix(item)}:{item.name}")


def matches(item: _MatchableCheckItem, rules: Iterable[IgnoreRule]) -> bool:
    for rule in rules:
        if rule.prefix == "workflow":
            # A placeholder's name is its workflow's, or a label when the
            # workflow is unknown, so match on that too.
            if item.kind == "actions" and rule.name in (
                item.workflow_name, item.name if item.suite_placeholder else None,
            ):
                return True
        elif rule.prefix == "job":
            # A placeholder stands in for a whole run and carries its workflow's
            # name; a job rule with that name must not hide the run's pending jobs.
            if item.kind == "actions" and not item.suite_placeholder and item.name == rule.name:
                return True
        elif rule.prefix == "check":
            if item.kind in ("check_run", "status_context") and item.name == rule.name:
                return True
    return False
