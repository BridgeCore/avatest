"""
Reads all 8 sheets from the utilization Excel file, builds period calendar,
employee rosters, and exclusion/classification sets.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

KNOWN_SUBGROUPS = {
    "ProjectBillable", "ProjectNonBillable", "B&P", "G&A",
    "IR&D", "Overhead", "PTO", "Holiday", "LWOP", "Other",
}


@dataclass
class Period:
    index: int          # 1-based
    start: date
    end: date
    net_hours: float    # from Lookups row 3, or estimated from business days
    name: str = ""      # optional label override (e.g. "Jan 2026")

    @property
    def label(self) -> str:
        if self.name:
            return self.name
        return f"P{self.index} ({self.start.strftime('%m/%d')}–{self.end.strftime('%m/%d')})"


@dataclass
class EmployeeProfile:
    person: str
    person_org: str = ""
    division: str = ""
    portfolio_lead: str = ""
    primary_contract_type: str = ""
    is_pt: bool = False
    first_day: date | None = None
    last_day: date | None = None
    bus_week_hours: float | None = None
    hire_date: date | None = None
    pto_balance_available: float | None = None


@dataclass
class LoadedData:
    # Period calendar
    periods: list[Period]
    report_start: date
    report_end: date

    # Export data (filtered)
    export_df: pd.DataFrame

    # Employee collections
    all_employees: list[str]           # from Unique Employees sheet
    employee_profiles: dict[str, EmployeeProfile]
    pt_employees: set[str]
    partial_period_employees: set[str]

    # Exclusion sets (from Discrepancies)
    excluded_no_pto: set[str]          # in Time Details, not in PTO → excluded from analysis
    flagged_no_timesheet: set[str]     # in PTO, not in Time Details → possible missed timesheets

    # Data quality
    missing_lookup_combos: list[dict]
    pipeline_summary: dict[str, Any]
    ul_rows_excluded: int
    unexpected_subgroups: set[str]

    # Portfolio lead mapping
    portfolio_lead_map: dict[str, str]   # person → lead
    project_type_map: dict[str, str]     # person → project type


def _parse_date_value(val) -> date | None:
    """Convert Excel datetime / string / pandas Timestamp to date."""
    from datetime import datetime as _dt
    if val is None:
        return None
    try:
        if pd.isna(val):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(val, pd.Timestamp):
        return val.date()
    if isinstance(val, _dt):          # datetime is subclass of date — check first
        return val.date()
    if isinstance(val, date):
        return val
    if isinstance(val, str):
        if not val.strip():
            return None
        try:
            parsed = pd.to_datetime(val)
        except Exception:
            return None
        return None if pd.isna(parsed) else parsed.date()
    return None


def load(excel_path, cfg: dict) -> LoadedData:
    """Accept a file path (Path) or an in-memory buffer (BytesIO)."""
    snames = cfg["sheets"]

    xl = pd.ExcelFile(excel_path, engine="openpyxl")

    export_df = _load_export(xl, snames["export"], cfg)

    period_cfg = cfg.get("period_detection", {})
    if period_cfg.get("source") == "export":
        grouping = period_cfg.get("grouping", "monthly")
        date_col = cfg["columns"]["export"]["date"]
        periods, report_start, report_end = _derive_periods_from_export(export_df, date_col, grouping)
        _, _, _, pt_employees = _load_lookups_and_billable_ute(
            xl, snames["lookups"], snames["billable_ute"]
        )
    else:
        periods, report_start, report_end, pt_employees = _load_lookups_and_billable_ute(
            xl, snames["lookups"], snames["billable_ute"]
        )
    all_employees, org_map = _load_unique_employees(xl, snames["unique_employees"])
    first_last_map = _load_first_last(xl, snames["first_last"])
    pto_map = _load_pto_balances(xl, snames["pto_balances"])
    portfolio_lead_map, project_type_map, division_map = _load_portfolio(xl, snames["portfolio"], snames["lookups"], cfg)
    (excluded_no_pto, flagged_no_timesheet, missing_combos, pipeline_summary) = _load_discrepancies(
        xl, snames["discrepancies"]
    )

    # Extract PersonDivision from Export as a fallback source
    p_col = cfg["columns"]["export"]["person"]
    div_col = cfg["columns"]["export"]["person_div"]
    export_division_map: dict[str, str] = {}
    if p_col in export_df.columns and div_col in export_df.columns:
        for _, row in export_df[[p_col, div_col]].dropna(subset=[p_col]).iterrows():
            name = str(row[p_col]).strip()
            div_val = str(row[div_col]).strip() if pd.notna(row[div_col]) else ""
            if name and div_val and name not in export_division_map:
                export_division_map[name] = div_val

    # Build employee profiles
    profiles: dict[str, EmployeeProfile] = {}
    for person in all_employees:
        prof = EmployeeProfile(
            person=person,
            person_org=org_map.get(person, ""),
        )
        if person in pt_employees:
            prof.is_pt = True

        fld = first_last_map.get(person, {})
        prof.first_day = fld.get("first_day")
        prof.last_day = fld.get("last_day")

        pto = pto_map.get(person, {})
        prof.bus_week_hours = pto.get("bus_week_hours")
        prof.hire_date = pto.get("hire_date")
        prof.pto_balance_available = pto.get("available")

        prof.portfolio_lead = portfolio_lead_map.get(person, "")
        prof.primary_contract_type = project_type_map.get(person, "")
        # Division: prefer Lookups section C, fall back to Export PersonDivision
        prof.division = (
            division_map.get(person, "")
            or export_division_map.get(person, "")
        )

        profiles[person] = prof

    # Partial-period employees: those with a first_day or last_day in 2026
    partial_period_employees = {
        p for p, prof in profiles.items()
        if prof.first_day is not None or prof.last_day is not None
    }

    # Validate unexpected subgroups (warn, don't crash)
    unexpected_subgroups: set[str] = set()
    sg_col = cfg["columns"]["export"]["project_subgroup"]
    if sg_col in export_df.columns:
        found = set(export_df[sg_col].dropna().unique())
        unexpected_subgroups = found - KNOWN_SUBGROUPS
        for sg in unexpected_subgroups:
            warnings.warn(f"Unexpected ProjectSubGroup value: {sg!r}")

    return LoadedData(
        periods=periods,
        report_start=report_start,
        report_end=report_end,
        export_df=export_df,
        all_employees=all_employees,
        employee_profiles=profiles,
        pt_employees=pt_employees,
        partial_period_employees=partial_period_employees,
        excluded_no_pto=excluded_no_pto,
        flagged_no_timesheet=flagged_no_timesheet,
        missing_lookup_combos=missing_combos,
        pipeline_summary=pipeline_summary,
        ul_rows_excluded=0,  # computed in _load_export and stored on df attribute below
        unexpected_subgroups=unexpected_subgroups,
        portfolio_lead_map=portfolio_lead_map,
        project_type_map=project_type_map,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Sheet loaders
# ─────────────────────────────────────────────────────────────────────────────

def _load_export(xl: pd.ExcelFile, sheet: str, cfg: dict) -> pd.DataFrame:
    df = xl.parse(sheet, header=0)
    cols = cfg["columns"]["export"]
    pc_col = cols["paycode"]
    exclude_code = cfg["paycode_exclude"]

    # Store UL count as a module-level side-channel (accessed by analyze.py)
    global _ul_row_count
    if pc_col in df.columns:
        _ul_row_count = int((df[pc_col] == exclude_code).sum())
        df = df[df[pc_col] != exclude_code].copy()
    else:
        _ul_row_count = 0

    # Ensure Date column is datetime
    date_col = cols["date"]
    if date_col in df.columns:
        df[date_col] = pd.to_datetime(df[date_col], errors="coerce")

    # Ensure Hours is numeric
    h_col = cols["hours"]
    if h_col in df.columns:
        df[h_col] = pd.to_numeric(df[h_col], errors="coerce").fillna(0.0)

    return df


_ul_row_count: int = 0


def get_ul_row_count() -> int:
    return _ul_row_count


def _derive_periods_from_export(
    df: pd.DataFrame, date_col: str, grouping: str = "monthly"
) -> tuple[list[Period], date, date]:
    """Build a period calendar from the Export sheet's Date column.

    grouping='monthly'  → one Period per calendar month (default)
    grouping='biweekly' → 14-day periods starting from the earliest date
    """
    dates = pd.to_datetime(df[date_col], errors="coerce").dropna()
    if dates.empty:
        return [], date.today(), date.today()

    min_date = dates.min().date()
    max_date = dates.max().date()
    periods: list[Period] = []

    if grouping == "biweekly":
        start = min_date
        idx = 1
        while start <= max_date:
            end = min(start + timedelta(days=13), max_date)
            bdays = len(pd.bdate_range(start, end))
            periods.append(Period(index=idx, start=start, end=end,
                                  net_hours=bdays * 8,
                                  name=f"{start.strftime('%m/%d')}–{end.strftime('%m/%d')}"))
            start = end + timedelta(days=1)
            idx += 1
    else:  # monthly (default)
        from calendar import monthrange
        cur = min_date.replace(day=1)
        idx = 1
        while cur <= max_date:
            last_day = monthrange(cur.year, cur.month)[1]
            end = min(cur.replace(day=last_day), max_date)
            bdays = len(pd.bdate_range(cur, end))
            periods.append(Period(index=idx, start=cur, end=end,
                                  net_hours=bdays * 8,
                                  name=cur.strftime("%b %Y")))
            if cur.month == 12:
                cur = cur.replace(year=cur.year + 1, month=1, day=1)
            else:
                cur = cur.replace(month=cur.month + 1, day=1)
            idx += 1

    return periods, min_date, max_date


def _load_lookups_and_billable_ute(
    xl: pd.ExcelFile, lookups_sheet: str, billable_ute_sheet: str
) -> tuple[list[Period], date, date, set[str]]:

    # ── Billable Ute: period bounds from B1 / B2 ──────────────────────────────
    bu = xl.parse(billable_ute_sheet, header=None)
    report_start = _parse_date_value(bu.iloc[0, 1])
    report_end = _parse_date_value(bu.iloc[1, 1])

    # ── Lookups: period calendar from section A ────────────────────────────────
    lu = xl.parse(lookups_sheet, header=None)

    # Section A: starts at column G (index 6). Row 0 = start dates, row 2 = net hours.
    # We scan row 0 for date values starting from col index 6.
    start_dates: list[date] = []
    net_hours_list: list[float] = []

    for col_idx in range(6, lu.shape[1]):
        raw_start = lu.iloc[0, col_idx]
        raw_net = lu.iloc[2, col_idx]

        parsed = _parse_date_value(raw_start)
        if parsed is None:
            continue

        # Only include periods up through report_end
        if report_end is not None and parsed > report_end:
            break

        try:
            net = float(raw_net)
        except (TypeError, ValueError):
            net = 0.0

        start_dates.append(parsed)
        net_hours_list.append(net)

    # Build period list
    periods: list[Period] = []
    for i, (sd, nh) in enumerate(zip(start_dates, net_hours_list)):
        if i + 1 < len(start_dates):
            ed = start_dates[i + 1] - timedelta(days=1)
        else:
            ed = report_end if report_end else sd + timedelta(days=14)
        periods.append(Period(index=i + 1, start=sd, end=ed, net_hours=nh))

    # ── Lookups: PT employees (Section B) ─────────────────────────────────────
    # Find column named "PT Employees" in row 0
    pt_employees: set[str] = set()
    header_row = lu.iloc[0]
    pt_col_idx = None
    for ci, val in enumerate(header_row):
        if isinstance(val, str) and "PT Employees" in val:
            pt_col_idx = ci
            break

    if pt_col_idx is not None:
        for ri in range(1, lu.shape[0]):
            val = lu.iloc[ri, pt_col_idx]
            if isinstance(val, str) and val.strip():
                pt_employees.add(val.strip())

    return periods, report_start, report_end, pt_employees


def _load_unique_employees(xl: pd.ExcelFile, sheet: str) -> tuple[list[str], dict[str, str]]:
    df = xl.parse(sheet, header=0)
    person_col = "Person"
    org_col = "PersonOrganization"
    employees: list[str] = []
    org_map: dict[str, str] = {}
    if person_col in df.columns:
        for _, row in df.iterrows():
            name = row.get(person_col)
            org = row.get(org_col, "")
            if isinstance(name, str) and name.strip():
                employees.append(name.strip())
                org_map[name.strip()] = str(org).strip() if isinstance(org, str) else ""
    return employees, org_map


def _load_first_last(xl: pd.ExcelFile, sheet: str) -> dict[str, dict]:
    df = xl.parse(sheet, header=0)
    result: dict[str, dict] = {}
    person_col = "Person"
    first_col = "2026 First Day"
    last_col = "2026 Last Day"
    if person_col not in df.columns:
        return result
    for _, row in df.iterrows():
        name = row.get(person_col)
        if not isinstance(name, str) or not name.strip():
            continue
        name = name.strip()
        fd = _parse_date_value(row.get(first_col))
        ld = _parse_date_value(row.get(last_col))
        result[name] = {"first_day": fd, "last_day": ld}
    return result


def _load_pto_balances(xl: pd.ExcelFile, sheet: str) -> dict[str, dict]:
    df = xl.parse(sheet, header=0)
    result: dict[str, dict] = {}
    person_col = "Person"
    if person_col not in df.columns:
        return result
    for _, row in df.iterrows():
        name = row.get(person_col)
        if not isinstance(name, str) or not name.strip():
            continue
        name = name.strip()
        try:
            bwh = float(row.get("Bus Week Hours", 0) or 0)
        except (ValueError, TypeError):
            bwh = None
        hire = _parse_date_value(row.get("Hire Date"))
        try:
            avail = float(row.get("Period Hours Available", 0) or 0)
        except (ValueError, TypeError):
            avail = None
        result[name] = {"bus_week_hours": bwh, "hire_date": hire, "available": avail}
    return result


def _load_portfolio(
    xl: pd.ExcelFile, portfolio_sheet: str, lookups_sheet: str, cfg: dict
) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    lead_map: dict[str, str] = {}
    type_map: dict[str, str] = {}
    div_map: dict[str, str] = {}

    # Primary: Lookups section C
    lu = xl.parse(lookups_sheet, header=None)
    lu_header_row = lu.iloc[0]
    org_col_idx = None
    for ci, val in enumerate(lu_header_row):
        if isinstance(val, str) and "PersonOrganization" in val:
            org_col_idx = ci
            break
    if org_col_idx is not None:
        # Headers: PersonOrganization, Division, Portfolio Lead, PrimaryContractType
        sub = lu.iloc[1:, org_col_idx:org_col_idx + 4].reset_index(drop=True)
        sub.columns = ["PersonOrganization", "Division", "Portfolio Lead", "PrimaryContractType"]
        for _, row in sub.iterrows():
            name = row.get("PersonOrganization", "")
            lead = row.get("Portfolio Lead", "")
            ct = row.get("PrimaryContractType", "")
            div = row.get("Division", "")
            if isinstance(name, str) and name.strip():
                n = name.strip()
                if isinstance(lead, str) and lead.strip():
                    lead_map[n] = lead.strip()
                if isinstance(ct, str) and ct.strip():
                    type_map[n] = ct.strip()
                if isinstance(div, str) and div.strip():
                    div_map[n] = div.strip()

    # Secondary / fallback: Portfolio Leads Proj Types sheet
    try:
        pcols = cfg["columns"]["portfolio"]
        df = xl.parse(portfolio_sheet, header=0)
        p_col = pcols["person"]
        l_col = pcols["lead"]
        t_col = pcols["project_type"]
        for _, row in df.iterrows():
            name = row.get(p_col, "")
            lead = row.get(l_col, "")
            pt = row.get(t_col, "")
            if isinstance(name, str) and name.strip():
                n = name.strip()
                if n not in lead_map and isinstance(lead, str) and lead.strip():
                    lead_map[n] = lead.strip()
                if n not in type_map and isinstance(pt, str) and pt.strip():
                    type_map[n] = pt.strip()
    except Exception:
        pass

    return lead_map, type_map, div_map


def _load_discrepancies(
    xl: pd.ExcelFile, sheet: str
) -> tuple[set[str], set[str], list[dict], dict]:
    df = xl.parse(sheet, header=None)
    excluded_no_pto: set[str] = set()
    flagged_no_timesheet: set[str] = set()
    missing_combos: list[dict] = []
    pipeline_summary: dict = {}

    # Scan rows for known header strings
    section_a_header = None
    section_b_header = None
    pipeline_header = None

    for ri in range(len(df)):
        row_vals = [str(v).strip() if not pd.isna(v) else "" for v in df.iloc[ri]]
        row_str = " ".join(row_vals)

        if "Name" in row_vals and "Found In" in row_vals and "Missing From" in row_vals:
            if section_a_header is None:
                section_a_header = ri
            elif section_a_header is not None:
                # second occurrence — handle below
                pass

        if "ProjectType" in row_vals and "PayCode" in row_vals:
            section_b_header = ri

        if "Run Date" in row_str or "run date" in row_str.lower():
            pipeline_header = ri

    # Parse section A — Name Mismatches
    # Two directions: one block for "Time Details → PTO Balances" (excluded),
    # one for "PTO Balances → Time Details" (flagged).
    if section_a_header is not None:
        # Find the Name, Found In, Missing From column positions
        header_row = df.iloc[section_a_header]
        col_map = {}
        for ci, val in enumerate(header_row):
            s = str(val).strip()
            if s in ("Name", "Found In", "Missing From"):
                col_map[s] = ci

        name_ci = col_map.get("Name")
        found_ci = col_map.get("Found In")
        missing_ci = col_map.get("Missing From")

        if name_ci is not None:
            for ri in range(section_a_header + 1, len(df)):
                row = df.iloc[ri]
                # Stop at blank row or new header
                name_val = row.iloc[name_ci] if name_ci < len(row) else None
                if pd.isna(name_val) or str(name_val).strip() == "":
                    break
                name_str = str(name_val).strip()
                found_str = str(row.iloc[found_ci]).strip() if found_ci is not None else ""
                missing_str = str(row.iloc[missing_ci]).strip() if missing_ci is not None else ""

                if "Time Details" in found_str and "PTO" in missing_str:
                    excluded_no_pto.add(name_str)
                elif "PTO" in found_str and "Time Details" in missing_str:
                    flagged_no_timesheet.add(name_str)

    # Parse section B — Missing Lookup Combos
    if section_b_header is not None:
        for ri in range(section_b_header + 1, len(df)):
            row = df.iloc[ri]
            vals = [str(v).strip() if not pd.isna(v) else "" for v in row]
            if not any(vals):
                break
            if "None" in vals or any("None" in v for v in vals):
                break
            if vals[0]:
                missing_combos.append({"ProjectType": vals[0], "PayCode": vals[1] if len(vals) > 1 else ""})

    # Parse pipeline summary
    if pipeline_header is not None:
        for ri in range(pipeline_header, min(pipeline_header + 10, len(df))):
            row = df.iloc[ri]
            vals = [str(v).strip() if not pd.isna(v) else "" for v in row]
            key = vals[0] if vals else ""
            val = vals[1] if len(vals) > 1 else ""
            if key:
                pipeline_summary[key] = val

    return excluded_no_pto, flagged_no_timesheet, missing_combos, pipeline_summary
