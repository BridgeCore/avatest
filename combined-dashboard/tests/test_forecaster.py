from pathlib import Path

import pytest
import yaml

from src.forecaster import project_gm_series, summarize_utilization_trends
from src.gm_loader import AopEntry, GMData, GMActual, MonthKey

ROOT = Path(__file__).parent.parent


def _cfg():
    with open(ROOT / "config.yaml", "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _month(idx, label):
    return MonthKey(idx=idx, year=2026, label=label, key=2026 * 12 + idx)


def _linear_gm_data(n_months: int) -> GMData:
    # MS1 revenue grows by exactly 10,000/month starting at 100,000, GM held
    # at a flat 30% — an exact linear series so the least-squares fit has a
    # known, hand-verifiable answer (no fitting noise to reason about).
    months = [_month(i, f"Month {i}") for i in range(n_months)]
    actuals = [
        GMActual(month=m, project_name="Orion", division="MS1",
                  revenue=100_000 + 10_000 * i, gross_margin=(100_000 + 10_000 * i) * 0.3,
                  gm_pct=30.0, total_direct_cost=0.0)
        for i, m in enumerate(months)
    ]
    aop = {"MS1": AopEntry(division="MS1", revenue=[150_000] * 12, gp=[45_000] * 12)}
    return GMData(months=months, projects={}, actuals=actuals, aop=aop,
                  compare=[], has_aop=True, sheet_log=[])


def test_project_gm_series_extrapolates_linear_trend_to_yearend():
    data = _linear_gm_data(3)  # Jan/Feb/Mar = 100k/110k/120k
    forecasts = project_gm_series(data, _cfg())

    ms1 = forecasts["MS1"]
    assert ms1.trend_direction == "up"
    assert ms1.projected_yearend_revenue == pytest.approx(1_860_000, rel=1e-6)
    assert ms1.projected_yearend_gm == pytest.approx(558_000, rel=1e-6)
    # Annual AOP for MS1 = 150,000 * 12 = 1,800,000
    assert ms1.variance_vs_aop_annual == pytest.approx(1_860_000 - 1_800_000, rel=1e-6)


def test_project_gm_series_skips_forecast_below_minimum_months():
    data = _linear_gm_data(2)  # only 2 months — below MIN_MONTHS_TO_FORECAST
    forecasts = project_gm_series(data, _cfg())

    ms1 = forecasts["MS1"]
    assert len(ms1.historical_monthly) == 2
    assert ms1.projected_yearend_revenue is None
    assert ms1.projected_yearend_gm is None
    assert ms1.variance_vs_aop_annual is None


def test_project_gm_series_all_division_combines_all_actuals():
    data = _linear_gm_data(3)
    forecasts = project_gm_series(data, _cfg())

    all_fc = forecasts["ALL"]
    # Only MS1 has data in this fixture, so ALL should match MS1 exactly.
    assert all_fc.projected_yearend_revenue == pytest.approx(1_860_000, rel=1e-6)


def test_project_gm_series_covers_every_configured_division_even_with_no_data():
    data = _linear_gm_data(3)
    forecasts = project_gm_series(data, _cfg())

    for div in _cfg()["gm"]["divisions"]:
        assert div in forecasts
    # A division with zero actuals has an all-zero, flat series and no crash.
    empty_div = next(d for d in _cfg()["gm"]["divisions"] if d != "MS1")
    assert forecasts[empty_div].historical_monthly == [("Month 0", 0, None), ("Month 1", 0, None), ("Month 2", 0, None)]


def _view(view_id, label, critical=None, warning=None, division_rows=None, portfolio_rows=None):
    return {
        "view_id": view_id,
        "view_label": label,
        "view_period_range": "2026-01-01 to 2026-03-31",
        "critical_flags": critical or [],
        "warning_flags": warning or [],
        "division_rows": division_rows or [],
        "portfolio_rows": portfolio_rows or [],
    }


def _flag(person, division, trend_type, explanation):
    return {"person": person, "division": division, "trend_type": trend_type, "explanation": explanation}


def test_summarize_utilization_trends_uses_ytd_and_last_quarter():
    views = [
        _view("last_period", "Last Period"),
        _view("last_month", "Last Month"),
        _view("last_quarter", "Last Quarter", critical=[_flag("Alice", "MS1", "low_billable", "explained")]),
        _view("ytd", "Year to Date", critical=[_flag("Bob", "MS2", "excessive_pto", "explained too")]),
    ]
    summary = summarize_utilization_trends(views, _cfg())

    assert summary["year_to_date"]["critical_count"] == 1
    assert summary["year_to_date"]["top_flags"][0]["person"] == "Bob"
    assert summary["last_quarter"]["top_flags"][0]["person"] == "Alice"


def test_summarize_utilization_trends_caps_at_max_flags_per_view():
    many_crit = [_flag(f"Person{i}", "MS1", "low_billable", "x") for i in range(20)]
    views = [_view("ytd", "Year to Date", critical=many_crit)]
    cfg = _cfg()
    cfg.setdefault("ai_commentary", {})["max_flags_per_view"] = 3

    summary = summarize_utilization_trends(views, cfg)
    assert len(summary["year_to_date"]["top_flags"]) == 3


def test_summarize_utilization_trends_empty_views_returns_empty_dict():
    assert summarize_utilization_trends([], _cfg()) == {}
