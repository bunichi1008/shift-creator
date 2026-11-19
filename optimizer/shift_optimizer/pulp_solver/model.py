from __future__ import annotations

from collections import Counter
from datetime import date, timedelta
from math import isfinite

import pulp

from ..models import Assignment, Cell, Problem


class RosterModel:
    """Binary assignments and indexes shared by constraints and objectives."""

    def __init__(self, problem: Problem):
        self.problem = problem
        self.mip = pulp.LpProblem("shift_roster", pulp.LpMinimize)
        self.days = tuple(problem.start_date + timedelta(days=offset) for offset in range(problem.days))
        self.employees = {row.id: row for row in problem.employees}
        self.shifts = {row.id: row for row in problem.shifts}
        self.cells: dict[Cell, dict[str, pulp.LpVariable]] = {}
        self.slots: dict[tuple[date, str], dict[str, pulp.LpVariable]] = {
            (day, shift_id): {} for day in self.days for shift_id in self.shifts
        }
        self.work: dict[Cell, pulp.LpAffineExpression] = {}
        self.minutes: dict[str, pulp.LpAffineExpression] = {}
        self.nights: dict[str, pulp.LpAffineExpression] = {}
        self._constraint_count: Counter[str] = Counter()
        forbidden = problem.unavailable_slots()
        for employee_index, employee in enumerate(problem.employees):
            minutes = []
            nights = []
            for day_index, day in enumerate(self.days):
                cell = employee.id, day
                unavailable = forbidden.get(cell, set())
                choices = {}
                if None not in unavailable:
                    for shift_index, shift in enumerate(problem.shifts):
                        if shift.id in unavailable:
                            continue
                        variable = self.mip.add_variable(
                            f"x_{employee_index}_{day_index}_{shift_index}", cat=pulp.LpBinary
                        )
                        choices[shift.id] = variable
                        self.slots[day, shift.id][employee.id] = variable
                        minutes.append(shift.paid_minutes * variable)
                        if shift.id == problem.limits.night_shift_id:
                            nights.append(variable)
                self.cells[cell] = choices
                self.work[cell] = pulp.lpSum(choices.values())
            self.minutes[employee.id] = pulp.lpSum(minutes)
            self.nights[employee.id] = pulp.lpSum(nights)

    def assignment(self, employee_id: str, day: date, shift_id: str) -> pulp.LpVariable | int:
        return self.cells[employee_id, day].get(shift_id, 0)

    def add(self, name: str, constraint: pulp.LpConstraint) -> None:
        index = self._constraint_count[name]
        self._constraint_count[name] += 1
        self.mip += constraint, f"{name}_{index}"

    def extract(self) -> list[Assignment]:
        assignments = []
        for (employee_id, day), choices in self.cells.items():
            for shift_id, variable in choices.items():
                value = variable.varValue
                if value is None or not isfinite(value) or min(abs(value), abs(value - 1)) > 1e-5:
                    raise ValueError(f"The solver returned a non-binary value for {variable.name}: {value}")
                if value > 0.5:
                    assignments.append(Assignment(employee_id=employee_id, date=day, shift_id=shift_id))
        return sorted(assignments, key=lambda row: (row.date, row.employee_id))
