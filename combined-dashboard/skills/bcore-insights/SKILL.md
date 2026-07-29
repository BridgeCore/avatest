---
name: bcore-insights
description: Summarize BCore division health (utilization + revenue) from the latest dashboard run
---

# BCore Insights

Read `exports/insights_latest.json` at the project root — a small, pre-computed
summary of the most recent dashboard run (`combined-dashboard`). All numbers in
it are already computed by Python; do not recompute or extrapolate figures
yourself, only narrate/prioritize what's there.

## Fields

- `generated_at` — ISO timestamp of the run this file describes.
- `periods_covered` — utilization period labels (or GM month labels if no
  utilization workbook was uploaded) included in this run.
- `divisions[]` — one entry per division:
  - `avg_utilization` — average billable utilization (0–1), or `null` if the
    division has no utilization data yet (e.g. a newly-onboarded division).
  - `mtd_revenue_variance_pct` / `ytd_revenue_variance_pct` — actual vs. AOP,
    as a fraction (e.g. `-0.06` = 6% under plan). `null` if no AOP/GM data
    maps to this division (e.g. a non-revenue-bearing division).
  - `low_utilization_flag` / `low_revenue_flag` — booleans, or `null` when the
    underlying metric is missing. Never treat `null` as `false`.
  - `combined_flag` — `true` only when utilization is low **and** the YTD
    revenue variance is also low — this is the strongest signal.
  - `severity` — `"critical"` (combined + sustained), `"warning"` (single
    metric, or combined but only for the latest month), or `"none"`.
  - `reason` — short human-readable explanation of the severity.
- `data_quality_flags[]` — pre-existing data issues carried over from the
  source workbook (missing timesheets, missing PTO records, unmapped lookup
  combinations) — not performance flags, just data-integrity notes.

## When asked to summarize

Prioritize `severity: "critical"` divisions first, then `"warning"`, then
mention `"none"` divisions only in passing. Call out any division with
`avg_utilization: null` or a `null` variance field as "no data yet" rather
than implying it's healthy. Mention `data_quality_flags` only if asked about
data quality specifically, or if there are several — they're not performance
issues.
