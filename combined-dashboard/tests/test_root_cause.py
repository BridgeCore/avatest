"""
Unit tests for src/root_cause.py.

Small DataFrames built inline — no real Excel files required.
"""

from datetime import date
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from src.root_cause import (
    DominantContract,
    PersonHours,
    RootCause,
    build_root_causes,
    find_dominant_contract,
    top_nonbillable_people,
)
from src.loader import LoadedData, Period, EmployeeProfile

# ── Fixtures ──────────────────────────────────────────────────────────────────

CFG = {
    "columns": {
        "export": {
            "person":           "Person",
            "person_div":       "PersonDivision",
            "person_org":       "PersonOrganization",
            "date":             "Date",
            "hours":            "Hours",
            "project_subgroup": "ProjectSubGroup",
            "project_code":     "ProjectCode",
            "project_title":    "ProjectTitle",
            "paycode":          "PayCode",
        }
    },
    "cross_flags": {
        "util_division_aliases": {"BL": "BL1", "IS": "IS1"},
    },
}


def _periods(n=3):
    return [
        Period(index=i + 1, start=date(2026, i + 1, 1), end=date(2026, i + 1, 14), net_hours=80.0)
        for i in range(n)
    ]


def _export_df(rows):
    return pd.DataFrame(rows, columns=[
        "Person", "PersonDivision", "ProjectSubGroup",
        "ProjectCode", "ProjectTitle", "Hours", "period_index",
    ])


# ── find_dominant_contract ────────────────────────────────────────────────────

def test_find_dominant_contract_returns_top_code():
    df = _export_df([
        ("Alice", "BL", "ProjectBillable",    "CODE-A", "Alpha", 100.0, 1),
        ("Bob",   "BL", "ProjectNonBillable", "CODE-A", "Alpha",  40.0, 1),
        ("Alice", "BL", "ProjectBillable",    "CODE-B", "Beta",   20.0, 1),
    ])
    periods = _periods(1)
    result = find_dominant_contract(df, "BL", CFG, periods)

    assert result is not None
    assert result.project_code == "CODE-A"
    assert result.share_of_hours == pytest.approx(140 / 160, abs=1e-4)


def test_find_dominant_contract_builds_period_trend():
    df = _export_df([
        ("Alice", "BL", "ProjectBillable",    "CODE-A", "Alpha", 80.0, 1),
        ("Alice", "BL", "ProjectNonBillable", "CODE-A", "Alpha", 20.0, 1),
        ("Alice", "BL", "ProjectBillable",    "CODE-A", "Alpha", 60.0, 2),
        ("Alice", "BL", "ProjectNonBillable", "CODE-A", "Alpha", 40.0, 2),
    ])
    periods = _periods(2)
    result = find_dominant_contract(df, "BL", CFG, periods)

    assert result is not None
    assert len(result.period_trend) == 2
    assert result.period_trend[0].nonbillable_pct == pytest.approx(20 / 100, abs=1e-4)
    assert result.period_trend[1].nonbillable_pct == pytest.approx(40 / 100, abs=1e-4)


def test_find_dominant_contract_none_when_no_rows():
    df = _export_df([
        ("Alice", "MS1", "ProjectBillable", "CODE-A", "Alpha", 100.0, 1),
    ])
    result = find_dominant_contract(df, "BL", CFG, _periods(1))
    assert result is None


def test_find_dominant_contract_none_when_missing_code_column():
    df = pd.DataFrame({
        "Person": ["Alice"],
        "PersonDivision": ["BL"],
        "ProjectSubGroup": ["ProjectBillable"],
        "Hours": [100.0],
        "period_index": [1],
    })
    result = find_dominant_contract(df, "BL", CFG, _periods(1))
    assert result is None


# ── top_nonbillable_people ────────────────────────────────────────────────────

def test_top_nonbillable_people_orders_descending():
    df = _export_df([
        ("Alice", "BL", "ProjectNonBillable", "CODE-A", "Alpha",  80.0, 1),
        ("Bob",   "BL", "ProjectNonBillable", "CODE-A", "Alpha", 120.0, 1),
        ("Carol", "BL", "ProjectNonBillable", "CODE-A", "Alpha",  40.0, 1),
    ])
    result = top_nonbillable_people(df, "BL", "CODE-A", 1, CFG)
    assert [p.person for p in result] == ["Bob", "Alice", "Carol"]
    assert result[0].nonbillable_hours == 120.0


def test_top_nonbillable_people_cap_respected():
    rows = [(f"Emp{i}", "BL", "ProjectNonBillable", "CODE-A", "A", float(100 - i * 5), 1)
            for i in range(10)]
    df = _export_df(rows)
    result = top_nonbillable_people(df, "BL", "CODE-A", 1, CFG, cap=3)
    assert len(result) == 3


def test_top_nonbillable_people_empty_on_no_match():
    df = _export_df([
        ("Alice", "BL", "ProjectBillable", "CODE-A", "Alpha", 100.0, 1),
    ])
    result = top_nonbillable_people(df, "BL", "CODE-A", 1, CFG)
    assert result == []


