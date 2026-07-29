"""
Open-ended trend discovery via Claude CLI.

Unlike ai_commentary.py which only narrates pre-computed flags, this module
sends all cross-dataset metrics in one payload and asks Claude to surface
non-obvious patterns — cross-dataset correlations, concentration risk,
recovery signals, leading indicators — things the hardcoded detectors skip.

Results are tagged by section + subject so the renderer can inject them
inline next to the relevant data.
"""

from __future__ import annotations

import glob
import hashlib
import json
import shutil
import subprocess
from pathlib import Path


def _claude_exe() -> str:
    """Return the claude binary — system PATH first, then VSCode extension fallback."""
    if shutil.which("claude"):
        return "claude"
    pattern = str(Path.home() / ".vscode" / "extensions" /
                  "anthropic.claude-code-*" / "resources" / "native-binary" / "claude.exe")
    matches = sorted(glob.glob(pattern), reverse=True)
    return matches[0] if matches else "claude"

CACHE_DIR = Path("output") / ".ai_cache"

DISCOVERY_SCHEMA = {
    "type": "object",
    "properties": {
        "discoveries": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "section":          {"type": "string", "enum": ["gm", "utilization", "workforce"]},
                    "subject":          {"type": "string"},
                    "title":            {"type": "string"},
                    "observation":      {"type": "string"},
                    "confidence":       {"type": "string", "enum": ["high", "medium", "low"]},
                    "suggested_action": {"type": "string"},
                },
                "required": ["section", "subject", "title", "observation",
                             "confidence", "suggested_action"],
            },
            "minItems": 1,
            "maxItems": 8,
        }
    },
    "required": ["discoveries"],
}

INSTRUCTION = (
    "You are a senior financial analyst reviewing a professional services company's "
    "complete operational data for the current reporting period. You will receive JSON "
    "with three datasets: utilization flags (billable efficiency per employee), GM/revenue "
    "actuals vs AOP plan per division, and workforce metrics (headcount, PTO). "
    "Surface 5-8 non-obvious patterns or anomalies that go BEYOND the already-detected "
    "flags listed in 'existing_flag_types'. Look especially for: cross-dataset correlations "
    "(e.g. a division with improving utilization but declining GM suggests a rate/pricing "
    "issue, not an effort issue); concentration risk (single contract or person dominating "
    "a flag set); recovery signals (a division trending up after prior flags); leading "
    "indicators (a workforce metric predicting a future utilization problem); or outlier "
    "ratios that seem fine in isolation but are notable in context. "
    "Cite specific numbers from the data. Be concrete and direct. "
    "Tag each discovery: 'section' must be 'gm', 'utilization', or 'workforce'. "
    "'subject' must be a division code (e.g. 'MS1'), a person name, or 'overall'."
)


