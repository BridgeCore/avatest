"""
Unit tests for src/workforce.py.

Small dataclass instances built inline — no real Excel files required.
"""

from datetime import date
from unittest.mock import MagicMock

import pytest

from src.workforce import compute_workforce_health, WorkforceHealth, PersonPTO
from src.loader import LoadedData, EmployeeProfile


# ── Fixtures ──────────────────────────────────────────────────────────────────

CFG = {
    "workforce": {
        "high_pto_threshold_hours": 150,
    }
}

AS_OF = 2026


def _profile(name, first_day=None, last_day=None, pto=None) -> EmployeeProfile:
    p = MagicMock(spec=EmployeeProfile)
    p.name = name
    p.first_day = first_day
    p.last_day = last_day
    p.pto_balance_available = pto
    return p


def _data(*profiles) -> LoadedData:
    data = MagicMock(spec=LoadedData)
    data.all_employees = [p.name for p in profiles]          # list[str] for headcount
    data.employee_profiles = {p.name: p for p in profiles}  # dict for PTO / dates
    return data


# ── headcount ─────────────────────────────────────────────────────────────────

def test_headcount_counts_all_employees():
    data = _data(
        _profile("Alice"),
        _profile("Bob"),
        _profile("Carol"),
    )
    wf = compute_workforce_health(data, CFG, AS_OF)
    assert wf.headcount_current == 3


# ── hires / departures ────────────────────────────────────────────────────────

def test_hires_ytd_counts_matching_year():
    data = _data(
        _profile("Alice", first_day=date(2026, 3, 1)),
        _profile("Bob",   first_day=date(2025, 1, 1)),  # prior year
        _profile("Carol", first_day=date(2026, 11, 15)),
    )
    wf = compute_workforce_health(data, CFG, AS_OF)
    assert wf.hires_ytd == 2


def test_departures_ytd_counts_matching_year():
    data = _data(
        _profile("Alice", last_day=date(2026, 6, 30)),
        _profile("Bob",   last_day=date(2025, 12, 31)),  # prior year
        _profile("Carol"),                               # no departure
    )
    wf = compute_workforce_health(data, CFG, AS_OF)
    assert wf.departures_ytd == 1


def test_net_change_ytd_is_hires_minus_departures():
    data = _data(
        _profile("A", first_day=date(2026, 1, 1)),
        _profile("B", first_day=date(2026, 1, 1)),
        _profile("C", last_day=date(2026, 3, 31)),
    )
    wf = compute_workforce_health(data, CFG, AS_OF)
    assert wf.net_change_ytd == 1  # 2 hires - 1 departure


def test_no_hires_or_departures():
    data = _data(_profile("Alice"), _profile("Bob"))
    wf = compute_workforce_health(data, CFG, AS_OF)
    assert wf.hires_ytd == 0
    assert wf.departures_ytd == 0
    assert wf.net_change_ytd == 0


# ── PTO boundary conditions ───────────────────────────────────────────────────

def test_high_pto_exactly_at_threshold_is_included():
    """PTO == 150 is high-PTO (≥ threshold, not just >)."""
    data = _data(_profile("Alice", pto=150.0))
    wf = compute_workforce_health(data, CFG, AS_OF)
    assert len(wf.high_pto_liability) == 1
    assert wf.high_pto_liability[0].person == "Alice"


def test_high_pto_just_below_threshold_is_excluded():
    data = _data(_profile("Alice", pto=149.9))
    wf = compute_workforce_health(data, CFG, AS_OF)
    assert len(wf.high_pto_liability) == 0


def test_negative_pto_exactly_zero_is_not_flagged():
    """PTO == 0 is not negative."""
    data = _data(_profile("Alice", pto=0.0))
    wf = compute_workforce_health(data, CFG, AS_OF)
    assert len(wf.negative_pto_balances) == 0


def test_negative_pto_below_zero_is_flagged():
    data = _data(_profile("Alice", pto=-0.5))
    wf = compute_workforce_health(data, CFG, AS_OF)
    assert len(wf.negative_pto_balances) == 1
    assert wf.negative_pto_balances[0].person == "Alice"


def test_none_pto_is_not_treated_as_zero():
    """None PTO means data is missing — must not appear in either PTO list."""
    data = _data(_profile("Alice", pto=None))
    wf = compute_workforce_health(data, CFG, AS_OF)
    assert len(wf.high_pto_liability) == 0
    assert len(wf.negative_pto_balances) == 0


# ── ordering ──────────────────────────────────────────────────────────────────

def test_high_pto_sorted_descending():
    data = _data(
        _profile("A", pto=200.0),
        _profile("B", pto=350.0),
        _profile("C", pto=150.0),
    )
    wf = compute_workforce_health(data, CFG, AS_OF)
    hours = [p.hours_available for p in wf.high_pto_liability]
    assert hours == sorted(hours, reverse=True)


def test_negative_pto_sorted_ascending_most_negative_first():
    data = _data(
        _profile("A", pto=-10.0),
        _profile("B", pto=-50.0),
        _profile("C", pto=-5.0),
    )
    wf = compute_workforce_health(data, CFG, AS_OF)
    hours = [p.hours_available for p in wf.negative_pto_balances]
    assert hours == sorted(hours)  # ascending = most negative first


# ── cap ───────────────────────────────────────────────────────────────────────

def test_high_pto_capped_at_10():
    profiles = [_profile(f"Emp{i}", pto=float(200 + i)) for i in range(15)]
    data = _data(*profiles)
    wf = compute_workforce_health(data, CFG, AS_OF)
    assert len(wf.high_pto_liability) == 10


def test_negative_pto_capped_at_10():
    profiles = [_profile(f"Emp{i}", pto=float(-10 - i)) for i in range(15)]
    data = _data(*profiles)
    wf = compute_workforce_health(data, CFG, AS_OF)
    assert len(wf.negative_pto_balances) == 10


# ── default threshold when config key missing ─────────────────────────────────

def test_missing_workforce_config_uses_default_threshold():
    """If the workforce config section is absent, default to 150."""
    data = _data(_profile("Alice", pto=150.0))
    wf = compute_workforce_health(data, {}, AS_OF)
    assert len(wf.high_pto_liability) == 1


# ── mixed scenarios ───────────────────────────────────────────────────────────

def test_same_person_cannot_appear_in_both_pto_lists():
    """High PTO (≥150) and negative PTO (<0) are mutually exclusive."""
    data = _data(
        _profile("HighPTO", pto=200.0),
        _profile("NegPTO",  pto=-20.0),
        _profile("Normal",  pto=80.0),
        _profile("Missing", pto=None),
    )
    wf = compute_workforce_health(data, CFG, AS_OF)
    high_names = {p.person for p in wf.high_pto_liability}
    neg_names  = {p.person for p in wf.negative_pto_balances}
    assert high_names & neg_names == set()
    assert "HighPTO" in high_names
    assert "NegPTO"  in neg_names
    assert "Normal"  not in high_names
    assert "Normal"  not in neg_names
    assert "Missing" not in high_names
    assert "Missing" not in neg_names
