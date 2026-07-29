from src.calculator import EmployeeStats, PeriodStats


def _ps(**kwargs) -> PeriodStats:
    defaults = dict(period_index=1, period=None, billable_hours=0.0, nonbillable_hours=0.0,
                     bp_hours=0.0, ga_hours=0.0, ird_hours=0.0, overhead_hours=0.0,
                     pto_hours=0.0, holiday_hours=0.0, lwop_hours=0.0, other_hours=0.0,
                     total_worked=0.0, net_available=0.0)
    defaults.update(kwargs)
    return PeriodStats(**defaults)


def test_billable_utilization_uses_effective_available_denominator():
    ps = _ps(billable_hours=60.0, pto_hours=16.0, net_available=80.0)
    # effective_available = 80 - 16 = 64; billable_utilization = 60/64
    assert ps.effective_available == 64.0
    assert round(ps.billable_utilization, 4) == round(60.0 / 64.0, 4)


def test_direct_utilization_includes_nonbillable_hours():
    ps = _ps(billable_hours=40.0, nonbillable_hours=10.0, net_available=80.0)
    assert round(ps.direct_utilization, 4) == round(50.0 / 80.0, 4)


def test_utilization_is_zero_when_net_available_is_zero_no_division_error():
    ps = _ps(billable_hours=10.0, net_available=0.0)
    assert ps.billable_utilization == 0.0
    assert ps.direct_utilization == 0.0
    assert ps.ga_ratio == 0.0


def test_effective_available_never_goes_negative_when_time_off_exceeds_net_available():
    ps = _ps(pto_hours=100.0, net_available=80.0)
    assert ps.effective_available == 0.0
    # denominator falls back to net_available when effective_available is 0
    ps2 = _ps(billable_hours=20.0, pto_hours=100.0, net_available=80.0)
    assert round(ps2.billable_utilization, 4) == round(20.0 / 80.0, 4)


def test_employee_stats_averages_only_over_active_periods():
    active_full = _ps(billable_hours=60.0, net_available=80.0)     # util = 0.75
    inactive = _ps(billable_hours=0.0, net_available=0.0)           # should be excluded from averages
    stats = EmployeeStats(person="Alice", period_stats=[active_full, inactive])

    assert stats.avg_billable_utilization == 0.75
    assert stats.periods_active == 1


def test_employee_stats_totals_sum_across_all_periods():
    p1 = _ps(billable_hours=40.0, pto_hours=8.0, net_available=80.0)
    p2 = _ps(billable_hours=50.0, pto_hours=4.0, net_available=80.0)
    stats = EmployeeStats(person="Bob", period_stats=[p1, p2])

    assert stats.total_billable == 90.0
    assert stats.total_pto == 12.0
    assert stats.total_available == 160.0
