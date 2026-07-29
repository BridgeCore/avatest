"""
Identifies employees who should be excluded from billable utilization
trend analysis and flagged with a "Corp Role" badge:

  Rule 1 — CORP org: PersonOrganization contains "CORP" (case-insensitive).
            These employees have no billable expectation by definition.

  Rule 2 — G&A/Overhead ratio: ga_hours + overhead_hours >= 80% of total
            hours AND zero billable hours across the full date range.
"""

from __future__ import annotations

import pandas as pd

from .loader import LoadedData


def identify_corporate_roles(data: LoadedData, cfg: dict) -> set[str]:
    threshold = cfg.get("corporate_role_threshold", 0.80)
    sg_col = cfg["columns"]["export"]["project_subgroup"]
    h_col = cfg["columns"]["export"]["hours"]
    p_col = cfg["columns"]["export"]["person"]
    labels = cfg["subgroup_labels"]

    corp_roles: set[str] = set()

    # Rule 1: PersonOrganization contains "CORP" (case-insensitive)
    for person, prof in data.employee_profiles.items():
        if prof.person_org and "CORP" in prof.person_org.upper():
            corp_roles.add(person)

    # Rule 2: G&A + Overhead >= threshold of total hours, zero billable
    df = data.export_df
    if df.empty or p_col not in df.columns:
        return corp_roles

    grp = df.groupby(p_col)[h_col].sum().rename("total")
    billable = (
        df[df[sg_col] == labels["billable"]]
        .groupby(p_col)[h_col].sum()
        .rename("billable")
    )
    ga = (
        df[df[sg_col] == labels["ga"]]
        .groupby(p_col)[h_col].sum()
        .rename("ga")
    )
    overhead = (
        df[df[sg_col] == labels["overhead"]]
        .groupby(p_col)[h_col].sum()
        .rename("overhead")
    )

    summary = pd.concat([grp, billable, ga, overhead], axis=1).fillna(0)

    for person, row in summary.iterrows():
        total = row["total"]
        if total <= 0:
            continue
        corp_ratio = (row["ga"] + row["overhead"]) / total
        if corp_ratio >= threshold and row["billable"] == 0:
            corp_roles.add(str(person))

    return corp_roles
