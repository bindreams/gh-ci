from __future__ import annotations

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


def matches(item: _MatchableCheckItem, rules: Iterable[IgnoreRule]) -> bool:
    for rule in rules:
        if rule.prefix == "workflow":
            if item.kind == "actions" and item.workflow_name == rule.name:
                return True
        elif rule.prefix == "job":
            if item.kind == "actions" and item.name == rule.name:
                return True
        elif rule.prefix == "check":
            if item.kind in ("check_run", "status_context") and item.name == rule.name:
                return True
    return False
