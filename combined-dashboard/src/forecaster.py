"""
Pure Python/numpy trend math — no LLM calls here. This module computes the
actual numbers (linear revenue/GM projections, trend-flag summaries); the
LLM in src/ai_commentary.py only narrates what this module already decided.

Keeping numeric extrapolation entirely in Python (verifiable, testable
without mocking a subprocess) rather than asking the LLM to compute figures
is a deliberate choice for a financial dashboard.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .gm_calculator import _aop_for
from .gm_loader import GMData

MIN_MONTHS_TO_FORECAST = 3


@dataclass
class DivisionForecast:
    division: str
    historical_monthly: list = field(default_factory=list)  # [(label, revenue, gm_pct|None)]
    projected_yearend_revenue: float | None = None
    projected_yearend_gm: float | None = None
    trend_direction: str = "flat"  # "up" | "down" | "flat"
    variance_vs_aop_annual: float | None = None


def _monthly_series(data: GMData, div: str) -> list:
    acts = data.actuals if div == "ALL" else [a for a in data.actuals if a.division == div]
    series = []
    for m in data.months:
        rows = [a for a in acts if a.month.key == m.key]
        rev = sum(a.revenue for a in rows)
        gm = sum(a.gross_margin for a in rows)
        gm_pct = (gm / rev * 100) if rev else None
        series.append((m.label, rev, gm))
    return series


def _project_annual_total(idxs: list[int], values: list[float]) -> tuple[float, str]:
    """Sums reported months as-is, extrapolates unreported months via a linear
    fit on (month-of-year index -> value), returns (annual_total, direction)."""
    xs = np.array(idxs, dtype=float)
    ys = np.array(values, dtype=float)
    slope, intercept = np.polyfit(xs, ys, 1)

    reported = set(idxs)
    total = float(sum(values))
    for i in range(12):
        if i not in reported:
            total += slope * i + intercept

    mean_abs = np.mean(np.abs(ys)) or 1.0
    if slope > mean_abs * 0.02:
        direction = "up"
    elif slope < -mean_abs * 0.02:
        direction = "down"
    else:
        direction = "flat"
    return total, direction


def project_gm_series(data: GMData, cfg: dict) -> dict[str, DivisionForecast]:
    """One DivisionForecast per "ALL" + each configured division.

    Forecasting (projected_yearend_*, variance_vs_aop_annual) is skipped —
    historical_monthly is still populated — when fewer than
    MIN_MONTHS_TO_FORECAST months of actuals exist, since a 1-2 point linear
    fit has no real predictive value and would just fabricate false
    confidence (same philosophy as trend_detector.py's pto_min_periods).
    """
    divisions = cfg["gm"]["divisions"]
    result: dict[str, DivisionForecast] = {}

    for div in ["ALL"] + divisions:
        monthly = _monthly_series(data, div)
        forecast = DivisionForecast(
            division=div,
            historical_monthly=[(lbl, rev, (gm / rev * 100) if rev else None) for lbl, rev, gm in monthly],
        )

        if len(monthly) >= MIN_MONTHS_TO_FORECAST:
            idxs = [m.idx for m in data.months]
            revs = [v[1] for v in monthly]
            gms = [v[2] for v in monthly]

            proj_rev, direction = _project_annual_total(idxs, revs)
            proj_gm, _ = _project_annual_total(idxs, gms)
            forecast.projected_yearend_revenue = proj_rev
            forecast.projected_yearend_gm = proj_gm
            forecast.trend_direction = direction

            aop_entry = _aop_for(data, div, divisions)
            if aop_entry is not None:
                annual_aop_revenue = sum(aop_entry.revenue)
                forecast.variance_vs_aop_annual = proj_rev - annual_aop_revenue

        result[div] = forecast

    return result


def summarize_utilization_trends(views: list, cfg: dict) -> dict:
    """Compacts already-computed view data (view_builder.py/trend_detector.py)
    into a small payload — no new trend math, purely selection/aggregation
    over data that already exists in each view context.
    """
    if not views:
        return {}

    max_flags = cfg.get("ai_commentary", {}).get("max_flags_per_view", 8)
    ytd = views[-1]
    last_quarter = next((v for v in views if v.get("view_id") == "last_quarter"), None)

    def _view_summary(view: dict) -> dict:
        crit = view.get("critical_flags", [])
        warn = view.get("warning_flags", [])
        top_flags = crit[:max_flags] + warn[:max(0, max_flags - len(crit))]
        return {
            "view_label": view.get("view_label"),
            "period_range": view.get("view_period_range"),
            "critical_count": len(crit),
            "warning_count": len(warn),
            "division_rows": view.get("division_rows", []),
            "portfolio_rows": view.get("portfolio_rows", []),
            "top_flags": [
                {
                    "person": f["person"],
                    "division": f["division"],
                    "trend_type": f["trend_type"],
                    "explanation": f["explanation"],
                }
                for f in top_flags[:max_flags]
            ],
        }

    summary = {"year_to_date": _view_summary(ytd)}
    if last_quarter is not None:
        summary["last_quarter"] = _view_summary(last_quarter)
    return summary
