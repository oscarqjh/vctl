"""Unit tests for vctl.duration._parse_duration."""

from __future__ import annotations

import pytest

from vctl.duration import _parse_duration


def test_parse_seconds() -> None:
    assert _parse_duration("300s") == 300


def test_parse_minutes() -> None:
    assert _parse_duration("5m") == 300


def test_parse_hours() -> None:
    assert _parse_duration("2h") == 7200


def test_parse_days() -> None:
    assert _parse_duration("1d") == 86400


def test_parse_one_second() -> None:
    assert _parse_duration("1s") == 1


def test_parse_empty_raises() -> None:
    with pytest.raises(ValueError, match="invalid duration"):
        _parse_duration("")


def test_parse_unknown_suffix_raises() -> None:
    with pytest.raises(ValueError, match="invalid duration"):
        _parse_duration("5x")


def test_parse_float_suffix_raises() -> None:
    with pytest.raises(ValueError, match="invalid duration"):
        _parse_duration("1.5m")


def test_parse_plain_digits_raises() -> None:
    with pytest.raises(ValueError, match="invalid duration"):
        _parse_duration("abc")
