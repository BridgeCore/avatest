from datetime import date

from src.calculator import PeriodStats
from src.loader import Period
from src.trend_detector import Severity, TrendType, detect_trends

CFG = {
    "billable_utilization_warning": 0.65,
    "billable_utilization_critical": 0.50,
    "direct_utilization_warning": 0.65,
    "direct_utilization_critical": 0.50,
    "pto_warning_threshold_hours": 16,
    "pto_critical_threshold_hours": 24,
    "pto_high_threshold_hours": 16,
    "lwop_high_threshold_hours": 16,
    "bp_ratio_threshold": 0.20,
    "nonbillable_threshold": 0.20,
    "ga_crowding_threshold": 0.40,
    "nonbillable_crowding_threshold": 0.20,
    "persistence_threshold": 3,
    "pto_min_periods": 3,
    "dominant_driver_min_share": 0.20,
}


def _period(i: int) -> Period:
    return Period(index=i, start=date(2026, 1, 1), end=date(2026, 1, 15), net_hours=80.0)


def _ps(i: int, **kwargs) -> PeriodStats:
    defaults = dict(period_index=i, period=_period(i), billable_hours=0.0, nonbillable_hours=0.0,
                     bp_hours=0.0, ga_hours=0.0, ird_hours=0.0, overhead_hours=0.0,
                     pto_hours=0.0, holiday_hours=0.0, lwop_hours=0.0, other_hours=0.0,
                     total_worked=0.0, net_available=80.0)
    defaults.update(kwargs)
    return PeriodStats(**defaults)


def test_low_billable_utilization_flagged_only_after_persistence_threshold():
    # 3 consecutive periods at 40% billable utilization (below 0.50 critical)
    low = [_ps(i, billable_hours=32.0) for i in range(1, 4)]  # 32/80 = 0.40
    flags = detect_trends("Alice", low, CFG)

    crit = [f for f in flags if f.trend_type == TrendType.LOW_BILLABLE and f.severity == Severity.CRITICAL]
    assert len(crit) == 1
    assert crit[0].period_indices == [1, 2, 3]


def test_low_billable_utilization_not_flagged_below_persistence_threshold():
    # Only 2 consecutive low periods — below the persistence_threshold of 3
    low = [_ps(i, billable_hours=32.0) for i in range(1, 3)]
    flags = detect_trends("Alice", low, CFG)
    assert not any(f.trend_type == TrendType.LOW_BILLABLE for f in flags)


def test_low_utilization_explained_by_high_pto_is_downgraded_to_warning():
    # Critical-range utilization, but with elevated PTO explaining it
    periods = [_ps(i, billable_hours=32.0, pto_hours=20.0) for i in range(1, 4)]
    flags = detect_trends("Bob", periods, CFG)

    low_flags = [f for f in flags if f.trend_type == TrendType.LOW_BILLABLE]
    assert len(low_flags) == 1
    assert low_flags[0].severity == Severity.WARNING  # downgraded despite critical-range utilization
    assert "PTO" in low_flags[0].explanation


def test_excessive_pto_requires_minimum_active_periods():
    # Only 2 active periods — below pto_min_periods (3), so PTO flag is suppressed
    # even though PTO hours exceed the critical threshold every period.
    periods = [_ps(i, pto_hours=30.0) for i in range(1, 3)]
    flags = detect_trends("Carla", periods, CFG)
    assert not any(f.trend_type == TrendType.EXCESSIVE_PTO for f in flags)


def test_excessive_pto_flagged_when_persistent_and_enough_periods():
    periods = [_ps(i, pto_hours=30.0) for i in range(1, 4)]
    flags = detect_trends("Dana", periods, CFG)
    pto_flags = [f for f in flags if f.trend_type == TrendType.EXCESSIVE_PTO]
    assert len(pto_flags) == 1
    assert pto_flags[0].severity == Severity.CRITICAL


def test_high_bp_ratio_flagged_as_warning():
    periods = [_ps(i, bp_hours=20.0) for i in range(1, 4)]  # 20/80 = 25% > 20% threshold
    flags = detect_trends("Ed", periods, CFG)
    bp_flags = [f for f in flags if f.trend_type == TrendType.HIGH_BP]
    assert len(bp_flags) == 1
    assert bp_flags[0].severity == Severity.WARNING


def test_ga_crowding_requires_both_high_ga_and_low_billable():
    # High G&A ratio but billable utilization is fine — should NOT trigger crowding
    ok_periods = [_ps(i, ga_hours=40.0, billable_hours=60.0) for i in range(1, 4)]
    flags = detect_trends("Fay", ok_periods, CFG)
    assert not any(f.trend_type == TrendType.GA_CROWDING for f in flags)

    # High G&A ratio AND low billable utilization together — should trigger
    crowded = [_ps(i, ga_hours=40.0, billable_hours=20.0) for i in range(1, 4)]
    flags2 = detect_trends("Gail", crowded, CFG)
    assert any(f.trend_type == TrendType.GA_CROWDING for f in flags2)


