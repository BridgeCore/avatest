"""
Writes a compact, LLM-friendly summary of the current run for the
bcore-insights Skill to read — summary stats only, never row-level data, so
it stays small enough to fit cleanly in a Skill's context.

This is a projection over numbers other modules already computed
(src/cross_flagger.py's CrossFlags, src/root_cause.py's RootCauses,
src/workforce.py's WorkforceHealth) — no new math here.
"""

from __future__ import annotations

import json
from pathlib import Path

from .cross_flagger import CrossFlag

DEFAULT_EXPORT_PATH = Path("exports") / "insights_latest.json"


def _root_cause_to_dict(rc) -> dict:
    dc = rc.dominant_contract
    trend = []
    nb_cur = None
    if dc:
        trend = [
            {"period_label": t.period_label, "nonbillable_pct": t.nonbillable_pct}
            for t in dc.period_trend
        ]
        nb_cur = dc.period_trend[-1].nonbillable_pct if dc.period_trend else None

    return {
        "division":                 rc.division,
        "contract_title":           dc.project_title if dc else None,
        "contract_code":            dc.project_code  if dc else None,
        "contract_share_of_hours":  dc.share_of_hours if dc else None,
        "nonbillable_pct_current":  nb_cur,
        "nonbillable_pct_trend":    trend,
        "top_people": [
            {"person": p.person, "nonbillable_hours": p.nonbillable_hours}
            for p in rc.top_people
        ],
        "narrative": rc.narrative,
    }


def _workforce_to_dict(wf) -> dict:
    return {
        "headcount_current":    wf.headcount_current,
        "hires_ytd":            wf.hires_ytd,
        "departures_ytd":       wf.departures_ytd,
        "net_change_ytd":       wf.net_change_ytd,
        "high_pto_liability": [
            {"person": p.person, "hours_available": p.hours_available}
            for p in wf.high_pto_liability
        ],
        "negative_pto_balances": [
            {"person": p.person, "hours_available": p.hours_available}
            for p in wf.negative_pto_balances
        ],
    }


def build_insights(
    gm_data,
    forecasts: dict | None,
    util_bundle: tuple | None,
    views: list | None,
    cross_flags: list[CrossFlag],
    cfg: dict,
    generated_at: str,
    root_causes: list | None = None,
    workforce=None,
) -> dict:
    periods_covered: list[str] = []
    data_quality_flags: list[dict] = []

    if util_bundle is not None:
        data, corp_roles, _views = util_bundle
        periods_covered = [p.label for p in data.periods]
        for person in sorted(data.excluded_no_pto):
            data_quality_flags.append({
                "type": "excluded_no_pto",
                "detail": f"{person} has timesheet entries but no PTO record",
            })
        for person in sorted(data.flagged_no_timesheet):
            data_quality_flags.append({
                "type": "flagged_no_timesheet",
                "detail": f"{person} has a PTO record but no timesheet entries",
            })
        for combo in data.missing_lookup_combos:
            data_quality_flags.append({"type": "missing_lookup_combo", "detail": combo})
    elif gm_data is not None:
        periods_covered = [m.label for m in gm_data.months]

    forecasts = forecasts or {}

    divisions = []
    for flag in cross_flags:
        low_revenue_flag = None
        if flag.low_revenue_mtd is not None or flag.low_revenue_ytd is not None:
            low_revenue_flag = bool(flag.low_revenue_mtd) or bool(flag.low_revenue_ytd)
        forecast = forecasts.get(flag.division)
        divisions.append({
            "name": flag.division,
            "avg_utilization": flag.avg_billable_util,
            "mtd_revenue_variance_pct": flag.mtd_variance_pct,
            "ytd_revenue_variance_pct": flag.ytd_variance_pct,
            "low_utilization_flag": flag.low_utilization,
            "low_revenue_flag": low_revenue_flag,
            "combined_flag": flag.severity == "critical",
            "severity": flag.severity,
            "reason": flag.reason,
            "annual_trend_direction": forecast.trend_direction if forecast else None,
        })

    result: dict = {
        "generated_at":       generated_at,
        "periods_covered":    periods_covered,
        "divisions":          divisions,
        "data_quality_flags": data_quality_flags,
    }

    if root_causes:
        result["root_causes"] = [_root_cause_to_dict(rc) for rc in root_causes]

    if workforce is not None:
        result["workforce"] = _workforce_to_dict(workforce)

    return result


def write_insights(insights: dict, path: Path = DEFAULT_EXPORT_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(insights, indent=2, default=str), encoding="utf-8")