# ── build_root_causes ─────────────────────────────────────────────────────────

def _make_flag(division, severity="critical"):
    flag = MagicMock()
    flag.division = division
    flag.severity = severity
    return flag


def _make_loaded_data(df, periods):
    data = MagicMock(spec=LoadedData)
    data.export_df = df
    data.periods = periods
    return data


def _rc_patch(data):
    """
    Patch assign_periods to return data.export_df as-is (it already has
    period_index set by the test helper).  This isolates build_root_causes
    logic from the date-bucketing logic tested separately in test_periodizer.
    """
    return patch("src.periodizer.assign_periods", side_effect=lambda d, c: d.export_df)


def test_build_root_causes_skips_none_severity():
    df = _export_df([
        ("Alice", "MS1", "ProjectBillable", "CODE-A", "Alpha", 100.0, 1),
    ])
    periods = _periods(1)
    data = _make_loaded_data(df, periods)
    flags = [_make_flag("MS1", severity="none")]

    with _rc_patch(data):
        results = build_root_causes(data, flags, CFG)
    assert results == []


def test_build_root_causes_skips_gm_only_division():
    """A CrossFlag for BL1 where export has no BL rows → skip, don't crash."""
    df = _export_df([
        ("Alice", "MS1", "ProjectBillable", "CODE-A", "Alpha", 100.0, 1),
    ])
    periods = _periods(1)
    data = _make_loaded_data(df, periods)
    flags = [_make_flag("BL1", severity="critical")]  # BL1 → util code BL, no BL rows

    with _rc_patch(data):
        results = build_root_causes(data, flags, CFG)
    assert results == []


def test_build_root_causes_reverse_alias_bl1_to_bl():
    """BL1 (GM canonical) must look up 'BL' rows in the export DataFrame."""
    df = _export_df([
        ("Alice", "BL",  "ProjectBillable",    "CODE-A", "Alpha", 60.0, 1),
        ("Alice", "BL",  "ProjectNonBillable", "CODE-A", "Alpha", 40.0, 1),
        ("Bob",   "MS1", "ProjectBillable",    "CODE-B", "Beta",  80.0, 1),
    ])
    periods = _periods(1)
    data = _make_loaded_data(df, periods)
    flags = [_make_flag("BL1", severity="critical")]

    with _rc_patch(data):
        results = build_root_causes(data, flags, CFG)
    assert len(results) == 1
    assert results[0].util_division_code == "BL"
    assert results[0].dominant_contract is not None
    assert results[0].dominant_contract.project_code == "CODE-A"


def test_build_root_causes_unmapped_code_passes_through():
    """MS2 has no alias → util_division_code == 'MS2'."""
    df = _export_df([
        ("Alice", "MS2", "ProjectBillable", "CODE-A", "Alpha", 100.0, 1),
    ])
    periods = _periods(1)
    data = _make_loaded_data(df, periods)
    flags = [_make_flag("MS2", severity="warning")]

    with _rc_patch(data):
        results = build_root_causes(data, flags, CFG)
    assert len(results) == 1
    assert results[0].util_division_code == "MS2"


def test_build_root_causes_corp_excluded_from_peer_average():
    """
    Without Corp exclusion, Corp's 0% billable rate would drag the peer average
    down and mask the BL anomaly.  With exclusion, the peer average is computed
    over delivery divisions only, so BL's low rate stands out.

    We assert that peer_avg_billable_pct > 0 (i.e. Corp was excluded) and that
    BL's current_billable_pct < peer_avg_billable_pct (i.e. anomaly detected).
    """
    df = _export_df([
        # BL: 50% billable
        ("A", "BL",   "ProjectBillable",    "CODE-A", "Alpha",  50.0, 1),
        ("A", "BL",   "ProjectNonBillable", "CODE-A", "Alpha",  50.0, 1),
        # MS1: 80% billable
        ("B", "MS1",  "ProjectBillable",    "CODE-B", "Beta",   80.0, 1),
        ("B", "MS1",  "ProjectNonBillable", "CODE-B", "Beta",   20.0, 1),
        # Corp: 0% billable — should be excluded from peer avg
        ("C", "Corp", "ProjectNonBillable", "CODE-C", "Gamma", 100.0, 1),
    ])
    periods = _periods(1)
    data = _make_loaded_data(df, periods)
    flags = [_make_flag("BL1", severity="critical")]

    with _rc_patch(data):
        results = build_root_causes(data, flags, CFG)
    assert len(results) == 1
    rc = results[0]

    # If Corp were included, peer_avg = (50% + 80% + 0%) / 3 ≈ 43%
    # BL at 50% would appear *above* average — anomaly masked.
    # With Corp excluded, peer_avg = (50% + 80%) / 2 = 65%
    # BL at 50% is clearly below.
    assert rc.peer_avg_billable_pct is not None
    assert rc.peer_avg_billable_pct > rc.current_billable_pct
