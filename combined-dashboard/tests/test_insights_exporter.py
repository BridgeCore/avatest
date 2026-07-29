import json
from datetime import date

from src.cross_flagger import CrossFlag
from src.gm_loader import MonthKey
from src.insights_exporter import build_insights, write_insights
from src.loader import Period


def _period(idx):
    return Period(index=idx, start=date(2026, 1, 1 + idx), end=date(2026, 1, 14 + idx), net_hours=80.0)


class _FakeLoadedData:
    def __init__(self):
        self.periods = [_period(0), _period(1)]
        self.excluded_no_pto = {"Alice"}
        self.flagged_no_timesheet = {"Bob"}
        self.missing_lookup_combos = []


def test_build_insights_maps_cross_flags_into_division_entries():
    cross_flags = [
        CrossFlag("MS1", 0.70, -0.05, -0.06, True, True, True, "critical", "Low utilization + sustained (YTD) revenue miss"),
        CrossFlag("Corp", 0.90, None, None, False, None, None, "none", "Healthy"),
        CrossFlag("Fuel", None, 0.02, 0.02, None, False, False, "none", "No utilization data"),
    ]
    util_bundle = (_FakeLoadedData(), set(), [])

    insights = build_insights(None, None, util_bundle, [], cross_flags, {}, "2026-07-13T00:00:00")

    by_name = {d["name"]: d for d in insights["divisions"]}
    assert by_name["MS1"]["combined_flag"] is True
    assert by_name["MS1"]["severity"] == "critical"
    assert by_name["Corp"]["mtd_revenue_variance_pct"] is None
    assert by_name["Fuel"]["avg_utilization"] is None
    assert by_name["Fuel"]["low_utilization_flag"] is None
    assert by_name["MS1"]["annual_trend_direction"] is None  # no forecasts passed


def test_build_insights_attaches_annual_trend_direction_from_forecasts():
    class _FakeForecast:
        trend_direction = "down"

    cross_flags = [CrossFlag("MS1", 0.70, -0.05, -0.06, True, True, True, "critical", "reason")]
    forecasts = {"MS1": _FakeForecast()}

    insights = build_insights(None, forecasts, None, [], cross_flags, {}, "2026-07-13T00:00:00")

    assert insights["divisions"][0]["annual_trend_direction"] == "down"


def test_build_insights_carries_periods_covered_and_data_quality_flags():
    util_bundle = (_FakeLoadedData(), set(), [])
    insights = build_insights(None, None, util_bundle, [], [], {}, "2026-07-13T00:00:00")

    assert len(insights["periods_covered"]) == 2
    types = {f["type"] for f in insights["data_quality_flags"]}
    assert types == {"excluded_no_pto", "flagged_no_timesheet"}


def test_build_insights_falls_back_to_gm_months_without_util_bundle():
    class _FakeGMData:
        months = [MonthKey(idx=0, year=2026, label="Jan 2026", key=2026 * 12)]

    insights = build_insights(_FakeGMData(), None, None, None, [], {}, "2026-07-13T00:00:00")
    assert insights["periods_covered"] == ["Jan 2026"]
    assert insights["data_quality_flags"] == []


def test_write_insights_round_trips_through_json(tmp_path):
    insights = {"generated_at": "x", "periods_covered": [], "divisions": [], "data_quality_flags": []}
    out_path = tmp_path / "exports" / "insights_latest.json"

    write_insights(insights, out_path)

    assert json.loads(out_path.read_text(encoding="utf-8")) == insights
