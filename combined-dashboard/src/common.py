"""
Shared helpers used by both the GM/Revenue pipeline and (optionally) the
utilization pipeline: numeric coercion, currency/percent formatting, and
alias-based column-name resolution.

Kept dependency-free (no pandas/numpy) so it can be unit tested in isolation.
"""

from __future__ import annotations

import re
from html import escape

from markupsafe import Markup


# ─────────────────────────────────────────────────────────────────────────────
# Numeric coercion
# ─────────────────────────────────────────────────────────────────────────────

_NUMERIC_STRIP_RE = re.compile(r"[$,%\s]")


def parse_number(value) -> float:
    """Best-effort coercion of a spreadsheet cell to a float.

    Mirrors the original client-side `pn()` helper: strips currency symbols,
    percent signs, commas, and whitespace from strings; treats None/blank/
    unparsable values as 0.0 rather than raising, since a single malformed
    cell should never abort a whole workbook parse.
    """
    if value is None or value == "":
        return 0.0
    if isinstance(value, bool):
        # bool is a subclass of int — guard against silently coercing True/False
        return 0.0
    if isinstance(value, (int, float)):
        try:
            if value != value:  # NaN check without importing math/numpy
                return 0.0
        except TypeError:
            pass
        return float(value)
    s = _NUMERIC_STRIP_RE.sub("", str(value))
    if s in ("", "-", "."):
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Formatting
# ─────────────────────────────────────────────────────────────────────────────

def fmt_money(n: float | None) -> str:
    """Compact currency: $1.2M / $340K / $5,000."""
    if n is None:
        return "—"
    a, sign = abs(n), "-" if n < 0 else ""
    if a >= 1e6:
        return f"{sign}${a / 1e6:.1f}M"
    if a >= 1e3:
        return f"{sign}${round(a / 1e3):,.0f}K"
    return f"{sign}${round(a):,.0f}"


def fmt_money_full(n: float | None) -> str:
    if n is None:
        return "—"
    sign = "-$" if n < 0 else "$"
    return f"{sign}{round(abs(n)):,.0f}"


def fmt_variance(n: float | None) -> str:
    if n is None:
        return "N/A"
    prefix = "+" if n >= 0 else ""
    return f"{prefix}{fmt_money(n)}"


def fmt_pct(n: float | None, decimals: int = 1) -> str:
    if n is None:
        return "—"
    return f"{n:.{decimals}f}%"


def fmt_pct_variance(n: float | None, decimals: int = 1) -> str:
    if n is None:
        return ""
    prefix = "+" if n >= 0 else ""
    return f"{prefix}{n:.{decimals}f}%"


# ─────────────────────────────────────────────────────────────────────────────
# Config validation
# ─────────────────────────────────────────────────────────────────────────────

REQUIRED_UTIL_CONFIG_KEYS = [
    "billable_utilization_warning", "billable_utilization_critical",
    "direct_utilization_warning", "direct_utilization_critical",
    "pto_warning_threshold_hours", "pto_critical_threshold_hours",
    "bp_ratio_threshold", "nonbillable_threshold", "ga_crowding_threshold",
    "corporate_role_threshold", "persistence_threshold",
    "sheets", "columns", "subgroup_labels", "paycode_exclude",
]

REQUIRED_GM_CONFIG_KEYS = [
    "divisions", "division_colors", "aop_layout", "aop_division_aliases",
    "gm_aliases", "compare_aliases", "gm_required_columns", "compare_required_columns",
]


def validate_config(cfg: dict, *, need_util: bool, need_gm: bool) -> None:
    """Fail fast with a clear message instead of a deep KeyError mid-pipeline."""
    missing = []
    if need_util:
        missing += [k for k in REQUIRED_UTIL_CONFIG_KEYS if k not in cfg]
    if need_gm:
        gm_cfg = cfg.get("gm")
        if gm_cfg is None:
            missing.append("gm")
        else:
            missing += [f"gm.{k}" for k in REQUIRED_GM_CONFIG_KEYS if k not in gm_cfg]
    if missing:
        raise ValueError(
            "config.yaml is missing required key(s): "
            f"{', '.join(missing)}. Check config.yaml against the documented schema."
        )


# ─────────────────────────────────────────────────────────────────────────────
# Column alias resolution
# ─────────────────────────────────────────────────────────────────────────────

def _normalize_header(key: str) -> str:
    return re.sub(r"[\s_]", "", key.strip().lower())


