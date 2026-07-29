"""
Workforce health: attrition and PTO liability derived entirely from the
already-parsed LoadedData — no additional Excel parsing required.

loader.py already captures first_day / last_day / pto_balance_available
on every EmployeeProfile; this module just aggregates them.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .loader import LoadedData


@dataclass
class PersonPTO:
    person: str
    hours_available: float


@dataclass
class WorkforceHealth:
    headcount_current: int
    hires_ytd: int
    departures_ytd: int
    net_change_ytd: int
    high_pto_liability: list[PersonPTO]    # sorted descending, capped at 10
    negative_pto_balances: list[PersonPTO]  # sorted ascending (most negative first), capped at 10


def compute_workforce_health(
    data: LoadedData,
    cfg: dict,
    as_of_year: int,
) -> WorkforceHealth:
    """
    hires_ytd: profiles whose first_day falls in as_of_year.
    departures_ytd: profiles whose last_day falls in as_of_year.

    high_pto_liability: pto_balance_available >= high_pto_threshold_hours
      (default 150), sorted descending, capped at 10.
    negative_pto_balances: pto_balance_available < 0, sorted ascending
      (most negative first), capped at 10.

    Profiles where pto_balance_available is None are skipped — those map to
    excluded_no_pto / flagged_no_timesheet and are already surfaced separately.
    """
    threshold = cfg.get("workforce", {}).get("high_pto_threshold_hours", 150)

    hires      = 0
    departures = 0
    high_pto: list[PersonPTO]     = []
    negative_pto: list[PersonPTO] = []

    for person, prof in data.employee_profiles.items():
        if prof.first_day is not None and prof.first_day.year == as_of_year:
            hires += 1
        if prof.last_day is not None and prof.last_day.year == as_of_year:
            departures += 1

        if prof.pto_balance_available is None:
            continue

        bal = prof.pto_balance_available
        if bal >= threshold:
            high_pto.append(PersonPTO(person=person, hours_available=bal))
        elif bal < 0:
            negative_pto.append(PersonPTO(person=person, hours_available=bal))

    high_pto.sort(key=lambda x: x.hours_available, reverse=True)
    negative_pto.sort(key=lambda x: x.hours_available)

    return WorkforceHealth(
        headcount_current=len(data.all_employees),
        hires_ytd=hires,
        departures_ytd=departures,
        net_change_ytd=hires - departures,
        high_pto_liability=high_pto[:10],
        negative_pto_balances=negative_pto[:10],
    )
