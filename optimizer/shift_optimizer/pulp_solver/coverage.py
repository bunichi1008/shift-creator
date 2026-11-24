from dataclasses import dataclass

import pulp

from ..time_staffing import time_staffing_segments
from .model import RosterModel


@dataclass(frozen=True)
class Shortage:
    skill_id: str | None
    scope: dict
    variable: pulp.LpVariable


def add_coverage(roster: RosterModel, *, relax_minimums: bool = False) -> list[Shortage]:
    """Share staffing expressions between the strict model and the diagnostic model."""
    shortages: list[Shortage] = []

    def lower_bound(count, minimum, scope, skill_id=None) -> None:
        if relax_minimums:
            slack = roster.mip.add_variable(f"shortage_{len(shortages)}", lowBound=0, upBound=minimum)
            shortages.append(Shortage(skill_id, scope, slack))
            count = count + slack
        roster.add("staffing_min", count >= minimum)

    def eligible(employee_id, skill_id):
        return skill_id is None or skill_id in roster.employees[employee_id].skills

    def assigned(slots, skill_id=None):
        return pulp.lpSum(variable for slot in slots for employee_id, variable in roster.slots.get(slot, {}).items()
                          if eligible(employee_id, skill_id))

    for row in roster.problem.shift_staffing:
        count = assigned([(row.date, row.shift_id)])
        roster.add("staffing_max", count <= row.maximum)
        if row.minimum:
            lower_bound(count, row.minimum, dict(date=row.date, shift_id=row.shift_id))
    for rule, day, shift in roster.problem.coverage_rule_slots():
        lower_bound(assigned([(day, shift)], rule.skill_id), rule.value,
                    dict(date=day, shift_id=shift, rule_id=rule.id), rule.skill_id)

    for segment in time_staffing_segments(roster.problem):
        skill = segment.rule.skill_id
        already_on_duty = {employee_id for employee_id in segment.historical if eligible(employee_id, skill)}
        on_break = pulp.lpSum(
            variable for slot in segment.slots
            for employee_id, start, end, variable in roster.break_schedule.by_slot.get(slot, ())
            if start <= segment.shift_minute(slot[0]) < end and eligible(employee_id, skill)
        )
        count = assigned(segment.slots, skill) + len(already_on_duty) - on_break
        lower_bound(count, segment.rule.value, segment.issue_scope(), skill)
    return shortages
