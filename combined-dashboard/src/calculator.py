"""
Aggregates Export rows into per-employee × per-period hour buckets
and computes all utilization rates.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from .loader import LoadedData, Period
from .periodizer import assign_periods, compute_available_hours


@dataclass
class PeriodStats:
    period_index: int
    period: Period
    billable_hours: float = 0.0
    nonbillable_hours: float = 0.0
    bp_hours: float = 0.0
    ga_hours: float = 0.0
    ird_hours: float = 0.0
    overhead_hours: float = 0.0
    pto_hours: float = 0.0
    holiday_hours: float = 0.0
    lwop_hours: float = 0.0
    other_hours: float = 0.0
    total_worked: float = 0.0
    net_available: float = 0.0

    @property
    def indirect_hours(self) -> float:
        return self.bp_hours + self.ga_hours + self.ird_hours + self.overhead_hours

    @property
    def time_off_hours(self) -> float:
        return self.pto_hours + self.holiday_hours + self.lwop_hours + self.other_hours

    @property
    def effective_available(self) -> float:
        """Net available hours minus documented time-off (PTO, Holiday, LWOP, Other).
        This is the denominator for utilization — hours the employee was actually
        expected to be working and available to bill."""
        return max(0.0, self.net_available - self.time_off_hours)

    def _denom(self) -> float:
        """Use effective_available as denominator; fall back to net_available if zero."""
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
class EmployeeStats:
    person: str
    period_stats: list[PeriodStats] = field(default_factory=list)

    @property
    def total_billable(self) -> float:
        return sum(p.billable_hours for p in self.period_stats)

    @property
    def total_nonbillable(self) -> float:
        return sum(p.nonbillable_hours for p in self.period_stats)

    @property
    def total_bp(self) -> float:
        return sum(p.bp_hours for p in self.period_stats)

    @property
    def total_ga(self) -> float:
        return sum(p.ga_hours for p in self.period_stats)

    @property
    def total_pto(self) -> float:
        return sum(p.pto_hours for p in self.period_stats)

    @property
    def total_lwop(self) -> float:
        return sum(p.lwop_hours for p in self.period_stats)

    @property
    def total_available(self) -> float:
        return sum(p.net_available for p in self.period_stats)

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
    def avg_nonbillable_ratio(self) -> float:
        active = [p for p in self.period_stats if p.net_available > 0]
        if not active:
            return 0.0
        return sum(p.nonbillable_ratio for p in active) / len(active)

    @property
    def avg_ga_ratio(self) -> float:
        active = [p for p in self.period_stats if p.net_available > 0]
        if not active:
            return 0.0
        return sum(p.ga_ratio for p in active) / len(active)

    @property
    def periods_active(self) -> int:
        return sum(1 for p in self.period_stats if p.net_available > 0 or p.total_worked > 0)


def compute_all(
    data: LoadedData,
    cfg: dict,
    corp_roles: set[str],
) -> dict[str, EmployeeStats]:
    """
    Returns EmployeeStats for every employee in the roster
    (excluding employees not in Unique Employees).
    """
    labels = cfg["subgroup_labels"]
    sg_col = cfg["columns"]["export"]["project_subgroup"]
    h_col = cfg["columns"]["export"]["hours"]
    p_col = cfg["columns"]["export"]["person"]

    # Assign period index to every Export row
    df = assign_periods(data, cfg)

    # Build per-(person, period) aggregated sums
    df_valid = df.dropna(subset=["period_index"]).copy()
    df_valid["period_index"] = df_valid["period_index"].astype(int)

    results: dict[str, EmployeeStats] = {}

    for person in data.all_employees:
        emp_df = df_valid[df_valid[p_col] == person]

        stats = EmployeeStats(person=person)

        for period in data.periods:
            pi = period.index
            period_df = emp_df[emp_df["period_index"] == pi]

            def _sum(sg_value: str) -> float:
                mask = period_df[sg_col] == sg_value
                return float(period_df.loc[mask, h_col].sum())

            billable = _sum(labels["billable"])
            nonbillable = _sum(labels["nonbillable"])
            bp = _sum(labels["bp"])
            ga = _sum(labels["ga"])
            ird = _sum(labels["ird"])
            overhead = _sum(labels["overhead"])
            pto = _sum(labels["pto"])
            holiday = _sum(labels["holiday"])
            lwop = _sum(labels["lwop"])
            other = _sum(labels["other"])
            total_worked = float(period_df[h_col].sum()) if not period_df.empty else 0.0

            net_available = compute_available_hours(
                person=person,
                period=period,
                data=data,
                cfg=cfg,
                total_worked=total_worked,
            )

            ps = PeriodStats(
                period_index=pi,
                period=period,
                billable_hours=billable,
                nonbillable_hours=nonbillable,
                bp_hours=bp,
                ga_hours=ga,
                ird_hours=ird,
                overhead_hours=overhead,
                pto_hours=pto,
                holiday_hours=holiday,
                lwop_hours=lwop,
                other_hours=other,
                total_worked=total_worked,
                net_available=net_available,
            )
            stats.period_stats.append(ps)

        results[person] = stats

    return results
