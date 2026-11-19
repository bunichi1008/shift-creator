from __future__ import annotations

import re
from datetime import date as Date, timedelta
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, FiniteFloat, model_validator

ID = Annotated[str, Field(pattern=r"^[a-zA-Z0-9_-]{1,48}$")]
Name = Annotated[str, Field(min_length=1, max_length=80)]
Clock = Annotated[str, Field(pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")]
Minutes = Annotated[int, Field(strict=True, ge=0, le=44_640)]
RuleType = Literal[
    "min_skill_coverage", "min_coverage", "unavailable", "fixed", "max_consecutive",
    "max_work_minutes",
]
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


class Shift(Record):
    id: ID
    name: Name
    start: Clock
    end: Clock
    paid_minutes: int = Field(strict=True, gt=0, le=1440)

    @model_validator(mode="after")
    def check_duration(self) -> Shift:
        start = clock_minutes(self.start)
        end = clock_minutes(self.end)
        if self.id == "off" or start == end or self.paid_minutes > (end - start) % 1440:
            raise ValueError("invalid working shift or paid duration")
        return self


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
    skill_id: ID | None = None
    value: int | None = Field(default=None, strict=True, ge=0, le=44_640)


class Limits(Record):
    max_consecutive: int = Field(strict=True, ge=1, le=31)
    min_rest_minutes: int = Field(strict=True, ge=0, le=1440)
    night_shift_id: ID | None = None
    no_next_day_after_night: bool = Field(strict=True)


class Problem(Record):
    schema_version: Literal[1]
    start_date: CalendarDate
    days: int = Field(strict=True, ge=1, le=31)
    employees: list[Employee] = Field(min_length=1, max_length=100)
    shifts: list[Shift] = Field(min_length=1, max_length=12)
    coverage: list[Coverage] = Field(max_length=372)
    preferences: list[Preference] = Field(max_length=3100)
    rules: list[Rule] = Field(max_length=120)
    locks: list[Assignment] = Field(max_length=3100)
    baseline: list[Assignment] = Field(max_length=3100)
    history: list[Assignment] = Field(max_length=3100)
    limits: Limits

    @property
    def end_date(self) -> Date:
        return self.start_date + timedelta(days=self.days - 1)

    @model_validator(mode="after")
    def check_references(self) -> Problem:
        if not 2000 <= self.start_date.year <= 2099:
            raise ValueError("start_date must be in 2000–2099")
        employees = {item.id for item in self.employees}
        shifts = {item.id for item in self.shifts}
        skills = {skill for item in self.employees for skill in item.skills}
        if len(employees) != len(self.employees) or len(shifts) != len(self.shifts):
            raise ValueError("duplicate employee or shift IDs")
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
                usable_shift = row.shift_id in shifts or (field == "locks" and row.shift_id == "off")
                usable_date = (
                    self.start_date - timedelta(days=31) <= row.date < self.start_date
                    if field == "history" else self.in_period(row.date)
                )
                if row.employee_id not in employees or not usable_shift or not usable_date:
                    raise ValueError(f"unknown employee, shift or date in {field}: {row.cell}")
        check_unique([r.id for r in self.rules], "rule ID")
        for rule in self.rules:
            self.check_rule(rule, employees, shifts, skills)
        return self

    def in_period(self, value: Date) -> bool:
        return self.start_date <= value <= self.end_date

    def check_rule(self, rule: Rule, employees: set[str], shifts: set[str], skills: set[str]) -> None:
        required, optional = {
            "min_skill_coverage": ({"date", "shift_id", "skill_id", "value"}, set()),
            "min_coverage": ({"date", "shift_id", "value"}, set()),
            "unavailable": ({"date", "employee_id"}, {"shift_id"}),
            "fixed": ({"date", "employee_id", "shift_id"}, set()),
            "max_consecutive": ({"value"}, {"employee_id"}),
            "max_work_minutes": ({"employee_id", "value"}, set()),
        }[rule.type]
        fields = rule.model_dump(exclude={"id", "type"}, exclude_none=True).keys()
        if not required <= fields <= required | optional:
            raise ValueError(f"missing or unexpected fields in rule {rule.id}")
        if rule.date is not None and not self.in_period(rule.date):
            raise ValueError(f"date outside the period in rule {rule.id}")
        if rule.employee_id is not None and rule.employee_id not in employees:
            raise ValueError(f"unknown employee in rule {rule.id}")
        if rule.shift_id is not None and rule.shift_id not in shifts:
            if rule.type != "fixed" or rule.shift_id != "off":
                raise ValueError(f"unknown shift in rule {rule.id}")
        if rule.skill_id is not None and rule.skill_id not in skills:
            raise ValueError(f"unknown skill in rule {rule.id}")
        if rule.type in {"min_coverage", "min_skill_coverage"} and not 1 <= rule.value <= 100:
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
