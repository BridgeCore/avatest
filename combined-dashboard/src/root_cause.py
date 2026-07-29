"""
Root-cause analysis for cross-dataset flags: identifies the dominant contract
and specific people driving a division's utilization shortfall.

Pure functions, no I/O, no subprocess. Callers pass already-loaded data;
this module only applies the drill-down algorithm.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from .loader import LoadedData, Period

# ProjectSubGroup values that represent real project work (not overhead / time-off)
_PROJECT_SUBGROUPS = {"ProjectBillable", "ProjectNonBillable"}

# Division codes that represent the corporate overhead pool; excluded from the
# peer-average billable baseline so Corp's near-0% rate doesn't mask real anomalies
# in delivery divisions.
_CORP_CODES = {"Corp", "CORP", "corp"}


@dataclass
class ContractPeriodTrend:
    period_index: int
    period_label: str
    billable_hrs: float
    nonbillable_hrs: float
    nonbillable_pct: float   # 0.0–1.0


@dataclass
class DominantContract:
    project_code: str
    project_title: str
    share_of_hours: float           # fraction of division's total project hours (0.0–1.0)
    period_trend: list[ContractPeriodTrend]


@dataclass
class PersonHours:
    person: str
    nonbillable_hours: float


@dataclass
class RootCause:
    division: str               # GM canonical code, e.g. "BL1"
    util_division_code: str     # utilization tracker code, e.g. "BL"
    current_billable_pct: float | None
    peer_avg_billable_pct: float | None
    dominant_contract: DominantContract | None
    top_people: list[PersonHours]
    narrative: str


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ecol(cfg: dict, key: str, default: str) -> str:
    return cfg.get("columns", {}).get("export", {}).get(key, default)


def _inverse_aliases(cfg: dict) -> dict[str, str]:
    """Reverse util_division_aliases: GM canonical code → util tracker code."""
    fwd = cfg.get("cross_flags", {}).get("util_division_aliases", {})
    return {gm: util for util, gm in fwd.items()}


# ── Public API ────────────────────────────────────────────────────────────────

def find_dominant_contract(
    indexed_df: pd.DataFrame,
    division_code: str,
    cfg: dict,
    periods: list[Period],
) -> DominantContract | None:
    """
    indexed_df must already have a `period_index` column (added by
    src/periodizer.py::assign_periods — don't call this on a raw export_df).

    Filters to this division's real project rows (ProjectBillable +
    ProjectNonBillable), groups by ProjectCode, takes the top contract by
    total hours, and builds a per-period billable/NB trend for that contract.

    Returns None if the division has no matching Export rows at all (a GM-only
    division, or a period with no activity yet).
    """
    div_col = _ecol(cfg, "person_div",       "PersonDivision")
    sg_col  = _ecol(cfg, "project_subgroup", "ProjectSubGroup")
    hrs_col = _ecol(cfg, "hours",            "Hours")
    cod_col = _ecol(cfg, "project_code",     "ProjectCode")
    ttl_col = _ecol(cfg, "project_title",    "ProjectTitle")

    for c in [div_col, sg_col, hrs_col, cod_col]:
        if c not in indexed_df.columns:
            return None

    mask   = (indexed_df[div_col] == division_code) & (indexed_df[sg_col].isin(_PROJECT_SUBGROUPS))
    div_df = indexed_df[mask]
    if div_df.empty:
        return None

    by_code  = div_df.groupby(cod_col)[hrs_col].sum().sort_values(ascending=False)
    top_code = by_code.index[0]
    total    = float(by_code.sum())
    share    = float(by_code.iloc[0]) / total if total > 0 else 0.0

    if ttl_col in div_df.columns:
        titles = div_df[div_df[cod_col] == top_code][ttl_col].dropna().unique()
        project_title = str(titles[0]).strip() if len(titles) else str(top_code)
    else:
        project_title = str(top_code)

    cdf = div_df[div_df[cod_col] == top_code]
    trend: list[ContractPeriodTrend] = []

    if "period_index" in cdf.columns:
        for p in periods:
            prows = cdf[cdf["period_index"] == p.index]
            if prows.empty:
                continue
            bill = float(prows[prows[sg_col] == "ProjectBillable"][hrs_col].sum())
            nb   = float(prows[prows[sg_col] == "ProjectNonBillable"][hrs_col].sum())
            tot  = bill + nb
            trend.append(ContractPeriodTrend(
                period_index=p.index,
                period_label=p.label,
                billable_hrs=round(bill, 1),
                nonbillable_hrs=round(nb, 1),
                nonbillable_pct=round(nb / tot, 4) if tot > 0 else 0.0,
            ))

    return DominantContract(
        project_code=str(top_code),
        project_title=project_title,
        share_of_hours=round(share, 4),
        period_trend=trend,
    )


def top_nonbillable_people(
    indexed_df: pd.DataFrame,
    division_code: str,
    contract_code: str,
    period_index: int,
    cfg: dict,
    cap: int = 5,
) -> list[PersonHours]:
    """
    For the given period, the people with the most non-billable hours logged
    to contract_code in division_code, descending, capped at cap.
    """
    div_col = _ecol(cfg, "person_div",       "PersonDivision")
    sg_col  = _ecol(cfg, "project_subgroup", "ProjectSubGroup")
    hrs_col = _ecol(cfg, "hours",            "Hours")
    per_col = _ecol(cfg, "person",           "Person")
    cod_col = _ecol(cfg, "project_code",     "ProjectCode")

    for c in [div_col, sg_col, hrs_col, per_col, cod_col]:
        if c not in indexed_df.columns:
            return []

    mask = (
        (indexed_df[div_col] == division_code) &
        (indexed_df[cod_col] == contract_code) &
        (indexed_df[sg_col] == "ProjectNonBillable")
    )
    if "period_index" in indexed_df.columns:
        mask = mask & (indexed_df["period_index"] == period_index)

    filtered = indexed_df[mask]
    if filtered.empty:
        return []

    by_person = (
        filtered.groupby(per_col)[hrs_col]
        .sum()
        .sort_values(ascending=False)
        .head(cap)
    )
    return [
        PersonHours(person=str(p), nonbillable_hours=round(float(h), 1))
        for p, h in by_person.items()
        if float(h) > 0
    ]


def build_root_causes(
    data: LoadedData,
    cross_flags: list,
    cfg: dict,
) -> list[RootCause]:
    """
    For each CrossFlag with severity != "none":
      1. Reverse-map the GM canonical division code (e.g. "BL1") to the
         utilization tracker code ("BL") via cfg.cross_flags.util_division_aliases.
         Unmapped codes (MS1, MS2, MS3) pass through unchanged.
      2. Find the dominant contract and top non-billable contributors.
      3. Skip silently if the division has no Export data (GM-only division).

    Corp is excluded from the peer-average billable baseline to avoid its
    near-0% rate masking real anomalies in delivery divisions.
    """
    from .periodizer import assign_periods

    indexed_df = assign_periods(data, cfg)
    periods    = data.periods
    if not periods:
        return []

    last_pi = periods[-1].index
    div_col = _ecol(cfg, "person_div",       "PersonDivision")
    sg_col  = _ecol(cfg, "project_subgroup", "ProjectSubGroup")
    hrs_col = _ecol(cfg, "hours",            "Hours")

    # Peer-average billable % (last period, delivery divisions only)
    peer_utils: dict[str, float] = {}
    if all(c in indexed_df.columns for c in [div_col, sg_col, hrs_col]):
        last_df = indexed_df[
            (indexed_df["period_index"] == last_pi) &
            (indexed_df[sg_col].isin(_PROJECT_SUBGROUPS))
        ]
        for div in last_df[div_col].dropna().unique():
            div_str = str(div).strip()
            if div_str in _CORP_CODES or not div_str:
                continue
            rows  = last_df[last_df[div_col] == div]
            total = float(rows[hrs_col].sum())
            if total == 0:
                continue
            bill = float(rows[rows[sg_col] == "ProjectBillable"][hrs_col].sum())
            peer_utils[div_str] = bill / total

    peer_avg = sum(peer_utils.values()) / len(peer_utils) if peer_utils else None

    inverse = _inverse_aliases(cfg)
    results: list[RootCause] = []

    for flag in cross_flags:
        if flag.severity == "none":
            continue

        util_code   = inverse.get(flag.division, flag.division)
        current_pct = peer_utils.get(util_code)

        dominant = find_dominant_contract(indexed_df, util_code, cfg, periods)
        if dominant is None:
            continue

        top_people = top_nonbillable_people(
            indexed_df, util_code, dominant.project_code, last_pi, cfg
        )

        cur_str  = f"{current_pct:.0%}" if current_pct  is not None else "N/A"
        peer_str = f"{peer_avg:.0%}"    if peer_avg      is not None else "N/A"
        nb_cur   = dominant.period_trend[-1].nonbillable_pct if dominant.period_trend else None
        nb_str   = f"{nb_cur:.0%}"     if nb_cur        is not None else "N/A"
        top_2    = ", ".join(
            f"{p.person} ({p.nonbillable_hours:.0f} hrs NB)" for p in top_people[:2]
        )

        narrative = (
            f"{flag.division} is billing only {cur_str} of project hours this period "
            f"vs {peer_str} across other delivery divisions. "
            f"{dominant.project_title} ({dominant.project_code}) is "
            f"{dominant.share_of_hours:.0%} of the division’s hours, "
            f"with {nb_str} running non-billable this period."
        )
        if top_2:
            narrative += f" Concentrated in: {top_2}."

        results.append(RootCause(
            division=flag.division,
            util_division_code=util_code,
            current_billable_pct=current_pct,
            peer_avg_billable_pct=peer_avg,
            dominant_contract=dominant,
            top_people=top_people,
            narrative=narrative,
        ))

    return results
