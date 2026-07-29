"""
Builds the four time-range views (Last Period, Last Month, Last Quarter, YTD).

For each view:
  - Filters employee period stats to the active date range
  - Scales the persistence threshold to match the window size
  - Runs trend detection and roll-ups on the filtered data
  - Returns a ready-to-render context dict
"""

from __future__ import annotations

from datetime import date
from typing import Any

VIEWS = [
    {
        "id":     "last_period",
        "label":  "Last Period",
        "sublabel": "Most recent half-month",
        "n_last": 1,
    },
    {
        "id":     "last_month",
        "label":  "Last Month",
        "sublabel": "Last 2 half-month periods",
        "n_last": 2,
    },
    {
        "id":     "last_quarter",
        "label":  "Last Quarter",
        "sublabel": "Last 6 half-month periods (~3 months)",
        "n_last": 6,
    },
    {
        "id":     "ytd",
        "label":  "Year to Date",
        "sublabel": "All available periods",
        "n_last": None,
    },
]


def scaled_persistence(n_periods: int, default: int) -> int:
    """Auto-scale persistence threshold to the selected window size."""
    if n_periods <= 1:
        return 1
    if n_periods <= 3:
        return 2
    return default


def build_all_views(
    data,
    emp_stats: dict,
    corp_roles: set,
    cfg: dict,
    generated_date: date,
) -> list[dict[str, Any]]:
    from .aggregator import build_rollups
    from .renderer import build_template_context
    from .trend_detector import detect_trends

    default_persistence = cfg.get("persistence_threshold", 3)
    all_periods = data.periods

    trend_eligible = {
        p for p in data.all_employees
        if p not in corp_roles and p not in data.excluded_no_pto
    }

    view_contexts = []

    for view_def in VIEWS:
        n_last = view_def["n_last"]

        # Select active periods (last N, or all)
        if n_last and n_last < len(all_periods):
            active_periods = all_periods[-n_last:]
        else:
            active_periods = all_periods

        active_indices = {p.index for p in active_periods}
        persistence = scaled_persistence(len(active_periods), default_persistence)
        view_cfg = {**cfg, "persistence_threshold": persistence}

        # Trend detection — filter each employee's period stats to the active window
        all_flags: dict = {}
        for person in trend_eligible:
            stats = emp_stats.get(person)
            if stats is None:
                continue
            prof = data.employee_profiles.get(person)
            filtered_ps = [ps for ps in stats.period_stats if ps.period_index in active_indices]
            if not filtered_ps:
                continue
            flags = detect_trends(
                person=person,
                period_stats_list=filtered_ps,
                cfg=view_cfg,
                is_pt=(prof.is_pt if prof else False),
                is_partial=(person in data.partial_period_employees),
                division=(prof.division if prof else ""),
            )
            if flags:
                all_flags[person] = flags

        # Roll-ups filtered to active periods
        portfolio_rollups, division_rollups = build_rollups(
            data, emp_stats, corp_roles, view_cfg,
            active_period_indices=active_indices,
        )

        # Renderer context with active period filter
        ctx = build_template_context(
            data=data,
            emp_stats=emp_stats,
            corp_roles=corp_roles,
            all_flags=all_flags,
            portfolio_rollups=portfolio_rollups,
            division_rollups=division_rollups,
            cfg=view_cfg,
            generated_date=generated_date,
            active_period_indices=active_indices,
        )

        period_range = (
            f"{active_periods[0].start} to {active_periods[-1].end}"
            if active_periods else ""
        )

        ctx.update({
            "view_id":           view_def["id"],
            "view_label":        view_def["label"],
            "view_sublabel":     view_def["sublabel"],
            "view_persistence":  persistence,
            "view_period_count": len(active_periods),
            "view_period_range": period_range,
        })

        view_contexts.append(ctx)

    return view_contexts
