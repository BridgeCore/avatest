"""
Structured, queryable per-period persistence — SQLite, alongside (not
replacing) output/history/'s rendered-HTML archive.

output/history/ answers "show me the dashboard I generated on date X."
This module answers "has period X already been ingested for division Y,"
which is what period-collision detection and cross-dataset flagging need.
Nothing here renders HTML or touches output/history/.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .gm_calculator import _acts_for, _aop_for
from .gm_loader import GMData

DB_PATH = Path("data") / "bcore.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS periods_util (
    period_index INTEGER,
    division TEXT,
    period_start TEXT,
    period_end TEXT,
    headcount INTEGER,
    avg_billable_util REAL,
    avg_direct_util REAL,
    critical_count INTEGER,
    warning_count INTEGER,
    source_file TEXT,
    ingested_at TEXT,
    PRIMARY KEY (period_index, division)
);
CREATE TABLE IF NOT EXISTS periods_gm (
    month_key INTEGER,
    division TEXT,
    month_label TEXT,
    revenue REAL,
    gp REAL,
    gm_pct REAL,
    aop_revenue REAL,
    aop_gp REAL,
    mtd_variance_pct REAL,
    ytd_variance_pct REAL,
    source_file TEXT,
    ingested_at TEXT,
    PRIMARY KEY (month_key, division)
);
CREATE TABLE IF NOT EXISTS discrepancies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT,
    ref TEXT,
    detail TEXT,
    source_file TEXT,
    ingested_at TEXT
);
"""


def connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


def check_collisions(conn: sqlite3.Connection, kind: str, keys: list[tuple]) -> list[tuple]:
    """kind is "util" (keys are (period_index, division)) or "gm" (keys are
    (month_key, division)). Returns the subset of `keys` already present."""
    table = "periods_util" if kind == "util" else "periods_gm"
    key_col = "period_index" if kind == "util" else "month_key"
    existing = []
    for key_val, division in keys:
        row = conn.execute(
            f"SELECT 1 FROM {table} WHERE {key_col} = ? AND division = ?",
            (key_val, division),
        ).fetchone()
        if row is not None:
            existing.append((key_val, division))
    return existing


def write_util_periods(conn: sqlite3.Connection, rows: list[dict]) -> None:
    conn.executemany(
        """INSERT OR REPLACE INTO periods_util
           (period_index, division, period_start, period_end, headcount,
            avg_billable_util, avg_direct_util, critical_count, warning_count,
            source_file, ingested_at)
           VALUES (:period_index, :division, :period_start, :period_end, :headcount,
                   :avg_billable_util, :avg_direct_util, :critical_count, :warning_count,
                   :source_file, :ingested_at)""",
        rows,
    )
    conn.commit()


def write_gm_periods(conn: sqlite3.Connection, rows: list[dict]) -> None:
    conn.executemany(
        """INSERT OR REPLACE INTO periods_gm
           (month_key, division, month_label, revenue, gp, gm_pct,
            aop_revenue, aop_gp, mtd_variance_pct, ytd_variance_pct,
            source_file, ingested_at)
           VALUES (:month_key, :division, :month_label, :revenue, :gp, :gm_pct,
                   :aop_revenue, :aop_gp, :mtd_variance_pct, :ytd_variance_pct,
                   :source_file, :ingested_at)""",
        rows,
    )
    conn.commit()


def write_discrepancies(conn: sqlite3.Connection, rows: list[dict]) -> None:
    conn.executemany(
        """INSERT INTO discrepancies (kind, ref, detail, source_file, ingested_at)
           VALUES (:kind, :ref, :detail, :source_file, :ingested_at)""",
        rows,
    )
    conn.commit()


