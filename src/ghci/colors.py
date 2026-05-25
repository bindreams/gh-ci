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


_RESULT_COLOR: dict[_ResultStyle, str] = {
    _ResultStyle.GREEN: "green",
    _ResultStyle.RED: "red",
    _ResultStyle.YELLOW: "yellow",
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
    except (AttributeError, ValueError):
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
        if not self.enabled:
            return text
        codes: list[str] = []
        if bold:
            codes.append(_BOLD)
        if dim:
            codes.append(_DIM)
        if color is not None:
            codes.append(_COLOR_CODE[color])
        if not codes:
            return text
        return f"{''.join(codes)}{text}{_RESET}"


def ensure_palette(palette: Palette | None) -> Palette:
    return palette if palette is not None else Palette.disabled()
