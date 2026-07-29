"""
Aggregates parsed GM workbook data (src/gm_loader.py) into everything the
template needs: KPI cards, trend charts, the project detail table, and the
month-over-month compare panel — one precomputed view per division tab
(plus "ALL"), mirroring how src/view_builder.py precomputes the utilization
side's four time-range views.

This is a Python port of the client-side aggregation/render logic in
cfo_dashboard.html (renderKPIs / renderCharts / renderTable / renderCompare).
"""

from __future__ import annotations

from .common import fmt_money, fmt_money_full, fmt_pct, fmt_pct_variance, fmt_variance, render_line_chart_svg
from .gm_loader import AopEntry, GMData


def _acts_for(data: GMData, div: str) -> list:
    if div == "ALL":
        return data.actuals
    return [a for a in data.actuals if a.division == div]


def _aop_for(data: GMData, div: str, divisions: list[str]) -> AopEntry | None:
    if not data.has_aop:
        return None
    if div == "ALL":
        combined = AopEntry(division="ALL")
        any_found = False
        for d in divisions:
            t = data.aop.get(d)
            if not t:
                continue
            any_found = True
            for m in range(12):
                combined.revenue[m] += t.revenue[m]
                combined.gp[m] += t.gp[m]
        return combined if any_found else None
    return data.aop.get(div)


def _ytd_aop(aop_entry: AopEntry | None, months: list) -> dict | None:
    if not aop_entry:
        return None
    rev = sum(aop_entry.revenue[m.idx] for m in months)
    gp = sum(aop_entry.gp[m.idx] for m in months)
    return {"rev": rev, "gp": gp}


def _variance_class(v: float | None) -> str:
    if v is None:
        return "vna"
    return "vpos" if v >= 0 else "vneg"


def _variance_word(v: float | None) -> str:
    if v is None:
        return ""
    return "pos" if v >= 0 else "neg"


# ─────────────────────────────────────────────────────────────────────────────
# KPIs
# ─────────────────────────────────────────────────────────────────────────────

