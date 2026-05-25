from __future__ import annotations

import io

import pytest

from ghci.colors import (
    Palette,
    _ResultStyle,
    decide_color,
    ensure_palette,
)


# Test helpers =====


class _TtyStub(io.StringIO):
    """A StringIO that pretends to be a TTY."""

    def isatty(self) -> bool:  # type: ignore[override]
        return True


def _non_tty() -> io.StringIO:
    # Plain StringIO.isatty() returns False, which is what we want.
    return io.StringIO()


def _tty() -> _TtyStub:
    return _TtyStub()


# Decision tree branches =====


def test_always_returns_true_regardless_of_stream_or_env():
    assert decide_color("always", stderr=_non_tty(), env={}) is True
    assert decide_color("always", stderr=_tty(), env={"NO_COLOR": "1"}) is True


def test_never_returns_false_regardless_of_stream_or_env():
    assert decide_color("never", stderr=_tty(), env={}) is False
    assert decide_color("never", stderr=_tty(), env={"FORCE_COLOR": "1"}) is False


def test_auto_with_tty_enables():
    assert decide_color("auto", stderr=_tty(), env={}) is True


def test_auto_without_tty_disables():
    assert decide_color("auto", stderr=_non_tty(), env={}) is False


def test_auto_bypasses_no_color():
    # auto only checks TTY; NO_COLOR is ignored.
    assert decide_color("auto", stderr=_tty(), env={"NO_COLOR": "1"}) is True


def test_auto_bypasses_force_color():
    # auto only checks TTY; FORCE_COLOR is ignored.
    assert decide_color("auto", stderr=_non_tty(), env={"FORCE_COLOR": "1"}) is False


def test_default_no_color_disables():
    assert decide_color(None, stderr=_tty(), env={"NO_COLOR": "1"}) is False


def test_default_force_color_enables_even_when_non_tty():
    assert decide_color(None, stderr=_non_tty(), env={"FORCE_COLOR": "1"}) is True


def test_default_tty_enables():
    assert decide_color(None, stderr=_tty(), env={}) is True


def test_default_non_tty_disables():
    assert decide_color(None, stderr=_non_tty(), env={}) is False


def test_default_no_color_wins_over_force_color():
    # Step 4 (NO_COLOR) fires before step 5 (FORCE_COLOR).
    assert (
        decide_color(
            None, stderr=_tty(), env={"NO_COLOR": "1", "FORCE_COLOR": "1"}
        )
        is False
    )


def test_no_color_empty_treated_as_unset():
    # "set and non-empty" per the spec.
    assert decide_color(None, stderr=_non_tty(), env={"NO_COLOR": ""}) is False
    # Also: NO_COLOR="" does not block FORCE_COLOR.
    assert (
        decide_color(
            None, stderr=_non_tty(), env={"NO_COLOR": "", "FORCE_COLOR": "1"}
        )
        is True
    )


def test_force_color_empty_treated_as_unset():
    # FORCE_COLOR="" does not enable; falls through to TTY check.
    assert decide_color(None, stderr=_non_tty(), env={"FORCE_COLOR": ""}) is False
    assert decide_color(None, stderr=_tty(), env={"FORCE_COLOR": ""}) is True


def test_force_color_literal_zero_enables():
    # Per the locked spec (and Rich/force-color.org standard): any
    # non-empty value enables, including "0". Diverges from Node/chalk.
    assert decide_color(None, stderr=_non_tty(), env={"FORCE_COLOR": "0"}) is True


def test_isatty_swallows_attribute_error():
    # An object without an isatty method must not crash; treated as non-TTY.
    class NoIsatty:
        def write(self, _: str) -> int:  # pragma: no cover - not used
            return 0

    assert decide_color(None, stderr=NoIsatty(), env={}) is False  # type: ignore[arg-type]


def test_isatty_swallows_value_error():
    class ClosedStream:
        def isatty(self) -> bool:
            raise ValueError("closed")

    assert decide_color(None, stderr=ClosedStream(), env={}) is False  # type: ignore[arg-type]


def test_default_env_arg_reads_os_environ(monkeypatch):
    # When env is omitted, decide_color reads os.environ.
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    assert decide_color(None, stderr=_tty()) is False


# Palette =====


def test_disabled_palette_returns_text_unchanged():
    p = Palette(False)
    assert p.style("hello") == "hello"
    assert p.style("hello", color="red", bold=True, dim=True) == "hello"


def test_disabled_classmethod_is_disabled():
    p = Palette.disabled()
    assert p.enabled is False
    assert p.style("hello", color="green") == "hello"


def test_enabled_palette_wraps_red_bold():
    p = Palette(True)
    assert p.style("x", color="red", bold=True) == "\033[1m\033[31mx\033[0m"


def test_enabled_palette_wraps_green():
    p = Palette(True)
    assert p.style("ok", color="green") == "\033[32mok\033[0m"


def test_enabled_palette_wraps_yellow_dim():
    p = Palette(True)
    assert p.style("warn", color="yellow", dim=True) == "\033[2m\033[33mwarn\033[0m"


def test_enabled_palette_no_flags_returns_text_unchanged():
    p = Palette(True)
    assert p.style("plain") == "plain"


def test_enabled_palette_empty_text_still_wraps():
    # Documented harmless edge case: empty payload still produces a valid
    # (no-op when rendered) escape pair.
    p = Palette(True)
    assert p.style("", color="red") == "\033[31m\033[0m"


def test_enabled_palette_single_reset_per_style():
    # The whole point of the single `style()` API: exactly one trailing
    # RESET regardless of how many codes are combined.
    p = Palette(True)
    out = p.style("z", color="red", bold=True, dim=True)
    assert out.count("\033[0m") == 1


def test_enabled_palette_rejects_unknown_color():
    p = Palette(True)
    with pytest.raises(KeyError):
        p.style("x", color="purple")


def test_ensure_palette_passthrough():
    p = Palette(True)
    assert ensure_palette(p) is p


def test_ensure_palette_none_returns_disabled():
    out = ensure_palette(None)
    assert isinstance(out, Palette)
    assert out.enabled is False


# _ResultStyle =====


def test_result_style_values_distinct():
    assert {_ResultStyle.GREEN, _ResultStyle.RED, _ResultStyle.YELLOW} == {
        _ResultStyle.GREEN,
        _ResultStyle.RED,
        _ResultStyle.YELLOW,
    }
    assert int(_ResultStyle.GREEN) != int(_ResultStyle.RED)
    assert int(_ResultStyle.GREEN) != int(_ResultStyle.YELLOW)
    assert int(_ResultStyle.RED) != int(_ResultStyle.YELLOW)
