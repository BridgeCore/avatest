"""
Cross-dataset flagging: does a division show low utilization AND low revenue
at the same time? That combination usually means something structural, not
noise, so it's surfaced distinctly (critical) from either signal alone
(warning).

Standalone and pure — no I/O, no subprocess, no other src/ module imports.
Callers pass in already-computed per-division numbers (utilization rollups
from src/aggregator.py, revenue variance from src/store.py's row builders);
this module only applies the threshold/severity logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

DEFAULT_UTILIZATION_TARGET = 0.85
DEFAULT_REVENUE_VARIANCE_TARGET = 0.00

Severity = Literal["none", "warning", "critical"]


@dataclass
class CrossFlag:
    division: str
    avg_billable_util: float | None
    mtd_variance_pct: float | None
    ytd_variance_pct: float | None
    low_utilization: bool | None
    low_revenue_mtd: bool | None
    low_revenue_ytd: bool | None
    severity: Severity
    reason: str


def _thresholds(cfg: dict) -> tuple[float, float]:
    cross_cfg = cfg.get("cross_flags", {}) if cfg else {}
    return (
        cross_cfg.get("utilization_target", DEFAULT_UTILIZATION_TARGET),
        cross_cfg.get("revenue_variance_target", DEFAULT_REVENUE_VARIANCE_TARGET),
    )


def evaluate_division(
    division: str,
    avg_billable_util: float | None,
    mtd_variance_pct: float | None,
    ytd_variance_pct: float | None,
    cfg: dict | None = None,
) -> CrossFlag:
    util_target, rev_target = _thresholds(cfg or {})

    low_utilization = None if avg_billable_util is None else avg_billable_util < util_target
    low_revenue_mtd = None if mtd_variance_pct is None else mtd_variance_pct < rev_target
    low_revenue_ytd = None if ytd_variance_pct is None else ytd_variance_pct < rev_target

    combined_ytd = bool(low_utilization) and bool(low_revenue_ytd)
    combined_mtd_only = bool(low_utilization) and bool(low_revenue_mtd) and not combined_ytd
    single_metric = (not combined_ytd and not combined_mtd_only) and (
        bool(low_utilization) or bool(low_revenue_mtd) or bool(low_revenue_ytd)
    )

    if combined_ytd:
        severity: Severity = "critical"
        reason = "Low utilization + sustained (YTD) revenue miss"
    elif combined_mtd_only:
        severity = "warning"
        reason = "Low utilization + recent (MTD) revenue miss"
    elif single_metric:
        severity = "warning"
        parts = []
        if low_utilization:
            parts.append("low utilization")
        if low_revenue_mtd:
            parts.append("MTD revenue miss")
        if low_revenue_ytd:
            parts.append("YTD revenue miss")
        reason = " + ".join(parts) if parts else "Flagged metric"
    else:
        severity = "none"
        reason = "Healthy" if avg_billable_util is not None else "No utilization data"

    if avg_billable_util is None and severity == "none":
        reason = "No utilization data"

    return CrossFlag(
        division=division,
        avg_billable_util=avg_billable_util,
        mtd_variance_pct=mtd_variance_pct,
        ytd_variance_pct=ytd_variance_pct,
        low_utilization=low_utilization,
        low_revenue_mtd=low_revenue_mtd,
        low_revenue_ytd=low_revenue_ytd,
        severity=severity,
        reason=reason,
    )


def evaluate_all(
    util_rollups: dict[str, dict],
    gm_variance_by_division: dict[str, dict],
    cfg: dict | None = None,
) -> list[CrossFlag]:
    """util_rollups: division -> {"avg_billable_util": float|None}
    gm_variance_by_division: division -> {"mtd_variance_pct": float|None, "ytd_variance_pct": float|None}

    A division present in only one of the two dicts still gets a CrossFlag —
    the missing side's fields stay None rather than being coerced to a
    misleading False/0 (this is how Corp — utilization-only, no revenue
    mapping — and a newly-onboarded, revenue-only division both fall out
    naturally, with no division name hardcoded here).
    """
    divisions = sorted(set(util_rollups) | set(gm_variance_by_division))
    flags = []
    for division in divisions:
        util = util_rollups.get(division, {})
        gm = gm_variance_by_division.get(division, {})
        flags.append(evaluate_division(
            division,
            util.get("avg_billable_util"),
            gm.get("mtd_variance_pct"),
            gm.get("ytd_variance_pct"),
            cfg,
        ))
    return flags
