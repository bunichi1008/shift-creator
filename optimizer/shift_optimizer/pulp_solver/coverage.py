from dataclasses import dataclass
from datetime import date

import pulp

from .model import RosterModel


@dataclass(frozen=True)
class Shortage:
    date: date
    shift_id: str
    skill_id: str | None
    rule_id: str | None
    variable: pulp.LpVariable


def add_coverage(roster: RosterModel, *, relax_minimums: bool = False) -> list[Shortage]:
    """Share staffing expressions between the strict model and the diagnostic model."""
    shortages: list[Shortage] = []

    def lower_bound(day: date, shift_id: str, minimum: int, skill_id: str | None = None,
                    rule_id: str | None = None) -> None:
        candidates = roster.slots[day, shift_id]
        count = pulp.lpSum(variable for employee_id, variable in candidates.items()
                           if skill_id is None or skill_id in roster.employees[employee_id].skills)
        if relax_minimums:
            slack = roster.mip.add_variable(f"shortage_{len(shortages)}", lowBound=0, upBound=minimum)
            shortages.append(Shortage(day, shift_id, skill_id, rule_id, slack))
            count += slack
        roster.add("staffing_min", count >= minimum)

    for row in roster.problem.coverage:
        if row.minimum:
            lower_bound(row.date, row.shift_id, row.minimum)
        roster.add("staffing_max", pulp.lpSum(roster.slots[row.date, row.shift_id].values()) <= row.maximum)
    for rule in roster.problem.rules:
        if rule.type in {"min_coverage", "min_skill_coverage"}:
            lower_bound(rule.date, rule.shift_id, rule.value, rule.skill_id, rule.id)
    return shortages
