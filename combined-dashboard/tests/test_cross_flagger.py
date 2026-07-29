from src.cross_flagger import evaluate_all, evaluate_division

CFG = {"cross_flags": {"utilization_target": 0.85, "revenue_variance_target": 0.0}}


def test_critical_when_low_utilization_and_low_ytd_revenue():
    flag = evaluate_division("MS1", 0.70, -0.05, -0.06, CFG)
    assert flag.low_utilization is True
    assert flag.low_revenue_ytd is True
    assert flag.severity == "critical"


def test_warning_when_low_utilization_and_low_mtd_only():
    flag = evaluate_division("MS1", 0.70, -0.05, 0.02, CFG)
    assert flag.low_utilization is True
    assert flag.low_revenue_mtd is True
    assert flag.low_revenue_ytd is False
    assert flag.severity == "warning"
    assert "MTD" in flag.reason


def test_warning_when_single_metric_low_utilization_only():
    flag = evaluate_division("MS1", 0.70, 0.02, 0.02, CFG)
    assert flag.low_utilization is True
    assert flag.severity == "warning"


def test_warning_when_single_metric_low_ytd_revenue_only():
    flag = evaluate_division("MS1", 0.90, 0.02, -0.06, CFG)
    assert flag.low_utilization is False
    assert flag.low_revenue_ytd is True
    assert flag.severity == "warning"


def test_none_severity_when_healthy():
    flag = evaluate_division("MS1", 0.90, 0.02, 0.02, CFG)
    assert flag.severity == "none"
    assert flag.reason == "Healthy"


def test_no_utilization_data_is_none_not_false():
    flag = evaluate_division("Fuel", None, 0.02, -0.06, CFG)
    assert flag.low_utilization is None
    assert flag.severity == "warning"  # single-metric revenue-side flag still fires
    assert flag.reason.count("low utilization") == 0


def test_no_utilization_data_and_healthy_revenue_reports_no_data():
    flag = evaluate_division("Fuel", None, 0.02, 0.02, CFG)
    assert flag.severity == "none"
    assert flag.reason == "No utilization data"


def test_corp_like_division_has_no_revenue_side():
    flag = evaluate_division("Corp", 0.70, None, None, CFG)
    assert flag.low_revenue_mtd is None
    assert flag.low_revenue_ytd is None
    assert flag.low_utilization is True
    assert flag.severity == "warning"  # single-metric: utilization only


def test_evaluate_all_covers_union_of_divisions_from_both_sources():
    util_rollups = {"MS1": {"avg_billable_util": 0.70}, "Corp": {"avg_billable_util": 0.90}}
    gm_variance = {"MS1": {"mtd_variance_pct": -0.05, "ytd_variance_pct": -0.06}, "BL1": {"mtd_variance_pct": -0.10, "ytd_variance_pct": -0.10}}

    flags = evaluate_all(util_rollups, gm_variance, CFG)
    by_division = {f.division: f for f in flags}

    assert set(by_division) == {"MS1", "Corp", "BL1"}
    assert by_division["MS1"].severity == "critical"
    assert by_division["Corp"].mtd_variance_pct is None
    assert by_division["Corp"].severity == "none"
    assert by_division["BL1"].avg_billable_util is None
    assert by_division["BL1"].severity == "warning"


def test_defaults_used_when_cfg_missing_cross_flags_section():
    flag = evaluate_division("MS1", 0.80, -0.01, -0.01, cfg={})
    assert flag.low_utilization is True  # 0.80 < default 0.85
    assert flag.severity == "critical"
