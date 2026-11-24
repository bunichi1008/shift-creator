from __future__ import annotations

from datetime import timedelta

import pulp

from ..calendar_rules import conflict_reason
from .model import RosterModel


def add_work_rules(roster: RosterModel) -> None:
    for count in roster.work.values():
        roster.add("daily_assignment", count <= 1)
    for row in roster.problem.fixed_slots():
        if row.shift_id == "off":
            roster.add("fixed_day_off", roster.work[row.cell] == 0)
        else:
            chosen = roster.assignment(row.employee_id, row.date, row.shift_id)
            roster.add("fixed_shift", pulp.lpSum([chosen]) == 1)
    add_rest_rules(roster)
    add_workload_limits(roster)


def add_rest_rules(roster: RosterModel) -> None:
    problem = roster.problem
    # A shift lasts less than a day and the minimum rest is at most one day.
    # With distinct start dates, only the next two dates can conflict.
    conflicts = {
        (first.id, offset): tuple(second.id for second in problem.shifts if conflict_reason(
            problem.limits, problem.start_date, first, problem.start_date + timedelta(days=offset), second
        ))
        for first in problem.shifts for offset in (1, 2)
    }
    for (employee_id, day), choices in roster.cells.items():
        for offset in (1, 2):
            later = roster.cells.get((employee_id, day + timedelta(days=offset)))
            if not later:
                continue
            for shift_id, variable in choices.items():
                blocked = [later[key] for key in conflicts[shift_id, offset] if key in later]
                if blocked:
                    # The later day already allows at most one assignment.
                    roster.add("rest", variable + pulp.lpSum(blocked) <= 1)

    historical = problem.historical_shifts
    for row in problem.history:
        shift = historical[row.shift_id]
        for offset in (1, 2):
            day = row.date + timedelta(days=offset)
            later = roster.cells.get((row.employee_id, day))
            if later:
                blocked = [variable for key, variable in later.items()
                           if conflict_reason(problem.limits, row.date, shift, day, roster.shifts[key])]
                if blocked:
                    roster.add("history_rest", pulp.lpSum(blocked) == 0)


def add_workload_limits(roster: RosterModel) -> None:
    problem = roster.problem
    past = {row.cell for row in problem.history}
    minute_limit = {row.id: row.max_minutes for row in problem.employees}
    consecutive_limit = dict.fromkeys(roster.employees, problem.limits.max_consecutive)
    for rule in problem.rules:
        if rule.type == "max_work_minutes":
            minute_limit[rule.employee_id] = min(minute_limit[rule.employee_id], rule.value)
        elif rule.type == "max_consecutive":
            targets = (rule.employee_id,) if rule.employee_id else roster.employees
            for employee_id in targets:
                consecutive_limit[employee_id] = min(consecutive_limit[employee_id], rule.value)

    for employee_id, limit in minute_limit.items():
        roster.add("work_minutes", roster.minutes[employee_id] <= limit)
    for employee_id, limit in consecutive_limit.items():
        for end_index, day in enumerate(roster.days):
            earliest = max(0, end_index - limit)
            current_days = roster.days[earliest:end_index + 1]
            previous = sum((employee_id, day - timedelta(days=offset)) in past
                           for offset in range(end_index + 1, limit + 1))
            if len(current_days) + previous > limit:
                total = pulp.lpSum(roster.work[employee_id, item] for item in current_days)
                roster.add("consecutive", total <= limit - previous)
