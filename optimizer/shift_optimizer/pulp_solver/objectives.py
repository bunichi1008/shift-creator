from __future__ import annotations

import pulp

from .model import RosterModel

PREFERENCES = "weighted_preference_violations"
CHANGES = "changed_cells"
FAIRNESS = "fairness_cost"
BREAK_OVERLAP = "break_overlap_minutes"


def build_objectives(roster: RosterModel) -> dict[str, pulp.LpAffineExpression]:
    settings = roster.problem.objective_settings
    costs = {}
    preferences = []
    for preference in roster.problem.preferences:
        if preference.kind == "prefer_off":
            preferences.append(preference.weight * roster.work[preference.employee_id, preference.date])
        elif preference.kind == "prefer_shift":
            chosen = roster.assignment(preference.employee_id, preference.date, preference.shift_id)
            preferences.append(preference.weight * (1 - chosen))
    costs[PREFERENCES] = pulp.lpSum(preferences)

    if roster.problem.baseline:
        baseline = {row.cell: row.shift_id for row in roster.problem.baseline}
        costs[CHANGES] = pulp.lpSum(
            1 - roster.assignment(*cell, baseline[cell]) if cell in baseline else work
            for cell, work in roster.work.items()
        )

    deviations = []
    for index, employee in enumerate(roster.problem.employees):
        minutes = deviation(roster, f"minutes_deviation_{index}", roster.minutes[employee.id], employee.target_minutes)
        deviations.append(minutes)
        if settings.night_deviation_weight:
            nights = deviation(roster, f"night_deviation_{index}", roster.nights[employee.id], employee.target_nights)
            deviations.append(settings.night_deviation_weight * nights)
    costs[FAIRNESS] = pulp.lpSum(deviations)
    ordered = {stage: costs[stage] for stage in settings.order if stage in costs}
    overlap = roster.break_schedule.overlap_cost(roster)
    if len(overlap):
        ordered[BREAK_OVERLAP] = overlap
    return ordered


def deviation(roster: RosterModel, name: str, expression: pulp.LpAffineExpression, target: int):
    if not len(expression):
        return abs(round(expression.constant) - target)
    delta = roster.mip.add_variable(name, lowBound=0)
    roster.add("deviation_lower", delta >= expression - target)
    roster.add("deviation_upper", delta >= target - expression)
    return delta


def scores(roster: RosterModel, objectives: dict[str, pulp.LpAffineExpression]) -> dict[str, int]:
    result = {name: round(pulp.value(value)) for name, value in objectives.items() if name not in {FAIRNESS, BREAK_OVERLAP}}
    if BREAK_OVERLAP in objectives:
        result[BREAK_OVERLAP] = roster.break_schedule.actual_overlap_minutes()
    # Read the actual assignments' totals, since a time-limited incumbent can
    # contain auxiliary deviation values with avoidable slack.
    night_weight = roster.problem.objective_settings.night_deviation_weight
    result[FAIRNESS] = sum(
        abs(round(pulp.value(roster.minutes[e.id])) - e.target_minutes)
        + night_weight * abs(round(pulp.value(roster.nights[e.id])) - e.target_nights)
        for e in roster.problem.employees
    )
    return result