class ColumnResolver:
    """Given a workbook sheet's real header row and a canonical-name -> alias
    list map, resolves a canonical name to whichever actual header is present.

    Generalizes the original JS `makeResolver()` helper.
    """

    def __init__(self, actual_columns: list[str], alias_map: dict[str, list[str]]):
        self._alias_map = alias_map
        self._by_norm: dict[str, str] = {}
        for col in actual_columns:
            self._by_norm[_normalize_header(str(col))] = col

    def resolve(self, canonical: str) -> str | None:
        for alias in self._alias_map.get(canonical, [canonical]):
            found = self._by_norm.get(_normalize_header(alias))
            if found is not None:
                return found
        return None

    def missing(self, required: list[str]) -> list[str]:
        return [name for name in required if self.resolve(name) is None]


# ─────────────────────────────────────────────────────────────────────────────
# Minimal inline SVG line chart (no external chart library / no CDN)
# ─────────────────────────────────────────────────────────────────────────────

def render_line_chart_svg(
    series: list[dict],
    labels: list[str],
    width: int = 560,
    height: int = 200,
    y_fmt=None,
) -> str:
    """Render a small multi-series line chart as a self-contained inline SVG.

    `series` is a list of {"label": str, "color": str, "data": list[float|None],
    "dashed": bool} dicts, all aligned to `labels`. None values create a gap
    (spanGaps-style) rather than being plotted as zero.
    """
    y_fmt = y_fmt or (lambda v: f"{v:,.0f}")
    pad_l, pad_r, pad_t, pad_b = 44, 12, 12, 24
    plot_w = max(1, width - pad_l - pad_r)
    plot_h = max(1, height - pad_t - pad_b)

    all_vals = [v for s in series for v in s["data"] if v is not None]
    if not all_vals:
        return Markup(
            f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
            f'class="linechart" role="img" aria-label="No data">'
            f'<text x="{width/2}" y="{height/2}" text-anchor="middle" '
            f'font-size="11" fill="#8C8CA0">No data</text></svg>'
        )

    y_max = max(all_vals)
    y_min = min(0.0, min(all_vals))
    if y_max == y_min:
        y_max = y_min + 1

    n = len(labels)

    def x_at(i: int) -> float:
        return pad_l + (plot_w * i / (n - 1) if n > 1 else plot_w / 2)

    def y_at(v: float) -> float:
        frac = (v - y_min) / (y_max - y_min)
        return pad_t + plot_h * (1 - frac)

    parts = [
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
        f'class="linechart" role="img" aria-label="Trend chart">'
    ]

    # Gridlines + y-axis labels (4 bands)
    for i in range(5):
        gy = pad_t + plot_h * i / 4
        val = y_max - (y_max - y_min) * i / 4
        parts.append(
            f'<line x1="{pad_l}" y1="{gy:.1f}" x2="{width - pad_r}" y2="{gy:.1f}" '
            f'stroke="#EEEEF0" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{pad_l - 6}" y="{gy + 3:.1f}" text-anchor="end" '
            f'font-size="9" fill="#8C8CA0">{escape(y_fmt(val))}</text>'
        )

    # X-axis labels (thin out if too many)
    step = max(1, n // 6)
    for i in range(0, n, step):
        parts.append(
            f'<text x="{x_at(i):.1f}" y="{height - 6}" text-anchor="middle" '
            f'font-size="9" fill="#8C8CA0">{escape(labels[i])}</text>'
        )

    # Series lines — break into contiguous runs around None gaps
    for s in series:
        points = s["data"]
        color = s.get("color", "#1D4ED8")
        dashed = s.get("dashed", False)
        dash_attr = ' stroke-dasharray="5,4"' if dashed else ""
        run: list[str] = []
        for i, v in enumerate(points):
            if v is None:
                if len(run) > 1:
                    parts.append(
                        f'<polyline points="{" ".join(run)}" fill="none" '
                        f'stroke="{color}" stroke-width="2"{dash_attr}/>'
                    )
                run = []
                continue
            run.append(f"{x_at(i):.1f},{y_at(v):.1f}")
        if len(run) > 1:
            parts.append(
                f'<polyline points="{" ".join(run)}" fill="none" '
                f'stroke="{color}" stroke-width="2"{dash_attr}/>'
            )
        # Points
        if not dashed:
            for i, v in enumerate(points):
                if v is not None:
                    parts.append(
                        f'<circle cx="{x_at(i):.1f}" cy="{y_at(v):.1f}" r="2.5" fill="{color}"/>'
                    )

    parts.append("</svg>")
    return Markup("".join(parts))