def clear_all(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM periods_util")
    conn.execute("DELETE FROM periods_gm")
    conn.execute("DELETE FROM discrepancies")
    conn.commit()


# ─────────────────────────────────────────────────────────────────────────────
# Cross-flagger inputs — derived from rows this module already built/holds
# ─────────────────────────────────────────────────────────────────────────────

def util_rollups_to_dict(division_rollups: dict, cfg: dict | None = None) -> dict[str, dict]:
    """division_rollups: dict[division_code -> GroupStats] from src/aggregator.py.
    Only divisions with at least one mapped employee appear as keys, which is
    exactly the "no data" signal src/cross_flagger.py wants — a division absent
    here contributes avg_billable_util=None rather than a misleading 0.0.

    Re-keys onto the GM workbook's division codes via cfg's
    cross_flags.util_division_aliases (e.g. utilization's "BL"/"IS" -> GM's
    "BL1"/"IS1" — confirmed against both real workbooks; without this, those
    two divisions can never produce a combined utilization+revenue flag since
    the two datasets would never share a key). Divisions with no configured
    alias (e.g. "Corp", which has no GM counterpart) pass through unchanged.
    """
    aliases = (cfg or {}).get("cross_flags", {}).get("util_division_aliases", {})
    return {aliases.get(division, division): {"avg_billable_util": group.avg_billable_utilization}
            for division, group in division_rollups.items()}


def latest_gm_variance_by_division(gm_rows: list[dict]) -> dict[str, dict]:
    """Most-recent (by month_key) row per division from build_gm_rows() output,
    projected down to just the two variance fields src/cross_flagger.py needs."""
    latest: dict[str, dict] = {}
    for row in gm_rows:
        div = row["division"]
        if div not in latest or row["month_key"] > latest[div]["month_key"]:
            latest[div] = row
    return {
        div: {"mtd_variance_pct": row["mtd_variance_pct"], "ytd_variance_pct": row["ytd_variance_pct"]}
        for div, row in latest.items()
    }


# ─────────────────────────────────────────────────────────────────────────────
# DB-backed reads — the persisted, cumulative state across ALL past
# ingestions, not just the rows the current job just wrote. This is what
# makes cross-dataset flagging useful when the GM and utilization workbooks
# are uploaded in separate sessions: a util-only upload still sees whatever
# GM data was committed in an earlier session, and vice versa.
# ─────────────────────────────────────────────────────────────────────────────

def latest_util_avg_by_division_db(conn: sqlite3.Connection, cfg: dict | None = None) -> dict[str, dict]:
    rows = conn.execute(
        """SELECT division, avg_billable_util FROM periods_util p
           WHERE period_index = (
               SELECT MAX(period_index) FROM periods_util WHERE division = p.division
           )"""
    ).fetchall()
    aliases = (cfg or {}).get("cross_flags", {}).get("util_division_aliases", {})
    return {aliases.get(div, div): {"avg_billable_util": val} for div, val in rows}


def latest_gm_variance_by_division_db(conn: sqlite3.Connection) -> dict[str, dict]:
    rows = conn.execute(
        """SELECT division, mtd_variance_pct, ytd_variance_pct FROM periods_gm p
           WHERE month_key = (
               SELECT MAX(month_key) FROM periods_gm WHERE division = p.division
           )"""
    ).fetchall()
    return {div: {"mtd_variance_pct": mtd, "ytd_variance_pct": ytd} for div, mtd, ytd in rows}


# ─────────────────────────────────────────────────────────────────────────────
# Row builders — thin projections off data the pipeline already computed
# ─────────────────────────────────────────────────────────────────────────────

def build_util_rows(division_rollups: dict, source_file: str, ingested_at: str) -> list[dict]:
    """division_rollups: dict[division_code -> GroupStats] from src/aggregator.py."""
    rows = []
    for division, group in division_rollups.items():
        for gps in group.period_stats:
            rows.append({
                "period_index": gps.period_index,
                "division": division,
                "period_start": gps.period.start.isoformat(),
                "period_end": gps.period.end.isoformat(),
                "headcount": gps.employee_count,
                "avg_billable_util": gps.billable_utilization,
                "avg_direct_util": gps.direct_utilization,
                "critical_count": group.critical_count,
                "warning_count": group.warning_count,
                "source_file": source_file,
                "ingested_at": ingested_at,
            })
    return rows


def build_gm_rows(data: GMData, cfg: dict, source_file: str, ingested_at: str) -> list[dict]:
    """One row per (month, division) actually reported in the workbook.
    MTD variance compares that single month's actual vs its AOP entry;
    YTD variance compares the cumulative total through that month vs the
    cumulative AOP through that month — same arithmetic gm_calculator.py's
    _build_kpis already uses for the all-months YTD case, just evaluated at
    every month checkpoint instead of only the final one.
    """
    divisions = cfg["gm"]["divisions"]
    rows = []
    for div in divisions:
        acts = _acts_for(data, div)
        aop_entry = _aop_for(data, div, divisions)

        cum_rev = cum_gp = cum_aop_rev = cum_aop_gp = 0.0
        for m in data.months:
            month_acts = [a for a in acts if a.month.key == m.key]
            if not month_acts:
                continue
            month_rev = sum(a.revenue for a in month_acts)
            month_gp = sum(a.gross_margin for a in month_acts)
            month_aop_rev = aop_entry.revenue[m.idx] if aop_entry else None
            month_aop_gp = aop_entry.gp[m.idx] if aop_entry else None

            cum_rev += month_rev
            cum_gp += month_gp
            if aop_entry:
                cum_aop_rev += month_aop_rev
                cum_aop_gp += month_aop_gp

            mtd_variance_pct = (
                (month_rev - month_aop_rev) / month_aop_rev if month_aop_rev else None
            )
            ytd_variance_pct = (
                (cum_rev - cum_aop_rev) / cum_aop_rev if aop_entry and cum_aop_rev else None
            )

            rows.append({
                "month_key": m.key,
                "division": div,
                "month_label": m.label,
                "revenue": month_rev,
                "gp": month_gp,
                "gm_pct": (month_gp / month_rev * 100) if month_rev else None,
                "aop_revenue": month_aop_rev,
                "aop_gp": month_aop_gp,
                "mtd_variance_pct": mtd_variance_pct,
                "ytd_variance_pct": ytd_variance_pct,
                "source_file": source_file,
                "ingested_at": ingested_at,
            })
    return rows
