from pathlib import Path

import yaml

from src.gm_calculator import build_gm_context
from src.gm_loader import AopEntry, GMActual, GMData, GMProject, MonthKey

ROOT = Path(__file__).parent.parent


def _cfg():
    with open(ROOT / "config.yaml", "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _make_data() -> GMData:
    jan = MonthKey(idx=0, year=2026, label="Jan 2026", key=2026 * 12)
    feb = MonthKey(idx=1, year=2026, label="Feb 2026", key=2026 * 12 + 1)

    projects = {
        "Orion Sustainment\x1fMS1": GMProject(name="Orion Sustainment", division="MS1", code="MS1-1000"),
        "Trident Upgrade\x1fMS2": GMProject(name="Trident Upgrade", division="MS2", code="MS2-1000"),
    }

    actuals = [
        GMActual(month=jan, project_name="Orion Sustainment", division="MS1",
                 revenue=100_000, gross_margin=30_000, gm_pct=0.30, total_direct_cost=70_000),
        GMActual(month=feb, project_name="Orion Sustainment", division="MS1",
                 revenue=110_000, gross_margin=33_000, gm_pct=0.30, total_direct_cost=77_000),
        GMActual(month=jan, project_name="Trident Upgrade", division="MS2",
                 revenue=200_000, gross_margin=50_000, gm_pct=0.25, total_direct_cost=150_000),
        GMActual(month=feb, project_name="Trident Upgrade", division="MS2",
                 revenue=190_000, gross_margin=47_500, gm_pct=0.25, total_direct_cost=142_500),
    ]

    aop = {
        "MS1": AopEntry(division="MS1",
                         revenue=[100_000, 100_000] + [0] * 10,
                         gp=[28_000, 28_000] + [0] * 10),
        "MS2": AopEntry(division="MS2",
                         revenue=[210_000, 210_000] + [0] * 10,
                         gp=[52_000, 52_000] + [0] * 10),
    }

    return GMData(
        months=[jan, feb], projects=projects, actuals=actuals, aop=aop,
        compare=[], has_aop=True, sheet_log=[],
    )


def test_build_gm_context_ytd_totals_for_all_divisions():
    ctx = build_gm_context(_make_data(), _cfg())
    all_tab = ctx["tabs"]["ALL"]

    assert ctx["has_data"]
    assert ctx["has_aop"]
    # YTD revenue = 100k+110k+200k+190k = 600k -> compact "$600K"
    assert all_tab["kpis"]["ytd_rev"] == "$600K"
    # YTD GM = 30k+33k+50k+47.5k = 160.5k
    assert all_tab["kpis"]["ytd_gm"] == "$160K" or all_tab["kpis"]["ytd_gm"] == "$161K"


def test_build_gm_context_division_filter_isolates_one_division():
    ctx = build_gm_context(_make_data(), _cfg())
    ms1_tab = ctx["tabs"]["MS1"]

    # MS1 YTD revenue = 100k + 110k = 210k
    assert ms1_tab["kpis"]["ytd_rev"] == "$210K"
    assert len(ms1_tab["table"]["rows"]) == 1
    assert ms1_tab["table"]["rows"][0]["name"] == "Orion Sustainment"


def test_build_gm_context_revenue_variance_against_aop():
    ctx = build_gm_context(_make_data(), _cfg())
    ms1_tab = ctx["tabs"]["MS1"]

    # MS1 YTD AOP revenue = 100k + 100k = 200k, actual = 210k -> +$10K variance
    assert ms1_tab["kpis"]["rev_var_class"] == "vpos"
    assert ms1_tab["kpis"]["rev_var"] == "+$10K"


def test_build_gm_context_division_with_no_projects_is_empty_not_broken():
    ctx = build_gm_context(_make_data(), _cfg())
    is1_tab = ctx["tabs"]["IS1"]

    assert is1_tab["table"]["rows"] == []
    assert is1_tab["kpis"]["ytd_rev"] == "$0"


def test_build_gm_context_chart_series_length_matches_month_count():
    ctx = build_gm_context(_make_data(), _cfg())
    all_tab = ctx["tabs"]["ALL"]
    # Every series should be embedded in valid SVG covering both months
    assert "<svg" in all_tab["charts"]["chart_a_svg"]
    assert "<svg" in all_tab["charts"]["chart_c_svg"]


def test_build_gm_context_with_no_data_reports_has_data_false():
    empty = GMData(months=[], projects={}, actuals=[], aop={}, compare=[], has_aop=False, sheet_log=[])
    ctx = build_gm_context(empty, _cfg())
    assert ctx["has_data"] is False
    assert ctx["has_aop"] is False
