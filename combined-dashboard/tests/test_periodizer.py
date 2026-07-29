from datetime import date

from src.loader import EmployeeProfile, LoadedData, Period
from src.periodizer import _busday_count, compute_available_hours


def _period(start, end, net_hours=80.0) -> Period:
    return Period(index=1, start=start, end=end, net_hours=net_hours)


def _data_with(profile: EmployeeProfile) -> LoadedData:
    return LoadedData(
        periods=[], report_start=date(2026, 1, 1), report_end=date(2026, 12, 31),
        export_df=None, all_employees=[profile.person],
        employee_profiles={profile.person: profile},
        pt_employees=set(), partial_period_employees=set(),
        excluded_no_pto=set(), flagged_no_timesheet=set(),
        missing_lookup_combos=[], pipeline_summary={}, ul_rows_excluded=0,
        unexpected_subgroups=set(), portfolio_lead_map={}, project_type_map={},
    )


def test_full_time_employee_with_no_partial_dates_gets_full_period_hours():
    period = _period(date(2026, 1, 1), date(2026, 1, 15), net_hours=80.0)
    prof = EmployeeProfile(person="Alice")
    data = _data_with(prof)

    available = compute_available_hours("Alice", period, data, cfg={}, total_worked=40.0)
    assert available == 80.0


def test_pt_employee_available_equals_hours_worked_regardless_of_period_net_hours():
    period = _period(date(2026, 1, 1), date(2026, 1, 15), net_hours=80.0)
    prof = EmployeeProfile(person="Grace", is_pt=True)
    data = _data_with(prof)

    available = compute_available_hours("Grace", period, data, cfg={}, total_worked=22.5)
    assert available == 22.5


def test_partial_period_employee_is_prorated_by_business_days():
    # A 2-week period (10 business days); employee starts on day 6 of 10.
    period = _period(date(2026, 1, 1), date(2026, 1, 14), net_hours=80.0)  # Thu Jan1 - Wed Jan14
    prof = EmployeeProfile(person="Frank", first_day=date(2026, 1, 8))
    data = _data_with(prof)

    available = compute_available_hours("Frank", period, data, cfg={}, total_worked=10.0)
    wd_period = _busday_count(period.start, period.end)
    wd_active = _busday_count(date(2026, 1, 8), period.end)
    expected = (wd_active / wd_period) * period.net_hours
    assert available == expected
    assert 0 < available < 80.0


def test_employee_inactive_for_the_entire_period_gets_zero_hours():
    period = _period(date(2026, 1, 1), date(2026, 1, 15), net_hours=80.0)
    # Employee's last day was well before this period started
    prof = EmployeeProfile(person="Departed", last_day=date(2025, 12, 1))
    data = _data_with(prof)

    available = compute_available_hours("Departed", period, data, cfg={}, total_worked=0.0)
    assert available == 0.0


def test_unknown_employee_falls_back_to_period_net_hours():
    period = _period(date(2026, 1, 1), date(2026, 1, 15), net_hours=80.0)
    data = _data_with(EmployeeProfile(person="Someone Else"))

    available = compute_available_hours("Not In Roster", period, data, cfg={}, total_worked=5.0)
    assert available == 80.0


def test_busday_count_is_inclusive_of_both_endpoints():
    # Mon Jan 5 2026 - Fri Jan 9 2026 = 5 business days
    assert _busday_count(date(2026, 1, 5), date(2026, 1, 9)) == 5


def test_busday_count_returns_zero_when_start_after_end():
    assert _busday_count(date(2026, 1, 9), date(2026, 1, 5)) == 0
