"""
Buckets Export rows into half-month periods and computes available hours
per employee per period (with proration for partial-period employees and
variable tracking for PT employees).
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd

from .loader import LoadedData, Period


def assign_periods(data: LoadedData, cfg: dict) -> pd.DataFrame:
    """
    Returns a copy of export_df with an added 'period_index' column (1-based).
    Rows whose Date does not fall in any known period get period_index = NaN.
    """
    df = data.export_df.copy()
    date_col = cfg["columns"]["export"]["date"]

    if date_col not in df.columns:
        df["period_index"] = pd.NA
        return df

    periods = data.periods

    def _find_period(dt):
        if pd.isna(dt):
            return pd.NA
        # Normalize to date regardless of whether dt is datetime or date
        if hasattr(dt, "date") and callable(dt.date):
            d = dt.date()
        elif isinstance(dt, date):
            d = dt
        else:
            return pd.NA
        for p in periods:
            if p.start <= d <= p.end:
                return p.index
        return pd.NA

    df["period_index"] = df[date_col].map(_find_period)
    return df


def compute_available_hours(
    person: str,
    period: Period,
    data: LoadedData,
    cfg: dict,
    total_worked: float,
) -> float:
    """
    Returns the net available hours for one employee in one period.

    PT employees:   available = total hours worked (variable schedule).
    Partial-period: prorate using numpy.busday_count.
    Full FT:        period.net_hours as-is.
    """
    prof = data.employee_profiles.get(person)
    if prof is None:
        return period.net_hours

    # PT employees — available = what they worked
    if prof.is_pt:
        return total_worked

    # Check for partial period
    first_day = prof.first_day
    last_day = prof.last_day

    if first_day is None and last_day is None:
        return period.net_hours

    # Determine effective active window within this period
    emp_start = max(first_day, period.start) if first_day else period.start
    emp_end = min(last_day, period.end) if last_day else period.end

    # If the employee was not active during this period at all, return 0
    if emp_start > period.end or emp_end < period.start:
        return 0.0

    # Prorate by working days
    wd_active = _busday_count(emp_start, emp_end)
    wd_period = _busday_count(period.start, period.end)

    if wd_period == 0:
        return 0.0

    return (wd_active / wd_period) * period.net_hours


def _busday_count(start: date, end: date) -> int:
    """Count business days inclusive on both ends."""
    if start > end:
        return 0
    return int(np.busday_count(start, end + timedelta(days=1)))