def _build_payload(views: list, gm_data, workforce, cfg: dict) -> dict:
    payload: dict = {}

    # ── Utilization ────────────────────────────────────────────────────────────
    ytd = next((v for v in views if v.get("view_id") == "ytd"), views[-1] if views else None)
    if ytd:
        all_flags = ytd.get("critical_flags", []) + ytd.get("warning_flags", [])
        top_flags = sorted(all_flags, key=lambda f: f.get("periods_flagged", 0), reverse=True)[:15]

        div_counts: dict = {}
        for f in ytd.get("critical_flags", []):
            d = f.get("division", "—")
            div_counts.setdefault(d, {"critical": 0, "warning": 0})
            div_counts[d]["critical"] += 1
        for f in ytd.get("warning_flags", []):
            d = f.get("division", "—")
            div_counts.setdefault(d, {"critical": 0, "warning": 0})
            div_counts[d]["warning"] += 1

        payload["utilization"] = {
            "period":             ytd.get("view_label", "YTD"),
            "employees_analyzed": ytd.get("employees_analyzed", 0),
            "total_critical":     ytd.get("critical_count", 0),
            "total_warnings":     ytd.get("warning_count", 0),
            "division_flag_summary": [
                {"division": d, **counts} for d, counts in sorted(div_counts.items())
            ],
            "top_flagged_employees": [
                {
                    "person":          f["person"],
                    "division":        f["division"],
                    "flag_type":       f["flag_type"],
                    "severity":        f["severity"],
                    "periods_flagged": f["periods_flagged"],
                    "reason":          f["reason"],
                }
                for f in top_flags
            ],
            "existing_flag_types": sorted({f["flag_type"] for f in all_flags}),
        }

    # ── GM / Revenue ───────────────────────────────────────────────────────────
    if gm_data is not None:
        divisions = cfg.get("gm", {}).get("divisions", [])
        div_summaries = []
        for div in divisions:
            acts = [a for a in gm_data.actuals if a.division == div]
            ytd_rev = sum(a.revenue for a in acts)
            ytd_gm  = sum(a.gross_margin for a in acts)
            ytd_gmp = (ytd_gm / ytd_rev * 100) if ytd_rev else 0.0

            aop_entry = gm_data.aop.get(div)
            if aop_entry and gm_data.months:
                aop_rev_ytd = sum(aop_entry.revenue[m.idx] for m in gm_data.months)
                aop_gp_ytd  = sum(aop_entry.gp[m.idx]      for m in gm_data.months)
                rev_var_pct = (
                    (ytd_rev - aop_rev_ytd) / aop_rev_ytd * 100
                    if aop_rev_ytd else None
                )
                gm_var_pct = (
                    (ytd_gm - aop_gp_ytd) / aop_gp_ytd * 100
                    if aop_gp_ytd else None
                )
            else:
                aop_rev_ytd = aop_gp_ytd = rev_var_pct = gm_var_pct = None

            # Last 3 months of monthly revenue + GM%
            monthly = []
            for m in gm_data.months[-3:]:
                m_acts = [a for a in acts if a.month == m.label]
                m_rev = sum(a.revenue for a in m_acts)
                m_gm  = sum(a.gross_margin for a in m_acts)
                monthly.append({
                    "month":   m.label,
                    "revenue": round(m_rev),
                    "gm_pct":  round(m_gm / m_rev * 100, 1) if m_rev else 0.0,
                })

            div_summaries.append({
                "division":             div,
                "ytd_revenue":          round(ytd_rev),
                "ytd_gm_pct":           round(ytd_gmp, 1),
                "aop_revenue_ytd":      round(aop_rev_ytd) if aop_rev_ytd else None,
                "revenue_variance_pct": round(rev_var_pct, 1) if rev_var_pct is not None else None,
                "gm_variance_pct":      round(gm_var_pct,  1) if gm_var_pct  is not None else None,
                "last_3_months":        monthly,
            })

        payload["gm"] = {
            "has_aop":  gm_data.has_aop,
            "divisions": div_summaries,
        }

    # ── Workforce ──────────────────────────────────────────────────────────────
    if workforce is not None:
        payload["workforce"] = {
            "headcount_current": workforce.headcount_current,
            "hires_ytd":         workforce.hires_ytd,
            "departures_ytd":    workforce.departures_ytd,
            "net_change_ytd":    workforce.net_change_ytd,
            "high_pto_top5": [
                {"person": p.person, "hours": round(p.hours_available, 1)}
                for p in workforce.high_pto_liability[:5]
            ],
            "negative_pto_top5": [
                {"person": p.person, "hours": round(p.hours_available, 1)}
                for p in workforce.negative_pto_balances[:5]
            ],
        }

    return payload


def _call_claude(payload: dict, timeout_sec: int) -> list[dict] | None:
    try:
        proc = subprocess.run(
            [_claude_exe(), "-p", INSTRUCTION, "--output-format", "json",
             "--json-schema", json.dumps(DISCOVERY_SCHEMA), "--allowedTools", ""],
            input=json.dumps(payload, default=str),
            capture_output=True, text=True, timeout=timeout_sec,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    if proc.returncode != 0:
        return None

    try:
        envelope = json.loads(proc.stdout)
    except (json.JSONDecodeError, ValueError):
        return None

    structured = envelope.get("structured_output")
    if not isinstance(structured, dict):
        return None
    discoveries = structured.get("discoveries")
    if not isinstance(discoveries, list):
        return None
    return [d for d in discoveries if isinstance(d, dict)]


def generate_discoveries(views: list, gm_data, workforce, cfg: dict) -> list[dict]:
    """Return AI-discovered trend observations, or [] on failure / disabled."""
    if not cfg.get("ai_commentary", {}).get("enabled", False):
        return []

    timeout = cfg.get("ai_commentary", {}).get("timeout_seconds", 45)
    payload = _build_payload(views, gm_data, workforce, cfg)

    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    cache_file = CACHE_DIR / f"discoveries_{digest}.json"

    if cache_file.exists():
        try:
            return json.loads(cache_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    result = _call_claude(payload, timeout)
    if not result:
        return []

    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(result), encoding="utf-8")
    except OSError:
        pass

    return result
