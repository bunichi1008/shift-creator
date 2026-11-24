from collections import defaultdict
from datetime import date
from typing import TYPE_CHECKING

import pulp

if TYPE_CHECKING:
    from .model import RosterModel


class BreakSchedule:
    def __init__(self, roster: "RosterModel"):
        self.choices = {}
        self.starts = {}
        self.by_slot = defaultdict(list)
        self.overlap_intervals = []
        layouts = {shift.id: shift.flexible_break_options() for shift in roster.shifts.values()}
        exclusions = {shift_id: overlap_sets(groups) for shift_id, groups in layouts.items() if len(groups) > 1}
        for (day, shift_id), assignments in roster.slots.items():
            groups = layouts[shift_id]
            if not groups:
                continue
            for employee_id, shift_var in assignments.items():
                key = employee_id, day, shift_id
                choices = []
                for index, options in enumerate(groups):
                    variables = [
                        roster.mip.add_variable(f"break_{shift_var.name}_{index}_{start}", cat=pulp.LpBinary)
                        for start, _ in options
                    ]
                    roster.add("select_break", pulp.lpSum(variables) == shift_var)
                    choices.append(variables)
                    self.by_slot[day, shift_id].extend(
                        (employee_id, start, end, variable) for (start, end), variable in zip(options, variables)
                    )
                for together in exclusions.get(shift_id, ()):
                    roster.add("break_overlap", pulp.lpSum(choices[group][option] for group, option in together) <= shift_var)
                self.choices[key] = choices
                self.starts[key] = [[start for start, _ in options] for options in groups]

    def chosen_starts(self, employee_id: str, day: date, shift_id: str) -> list[str] | None:
        key = employee_id, day, shift_id
        if key not in self.choices:
            return None
        starts = []
        for choices, possible in zip(self.choices[key], self.starts[key]):
            selected = [start for var, start in zip(choices, possible) if (var.varValue or 0) > 0.5]
            if len(selected) != 1:
                raise ValueError("solver did not choose exactly one start for a flexible break")
            hour, minute = divmod(selected[0] % 1440, 60)
            starts.append(f"{hour:02d}:{minute:02d}")
        return starts

    def overlap_cost(self, roster: "RosterModel"):
        if not self.by_slot:
            return pulp.lpSum([])
        events = defaultdict(list)
        for (day, shift_id), assignments in roster.slots.items():
            origin = day.toordinal() * 1440
            schedules = [(employee, start, end, var, True)
                         for employee, start, end, var in self.by_slot.get((day, shift_id), ())]
            schedules.extend((employee, start, end, var, False)
                             for employee, var in assignments.items()
                             for start, end in roster.shifts[shift_id].break_ranges())
            for employee, start, end, var, flexible in schedules:
                item = (employee, var, flexible)
                events[origin + start].append((item, 1))
                events[origin + end].append((item, -1))

        active = {}
        penalties = []
        points = sorted(events)
        for left, right in zip(points, points[1:]):
            for item, delta in events[left]:
                count = active.get(item, 0) + delta
                if count:
                    active[item] = count
                else:
                    active.pop(item, None)
            if not any(flexible for _, _, flexible in active) or len({employee for employee, _, _ in active}) < 2:
                continue
            variables = tuple(var for _, var, _ in active)
            slack = roster.mip.add_variable(f"break_overlap_minutes_{len(penalties)}", lowBound=0)
            roster.add("concurrent_breaks", slack >= pulp.lpSum(variables) - 1)
            self.overlap_intervals.append((right - left, variables))
            penalties.append((right - left) * slack)
        return pulp.lpSum(penalties)

    def actual_overlap_minutes(self) -> int:
        return sum(duration * max(0, sum((var.varValue or 0) > 0.5 for var in variables) - 1)
                   for duration, variables in self.overlap_intervals)


def overlap_sets(groups) -> set[tuple[tuple[int, int], ...]]:
    intervals = [(group, option, start, end) for group, options in enumerate(groups)
                 for option, (start, end) in enumerate(options)]
    overlaps = set()
    for point in {start for _, _, start, _ in intervals}:
        active = tuple((group, option) for group, option, start, end in intervals if start <= point < end)
        if len({group for group, _ in active}) > 1:
            overlaps.add(active)
    return overlaps
