"""
Reads a Revenue / Gross Margin (GM) workbook: monthly "GM Report" actuals
sheets, an "AOP" plan sheet, and monthly "Compare" sheets.

This is a Python port of the client-side parser in cfo_dashboard.html
(parseWB / parseGM / parseAOPSheet / parseCmp). It follows the same
warn-don't-crash philosophy as src/loader.py: a bad or missing sheet/column
is recorded as an issue on that sheet's SheetScanEntry rather than aborting
the whole workbook parse.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import pandas as pd

from .common import ColumnResolver, parse_number

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

_YEAR_RE = re.compile(r"(\d{4}|\d{2})")


# ─────────────────────────────────────────────────────────────────────────────
# Data model
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class MonthKey:
    idx: int      # 0-based, Jan=0
    year: int
    label: str    # "Jan 2026"
    key: int      # year*12 + idx, sortable


@dataclass
class GMActual:
    month: MonthKey
    project_name: str
    division: str
    revenue: float = 0.0
    gross_margin: float = 0.0
    gm_pct: float = 0.0
    total_direct_cost: float = 0.0


@dataclass
class GMProject:
    name: str
    division: str
    code: str = ""
    portfolio_lead: str = "—"


@dataclass
class AopEntry:
    division: str
    revenue: list = field(default_factory=lambda: [0.0] * 12)
    gp: list = field(default_factory=lambda: [0.0] * 12)


@dataclass
class CompareRow:
    month: MonthKey
    program: str
    portfolio: str = "—"
    actuals: float = 0.0
    aop: float = 0.0
    var_rev: float = 0.0
    gp_actual: float = 0.0
    aop_gm_pct: float = 0.0
    gp_aop: float = 0.0


@dataclass
class SheetScanEntry:
    name: str
    type: str = "ignored"  # "gm" | "aop" | "compare" | "ignored"
    month: str | None = None
    rows: int = 0
    issues: list = field(default_factory=list)
    cols: list | None = None
    cell_preview: list = field(default_factory=list)


@dataclass
class GMData:
    months: list
    projects: dict            # "name\x1fdiv" -> GMProject
    actuals: list             # list[GMActual]
    aop: dict                 # division -> AopEntry
    compare: list             # list[CompareRow]
    has_aop: bool
    sheet_log: list           # list[SheetScanEntry]


# ─────────────────────────────────────────────────────────────────────────────
# Sheet-name classification
# ─────────────────────────────────────────────────────────────────────────────

def is_gm_sheet(name: str) -> bool:
    return name.strip() == "Jan26" or re.search(r"gm[\s_\-]?reports?", name, re.I) is not None


def is_aop_sheet(name: str) -> bool:
    return re.search(r"aop", name, re.I) is not None and re.search(r"compare", name, re.I) is None


def is_compare_sheet(name: str) -> bool:
    return re.search(r"compare", name, re.I) is not None


def extract_month(name: str) -> MonthKey | None:
    lo = name.lower()
    for i, mo in enumerate(MONTHS):
        if lo.startswith(mo.lower()):
            m = _YEAR_RE.search(name)
            yr = 2026
            if m:
                yr = int(m.group(1))
                if yr < 100:
                    yr += 2000
            return MonthKey(idx=i, year=yr, label=f"{mo} {yr}", key=yr * 12 + i)
    return None


def _project_key(name: str, division: str) -> str:
    return f"{name}\x1f{division}"


# ─────────────────────────────────────────────────────────────────────────────
# Sheet parsers
# ─────────────────────────────────────────────────────────────────────────────

def _parse_gm_sheet(
    df: pd.DataFrame, name: str, gm_cfg: dict, divisions: set[str],
    actuals: list, projects: dict, entry: SheetScanEntry,
) -> None:
    mo = extract_month(name)
    if mo:
        entry.month = mo.label
    else:
        entry.issues.append(
            'Could not detect month from sheet name — expected e.g. "Jan26" or "Feb 2026 GM Report"'
        )
        return

    if df.empty:
        entry.issues.append("Sheet appears empty")
        return

    raw_cols = [str(c) for c in df.columns]
    entry.cols = raw_cols
    resolver = ColumnResolver(raw_cols, gm_cfg["gm_aliases"])

    required = gm_cfg["gm_required_columns"]
    missing = resolver.missing(required)
    if missing:
        entry.issues.append(f"Missing required columns: {', '.join(missing)}")
        return

    org_col = resolver.resolve("Org")
    name_col = resolver.resolve("AOP Legend")
    code_col = resolver.resolve("Project Code")
    rev_col = resolver.resolve("Revenue")
    gm_col = resolver.resolve("GrossMargin")
    gmp_col = resolver.resolve("GrossMarginPercentage")
    tdc_col = resolver.resolve("TotalDirectCost")

    rows_added = 0
    case_corrected = 0
    for _, row in df.iterrows():
        div_raw = str(row.get(org_col) or "").strip()
        div = div_raw.upper()
        if div not in divisions:
            continue
        if div != div_raw:
            case_corrected += 1
        pname = str(row.get(name_col) or "").strip()
        if not pname:
            continue
        pk = _project_key(pname, div)
        if pk not in projects:
            projects[pk] = GMProject(
                name=pname, division=div,
                code=str(row.get(code_col) or "").strip() if code_col else "",
            )
        actuals.append(GMActual(
            month=mo, project_name=pname, division=div,
            revenue=parse_number(row.get(rev_col)),
            gross_margin=parse_number(row.get(gm_col)),
            gm_pct=parse_number(row.get(gmp_col)),
            total_direct_cost=parse_number(row.get(tdc_col)),
        ))
        rows_added += 1

    entry.rows = rows_added
    if case_corrected:
        entry.issues.append(
            f"{case_corrected} row(s) had non-standard division casing (e.g. \"Bl1\") "
            "— normalized to the canonical code"
        )
    if rows_added == 0 and not entry.issues:
        entry.issues.append(
            "0 rows parsed — check Org column values "
            f"(expected: {', '.join(sorted(divisions))})"
        )


def _cell(df: pd.DataFrame, r: int, c: int):
    if r < 0 or c < 0 or r >= df.shape[0] or c >= df.shape[1]:
        return None
    v = df.iat[r, c]
    return None if pd.isna(v) else v


def _parse_aop_sheet(df: pd.DataFrame, gm_cfg: dict, aop: dict, entry: SheetScanEntry) -> None:
    layout = gm_cfg["aop_layout"]
    col_div = layout["col_division"]
    col_mo0 = layout["col_first_month"]
    alias_map = {str(k).strip().lower(): v for k, v in gm_cfg["aop_division_aliases"].items()}

    def map_div(val) -> str | None:
        return alias_map.get(str(val or "").strip().lower())

    preview = []
    for r in range(1, 16):  # Excel rows 2-16
        c_val = _cell(df, r, col_div)
        d_val = _cell(df, r, col_mo0)
        if c_val is not None or d_val is not None:
            preview.append(f'Row {r + 1}: C="{c_val if c_val is not None else "(empty)"}"  '
                            f'D(Jan)="{d_val if d_val is not None else "(empty)"}"')
    entry.cell_preview = preview

    found_divs = []
    for r in range(layout["revenue_row_start"], layout["revenue_row_end"] + 1):
        div = map_div(_cell(df, r, col_div))
        if not div:
            continue
        found_divs.append(div)
        if div not in aop:
            aop[div] = AopEntry(division=div)
        for m in range(12):
            aop[div].revenue[m] = parse_number(_cell(df, r, col_mo0 + m))

    for r in range(layout["gp_row_start"], layout["gp_row_end"] + 1):
        div = map_div(_cell(df, r, col_div))
        if not div:
            continue
        if div not in aop:
            aop[div] = AopEntry(division=div)
        for m in range(12):
            aop[div].gp[m] = parse_number(_cell(df, r, col_mo0 + m))

    entry.rows = len(set(found_divs))
    if not found_divs:
        entry.issues.append(
            "No divisions matched. Check the cell preview — division names "
            "must be in the configured division column, revenue rows."
        )


def _parse_compare_sheet(df: pd.DataFrame, name: str, gm_cfg: dict, compare: list, entry: SheetScanEntry) -> None:
    mo = extract_month(name)
    if not mo:
        entry.issues.append("Could not detect month from sheet name")
        return
    entry.month = mo.label

    if df.empty:
        entry.issues.append("Sheet appears empty")
        return

    raw_cols = [str(c) for c in df.columns]
    entry.cols = raw_cols
    resolver = ColumnResolver(raw_cols, gm_cfg["compare_aliases"])

    prog_col = resolver.resolve("Program")
    port_col = resolver.resolve("Portfolio")
    act_col = resolver.resolve("Actuals")
    aop_col = resolver.resolve("AOP")
    var_col = resolver.resolve("Var")
    gpa_col = resolver.resolve("GP Actual")
    aopgm_col = resolver.resolve("AOP GM%")
    gpaop_col = resolver.resolve("GP AOP")

    rows_added = 0
    for _, row in df.iterrows():
        prog = str(row.get(prog_col) or "").strip() if prog_col else ""
        if not prog:
            continue
        portfolio = str(row.get(port_col) or "").strip() if port_col else ""
        compare.append(CompareRow(
            month=mo, program=prog,
            portfolio=portfolio or "—",
            actuals=parse_number(row.get(act_col)) if act_col else 0.0,
            aop=parse_number(row.get(aop_col)) if aop_col else 0.0,
            var_rev=parse_number(row.get(var_col)) if var_col else 0.0,
            gp_actual=parse_number(row.get(gpa_col)) if gpa_col else 0.0,
            aop_gm_pct=parse_number(row.get(aopgm_col)) if aopgm_col else 0.0,
            gp_aop=parse_number(row.get(gpaop_col)) if gpaop_col else 0.0,
        ))
        rows_added += 1
    entry.rows = rows_added


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def load_gm_workbook(excel_path, cfg: dict) -> GMData:
    """Accept a file path (Path) or an in-memory buffer (BytesIO)."""
    gm_cfg = cfg["gm"]
    divisions = set(gm_cfg["divisions"])

    xl = pd.ExcelFile(excel_path, engine="openpyxl")

    projects: dict[str, GMProject] = {}
    actuals: list[GMActual] = []
    aop: dict[str, AopEntry] = {}
    compare: list[CompareRow] = []
    sheet_log: list[SheetScanEntry] = []

    for sheet_name in xl.sheet_names:
        entry = SheetScanEntry(name=sheet_name)
        try:
            if is_gm_sheet(sheet_name):
                entry.type = "gm"
                df = xl.parse(sheet_name, header=0)
                _parse_gm_sheet(df, sheet_name, gm_cfg, divisions, actuals, projects, entry)
            elif is_aop_sheet(sheet_name):
                entry.type = "aop"
                df = xl.parse(sheet_name, header=None)
                _parse_aop_sheet(df, gm_cfg, aop, entry)
            elif is_compare_sheet(sheet_name):
                entry.type = "compare"
                df = xl.parse(sheet_name, header=0)
                _parse_compare_sheet(df, sheet_name, gm_cfg, compare, entry)
        except Exception as exc:  # noqa: BLE001 — a single bad sheet must not abort the workbook
            entry.issues.append(f"Parse error: {exc}")
        sheet_log.append(entry)

    # Attach portfolio leads from compare data (first match wins, case-insensitive)
    port_map: dict[str, str] = {}
    for c in compare:
        key = c.program.lower()
        if key not in port_map:
            port_map[key] = c.portfolio
    for p in projects.values():
        p.portfolio_lead = port_map.get(p.name.lower(), "—")

    # Unique, sorted month list
    seen: dict[int, MonthKey] = {}
    for a in actuals:
        seen[a.month.key] = a.month
    months = sorted(seen.values(), key=lambda m: m.key)

    return GMData(
        months=months, projects=projects, actuals=actuals, aop=aop,
        compare=compare, has_aop=len(aop) > 0, sheet_log=sheet_log,
    )
