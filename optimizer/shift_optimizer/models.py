from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterator
from datetime import date as Date, timedelta
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, FiniteFloat, model_validator

ID = Annotated[str, Field(pattern=r"^[a-zA-Z0-9_-]{1,48}$")]
Name = Annotated[str, Field(min_length=1, max_length=80)]
Clock = Annotated[str, Field(pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")]
Minutes = Annotated[int, Field(strict=True, ge=0, le=44_640)]
RuleType = Literal[
    "min_skill_coverage", "min_coverage", "unavailable", "fixed", "max_consecutive",
    "max_work_minutes", "min_time_coverage",
]
ObjectiveStage = Literal["weighted_preference_violations", "changed_cells", "fairness_cost"]
DEFAULT_OBJECTIVE_ORDER: tuple[ObjectiveStage, ...] = (
    "weighted_preference_violations", "changed_cells", "fairness_cost",
)


def date_only(value: object) -> object:
    if type(value) is Date or (isinstance(value, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value)):
        return value
    raise ValueError("date must be a YYYY-MM-DD calendar date")


CalendarDate = Annotated[Date, BeforeValidator(date_only)]
Cell = tuple[str, Date]


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Employee(Record):
    id: ID
    name: Name
    skills: list[ID] = Field(max_length=20)
    target_minutes: Minutes
    max_minutes: Minutes
    target_nights: int = Field(strict=True, ge=0, le=31)

    @model_validator(mode="after")
    def check_target(self) -> Employee:
        if self.target_minutes > self.max_minutes:
            raise ValueError("target_minutes exceeds max_minutes")
        if len(set(self.skills)) != len(self.skills):
            raise ValueError("duplicate skill IDs")
        return self


class ShiftBreak(Record):
    start: Clock
    end: Clock
    latest_start: Clock | None = None


class Shift(Record):
    id: ID
    name: Name
    start: Clock
    end: Clock
    paid_minutes: int = Field(strict=True, gt=0, le=1440)
    breaks: list[ShiftBreak] = Field(default_factory=list, max_length=12)

    @model_validator(mode="after")
    def check_duration(self) -> Shift:
        start, end = self.clock_range()
        if self.id == "off" or start == end:
            raise ValueError("invalid working shift or paid duration")
        previous_end = start
        for first, last in self.break_ranges():
            if first < previous_end or last > end or first == last:
                raise ValueError(f"breaks in {self.name} must be inside the shift and must not overlap")
            previous_end = last
        for item in self.breaks:
            first = self.shift_minute(item.start)
            last = self.shift_minute(item.latest_start or item.start)
            duration = (clock_minutes(item.end) - clock_minutes(item.start)) % 1440
            if not duration or last < first or last + duration > end:
                raise ValueError(f"breaks in {self.name} must be inside the shift")
        if any(not choices for choices in self.flexible_break_options()):
            raise ValueError(f"a flexible break in {self.name} has no start time outside fixed breaks")
        unpaid = sum((clock_minutes(item.end) - clock_minutes(item.start)) % 1440 for item in self.breaks)
        if self.paid_minutes > end - start - unpaid:
            raise ValueError(f"paid duration in {self.name} exceeds working time after breaks")
        return self

    def clock_range(self) -> tuple[int, int]:
        start = clock_minutes(self.start)
        return start, start + (clock_minutes(self.end) - start) % 1440

    def break_ranges(self) -> list[tuple[int, int]]:
        return sorted((self.shift_minute(item.start), self.shift_minute(item.start) +
                       (clock_minutes(item.end) - clock_minutes(item.start)) % 1440)
                      for item in self.breaks if item.latest_start is None)

    def shift_minute(self, clock: str) -> int:
        start = clock_minutes(self.start)
        return start + (clock_minutes(clock) - start) % 1440

    def flexible_break_options(self) -> tuple[tuple[tuple[int, int], ...], ...]:
        fixed = self.break_ranges()
        options = []
        for item in self.breaks:
            if item.latest_start is None:
                continue
            first, latest = self.shift_minute(item.start), self.shift_minute(item.latest_start)
            length = (clock_minutes(item.end) - clock_minutes(item.start)) % 1440
            starts = sorted(set(range(first, latest + 1, 5)) | {latest})
            options.append(tuple((start, start + length) for start in starts
                                 if not any(start < right and left < start + length for left, right in fixed)))
        return tuple(options)

    def selected_flexible_breaks(self, starts: list[str] | None) -> tuple[tuple[int, int], ...] | None:
        options = self.flexible_break_options()
        if len(starts or []) != len(options):
            return None
        selected = []
        for clock, choices in zip(starts or [], options):
            start = self.shift_minute(clock)
            match = next((choice for choice in choices if choice[0] == start), None)
            if match is None:
                return None
            selected.append(match)
        ordered = sorted(selected)
        if any(left[1] > right[0] for left, right in zip(ordered, ordered[1:])):
            return None
        return tuple(selected)

    def working_ranges(self, flexible: tuple[tuple[int, int], ...] = ()) -> Iterator[tuple[int, int]]:
        first, end = self.clock_range()
        for break_start, break_end in sorted([*self.break_ranges(), *flexible]):
            if first < break_start:
                yield first, break_start
            first = max(first, break_end)
        if first < end:
            yield first, end


def clock_minutes(value: str) -> int:
    hour, minute = map(int, value.split(":"))
    return hour * 60 + minute


class Coverage(Record):
    date: CalendarDate
    shift_id: ID
    minimum: int = Field(strict=True, ge=0, le=100)
    maximum: int = Field(strict=True, ge=0, le=100)


class Assignment(Record):
    employee_id: ID
    date: CalendarDate
    shift_id: ID
    break_starts: list[Clock] | None = Field(default=None, max_length=12)

    @property
    def cell(self) -> Cell:
        return self.employee_id, self.date


class Preference(Record):
    employee_id: ID
    date: CalendarDate
    kind: Literal["unavailable", "prefer_off", "prefer_shift"]
    shift_id: ID | None = None
    weight: int = Field(strict=True, ge=1, le=100)


class Rule(Record):
    id: ID
    type: RuleType
    date: CalendarDate | None = None
    employee_id: ID | None = None
    shift_id: ID | None = None
    all_dates: bool = Field(default=False, strict=True)
    all_shifts: bool = Field(default=False, strict=True)
    skill_id: ID | None = None
    start_time: Clock | None = None
    end_time: Clock | None = None
    value: int | None = Field(default=None, strict=True, ge=0, le=44_640)


class Limits(Record):
    max_consecutive: int = Field(strict=True, ge=1, le=31)
    min_rest_minutes: int = Field(strict=True, ge=0, le=1440)
    night_shift_id: ID | None = None
    no_next_day_after_night: bool = Field(strict=True)


class Optimization(Record):
    order: tuple[ObjectiveStage, ...] = Field(default=DEFAULT_OBJECTIVE_ORDER, min_length=3, max_length=3)
    night_deviation_weight: int = Field(default=480, strict=True, ge=0, le=1440)

    @model_validator(mode="after")
    def check_order(self) -> Optimization:
        if len(set(self.order)) != len(self.order):
            raise ValueError("optimization order must include each objective once")
        return self


class Problem(Record):
    schema_version: Literal[1]
    start_date: CalendarDate
    days: int = Field(strict=True, ge=1, le=31)
    employees: list[Employee] = Field(min_length=1, max_length=100)
    shifts: list[Shift] = Field(min_length=1, max_length=12)
    history_shifts: list[Shift] = Field(default_factory=list, max_length=100)
    coverage: list[Coverage] = Field(max_length=372)
    use_shift_coverage: bool = Field(default=True, strict=True)
    preferences: list[Preference] = Field(max_length=3100)
    rules: list[Rule] = Field(max_length=120)
    locks: list[Assignment] = Field(max_length=3100)
    baseline: list[Assignment] = Field(max_length=3100)
    history: list[Assignment] = Field(max_length=3100)
    limits: Limits
    optimization: Optimization | None = None

    @property
    def objective_settings(self) -> Optimization:
        return self.optimization if self.optimization is not None else Optimization()

    @property
    def end_date(self) -> Date:
        return self.start_date + timedelta(days=self.days - 1)

    @property
    def shift_staffing(self) -> list[Coverage]:
        return self.coverage if self.use_shift_coverage else []

    @property
    def historical_shifts(self) -> dict[str, Shift]:
        return {shift.id: shift for shift in [*self.shifts, *self.history_shifts]}

    @model_validator(mode="after")
    def check_references(self) -> Problem:
        if not 2000 <= self.start_date.year <= 2099:
            raise ValueError("start_date must be in 2000–2099")
        employees = {item.id for item in self.employees}
        shifts = {item.id for item in self.shifts}
        skills = {skill for item in self.employees for skill in item.skills}
        if len(employees) != len(self.employees) or len(shifts) != len(self.shifts):
            raise ValueError("duplicate employee or shift IDs")
        check_unique([shift.id for shift in self.history_shifts], "historical shift ID")
        historical = self.historical_shifts
        if self.limits.night_shift_id and self.limits.night_shift_id not in shifts:
            raise ValueError("unknown night shift")
        if self.limits.no_next_day_after_night and not self.limits.night_shift_id:
            raise ValueError("night shift required for the day-after-night rule")
        check_unique([(p.date, p.shift_id) for p in self.coverage], "coverage slot")
        for row in self.coverage:
            if row.shift_id not in shifts or not self.in_period(row.date) or row.minimum > row.maximum:
                raise ValueError(f"invalid staffing requirement for {row.date} / {row.shift_id}")
        check_unique(
            [(p.employee_id, p.date, p.kind, p.shift_id) for p in self.preferences], "preference"
        )
        for row in self.preferences:
            if row.employee_id not in employees or not self.in_period(row.date):
                raise ValueError(f"unknown employee or date in preference for {row.employee_id}")
            if row.kind == "prefer_shift":
                if row.shift_id not in shifts:
                    raise ValueError("a shift preference requires a working shift")
            elif row.shift_id and (row.kind != "unavailable" or row.shift_id not in shifts):
                raise ValueError("unexpected or unknown shift in preference")
        for field, rows in (("baseline", self.baseline), ("locks", self.locks), ("history", self.history)):
            check_unique([row.cell for row in rows], f"{field} cell")
            for row in rows:
                usable_shift = row.shift_id in (historical if field == "history" else shifts) or (
                    field == "locks" and row.shift_id == "off"
                )
                usable_date = (
                    self.start_date - timedelta(days=31) <= row.date < self.start_date
                    if field == "history" else self.in_period(row.date)
                )
                if row.employee_id not in employees or not usable_shift or not usable_date:
                    raise ValueError(f"unknown employee, shift or date in {field}: {row.cell}")
                if field == "history" and historical[row.shift_id].selected_flexible_breaks(row.break_starts) is None:
                    raise ValueError(f"history requires actual flexible break times: {row.cell}")
        check_unique([r.id for r in self.rules], "rule ID")
        for rule in self.rules:
            self.check_rule(rule, employees, shifts, skills)
        return self

    def unavailable_slots(self) -> dict[Cell, set[str | None]]:
        result = defaultdict(set)
        for preference in self.preferences:
            if preference.kind == "unavailable":
                result[preference.employee_id, preference.date].add(preference.shift_id)
        for rule in self.rules:
            if rule.type == "unavailable":
                result[rule.employee_id, rule.date].add(rule.shift_id)
        return result

    def fixed_slots(self) -> Iterator[Assignment]:
        yield from self.locks
        for rule in self.rules:
            if rule.type == "fixed":
                yield Assignment(employee_id=rule.employee_id, date=rule.date, shift_id=rule.shift_id)

    def in_period(self, value: Date) -> bool:
        return self.start_date <= value <= self.end_date

    def coverage_rule_slots(self) -> Iterator[tuple[Rule, Date, str]]:
        days = tuple(self.start_date + timedelta(days=i) for i in range(self.days))
        shifts = tuple(shift.id for shift in self.shifts)
        for rule in self.rules:
            if rule.type in {"min_coverage", "min_skill_coverage"}:
                for day in days if rule.all_dates else (rule.date,):
                    for shift in shifts if rule.all_shifts else (rule.shift_id,):
                        yield rule, day, shift

    def check_rule(self, rule: Rule, employees: set[str], shifts: set[str], skills: set[str]) -> None:
        required, optional = {
            "min_skill_coverage": ({"date", "shift_id", "skill_id", "value"}, set()),
            "min_coverage": ({"date", "shift_id", "value"}, set()),
            "min_time_coverage": ({"date", "start_time", "end_time", "value"}, {"skill_id"}),
            "unavailable": ({"date", "employee_id"}, {"shift_id"}),
            "fixed": ({"date", "employee_id", "shift_id"}, set()),
            "max_consecutive": ({"value"}, {"employee_id"}),
            "max_work_minutes": ({"employee_id", "value"}, set()),
        }[rule.type]
        if rule.type in {"min_coverage", "min_skill_coverage", "min_time_coverage"}:
            if rule.all_dates:
                required = required - {"date"} | {"all_dates"}
            if rule.all_shifts and rule.type != "min_time_coverage":
                required = required - {"shift_id"} | {"all_shifts"}
        fields = rule.model_dump(exclude={"id", "type"}, exclude_defaults=True).keys()
        if not required <= fields <= required | optional:
            raise ValueError(f"missing or unexpected fields in rule {rule.id}")
        if rule.date is not None and not self.in_period(rule.date):
            raise ValueError(f"date outside the period in rule {rule.id}")
        if rule.type == "min_time_coverage" and rule.start_time == rule.end_time != "00:00":
            raise ValueError(f"start and end times must differ in rule {rule.id}")
        if rule.employee_id is not None and rule.employee_id not in employees:
            raise ValueError(f"unknown employee in rule {rule.id}")
        if rule.shift_id is not None and rule.shift_id not in shifts:
            if rule.type != "fixed" or rule.shift_id != "off":
                raise ValueError(f"unknown shift in rule {rule.id}")
        if rule.skill_id is not None and rule.skill_id not in skills:
            raise ValueError(f"unknown skill in rule {rule.id}")
        if rule.type in {"min_coverage", "min_skill_coverage", "min_time_coverage"} and not 1 <= rule.value <= 100:
            raise ValueError(f"staffing rule must use a value from 1 to 100: {rule.id}")
        if rule.type == "max_consecutive" and not 1 <= rule.value <= 31:
            raise ValueError(f"consecutive limit must be from 1 to 31: {rule.id}")


def check_unique(values: list, label: str) -> None:
    if len(set(values)) != len(values):
        raise ValueError(f"duplicate {label}")


class SolveRequest(Record):
    problem: Problem
    time_limit_seconds: int = Field(strict=True, ge=1, le=60)


class ValidationRequest(Record):
    problem: Problem
    assignments: list[Assignment] = Field(max_length=3100)


class Issue(Record):
    code: str
    message: str
    employee_id: ID | None = None
    date: CalendarDate | None = None
    shift_id: ID | None = None
    rule_id: ID | None = None
    start_time: str | None = None
    end_time: str | None = None


class EmployeeMetrics(Record):
    employee_id: ID
    minutes: int
    nights: int
    minutes_deviation: int
    nights_deviation: int


class Metrics(Record):
    preference_total: int
    preference_met: int
    weighted_preference_violations: int
    covered_slots: int
    total_slots: int
    covered_time_segments: int = 0
    total_time_segments: int = 0
    changed_cells: int | None
    employees: list[EmployeeMetrics]


class Report(Record):
    issues: list[Issue]
    metrics: Metrics


class SolveResult(Record):
    status: Literal[
        "optimal", "feasible", "infeasible", "no_solution",
        "solver_unavailable", "demo_valid", "demo_invalid", "error",
    ]
    solver: str
    message: str
    assignments: list[Assignment] = Field(default_factory=list, max_length=3100)
    report: Report | None = None
    diagnostics: list[Issue] = Field(default_factory=list, max_length=3100)
    objective: dict[str, FiniteFloat] = Field(default_factory=dict)
    best_bound: FiniteFloat | None = None
    gap: FiniteFloat | None = None
    elapsed_ms: int = 0
