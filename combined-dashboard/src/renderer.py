"""
Renders the self-contained HTML dashboard using Jinja2.
All CSS and JS are inlined. No external CDN dependencies.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from .aggregator import GroupStats
from .ai_commentary import generate_gm_commentary, generate_utilization_commentary
from .calculator import EmployeeStats
from .forecaster import project_gm_series
from .gm_calculator import build_gm_context
from .gm_loader import GMData
from .loader import LoadedData
from .trend_detector import TrendFlag, Severity, TrendType

# summarize_utilization_trends() keys its output by this view_id -> summary-key map.
_UTIL_COMMENTARY_VIEW_KEYS = {"ytd": "year_to_date", "last_quarter": "last_quarter"}


def _pct(val: float, decimals: int = 1) -> str:
    return f"{val * 100:.{decimals}f}%"


def _hrs(val: float) -> str:
    return f"{val:.1f}"


def _sparkline_data(emp_stats: EmployeeStats) -> list[dict]:
    """Always uses all periods for full-trend sparkline context."""
    return [
        {
            "period_label": ps.period.label,
            "billable_util": round(ps.billable_utilization, 4),
            "net_available": round(ps.net_available, 1),
        }
        for ps in emp_stats.period_stats
    ]


def _group_sparkline_data(gs: GroupStats) -> list[dict]:
    return [
        {
            "period_label": ps.period.label,
            "billable_util": round(ps.billable_utilization, 4),
            "net_available": round(ps.net_available, 1),
        }
        for ps in gs.period_stats
    ]


def _emp_detail_periods(emp: EmployeeStats, active_indices: set[int] | None) -> list[dict]:
    """Full per-period breakdown for the employee detail modal."""
    if active_indices is None:
        periods = emp.period_stats
    else:
        periods = [ps for ps in emp.period_stats if ps.period_index in active_indices]

    result = []
    for ps in periods:
        result.append({
            "period_label":      ps.period.label,
            "period_index":      ps.period_index,
            "billable_hrs":      round(ps.billable_hours, 1),
            "nonbillable_hrs":   round(ps.nonbillable_hours, 1),
            "ga_hrs":            round(ps.ga_hours, 1),
            "bp_hrs":            round(ps.bp_hours, 1),
            "ird_hrs":           round(ps.ird_hours, 1),
            "overhead_hrs":      round(ps.overhead_hours, 1),
            "pto_hrs":           round(ps.pto_hours, 1),
            "holiday_hrs":       round(ps.holiday_hours, 1),
            "lwop_hrs":          round(ps.lwop_hours, 1),
            "other_hrs":         round(ps.other_hours, 1),
            "time_off_hrs":      round(ps.time_off_hours, 1),
            "net_available":     round(ps.net_available, 1),
            "effective_avail":   round(ps.effective_available, 1),
            "billable_util":     round(ps.billable_utilization * 100, 1),
            "direct_util":       round(ps.direct_utilization * 100, 1),
            "nonbillable_pct":   round(ps.nonbillable_ratio * 100, 1),
            "ga_pct":            round(ps.ga_ratio * 100, 1),
            "bp_pct":            round(ps.bp_ratio * 100, 1),
        })
    return result


def _emp_filtered_metrics(emp: EmployeeStats, active_indices: set[int] | None) -> dict:
    """Compute view-specific averages filtered to active period indices."""
    if active_indices is None:
        ps_all = emp.period_stats
        ps_active = [ps for ps in ps_all if ps.net_available > 0]
    else:
        ps_all = [ps for ps in emp.period_stats if ps.period_index in active_indices]
        ps_active = [ps for ps in ps_all if ps.net_available > 0]

    n = len(ps_active)
    if n == 0:
        return {
            "avg_billable_util":     _pct(0),
            "avg_direct_util":       _pct(0),
            "avg_nonbillable_pct":   _pct(0),
            "avg_ga_pct":            _pct(0),
            "total_bp_hrs":          _hrs(0),
            "total_pto_hrs":         _hrs(0),
            "total_lwop_hrs":        _hrs(0),
            "periods_active":        0,
            "avg_billable_util_raw": 0.0,
            "avg_direct_util_raw":   0.0,
        }

    avg_bu = sum(ps.billable_utilization for ps in ps_active) / n
    avg_du = sum(ps.direct_utilization for ps in ps_active) / n

    return {
        "avg_billable_util":     _pct(avg_bu),
        "avg_direct_util":       _pct(avg_du),
        "avg_nonbillable_pct":   _pct(sum(ps.nonbillable_ratio for ps in ps_active) / n),
        "avg_ga_pct":            _pct(sum(ps.ga_ratio for ps in ps_active) / n),
        "total_bp_hrs":          _hrs(sum(ps.bp_hours for ps in ps_all)),
        "total_pto_hrs":         _hrs(sum(ps.pto_hours for ps in ps_all)),
        "total_lwop_hrs":        _hrs(sum(ps.lwop_hours for ps in ps_all)),
        "periods_active":        n,
        "avg_billable_util_raw": avg_bu,
        "avg_direct_util_raw":   avg_du,
    }


def build_template_context(
    data: LoadedData,
    emp_stats: dict[str, EmployeeStats],
    corp_roles: set[str],
    all_flags: dict[str, list[TrendFlag]],
    portfolio_rollups: dict[str, GroupStats],
    division_rollups: dict[str, GroupStats],
    cfg: dict,
    generated_date: date,
    active_period_indices: set[int] | None = None,
) -> dict:
    profiles = data.employee_profiles

    # ── Flags — one card per person, consolidating all their flag types ────────
    critical_flags = []
    warning_flags = []
    for person, flags in all_flags.items():
        if not flags:
            continue
        prof = profiles.get(person)
        has_critical = any(f.severity == Severity.CRITICAL for f in flags)

        # Merge LOW_BILLABLE + HIGH_NONBILLABLE when both present — they describe
        # the same condition (non-billable hours crowding out billable work).
        lb_flags = [f for f in flags if f.trend_type == TrendType.LOW_BILLABLE]
        nb_flags = [f for f in flags if f.trend_type == TrendType.HIGH_NONBILLABLE]
        merged_ids: set[int] = set()
        merged_issues: list[dict] = []
        if lb_flags and nb_flags:
            lb, nb = lb_flags[0], nb_flags[0]
            merged_ids = {id(lb), id(nb)}
            merged_sev = lb.severity if lb.severity == Severity.CRITICAL else nb.severity
            merged_period_indices = sorted(set(lb.period_indices) | set(nb.period_indices))
            merged_issues.append({
                "trend_label": "Low Billable / High Non-Billable",
                "explanation": lb.explanation,
                "period_labels": [
                    data.periods[i - 1].label if 0 < i <= len(data.periods) else f"P{i}"
                    for i in merged_period_indices
                ],
                "per_period_metrics": lb.per_period_metrics,
                "severity": merged_sev.value,
            })

        # Build the full issues list: merged entry first, then any remaining flags
        remaining = sorted(
            [f for f in flags if id(f) not in merged_ids],
            key=lambda f: f.severity == Severity.CRITICAL,
            reverse=True,
        )
        issues = merged_issues + [
            {
                "trend_label": f.label,
                "explanation": f.explanation,
                "period_labels": [
                    data.periods[i - 1].label if 0 < i <= len(data.periods) else f"P{i}"
                    for i in f.period_indices
                ],
                "per_period_metrics": f.per_period_metrics,
                "severity": f.severity.value,
            }
            for f in remaining
        ]

        primary_label = issues[0]["trend_label"] if issues else flags[0].label
        card = {
            "person": person,
            "division": prof.division if prof else "",
            "portfolio_lead": prof.portfolio_lead if prof else "",
            "is_pt": prof.is_pt if prof else False,
            "is_partial": person in data.partial_period_employees,
            "trend_label": primary_label,
            "issues": issues,
        }
        if has_critical:
            critical_flags.append(card)
        else:
            warning_flags.append(card)

    # ── Employee table rows ───────────────────────────────────────────────────
    table_rows = []
    for person in data.all_employees:
        if person in data.excluded_no_pto:
            continue
        emp = emp_stats.get(person)
        prof = profiles.get(person)
        if emp is None or prof is None:
            continue

        flags = all_flags.get(person, [])
        has_critical = any(f.severity == Severity.CRITICAL for f in flags)
        has_warning = any(f.severity == Severity.WARNING for f in flags)
        is_corp = person in corp_roles
        is_pt = prof.is_pt
        is_partial = person in data.partial_period_employees

        if has_critical:
            row_color = "critical"
        elif has_warning:
            row_color = "warning"
        elif is_corp or is_partial:
            row_color = "gray"
        else:
            row_color = "ok"

        flag_types = list({f.trend_type.value for f in flags})
        metrics = _emp_filtered_metrics(emp, active_period_indices)

        table_rows.append({
            "person": person,
            "person_org": prof.person_org,
            "division": prof.division,
            "portfolio_lead": prof.portfolio_lead,
            "project_type": prof.primary_contract_type,
            "is_pt": is_pt,
            "is_partial": is_partial,
            "is_corp": is_corp,
            "row_color": row_color,
            "flag_status": "Critical" if has_critical else ("Warning" if has_warning else "OK"),
            "flag_types": flag_types,
            "sparkline": json.dumps(_sparkline_data(emp)),
            "detail_periods": json.dumps(_emp_detail_periods(emp, active_period_indices)),
            **metrics,
        })

    # ── Portfolio table ────────────────────────────────────────────────────────
    portfolio_rows = []
    for key, gs in sorted(portfolio_rollups.items()):
        critical_count = sum(1 for f in gs.trend_flags if f.severity == Severity.CRITICAL)
        warning_count = sum(1 for f in gs.trend_flags if f.severity == Severity.WARNING)
        portfolio_rows.append({
            "lead": key,
            "member_count": gs.member_count,
            "avg_billable_util": _pct(gs.avg_billable_utilization),
            "avg_direct_util": _pct(gs.avg_direct_utilization),
            "critical_count": critical_count,
            "warning_count": warning_count,
            "sparkline": json.dumps(_group_sparkline_data(gs)),
        })

    # ── Team groups (division-first view; replaces the flat employee table) ────
    _flag_rank = {"critical": 0, "warning": 1, "ok": 2, "gray": 3}
    _groups: dict[str, list[dict]] = {}
    for row in table_rows:
        _groups.setdefault(row["division"] or "Unknown", []).append(row)

    team_groups = []
    for division, rows in _groups.items():
        rows_sorted = sorted(rows, key=lambda r: (_flag_rank.get(r["row_color"], 4), r["person"]))
        active_rows = [r for r in rows if r["periods_active"] > 0]
        n = len(active_rows)
        avg_bu = sum(r["avg_billable_util_raw"] for r in active_rows) / n if n else 0.0
        avg_du = sum(r["avg_direct_util_raw"] for r in active_rows) / n if n else 0.0
        critical_count = sum(1 for r in rows if r["flag_status"] == "Critical")
        warning_count = sum(1 for r in rows if r["flag_status"] == "Warning")
        team_groups.append({
            "division": division,
            "member_count": len(rows),
            "critical_count": critical_count,
            "warning_count": warning_count,
            "avg_billable_util": _pct(avg_bu),
            "avg_direct_util": _pct(avg_du),
            "rows": rows_sorted,
        })
    team_groups.sort(key=lambda g: (-g["critical_count"], -g["warning_count"], g["division"]))

    # ── Division table ─────────────────────────────────────────────────────────
    division_rows = []
    for key, gs in sorted(division_rollups.items()):
        critical_count = sum(1 for f in gs.trend_flags if f.severity == Severity.CRITICAL)
        warning_count = sum(1 for f in gs.trend_flags if f.severity == Severity.WARNING)
        division_rows.append({
            "division": key,
            "member_count": gs.member_count,
            "avg_billable_util": _pct(gs.avg_billable_utilization),
            "avg_direct_util": _pct(gs.avg_direct_utilization),
            "critical_count": critical_count,
            "warning_count": warning_count,
            "sparkline": json.dumps(_group_sparkline_data(gs)),
        })

    return {
        "report_title": "BCore Billable Utilization Dashboard",
        "period_range": f"{data.report_start} to {data.report_end}" if data.report_start and data.report_end else "2026",
        "generated_date": str(generated_date),
        "pipeline_summary": data.pipeline_summary,

        # Summary cards
        "employees_analyzed": len([p for p in data.all_employees if p not in data.excluded_no_pto]),
        "corp_roles_count": len(corp_roles),
        "pt_count": len(data.pt_employees),
        "critical_count": len(critical_flags),
        "warning_count": len(warning_flags),
        "data_quality_issues": len(data.excluded_no_pto) + len(data.flagged_no_timesheet),

        # Data quality
        "excluded_no_pto": sorted(data.excluded_no_pto),
        "flagged_no_timesheet": sorted(data.flagged_no_timesheet),
        "missing_lookup_combos": data.missing_lookup_combos,
        "pt_employees": sorted(data.pt_employees),
        "corp_roles": sorted(corp_roles),
        "partial_period_employees": sorted(data.partial_period_employees),

        # Flags
        "critical_flags": critical_flags,
        "warning_flags": warning_flags,

        # Tables
        "table_rows": table_rows,
        "team_groups": team_groups,
        "portfolio_rows": portfolio_rows,
        "division_rows": division_rows,

        # Config display
        "cfg": cfg,

        # Period labels for sparkline x-axis
        "period_labels": [p.label for p in data.periods],

        # Thresholds for JS sparklines
        "billable_warning_threshold": cfg["billable_utilization_warning"],
        "billable_critical_threshold": cfg["billable_utilization_critical"],
        "billable_benchmark": cfg["billable_utilization_benchmark"],
    }


def build_combined_context(
    data: LoadedData,
    corp_roles: set[str],
    views: list[dict],
    cfg: dict,
) -> dict:
    """
    Merge per-view contexts into a single template context.
    Shared (data-quality, thresholds) comes from the YTD view (last in list).
    Each view dict contains its own flags, table rows, and summaries.
    """
    ytd = views[-1]  # Year-to-date has the most complete shared data
    return {
        "report_title":    ytd["report_title"],
        "generated_date":  ytd["generated_date"],
        "pipeline_summary": ytd["pipeline_summary"],
        "period_range":    ytd["period_range"],   # full year range in header

        # Summary card totals (YTD as headline)
        "employees_analyzed": ytd["employees_analyzed"],
        "corp_roles_count":   ytd["corp_roles_count"],
        "pt_count":           ytd["pt_count"],
        "data_quality_issues": ytd["data_quality_issues"],

        # Data quality (same for all views)
        "excluded_no_pto":          ytd["excluded_no_pto"],
        "flagged_no_timesheet":     ytd["flagged_no_timesheet"],
        "missing_lookup_combos":    ytd["missing_lookup_combos"],
        "pt_employees":             ytd["pt_employees"],
        "corp_roles":               ytd["corp_roles"],
        "partial_period_employees": ytd["partial_period_employees"],

        # Shared thresholds for sparkline rendering
        "period_labels":             ytd["period_labels"],
        "billable_warning_threshold": ytd["billable_warning_threshold"],
        "billable_critical_threshold": ytd["billable_critical_threshold"],
        "billable_benchmark":         ytd["billable_benchmark"],

        # Config (shared)
        "cfg": ytd["cfg"],

        # All 4 view contexts for the tab panels
        "views": views,
    }


def _make_env(template_dir: Path) -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(template_dir)),
        autoescape=True,
    )
    env.filters["pct"] = lambda v: f"{float(v) * 100:.1f}%"
    env.filters["hrs"] = lambda v: f"{float(v):.1f}"
    return env


def _render_template(template_name: str, context: dict, template_dir: Path) -> str:
    env = _make_env(template_dir)
    return env.get_template(template_name).render(**context)


def _build_division_health_ctx(cross_flags: list, root_causes: list | None) -> list[dict]:
    """Merge cross_flags and root_causes into one list for the Division Health template."""
    rc_by_div = {rc.division: rc for rc in (root_causes or [])}
    severity_rank = {"critical": 0, "warning": 1, "none": 2}
    result = []
    for flag in sorted(cross_flags or [], key=lambda f: (severity_rank.get(f.severity, 3), f.division)):
        rc = rc_by_div.get(flag.division)
        dc = rc.dominant_contract if rc else None
        entry = {
            "division":          flag.division,
            "severity":          flag.severity,
            "avg_billable_util": flag.avg_billable_util,
            "mtd_variance_pct":  flag.mtd_variance_pct,
            "ytd_variance_pct":  flag.ytd_variance_pct,
            "reason":            flag.reason,
            "narrative":         rc.narrative if rc else None,
            "contract_code":     dc.project_code  if dc else None,
            "contract_title":    dc.project_title if dc else None,
            "contract_share":    dc.share_of_hours if dc else None,
            "nb_pct_current": (
                dc.period_trend[-1].nonbillable_pct
                if dc and dc.period_trend else None
            ),
            "nb_pct_trend": [
                {"label": t.period_label, "pct": round(t.nonbillable_pct * 100, 1)}
                for t in (dc.period_trend if dc else [])
            ],
            "top_people": [
                {"person": p.person, "hours": p.nonbillable_hours}
                for p in (rc.top_people if rc else [])
            ],
        }
        result.append(entry)
    return result


def _workforce_to_ctx(wf) -> dict | None:
    if wf is None:
        return None
    return {
        "headcount_current":    wf.headcount_current,
        "hires_ytd":            wf.hires_ytd,
        "departures_ytd":       wf.departures_ytd,
        "net_change_ytd":       wf.net_change_ytd,
        "high_pto_liability": [
            {"person": p.person, "hours": p.hours_available}
            for p in wf.high_pto_liability
        ],
        "negative_pto_balances": [
            {"person": p.person, "hours": p.hours_available}
            for p in wf.negative_pto_balances
        ],
    }


def render_gm_section(gm_data: GMData, cfg: dict, template_dir: Path,
                      discoveries: list | None = None) -> str:
    """Render the Revenue & Gross Margin dashboard as a standalone HTML document."""
    context = build_gm_context(gm_data, cfg)

    try:
        forecasts = project_gm_series(gm_data, cfg)
        commentary = generate_gm_commentary(forecasts, cfg)
    except Exception:
        commentary = {}

    disc_list = discoveries or []
    for div, tab in context["tabs"].items():
        tab["ai_commentary"] = commentary.get(div)
        tab["discoveries"] = [
            d for d in disc_list
            if d.get("section") == "gm" and d.get("subject") == div
        ]

    return _render_template("gm_section.html.j2", context, template_dir)


def render_utilization_section(
    data: LoadedData,
    corp_roles: set[str],
    views: list[dict],
    cfg: dict,
    template_dir: Path,
    workforce=None,
    discoveries: list | None = None,
) -> str:
    """Render the utilization dashboard as a standalone HTML document."""
    try:
        commentary = generate_utilization_commentary(views, cfg)
    except Exception:
        commentary = {}

    for view in views:
        summary_key = _UTIL_COMMENTARY_VIEW_KEYS.get(view.get("view_id"))
        view["ai_commentary"] = commentary.get(summary_key) if summary_key else None

    disc_list = discoveries or []
    context = build_combined_context(data, corp_roles, views, cfg)
    context["workforce"] = _workforce_to_ctx(workforce)
    context["util_discoveries"] = [
        d for d in disc_list if d.get("section") in ("utilization", "overall")
    ]
    context["workforce_discoveries"] = [
        d for d in disc_list if d.get("section") == "workforce"
    ]
    return _render_template("utilization_section.html.j2", context, template_dir)


def combine_sections(
    gm_html: str | None,
    util_html: str | None,
    template_dir: Path,
    generated_date: date | None = None,
    cross_flags: list | None = None,
    root_causes: list | None = None,
) -> str:
    """
    Combines whichever rendered section(s) are given into one final HTML
    string.

    - Both provided: a combined shell (dashboard.html.j2) with a top-level
      tab row switching between two fully independent, self-contained
      sub-documents (embedded via <iframe srcdoc>, so neither side's CSS/JS
      can collide with the other's). A third "Division Health" tab shows
      cross_flags (src/cross_flagger.py CrossFlag objects) directly in the
      shell, not iframed, since it's simple markup rather than a full
      sub-document — only meaningful (and only passed) when both sections
      are being rendered together, since a single-metric-only flag isn't a
      useful signal on its own.
    - Only one provided: that section's HTML is returned directly — no
      shell, no top nav, matching how each tool behaved standalone.
    """
    if not gm_html and not util_html:
        raise ValueError("combine_sections requires at least one of gm_html or util_html")
    if gm_html and util_html:
        shell_context = {
            "gm_html":        gm_html,
            "util_html":      util_html,
            "generated_date": str(generated_date or date.today()),
            "division_health": _build_division_health_ctx(cross_flags, root_causes),
        }
        return _render_template("dashboard.html.j2", shell_context, template_dir)
    return gm_html or util_html


def render_dashboard(
    *,
    gm_data: GMData | None,
    util_bundle: tuple | None,   # (LoadedData, corp_roles, views) or None
    cfg: dict,
    template_dir: Path,
    output_path: Path,
    generated_date: date | None = None,
    cross_flags: list | None = None,
    root_causes: list | None = None,
    workforce=None,
) -> Path:
    """Renders whichever dataset(s) were supplied into a single output file."""
    if gm_data is None and util_bundle is None:
        raise ValueError("render_dashboard requires at least one of gm_data or util_bundle")

    gm_html = render_gm_section(gm_data, cfg, template_dir) if gm_data is not None else None

    util_html = None
    if util_bundle is not None:
        data, corp_roles, views = util_bundle
        util_html = render_utilization_section(
            data, corp_roles, views, cfg, template_dir, workforce=workforce
        )

    html = combine_sections(
        gm_html, util_html, template_dir, generated_date, cross_flags, root_causes
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    return output_path