def _build_kpis(data: GMData, div: str, divisions: list[str]) -> dict:
    acts = _acts_for(data, div)
    ytd_rev = sum(a.revenue for a in acts)
    ytd_gm = sum(a.gross_margin for a in acts)
    ytd_gmp = (ytd_gm / ytd_rev * 100) if ytd_rev else 0.0

    aop_entry = _aop_for(data, div, divisions)
    yta = _ytd_aop(aop_entry, data.months)

    rev_var = (ytd_rev - yta["rev"]) if yta else None
    rev_var_pct = (rev_var / yta["rev"] * 100) if (yta and yta["rev"]) else None
    gm_var = (ytd_gm - yta["gp"]) if yta else None
    gm_var_pct = (gm_var / yta["gp"] * 100) if (yta and yta["gp"]) else None

    return {
        "ytd_rev": fmt_money(ytd_rev),
        "ytd_rev_aop": f"AOP: {fmt_money(yta['rev'])}" if yta else "No AOP data",
        "rev_var_class": _variance_class(rev_var),
        "rev_var_word": _variance_word(rev_var),
        "rev_var": fmt_variance(rev_var) if rev_var is not None else "N/A",
        "rev_var_pct": f"{fmt_pct_variance(rev_var_pct)} vs plan" if rev_var_pct is not None else "AOP not available",
        "ytd_gm": fmt_money(ytd_gm),
        "ytd_gmp": f"GM%: {fmt_pct(ytd_gmp)}",
        "gm_var_class": _variance_class(gm_var),
        "gm_var_word": _variance_word(gm_var),
        "gm_var": fmt_variance(gm_var) if gm_var is not None else "N/A",
        "gm_var_pct": f"{fmt_pct_variance(gm_var_pct)} vs plan" if gm_var_pct is not None else "AOP not available",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Charts
# ─────────────────────────────────────────────────────────────────────────────

def _build_charts(data: GMData, div: str, divisions: list[str], division_colors: dict) -> dict:
    months = data.months
    labels = [m.label for m in months]
    aop_entry = _aop_for(data, div, divisions)

    cum_rev = cum_gm = cum_rev_aop = cum_gm_aop = 0.0
    d_rev, d_gm, d_rev_aop, d_gm_aop = [], [], [], []

    for m in months:
        rows = [a for a in _acts_for(data, div) if a.month.key == m.key]
        cum_rev += sum(a.revenue for a in rows)
        cum_gm += sum(a.gross_margin for a in rows)
        d_rev.append(cum_rev)
        d_gm.append(cum_gm)
        if aop_entry:
            cum_rev_aop += aop_entry.revenue[m.idx]
            cum_gm_aop += aop_entry.gp[m.idx]
        d_rev_aop.append(cum_rev_aop if aop_entry else None)
        d_gm_aop.append(cum_gm_aop if aop_entry else None)

    series_a = [{"label": "Actual", "color": "#1D4ED8", "data": d_rev, "dashed": False}]
    if aop_entry:
        series_a.append({"label": "AOP", "color": "#94A3B8", "data": d_rev_aop, "dashed": True})

    series_b = [{"label": "Actual", "color": "#15803D", "data": d_gm, "dashed": False}]
    if aop_entry:
        series_b.append({"label": "AOP", "color": "#94A3B8", "data": d_gm_aop, "dashed": True})

    if div == "ALL":
        series_c = []
        for d in divisions:
            data_pts = []
            for m in months:
                rows = [a for a in data.actuals if a.division == d and a.month.key == m.key]
                rev = sum(a.revenue for a in rows)
                gm = sum(a.gross_margin for a in rows)
                data_pts.append((gm / rev * 100) if rev else None)
            series_c.append({"label": d, "color": division_colors.get(d, "#1D4ED8"), "data": data_pts, "dashed": False})
    else:
        data_pts = []
        for m in months:
            rows = [a for a in _acts_for(data, div) if a.month.key == m.key]
            rev = sum(a.revenue for a in rows)
            gm = sum(a.gross_margin for a in rows)
            data_pts.append((gm / rev * 100) if rev else None)
        series_c = [{"label": f"{div} GM%", "color": division_colors.get(div, "#1D4ED8"), "data": data_pts, "dashed": False}]

    div_label = "All Divisions" if div == "ALL" else div

    return {
        "chart_a_svg": render_line_chart_svg(series_a, labels, y_fmt=fmt_money),
        "chart_a_label": f"YTD Revenue — {div_label}",
        "chart_b_svg": render_line_chart_svg(series_b, labels, y_fmt=fmt_money),
        "chart_b_label": f"YTD Gross Margin — {div_label}",
        "chart_c_svg": render_line_chart_svg(series_c, labels, y_fmt=lambda v: fmt_pct(v, 0)),
        "chart_c_label": f"Monthly GM% — {div_label}",
        "legend_a": [{"label": s["label"], "color": s["color"], "dashed": s["dashed"]} for s in series_a],
        "legend_b": [{"label": s["label"], "color": s["color"], "dashed": s["dashed"]} for s in series_b],
        "legend_c": [{"label": s["label"], "color": s["color"], "dashed": s["dashed"]} for s in series_c],
    }


# ─────────────────────────────────────────────────────────────────────────────
# Project detail table
# ─────────────────────────────────────────────────────────────────────────────

def _build_table(data: GMData, div: str) -> dict:
    projs = [p for p in data.projects.values() if div == "ALL" or p.division == div]

    rows = []
    for p in projs:
        acts = [a for a in data.actuals if a.project_name == p.name and a.division == p.division]
        ytd_rev = sum(a.revenue for a in acts)
        ytd_gm = sum(a.gross_margin for a in acts)
        ytd_gmp = (ytd_gm / ytd_rev * 100) if ytd_rev else 0.0

        div_aop = data.aop.get(p.division)
        da_rev = sum(div_aop.revenue[m.idx] for m in data.months) if div_aop else None
        da_gp = sum(div_aop.gp[m.idx] for m in data.months) if div_aop else None
        da_gmp = (da_gp / da_rev * 100) if (da_rev and da_gp) else None

        rows.append({
            "name": p.name,
            "code": p.code or "—",
            "division": p.division,
            "portfolio_lead": p.portfolio_lead,
            "ytd_rev": ytd_rev,
            "ytd_rev_fmt": fmt_money(ytd_rev),
            "ytd_gm": ytd_gm,
            "ytd_gm_fmt": fmt_money(ytd_gm),
            "ytd_gmp": ytd_gmp,
            "ytd_gmp_fmt": fmt_pct(ytd_gmp),
            "alert": da_gmp is not None and ytd_gmp < da_gmp - 10,
        })

    rows.sort(key=lambda r: r["ytd_rev"], reverse=True)
    return {"rows": rows, "title": f"Project Detail — {'All Divisions' if div == 'ALL' else div}"}


# ─────────────────────────────────────────────────────────────────────────────
# Month-over-month compare
# ─────────────────────────────────────────────────────────────────────────────

def _build_compare(data: GMData, div: str) -> dict:
    div_projects = {p.name.lower() for p in data.projects.values() if div == "ALL" or p.division == div}
    rows = [c for c in data.compare if c.program.lower() in div_projects]

    month_order = {m.label: i for i, m in enumerate(data.months)}
    rows.sort(key=lambda c: month_order.get(c.month.label, 999))

    labels_seen = []
    for c in rows:
        if c.month.label not in labels_seen:
            labels_seen.append(c.month.label)

    out_rows = []
    for c in rows:
        gmp_raw = c.aop_gm_pct
        if not gmp_raw:
            gmp_display = "—"
        elif gmp_raw < 2:
            gmp_display = fmt_pct(gmp_raw * 100)
        else:
            gmp_display = fmt_pct(gmp_raw)
        out_rows.append({
            "program": c.program,
            "portfolio": c.portfolio,
            "month": c.month.label,
            "actuals_fmt": fmt_money(c.actuals),
            "aop_fmt": fmt_money(c.aop),
            "var_class": "vp" if c.var_rev >= 0 else "vn",
            "var_fmt": fmt_variance(c.var_rev),
            "gp_actual_fmt": fmt_money(c.gp_actual),
            "gmp_display": gmp_display,
            "gp_aop_fmt": fmt_money(c.gp_aop),
        })

    return {"rows": out_rows, "months": labels_seen}


# ─────────────────────────────────────────────────────────────────────────────
# Revenue concentration
# ─────────────────────────────────────────────────────────────────────────────

def _build_concentration(data: GMData, cfg: dict) -> dict:
    conc = cfg.get("revenue_concentration", {})
    contract_thresh = conc.get("contract_flag_threshold", 0.30)
    division_thresh = conc.get("division_flag_threshold", 0.50)

    divisions = cfg["gm"]["divisions"]
    colors = cfg["gm"]["division_colors"]

    total_rev = sum(a.revenue for a in data.actuals)
    if total_rev == 0:
        return {"total_revenue_fmt": "$0", "divisions": [], "contracts": [], "has_flags": False,
                "contract_threshold_pct": int(contract_thresh * 100),
                "division_threshold_pct": int(division_thresh * 100)}

    # Division breakdown
    div_shares = []
    for div in divisions:
        rev = sum(a.revenue for a in data.actuals if a.division == div)
        pct = rev / total_rev * 100
        div_shares.append({
            "division": div,
            "revenue_fmt": fmt_money(rev),
            "pct": round(pct, 1),
            "color": colors.get(div, "#888"),
            "flagged": pct / 100 >= division_thresh,
        })
    div_shares.sort(key=lambda x: x["pct"], reverse=True)

    # Contract breakdown — aggregate by project name across all months
    contract_rev: dict[str, float] = {}
    contract_div: dict[str, str] = {}
    for a in data.actuals:
        contract_rev[a.project_name] = contract_rev.get(a.project_name, 0) + a.revenue
        contract_div[a.project_name] = a.division

    contracts = []
    for name, rev in contract_rev.items():
        pct = rev / total_rev * 100
        contracts.append({
            "name": name,
            "division": contract_div[name],
            "revenue_fmt": fmt_money(rev),
            "pct": round(pct, 1),
            "flagged": pct / 100 >= contract_thresh,
        })
    contracts.sort(key=lambda x: x["pct"], reverse=True)

    return {
        "total_revenue_fmt": fmt_money(total_rev),
        "divisions": div_shares,
        "contracts": contracts[:20],
        "has_flags": any(c["flagged"] for c in contracts) or any(d["flagged"] for d in div_shares),
        "contract_threshold_pct": int(contract_thresh * 100),
        "division_threshold_pct": int(division_thresh * 100),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Top-level context builder
# ─────────────────────────────────────────────────────────────────────────────

def build_gm_context(data: GMData, cfg: dict) -> dict:
    gm_cfg = cfg["gm"]
    divisions = gm_cfg["divisions"]
    division_colors = gm_cfg["division_colors"]

    tabs = {}
    for div in ["ALL"] + divisions:
        tabs[div] = {
            "kpis": _build_kpis(data, div, divisions),
            "charts": _build_charts(data, div, divisions, division_colors),
            "table": _build_table(data, div),
            "compare": _build_compare(data, div),
        }

    date_range = ""
    if data.months:
        date_range = data.months[0].label if len(data.months) == 1 else f"{data.months[0].label} – {data.months[-1].label}"

    scan_rows = []
    for s in data.sheet_log:
        scan_rows.append({
            "name": s.name,
            "type": s.type,
            "month": s.month,
            "cols": s.cols,
            "rows": s.rows,
            "issues": s.issues,
            "cell_preview": s.cell_preview,
        })
    gm_sheets = sum(1 for s in data.sheet_log if s.type == "gm")
    aop_sheets = sum(1 for s in data.sheet_log if s.type == "aop")
    cmp_sheets = sum(1 for s in data.sheet_log if s.type == "compare")
    ignored = sum(1 for s in data.sheet_log if s.type == "ignored")
    issues_count = sum(1 for s in data.sheet_log if s.issues and s.type != "ignored")

    return {
        "has_gm": True,
        "has_data": len(data.actuals) > 0,
        "has_aop": data.has_aop,
        "date_range": date_range,
        "divisions": divisions,
        "tabs": tabs,
        "concentration": _build_concentration(data, cfg),
        "scan": {
            "rows": scan_rows,
            "gm_sheets": gm_sheets,
            "aop_sheets": aop_sheets,
            "cmp_sheets": cmp_sheets,
            "ignored": ignored,
            "issues_count": issues_count,
            "total": len(data.sheet_log),
        },
    }
