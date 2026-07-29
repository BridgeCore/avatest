"""
Detects persistent workforce trends across 6 categories.
A trend is only raised when the condition holds for N consecutive periods.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Severity(str, Enum):
    WARNING = "warning"
    CRITICAL = "critical"


class TrendType(str, Enum):
    LOW_BILLABLE = "low_billable"
    LOW_DIRECT = "low_direct"
    EXCESSIVE_PTO = "excessive_pto"
    HIGH_BP = "high_bp"
    HIGH_NONBILLABLE = "high_nonbillable"
    GA_CROWDING = "ga_crowding"


TREND_LABELS = {
    TrendType.LOW_BILLABLE:     "Low Billable Utilization",
    TrendType.LOW_DIRECT:       "Low Direct Utilization",
    TrendType.EXCESSIVE_PTO:    "Excessive PTO",
    TrendType.HIGH_BP:          "High B&P Hours",
    TrendType.HIGH_NONBILLABLE: "High Non-Billable Project Hours",
    TrendType.GA_CROWDING:      "G&A Crowding Out Billable Work",
}


@dataclass
class TrendFlag:
    trend_type: TrendType
    severity: Severity
    person: str
    period_indices: list[int]      # 1-based period indices where condition held
    explanation: str
    per_period_metrics: list[dict]  # for display in flag cards

    @property
    def label(self) -> str:
        return TREND_LABELS[self.trend_type]


def detect_trends(
    person: str,
    period_stats_list,   # list[PeriodStats] ordered by period_index
    cfg: dict,
    is_pt: bool = False,
    is_partial: bool = False,
    division: str = "",
) -> list[TrendFlag]:
    """Run all 6 trend detectors for one employee.

    Per-division threshold overrides in cfg["division_thresholds"][division]
    take precedence over global values for this employee's division.
    """
    # Merge division-specific overrides so all internal helpers just use cfg
    div_overrides = cfg.get("division_thresholds", {}).get(division, {})
    effective_cfg = {**cfg, **div_overrides} if div_overrides else cfg

    flags: list[TrendFlag] = []
    n = effective_cfg.get("persistence_threshold", 3)

    flags += _trend_low_utilization(person, period_stats_list, effective_cfg, n, billable=True, is_pt=is_pt)
    flags += _trend_low_utilization(person, period_stats_list, effective_cfg, n, billable=False, is_pt=is_pt)
    flags += _trend_excessive_pto(person, period_stats_list, effective_cfg, n, is_pt=is_pt)
    flags += _trend_high_bp(person, period_stats_list, effective_cfg, n, is_pt=is_pt)
    flags += _trend_high_nonbillable(person, period_stats_list, effective_cfg, n, is_pt=is_pt)
    flags += _trend_ga_crowding(person, period_stats_list, effective_cfg, n, is_pt=is_pt)

    return flags


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _active_periods(period_stats_list):
    """Return periods where the employee has non-zero available hours."""
    return [p for p in period_stats_list if p.net_available > 0]


def _find_consecutive_runs(periods, condition_fn, n: int) -> list[list]:
    """
    Returns all maximal runs of length >= n where condition_fn(period) is True.
    Each run is a list of PeriodStats objects.
    """
    runs = []
    current_run = []
    for p in periods:
        if condition_fn(p):
            current_run.append(p)
        else:
            if len(current_run) >= n:
                runs.append(current_run)
            current_run = []
    if len(current_run) >= n:
        runs.append(current_run)
    return runs


def _fmt_pct(val: float) -> str:
    return f"{val * 100:.1f}%"


def _fmt_hrs(val: float) -> str:
    return f"{val:.1f}"


def _period_metric(ps) -> dict:
    time_off = ps.time_off_hours
    return {
        "period_label": ps.period.label,
        "period_index": ps.period_index,
        "billable_util": _fmt_pct(ps.billable_utilization),
        "direct_util": _fmt_pct(ps.direct_utilization),
        "billable_hrs": _fmt_hrs(ps.billable_hours),
        "nonbillable_hrs": _fmt_hrs(ps.nonbillable_hours),
        "pto_hrs": _fmt_hrs(ps.pto_hours),
        "holiday_hrs": _fmt_hrs(ps.holiday_hours),
        "lwop_hrs": _fmt_hrs(ps.lwop_hours),
        "other_hrs": _fmt_hrs(ps.other_hours),
        "time_off_hrs": _fmt_hrs(time_off),
        "ga_hrs": _fmt_hrs(ps.ga_hours),
        "bp_hrs": _fmt_hrs(ps.bp_hours),
        "ird_hrs": _fmt_hrs(ps.ird_hours),
        "overhead_hrs": _fmt_hrs(ps.overhead_hours),
        "net_available": _fmt_hrs(ps.net_available),
        "effective_available": _fmt_hrs(ps.effective_available),
        "ga_ratio": _fmt_pct(ps.ga_ratio),
        "nonbillable_ratio": _fmt_pct(ps.nonbillable_ratio),
        "bp_ratio": _fmt_pct(ps.bp_ratio),
        "ird_ratio": _fmt_pct(ps.ird_ratio),
        "overhead_ratio": _fmt_pct(ps.overhead_ratio),
        "no_data": _is_zero_period(ps),
    }


def _is_zero_period(ps) -> bool:
    """True when literally nothing was logged for this period (no billable,
    non-billable, B&P, G&A, IR&D, overhead, PTO, holiday, LWOP, or other hours)
    despite the employee having scheduled hours. This is a distinct, higher-
    priority root cause from any of the capacity-allocation causes below — it
    usually means an unsubmitted/unprocessed timesheet, not a real utilization
    problem, and conflating the two hides a data-quality issue from the CFO/CDO."""
    return (
        ps.billable_hours == 0 and ps.nonbillable_hours == 0
        and ps.bp_hours == 0 and ps.ga_hours == 0
        and ps.ird_hours == 0 and ps.overhead_hours == 0
        and ps.pto_hours == 0 and ps.holiday_hours == 0
        and ps.lwop_hours == 0 and ps.other_hours == 0
    )


# ─────────────────────────────────────────────────────────────────────────────
# Trend 1 & 2: Low Billable / Low Direct Utilization
# ─────────────────────────────────────────────────────────────────────────────

def _trend_low_utilization(
    person, period_stats_list, cfg, n, billable: bool, is_pt: bool
) -> list[TrendFlag]:
    active = _active_periods(period_stats_list)
    if len(active) < n:
        return []

    warn_t = cfg["billable_utilization_warning"] if billable else cfg["direct_utilization_warning"]
    crit_t = cfg["billable_utilization_critical"] if billable else cfg["direct_utilization_critical"]
    trend_type = TrendType.LOW_BILLABLE if billable else TrendType.LOW_DIRECT

    def util_val(ps):
        return ps.billable_utilization if billable else ps.direct_utilization

    # Check critical first, then warning
    flags = []
    for threshold, severity in [(crit_t, Severity.CRITICAL), (warn_t, Severity.WARNING)]:
        runs = _find_consecutive_runs(active, lambda ps, t=threshold: util_val(ps) < t, n)
        for run in runs:
            # Check if this run was already covered by the crit_t pass above.
            # Note: a crit_t-pass flag's *displayed* severity may have been
            # downgraded to WARNING by _explain_low_util (e.g. explained by
            # PTO), so this must not key off severity == CRITICAL — doing so
            # would miss the earlier flag and emit a duplicate for the same
            # period range.
            if severity == Severity.WARNING:
                run_indices = {ps.period_index for ps in run}
                already = any(
                    f.trend_type == trend_type
                    and run_indices.issubset(set(f.period_indices))
                    for f in flags
                )
                if already:
                    continue

            explanation, final_severity = _explain_low_util(run, cfg, severity, is_pt, billable)
            flags.append(TrendFlag(
                trend_type=trend_type,
                severity=final_severity,
                person=person,
                period_indices=[ps.period_index for ps in run],
                explanation=explanation,
                per_period_metrics=[_period_metric(ps) for ps in run],
            ))

    return flags


def _explain_low_util(run, cfg, severity: Severity, is_pt: bool, billable: bool) -> tuple[str, Severity]:
    pto_high = cfg.get("pto_high_threshold_hours", 16)
    lwop_high = cfg.get("lwop_high_threshold_hours", 16)
    min_share = cfg.get("dominant_driver_min_share", 0.20)

    pt_note = " (PT employee)" if is_pt else ""
    util_word = "billable" if billable else "direct"

    # A period where every bucket is literally zero (despite scheduled hours)
    # is a data-quality signal, not a utilization problem — check it before
    # anything else so it never gets mislabeled as e.g. "high G&A".
    zero_periods = [ps for ps in run if _is_zero_period(ps)]
    if len(zero_periods) == len(run):
        total_avail = sum(ps.net_available for ps in run)
        expl = (
            f"No timesheet data recorded{pt_note} for {len(run)} consecutive period(s) "
            f"({_fmt_hrs(total_avail)} scheduled hrs) — likely unsubmitted or unprocessed "
            f"timesheets rather than true low {util_word} utilization"
        )
        return expl, severity  # not downgraded — this needs follow-up, not sympathy

    total_pto = sum(ps.pto_hours for ps in run)
    total_lwop = sum(ps.lwop_hours for ps in run)

    # PTO/LWOP are checked first and independently downgrade severity — these
    # are expected life events, not a capacity-allocation problem.
    if total_pto > pto_high and total_lwop > lwop_high:
        expl = (
            f"Low {util_word} utilization{pt_note} — elevated PTO ({_fmt_hrs(total_pto)} hrs) "
            f"and LWOP ({_fmt_hrs(total_lwop)} hrs) in affected periods"
        )
        return expl, _downgrade(severity)

    if total_pto > pto_high:
        expl = (
            f"Low {util_word} utilization{pt_note} — elevated PTO ({_fmt_hrs(total_pto)} hrs) "
            f"in affected periods"
        )
        return expl, _downgrade(severity)

    if total_lwop > lwop_high:
        expl = (
            f"Low {util_word} utilization{pt_note} — LWOP ({_fmt_hrs(total_lwop)} hrs) "
            f"in affected periods"
        )
        return expl, _downgrade(severity)

    # Remaining candidates are capacity-allocation causes. Pick whichever
    # consumed the most hours across the *whole run* (not an average of
    # per-period ratios) so a spike concentrated in one period isn't diluted
    # away by quieter neighboring periods in the same run.
    total_available = sum(ps.effective_available for ps in run) or sum(ps.net_available for ps in run) or 1.0
    candidates = [
        (sum(ps.bp_hours for ps in run), "B&P (bid & proposal) work"),
        (sum(ps.ga_hours for ps in run), "G&A overhead work"),
        (sum(ps.ird_hours for ps in run), "IR&D work"),
        (sum(ps.overhead_hours for ps in run), "general overhead work"),
    ]
    if billable:
        # Non-billable project hours only explain low *billable* utilization —
        # they're part of the numerator for direct utilization, so blaming
        # them for low direct utilization would be backwards.
        candidates.append((sum(ps.nonbillable_hours for ps in run), "non-billable project work"))

    dominant_hrs, dominant_label = max(candidates, key=lambda c: c[0])
    if dominant_hrs > 0 and (dominant_hrs / total_available) >= min_share:
        share = dominant_hrs / total_available
        expl = (
            f"Low {util_word} utilization{pt_note} — {dominant_label} "
            f"({_fmt_hrs(dominant_hrs)} hrs, {_fmt_pct(share)} of available) is the "
            f"largest driver over the affected period(s)"
        )
        return expl, severity  # no downgrade — this is a capacity-allocation issue

    if zero_periods:
        expl = (
            f"Low {util_word} utilization{pt_note} — no timesheet data for "
            f"{len(zero_periods)} of {len(run)} period(s); remaining periods show "
            f"no single dominant cause"
        )
        return expl, severity

    expl = f"Unexplained low {util_word} utilization{pt_note}"
    return expl, severity


def _downgrade(severity: Severity) -> Severity:
    return Severity.WARNING if severity == Severity.CRITICAL else Severity.WARNING


# ─────────────────────────────────────────────────────────────────────────────
# Trend 3: Excessive PTO
# ─────────────────────────────────────────────────────────────────────────────

def _trend_excessive_pto(person, period_stats_list, cfg, n, is_pt: bool) -> list[TrendFlag]:
    active = _active_periods(period_stats_list)
    # Suppress PTO flag when the analysis window is too short to distinguish
    # a vacation from a persistent pattern (configurable, default 3 periods).
    pto_min = cfg.get("pto_min_periods", 3)
    if len(active) < pto_min:
        return []
    if len(active) < n:
        return []

    warn_t = cfg["pto_warning_threshold_hours"]
    crit_t = cfg["pto_critical_threshold_hours"]
    flags = []

    for threshold, severity in [(crit_t, Severity.CRITICAL), (warn_t, Severity.WARNING)]:
        runs = _find_consecutive_runs(active, lambda ps, t=threshold: ps.pto_hours > t, n)
        for run in runs:
            if severity == Severity.WARNING:
                run_indices = {ps.period_index for ps in run}
                already = any(
                    f.trend_type == TrendType.EXCESSIVE_PTO
                    and run_indices.issubset(set(f.period_indices))
                    for f in flags
                )
                if already:
                    continue
            total_pto = sum(ps.pto_hours for ps in run)
            pt_note = " (PT employee)" if is_pt else ""
            expl = (
                f"Persistently elevated PTO{pt_note} — {_fmt_hrs(total_pto)} hrs "
                f"over {len(run)} consecutive periods"
            )
            flags.append(TrendFlag(
                trend_type=TrendType.EXCESSIVE_PTO,
                severity=severity,
                person=person,
                period_indices=[ps.period_index for ps in run],
                explanation=expl,
                per_period_metrics=[_period_metric(ps) for ps in run],
            ))

    return flags


# ─────────────────────────────────────────────────────────────────────────────
# Trend 4: High B&P
# ─────────────────────────────────────────────────────────────────────────────

def _trend_high_bp(person, period_stats_list, cfg, n, is_pt: bool) -> list[TrendFlag]:
    active = _active_periods(period_stats_list)
    if len(active) < n:
        return []

    bp_t = cfg["bp_ratio_threshold"]
    runs = _find_consecutive_runs(active, lambda ps: ps.bp_ratio > bp_t, n)
    flags = []
    for run in runs:
        avg_bp = sum(ps.bp_ratio for ps in run) / len(run)
        total_bp = sum(ps.bp_hours for ps in run)
        pt_note = " (PT employee)" if is_pt else ""
        expl = (
            f"Persistently high B&P hours{pt_note} — avg {_fmt_pct(avg_bp)} of available "
            f"({_fmt_hrs(total_bp)} hrs over {len(run)} periods)"
        )
        flags.append(TrendFlag(
            trend_type=TrendType.HIGH_BP,
            severity=Severity.WARNING,
            person=person,
            period_indices=[ps.period_index for ps in run],
            explanation=expl,
            per_period_metrics=[_period_metric(ps) for ps in run],
        ))
    return flags


# ─────────────────────────────────────────────────────────────────────────────
# Trend 5: High Non-Billable Project Hours
# ─────────────────────────────────────────────────────────────────────────────

def _trend_high_nonbillable(person, period_stats_list, cfg, n, is_pt: bool) -> list[TrendFlag]:
    active = _active_periods(period_stats_list)
    if len(active) < n:
        return []

    nb_t = cfg["nonbillable_threshold"]
    runs = _find_consecutive_runs(active, lambda ps: ps.nonbillable_ratio > nb_t, n)
    flags = []
    for run in runs:
        total_nb = sum(ps.nonbillable_hours for ps in run)
        avg_nb = sum(ps.nonbillable_ratio for ps in run) / len(run)
        pt_note = " (PT employee)" if is_pt else ""
        expl = (
            f"Persistently high non-billable project hours{pt_note} "
            f"({_fmt_hrs(total_nb)} hrs, {_fmt_pct(avg_nb)} of available) — "
            f"employee is on projects but hours are not billing"
        )
        flags.append(TrendFlag(
            trend_type=TrendType.HIGH_NONBILLABLE,
            severity=Severity.WARNING,
            person=person,
            period_indices=[ps.period_index for ps in run],
            explanation=expl,
            per_period_metrics=[_period_metric(ps) for ps in run],
        ))
    return flags


# ─────────────────────────────────────────────────────────────────────────────
# Trend 6: G&A Crowding Out Billable Work
# ─────────────────────────────────────────────────────────────────────────────

def _trend_ga_crowding(person, period_stats_list, cfg, n, is_pt: bool) -> list[TrendFlag]:
    active = _active_periods(period_stats_list)
    if len(active) < n:
        return []

    ga_t = cfg["ga_crowding_threshold"]
    bill_warn = cfg["billable_utilization_warning"]

    def condition(ps):
        return ps.ga_ratio > ga_t and ps.billable_utilization < bill_warn

    runs = _find_consecutive_runs(active, condition, n)
    flags = []
    for run in runs:
        avg_ga = sum(ps.ga_ratio for ps in run) / len(run)
        total_ga = sum(ps.ga_hours for ps in run)
        pt_note = " (PT employee)" if is_pt else ""
        expl = (
            f"G&A hours{pt_note} ({_fmt_hrs(total_ga)} hrs, avg {_fmt_pct(avg_ga)}) are "
            f"persistently crowding out billable work — employee has billable capacity "
            f"but is absorbing overhead"
        )
        flags.append(TrendFlag(
            trend_type=TrendType.GA_CROWDING,
            severity=Severity.WARNING,
            person=person,
            period_indices=[ps.period_index for ps in run],
            explanation=expl,
            per_period_metrics=[_period_metric(ps) for ps in run],
        ))
    return flags