def test_downgraded_critical_run_is_not_duplicated_by_the_warning_pass():
    # Regression test: a run that crosses the CRITICAL threshold and gets
    # downgraded to WARNING (via a PTO/LWOP explanation) must not also be
    # independently re-flagged when the same run is found again by the
    # looser WARNING-threshold pass. Chosen values: net_available=80,
    # pto=6/period (18 total > pto_high=16, triggers the downgrade),
    # billable=35 -> effective_available=74 -> utilization=35/74=0.473,
    # which is below both critical (0.50) and warning (0.65) thresholds.
    periods = [_ps(i, billable_hours=35.0, pto_hours=6.0) for i in range(1, 4)]
    flags = detect_trends("Ivy", periods, CFG)

    low_flags = [f for f in flags if f.trend_type == TrendType.LOW_BILLABLE]
    assert len(low_flags) == 1
    assert low_flags[0].severity == Severity.WARNING
    assert low_flags[0].period_indices == [1, 2, 3]


def test_all_zero_periods_explained_as_no_timesheet_data_not_downgraded():
    # Every bucket is 0 despite 80 scheduled hrs/period — a missing-timesheet
    # signal, not a real utilization problem. Severity must NOT be downgraded
    # (unlike PTO/LWOP), since this needs follow-up rather than sympathy.
    periods = [_ps(i) for i in range(1, 4)]  # all defaults are 0
    flags = detect_trends("Jill", periods, CFG)

    low_flags = [f for f in flags if f.trend_type == TrendType.LOW_BILLABLE]
    assert len(low_flags) == 1
    assert low_flags[0].severity == Severity.CRITICAL
    assert "No timesheet data" in low_flags[0].explanation


def test_low_utilization_explained_by_dominant_bp_driver():
    # B&P consumes the majority of available hours across the run — B&P was
    # previously invisible to the low-utilization explainer entirely.
    periods = [
        _ps(1, billable_hours=16.0, bp_hours=61.0),
        _ps(2, billable_hours=0.0, bp_hours=25.0),
        _ps(3, billable_hours=0.0, bp_hours=48.0),
    ]
    flags = detect_trends("Kai", periods, CFG)

    low_flags = [f for f in flags if f.trend_type == TrendType.LOW_BILLABLE]
    assert len(low_flags) == 1
    assert low_flags[0].severity == Severity.CRITICAL  # no downgrade for B&P
    assert "B&P" in low_flags[0].explanation


def test_low_direct_utilization_never_blamed_on_nonbillable_hours():
    # Regression test: nonbillable hours are part of the DIRECT utilization
    # numerator, so they can never be a valid cause of low direct utilization
    # even when they're the single largest bucket. G&A (30 total) is below
    # the dominant-driver share threshold on its own here, so with
    # non-billable correctly excluded from consideration, this should fall
    # through to "Unexplained" rather than nonsensically blaming non-billable.
    periods = [_ps(i, nonbillable_hours=30.0, ga_hours=10.0) for i in range(1, 4)]
    flags = detect_trends("Lena", periods, CFG)

    low_flags = [f for f in flags if f.trend_type == TrendType.LOW_DIRECT]
    assert len(low_flags) == 1
    assert "non-billable" not in low_flags[0].explanation
    assert "Unexplained" in low_flags[0].explanation


def test_partial_no_data_periods_noted_when_no_dominant_cause_remains():
    # 2 of 3 flagged periods have zero everywhere; the 3rd has only scattered,
    # non-dominant hours — the explanation should call out the missing data
    # rather than silently falling back to a generic "Unexplained".
    periods = [
        _ps(1),
        _ps(2),
        _ps(3, billable_hours=5.0, bp_hours=5.0, ga_hours=5.0),
    ]
    flags = detect_trends("Mo", periods, CFG)

    low_flags = [f for f in flags if f.trend_type == TrendType.LOW_BILLABLE]
    assert len(low_flags) == 1
    assert "no timesheet data for 2 of 3 period(s)" in low_flags[0].explanation


def test_inactive_periods_are_dropped_not_treated_as_a_gap_in_the_run():
    # Persistence runs operate on the *filtered* active-period list, not on
    # raw period indices — an inactive period (net_available=0) is removed
    # entirely rather than breaking the run, so periods 1, 3, 4 are treated
    # as three CONSECUTIVE low periods even though period 2 sits between them.
    # This documents real (if surprising) behavior of _active_periods() +
    # _find_consecutive_runs() in src/trend_detector.py.
    periods = [
        _ps(1, billable_hours=32.0),
        _ps(2, billable_hours=32.0, net_available=0.0),  # inactive — dropped, not a gap
        _ps(3, billable_hours=32.0),
        _ps(4, billable_hours=32.0),
    ]
    flags = detect_trends("Hank", periods, CFG)
    low_flags = [f for f in flags if f.trend_type == TrendType.LOW_BILLABLE]
    assert len(low_flags) == 1
    assert low_flags[0].period_indices == [1, 3, 4]
