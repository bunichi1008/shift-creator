from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, timedelta
from functools import cache

from .calendar_rules import DAY
from .models import Problem, Rule, clock_minutes

MINUTES_PER_DAY = 1440
Slot = tuple[date, str]


@dataclass(frozen=True)
class TimeSegment:
    rule: Rule
    day: date
    start: int
    end: int
    slots: tuple[Slot, ...]

    def issue_scope(self) -> dict:
        def clock(minutes: int) -> str:
            return f"{minutes // 60:02d}:{minutes % 60:02d}"

        return dict(date=self.day, start_time=clock(self.start), end_time=clock(self.end), rule_id=self.rule.id)


def staffing_windows(problem: Problem, rule: Rule) -> Iterator[tuple[date, int, int]]:
    start, end = clock_minutes(rule.start_time), clock_minutes(rule.end_time)
    if rule.all_dates:
        days = (problem.start_date + timedelta(days=index) for index in range(problem.days))
        if end <= start and end:
            yield problem.start_date, 0, end
    else:
        days = (rule.date,)
    for day in days:
        if end > start:
            yield day, start, end
        else:
            yield day, start, MINUTES_PER_DAY
            if end:
                yield day + DAY, 0, end


def time_staffing_segments(problem: Problem) -> Iterator[TimeSegment]:
    history = {(row.date, row.shift_id) for row in problem.history if row.date == problem.start_date - DAY}
    historical = problem.historical_shifts
    working_ranges = {shift.id: tuple(shift.working_ranges()) for shift in problem.shifts}
    historical_ranges = {key: tuple(historical[key].working_ranges()) for _, key in history}

    @cache
    def operating_slots(day: date) -> list[tuple[Slot, int, int]]:
        slots = []
        for shift_day, offset in ((day - DAY, -MINUTES_PER_DAY), (day, 0)):
            if problem.in_period(shift_day):
                ranges = working_ranges
            elif shift_day == problem.start_date - DAY:
                ranges = historical_ranges
            else:
                continue
            for shift_id, intervals in ranges.items():
                for start, end in intervals:
                    if start + offset < MINUTES_PER_DAY and end + offset > 0:
                        slots.append(((shift_day, shift_id), start + offset, end + offset))
        return slots

    for rule in problem.rules:
        if rule.type != "min_time_coverage":
            continue
        for day, start, end in staffing_windows(problem, rule):
            slots = operating_slots(day)
            # Staffing changes at the boundaries of work and breaks. Include
            # uncovered segments so a staffing gap becomes an infeasibility.
            boundaries = sorted({start, end} | {minute for _, first, last in slots
                                                 for minute in (first, last) if start < minute < end})
            for left, right in zip(boundaries, boundaries[1:]):
                active = tuple(slot for slot, first, last in slots if first <= left and right <= last)
                yield TimeSegment(rule, day, left, right, active)
