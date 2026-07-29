import pytest
import yaml
from pathlib import Path

from src.common import (
    ColumnResolver,
    fmt_money,
    fmt_pct,
    fmt_variance,
    parse_number,
    validate_config,
)

ROOT = Path(__file__).parent.parent


def test_parse_number_handles_currency_and_percent_strings():
    assert parse_number("$1,234.50") == 1234.5
    assert parse_number("12%") == 12.0
    assert parse_number("  42  ") == 42.0
    assert parse_number("-$500") == -500.0


def test_parse_number_handles_blank_and_none():
    assert parse_number(None) == 0.0
    assert parse_number("") == 0.0
    assert parse_number("-") == 0.0
    assert parse_number("not a number") == 0.0


def test_parse_number_handles_nan_and_bool_without_crashing():
    assert parse_number(float("nan")) == 0.0
    assert parse_number(True) == 0.0
    assert parse_number(False) == 0.0


def test_parse_number_passes_through_numeric_types():
    assert parse_number(42) == 42.0
    assert parse_number(3.14) == 3.14


def test_fmt_money_compact_scaling():
    assert fmt_money(1_234_000) == "$1.2M"
    assert fmt_money(340_000) == "$340K"
    assert fmt_money(500) == "$500"
    assert fmt_money(-500) == "-$500"
    assert fmt_money(None) == "—"


def test_fmt_pct_and_variance():
    assert fmt_pct(12.345) == "12.3%"
    assert fmt_pct(None) == "—"
    assert fmt_variance(500) == "+$500"
    assert fmt_variance(-500) == "-$500"
    assert fmt_variance(None) == "N/A"


def test_column_resolver_matches_aliases_case_and_whitespace_insensitively():
    alias_map = {"Revenue": ["Revenue", "revenue"], "GrossMargin": ["GrossMargin", "Gross Margin"]}
    # Any amount of whitespace/underscores is stripped entirely before comparison,
    # so "Gross  Margin" (double space) still matches the "Gross Margin" alias.
    resolver = ColumnResolver(["revenue", "Gross  Margin", "Org"], alias_map)
    assert resolver.resolve("Revenue") == "revenue"
    assert resolver.resolve("GrossMargin") == "Gross  Margin"


def test_column_resolver_reports_missing_columns():
    alias_map = {"Revenue": ["Revenue"], "Org": ["Org"]}
    resolver = ColumnResolver(["Revenue"], alias_map)
    assert resolver.missing(["Revenue", "Org"]) == ["Org"]


def test_validate_config_accepts_the_real_config():
    with open(ROOT / "config.yaml", "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    # Should not raise
    validate_config(cfg, need_util=True, need_gm=True)


def test_validate_config_rejects_missing_util_keys():
    with pytest.raises(ValueError, match="persistence_threshold"):
        validate_config({}, need_util=True, need_gm=False)


def test_validate_config_rejects_missing_gm_section():
    with pytest.raises(ValueError, match="gm"):
        validate_config({}, need_util=False, need_gm=True)


def test_validate_config_skips_sections_not_needed():
    # An empty config is fine if neither section is required
    validate_config({}, need_util=False, need_gm=False)
