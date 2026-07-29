"""
Computes portfolio-lead and division roll-ups.
Corporate-role employees are excluded from group utilization calculations.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .calculator import EmployeeStats, PeriodStats
from .loader import LoadedData, Period
from .trend_detector import TrendFlag, detect_trends


@dataclass
class GroupPeriodStats:
    period_index: int
    period: Period
    employee_count: int = 0
    billable_hours: float = 0.0
    nonbillable_hours: float = 0.0
    net_available: float = 0.0
    bp_hours: float = 0.0
    ga_hours: float = 0.0
    ird_hours: float = 0.0
    overhead_hours: float = 0.0
    pto_hours: float = 0.0
    holiday_hours: float = 0.0
    lwop_hours: float = 0.0
    other_hours: float = 0.0

    @property
    def time_off_hours(self) -> float:
        return self.pto_hours + self.holiday_hours + self.lwop_hours + self.other_hours

    @property
    def effective_available(self) -> float:
        return max(0.0, self.net_available - self.time_off_hours)

    def _denom(self) -> float:
        return self.effective_available if self.effective_available > 0 else self.net_available

    @property
    def billable_utilization(self) -> float:
        d = self._denom()
        return self.billable_hours / d if d > 0 else 0.0

    @property
    def direct_utilization(self) -> float:
        d = self._denom()
        return (self.billable_hours + self.nonbillable_hours) / d if d > 0 else 0.0

    @property
    def nonbillable_ratio(self) -> float:
        d = self._denom()
        return self.nonbillable_hours / d if d > 0 else 0.0

    @property
    def ga_ratio(self) -> float:
        d = self._denom()
        return self.ga_hours / d if d > 0 else 0.0

    @property
    def bp_ratio(self) -> float:
        d = self._denom()
        return self.bp_hours / d if d > 0 else 0.0

    @property
    def ird_ratio(self) -> float:
        d = self._denom()
        return self.ird_hours / d if d > 0 else 0.0

    @property
    def overhead_ratio(self) -> float:
        d = self._denom()
        return self.overhead_hours / d if d > 0 else 0.0


@dataclass
class GroupStats:
    group_key: str         # portfolio lead name or division code
    group_type: str        # "portfolio" or "division"
    member_count: int = 0
    period_stats: list[GroupPeriodStats] = field(default_factory=list)
    trend_flags: list[TrendFlag] = field(default_factory=list)

    @property
    def avg_billable_utilization(self) -> float:
        active = [p for p in self.period_stats if p.net_available > 0]
        if not active:
            return 0.0
        return sum(p.billable_utilization for p in active) / len(active)

    @property
    def avg_direct_utilization(self) -> float:
        active = [p for p in self.period_stats if p.net_available > 0]
        if not active:
            return 0.0
        return sum(p.direct_utilization for p in active) / len(active)

    @property
    def critical_count(self) -> int:
        from .trend_detector import Severity
        return sum(1 for f in self.trend_flags if f.severity == Severity.CRITICAL)

    @property
    def warning_count(self) -> int:
        from .trend_detector import Severity
        return sum(1 for f in self.trend_flags if f.severity == Severity.WARNING)


def build_rollups(
    data: LoadedData,
    emp_stats: dict[str, EmployeeStats],
    corp_roles: set[str],
    cfg: dict,
    active_period_indices: set[int] | None = None,
) -> tuple[dict[str, GroupStats], dict[str, GroupStats]]:
    """
    Returns (portfolio_rollups, division_rollups).
    Both dicts are keyed by group name.
    When active_period_indices is provided, only those periods are aggregated.
    """
    periods = data.periods
    profiles = data.employee_profiles

    # Eligible employees: not in corp_roles, not excluded
    eligible = {
        p: s for p, s in emp_stats.items()
        if p not in corp_roles and p not in data.excluded_no_pto
    }

    portfolio_rollups = _build_group(eligible, periods, profiles, "portfolio_lead", cfg, active_period_indices)
    division_rollups = _build_group(eligible, periods, profiles, "division", cfg, active_period_indices)

    return portfolio_rollups, division_rollups


def _build_group(
    eligible: dict[str, EmployeeStats],
    periods: list[Period],
    profiles,
    attr: str,  # "portfolio_lead" or "division"
    cfg: dict,
    active_period_indices: set[int] | None = None,
) -> dict[str, GroupStats]:
    # Filter periods to the active window when provided
    if active_period_indices is not None:
        periods = [p for p in periods if p.index in active_period_indices]

    # Gather members per group
    group_members: dict[str, list[str]] = {}
    for person, _ in eligible.items():
        prof = profiles.get(person)
        if prof is None:
            continue
        key = getattr(prof, attr, "") or "Unknown"
        if not key:
            key = "Unknown"
        group_members.setdefault(key, []).append(person)

    results: dict[str, GroupStats] = {}
    for group_key, members in group_members.items():
        gs = GroupStats(
            group_key=group_key,
            group_type="portfolio" if attr == "portfolio_lead" else "division",
            member_count=len(members),
        )

        for period in periods:
            pi = period.index
            gps = GroupPeriodStats(period_index=pi, period=period)

            for person in members:
                emp = eligible.get(person)
                if emp is None:
                    continue
                ps = next((s for s in emp.period_stats if s.period_index == pi), None)
                if ps is None:
                    continue
                if ps.net_available > 0:
                    gps.employee_count += 1
                gps.billable_hours += ps.billable_hours
                gps.nonbillable_hours += ps.nonbillable_hours
                gps.net_available += ps.net_available
                gps.bp_hours += ps.bp_hours
                gps.ga_hours += ps.ga_hours
                gps.ird_hours += ps.ird_hours
                gps.overhead_hours += ps.overhead_hours
                gps.pto_hours += ps.pto_hours
                gps.holiday_hours += ps.holiday_hours
                gps.lwop_hours += ps.lwop_hours
                gps.other_hours += ps.other_hours

            gs.period_stats.append(gps)

        # Run trend detection on group aggregate using same thresholds
        gs.trend_flags = detect_trends(
            person=group_key,
            period_stats_list=gs.period_stats,
            cfg=cfg,
            is_pt=False,
            is_partial=False,
        )

        results[group_key] = gs

    return results
