from __future__ import annotations

import os
from enum import IntEnum
from typing import Mapping, TextIO


# ANSI sequences =====

_RESET = "\033[0m"
_RED = "\033[31m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_DIM = "\033[2m"
_BOLD = "\033[1m"

_COLOR_CODE = {"red": _RED, "green": _GREEN, "yellow": _YELLOW}


# Result-line style (shared across status/watch/conflicts) =====


class _ResultStyle(IntEnum):
    GREEN = 1
    RED = 2
    YELLOW = 3


# Result-style → color-name mapping consumed by status.format_summary and
# watch.loop / status._conflict_summary. Single source of truth.
RESULT_STYLE_COLOR: dict[_ResultStyle, str] = {
    _ResultStyle.GREEN: "green",
    _ResultStyle.RED: "red",
    _ResultStyle.YELLOW: "yellow",
}


# Group-label style (shared across status summaries and watch event lines) =====


class _GroupStyle(IntEnum):
    PASSED = 1
    FAILED = 2
    IN_FLIGHT = 3
    STALLED = 4
    UNKNOWN = 5
    NEUTRAL = 6


# _GroupStyle.NEUTRAL is rendered as `dim` (no color), so its entry is None.
GROUP_STYLE_COLOR: dict[_GroupStyle, str | None] = {
    _GroupStyle.PASSED: "green",
    _GroupStyle.FAILED: "red",
    _GroupStyle.IN_FLIGHT: "yellow",
    _GroupStyle.STALLED: "red",
    _GroupStyle.UNKNOWN: "yellow",
    _GroupStyle.NEUTRAL: None,
}


# Decision tree =====


def decide_color(
    color_arg: str | None,
    *,
    stderr: TextIO,
    env: Mapping[str, str] | None = None,
) -> bool:
    """Implement the gh-ci color decision tree.

    Order:
      1. --color=always           -> True
      2. --color=never            -> False
      3. --color=auto             -> stderr TTY check (env vars bypassed)
      4. NO_COLOR set & non-empty -> False
      5. FORCE_COLOR set & non-empty -> True
      6. stderr is a TTY          -> True
      7. else                     -> False

    Steps 4-5 only fire when `color_arg is None` (flag omitted).
    """
    if color_arg == "always":
        return True
    if color_arg == "never":
        return False
    if color_arg == "auto":
        return _isatty(stderr)
    # Flag omitted: consult env vars first, then TTY.
    e = env if env is not None else os.environ
    if e.get("NO_COLOR"):
        return False
    if e.get("FORCE_COLOR"):
        return True
    return _isatty(stderr)


def _isatty(stream: TextIO) -> bool:
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError, OSError):
        return False


# Palette =====


class Palette:
    """Wraps text in ANSI sequences when enabled; pass-through when disabled.

    A single rendering method `style(...)` composes color + bold + dim with
    exactly one trailing RESET. This avoids the nesting bug that would arise
    with separate `red()` / `bold()` methods, where the inner reset would
    terminate the outer style prematurely.
    """

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    @classmethod
    def disabled(cls) -> "Palette":
        return cls(False)

    def style(
        self,
        text: str,
        *,
        color: str | None = None,
        bold: bool = False,
        dim: bool = False,
    ) -> str:
        # Validate the color name eagerly — regardless of whether the
        # palette is enabled — so typos surface uniformly under all
        # --color modes (otherwise a misspelled color silently passes
        # under --color=never and crashes under --color=always).
        if color is not None and color not in _COLOR_CODE:
            raise KeyError(color)
        if not self.enabled:
            return text
        if not bold and not dim and color is None:
            return text
        codes: list[str] = []
        if bold:
            codes.append(_BOLD)
        if dim:
            codes.append(_DIM)
        if color is not None:
            codes.append(_COLOR_CODE[color])
        return f"{''.join(codes)}{text}{_RESET}"


def ensure_palette(palette: Palette | None) -> Palette:
    return palette if palette is not None else Palette.disabled()
