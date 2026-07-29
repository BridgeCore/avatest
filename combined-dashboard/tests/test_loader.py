from datetime import date

import pandas as pd

from src.loader import _parse_date_value


def test_parses_real_dates_from_various_types():
    assert _parse_date_value(date(2026, 1, 15)) == date(2026, 1, 15)
    assert _parse_date_value(pd.Timestamp("2026-01-15")) == date(2026, 1, 15)
    assert _parse_date_value("2026-01-15") == date(2026, 1, 15)


def test_blank_and_placeholder_strings_return_none_not_nat():
    # Regression: pd.to_datetime("") silently returns NaT (does not raise),
    # and NaT.date() returns NaT right back rather than a real date. A blank
    # "First Day"/"Last Day" cell read back as an empty/placeholder string
    # must resolve to None, not a NaT masquerading as a date — otherwise
    # periodizer.py's max(first_day, period.start) blows up with
    # "TypeError: Cannot compare NaT with datetime.date object".
    for placeholder in ("", "   ", "nan", "NaT"):
        result = _parse_date_value(placeholder)
        assert result is None, f"{placeholder!r} should parse to None, got {result!r}"


def test_none_and_native_nat_return_none():
    assert _parse_date_value(None) is None
    assert _parse_date_value(pd.NaT) is None
    assert _parse_date_value(float("nan")) is None


def test_unparsable_string_returns_none_without_raising():
    assert _parse_date_value("  -  ") is None
