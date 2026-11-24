import unittest
from unittest.mock import patch
from datetime import date, timedelta

import pulp
from pydantic import ValidationError

from shift_optimizer.models import (
    Assignment, Coverage, Employee, Limits, Optimization, Preference, Problem, Rule, Shift, ShiftBreak, SolveRequest,
)
from shift_optimizer.pulp_solver import optimize, solve
from shift_optimizer.pulp_solver.coverage import add_coverage
from shift_optimizer.pulp_solver.model import RosterModel
from shift_optimizer.pulp_solver.work_rules import add_work_rules
from shift_optimizer.solver import run_solver
from shift_optimizer.validation import validate

START = date(2026, 12, 1)
DAY = timedelta(days=1)
DAY_SHIFT = Shift(id="day", name="日勤", start="08:00", end="16:00", paid_minutes=480)
NIGHT = Shift(id="night", name="夜勤", start="22:00", end="07:00", paid_minutes=480)


def employee(id_, *, skills=(), maximum=3000, target=480, nights=0):
    return Employee(id=id_, name=id_, skills=list(skills), target_minutes=target,
                    max_minutes=maximum, target_nights=nights)


def problem(**changes):
    options = dict(
        schema_version=1, start_date=START, days=2, employees=[employee("first", skills=["leader"]), employee("second")],
        shifts=[DAY_SHIFT, NIGHT], coverage=[], preferences=[], rules=[], locks=[], baseline=[], history=[],
        limits=Limits(max_consecutive=5, min_rest_minutes=660, night_shift_id="night", no_next_day_after_night=True),
    )
    return Problem(**(options | changes))


def solve_constraints(p):
    roster = RosterModel(p)
    add_coverage(roster)
    add_work_rules(roster)
    roster.mip.setObjective(pulp.lpSum([]))
    stats = roster.mip.solve(pulp.HiGHS(msg=False, threads=1, timeLimit=3, gapRel=0))
    return stats, roster.extract() if stats.has_solution else []


class ConstraintTest(unittest.TestCase):
    def test_coverage_skill_and_daily_assignment(self):
        p = problem(
            coverage=[Coverage(date=START, shift_id=shift, minimum=1, maximum=1) for shift in ("day", "night")],
            rules=[Rule(id="lead", type="min_skill_coverage", date=START, shift_id="night", skill_id="leader", value=1)],
        )
        stats, rows = solve_constraints(p)
        self.assertEqual(stats.status, pulp.LpSolveStatus.Optimal)
        self.assertEqual({(row.employee_id, row.shift_id) for row in rows if row.date == START},
                         {("first", "night"), ("second", "day")})
        self.assertEqual(validate(p, rows).issues, [])

    def test_time_coverage_combines_overlapping_shifts(self):
        early = Shift(id="early", name="早番", start="07:00", end="15:00", paid_minutes=480)
        late = Shift(id="late", name="遅番", start="14:00", end="22:00", paid_minutes=480)
        p = problem(
            days=1, shifts=[early, late],
            limits=Limits(max_consecutive=5, min_rest_minutes=660, no_next_day_after_night=False),
            coverage=[Coverage(date=START, shift_id=s.id, minimum=0, maximum=1) for s in (early, late)],
            rules=[Rule(id="overlap", type="min_time_coverage", date=START,
                        start_time="14:00", end_time="15:00", value=2)],
        )
        result = run_solver(SolveRequest(problem=p, time_limit_seconds=3), solve)
        self.assertEqual(result.status, "optimal", result)
        self.assertEqual({row.shift_id for row in result.assignments}, {"early", "late"})
        self.assertEqual(result.report.metrics.covered_time_segments, 1)

        p.rules[0].end_time = "15:01"
        report = validate(p, result.assignments)
        self.assertEqual([(i.start_time, i.end_time) for i in report.issues], [("15:00", "15:01")])
        failed = run_solver(SolveRequest(problem=p, time_limit_seconds=3), solve)
        self.assertEqual(failed.status, "infeasible", failed)
        self.assertTrue(any((i.start_time, i.end_time) == ("15:00", "15:01") for i in failed.diagnostics))

    def test_time_coverage_counts_carry_in_and_last_night(self):
        p = problem(
            days=1,
            employees=[employee("first", skills=["leader"]), employee("second", skills=["leader"])],
            history=[Assignment(employee_id="first", date=START - DAY, shift_id="night")],
            use_shift_coverage=False,
            coverage=[Coverage(date=START, shift_id="night", minimum=0, maximum=0)],
            rules=[Rule(id="night-lead", type="min_time_coverage", all_dates=True, skill_id="leader",
                        start_time="22:00", end_time="07:00", value=1)],
        )
        result = run_solver(SolveRequest(problem=p, time_limit_seconds=3), solve)
        self.assertEqual(result.status, "optimal", result)
        self.assertIn(Assignment(employee_id="second", date=START, shift_id="night"), result.assignments)
        self.assertEqual(result.report.metrics.covered_time_segments, 3)
        self.assertEqual(result.report.metrics.total_slots, 0)
        missing = validate(p, [])
        self.assertEqual([(i.date, i.start_time, i.end_time) for i in missing.issues],
                         [(START, "22:00", "24:00"), (START + DAY, "00:00", "07:00")])

    def test_break_reduces_paid_time_and_needs_coverage_from_another_shift(self):
        split = Shift(id="split", name="日勤", start="09:00", end="17:00", paid_minutes=420,
                      breaks=[ShiftBreak(start="12:00", end="13:00")])
        relief = Shift(id="relief", name="休憩交代", start="12:00", end="13:00", paid_minutes=60)
        p = problem(
            days=1, shifts=[split, relief],
            limits=Limits(max_consecutive=5, min_rest_minutes=0, no_next_day_after_night=False),
            rules=[Rule(id="continuous", type="min_time_coverage", date=START, value=1,
                        start_time="09:00", end_time="17:00")],
        )
        result = run_solver(SolveRequest(problem=p, time_limit_seconds=3), solve)
        self.assertEqual(result.status, "optimal", result)
        self.assertEqual({row.shift_id for row in result.assignments}, {"split", "relief"})
        self.assertEqual(sorted(row.minutes for row in result.report.metrics.employees), [60, 420])
        only_split = [row for row in result.assignments if row.shift_id == "split"]
        self.assertEqual([(i.start_time, i.end_time) for i in validate(p, only_split).issues],
                         [("12:00", "13:00")])

    def test_historical_shift_retains_its_cross_midnight_break_and_rest(self):
        old_night = Shift(id="old-night", name="旧夜勤", start="22:00", end="07:00", paid_minutes=480,
                          breaks=[ShiftBreak(start="23:30", end="00:30")])
        p = problem(
            days=1, shifts=[DAY_SHIFT], history_shifts=[old_night],
            history=[Assignment(employee_id="first", date=START-DAY, shift_id="old-night")],
            limits=Limits(max_consecutive=5, min_rest_minutes=660, no_next_day_after_night=False),
            rules=[Rule(id="morning", type="min_time_coverage", date=START, value=1,
                        start_time="00:00", end_time="07:00")],
        )
        first_day = Assignment(employee_id="first", date=START, shift_id="day")
        issues = validate(p, [first_day]).issues
        self.assertEqual({i.code for i in issues}, {"min_time_coverage", "rest"})
        shortage = next(i for i in issues if i.code == "min_time_coverage")
        self.assertEqual((shortage.start_time, shortage.end_time), ("00:00", "00:30"))
        result = run_solver(SolveRequest(problem=p, time_limit_seconds=3), solve)
        self.assertEqual(result.status, "infeasible", result)

    def test_breaks_must_fit_without_overlaps(self):
        for breaks in (
            [ShiftBreak(start="06:00", end="07:00")],
            [ShiftBreak(start="12:00", end="12:30"), ShiftBreak(start="12:15", end="13:00")],
            [ShiftBreak(start="12:00", end="12:00")],
        ):
            with self.subTest(breaks=breaks), self.assertRaises(ValidationError):
                Shift(id="split", name="日勤", start="09:00", end="17:00", paid_minutes=420, breaks=breaks)

    def test_fixed_shift_for_an_unavailable_employee_is_infeasible(self):
        p = problem(
            preferences=[Preference(employee_id="first", date=START, kind="unavailable", shift_id="day", weight=1)],
            locks=[Assignment(employee_id="first", date=START, shift_id="day")],
        )
        stats, _ = solve_constraints(p)
        self.assertEqual(stats.status, pulp.LpSolveStatus.Infeasible)

    def test_rule_and_fixed_rest_are_enforced(self):
        p = problem(
            coverage=[Coverage(date=START, shift_id="day", minimum=1, maximum=1)],
            rules=[Rule(id="off", type="fixed", date=START, employee_id="first", shift_id="off"),
                   Rule(id="block", type="unavailable", date=START, employee_id="second")],
        )
        stats, _ = solve_constraints(p)
        self.assertEqual(stats.status, pulp.LpSolveStatus.Infeasible)

    def test_night_after_midnight_carries_in_from_previous_month(self):
        p = problem(employees=[employee("first")],
                    coverage=[Coverage(date=START, shift_id="day", minimum=1, maximum=1)],
                    history=[Assignment(employee_id="first", date=START - DAY, shift_id="night")])
        stats, _ = solve_constraints(p)
        self.assertEqual(stats.status, pulp.LpSolveStatus.Infeasible)

    def test_long_rest_conflict_can_reach_two_dates_ahead(self):
        long_night = Shift(id="night", name="夜勤", start="21:00", end="14:00", paid_minutes=600)
        p = problem(days=3, shifts=[DAY_SHIFT, long_night], employees=[employee("first")],
                    limits=Limits(max_consecutive=5, min_rest_minutes=1440, night_shift_id="night",
                                  no_next_day_after_night=False),
                    locks=[Assignment(employee_id="first", date=START, shift_id="night"),
                           Assignment(employee_id="first", date=START + 2 * DAY, shift_id="day")])
        stats, _ = solve_constraints(p)
        self.assertEqual(stats.status, pulp.LpSolveStatus.Infeasible)

    def test_month_boundary_consecutive_and_work_minutes_rules(self):
        fixed_day = Assignment(employee_id="first", date=START, shift_id="day")
        cases = [
            problem(
                locks=[fixed_day],
                rules=[Rule(id="streak", type="max_consecutive", employee_id="first", value=2)],
                history=[Assignment(employee_id="first", date=START - offset * DAY, shift_id="day")
                         for offset in (1, 2)],
            ),
            problem(
                locks=[fixed_day],
                rules=[Rule(id="minutes", type="max_work_minutes", employee_id="first", value=479)],
            ),
        ]
        for p in cases:
            with self.subTest(p.rules):
                stats, _ = solve_constraints(p)
                self.assertEqual(stats.status, pulp.LpSolveStatus.Infeasible)


class SchedulingTest(unittest.TestCase):
    def test_coverage_all_shifts_targets_only_selected_day(self):
        p = problem(employees=[employee("first", skills=["leader"]), employee("second", skills=["leader"])],
                    rules=[Rule(id="daily-lead", type="min_skill_coverage", date=START,
                                all_shifts=True, skill_id="leader", value=1)])
        result = run_solver(SolveRequest(problem=p, time_limit_seconds=3), solve)
        self.assertEqual(result.status, "optimal", result)
        self.assertEqual({row.shift_id for row in result.assignments if row.date == START}, {"day", "night"})
        issues = [x for x in validate(p, []).issues if x.rule_id == "daily-lead"]
        self.assertEqual({(x.date, x.shift_id) for x in issues}, {(START, "day"), (START, "night")})

    def test_coverage_all_dates_applies_to_each_day_in_period(self):
        p = problem(rules=[Rule(id="period-lead", type="min_coverage", all_dates=True,
                                all_shifts=True, value=1)], days=31)
        slots = list(p.coverage_rule_slots())
        self.assertEqual(len(slots), 62)
        self.assertEqual((slots[-1][1], slots[-1][2]), (START + 30 * DAY, "night"))

    def test_fairness_selects_the_employee_with_work_target(self):
        p = self.choice_problem()
        result = run_solver(SolveRequest(problem=p, time_limit_seconds=3), solve)
        self.assertEqual(result.status, "optimal", result)
        self.assertEqual([row.employee_id for row in result.assignments], ["second"])
        self.assertEqual(result.objective["fairness_cost"], 0)
        self.assertEqual(result.report.issues, [])

    def test_change_count_precedes_fairness_but_not_preferences(self):
        baseline = [Assignment(employee_id="first", date=START, shift_id="day")]
        p = self.choice_problem(baseline=baseline)
        result = run_solver(SolveRequest(problem=p, time_limit_seconds=3), solve)
        self.assertEqual(result.status, "optimal", result)
        self.assertEqual([row.employee_id for row in result.assignments], ["first"])
        self.assertEqual(result.objective["changed_cells"], 0)

        p.preferences = [Preference(employee_id="second", date=START, kind="prefer_shift", shift_id="day", weight=2)]
        result = run_solver(SolveRequest(problem=p, time_limit_seconds=3), solve)
        self.assertEqual(result.status, "optimal", result)
        self.assertEqual([row.employee_id for row in result.assignments], ["second"])
        self.assertEqual(result.objective["changed_cells"], 2)
        self.assertEqual(result.objective["weighted_preference_violations"], 0)

    def test_configured_fairness_precedes_preferences(self):
        p = self.choice_problem(
            preferences=[Preference(employee_id="first", date=START, kind="prefer_shift", shift_id="day", weight=2)],
            optimization=Optimization(order=("fairness_cost", "weighted_preference_violations", "changed_cells")),
        )
        result = run_solver(SolveRequest(problem=p, time_limit_seconds=3), solve)
        self.assertEqual(result.status, "optimal", result)
        self.assertEqual([row.employee_id for row in result.assignments], ["second"])
        self.assertEqual(result.objective["weighted_preference_violations"], 2)

    def test_night_weight_changes_the_assignment_and_reported_cost(self):
        p = problem(
            days=1,
            employees=[employee("first", nights=1, target=0), employee("second", target=480)],
            shifts=[NIGHT],
            coverage=[Coverage(date=START, shift_id="night", minimum=1, maximum=1)],
            optimization=Optimization(night_deviation_weight=10),
        )
        result = run_solver(SolveRequest(problem=p, time_limit_seconds=3), solve)
        self.assertEqual(result.status, "optimal", result)
        self.assertEqual([row.employee_id for row in result.assignments], ["second"])
        self.assertEqual(result.objective["fairness_cost"], 20)

        p.optimization = Optimization(night_deviation_weight=1000)
        result = run_solver(SolveRequest(problem=p, time_limit_seconds=3), solve)
        self.assertEqual(result.status, "optimal", result)
        self.assertEqual([row.employee_id for row in result.assignments], ["first"])
        self.assertEqual(result.objective["fairness_cost"], 960)

    def test_later_time_limit_preserves_previous_feasible_schedule(self):
        p = self.choice_problem(preferences=[
            Preference(employee_id="first", date=START, kind="prefer_shift", shift_id="day", weight=3),
        ])
        phases = 0

        def solve_stage(roster, seconds, *, warm_start=False):
            nonlocal phases
            phases += 1
            if phases == 1:
                return optimize(roster, seconds, warm_start=warm_start)
            return pulp.LpSolveStats(status=pulp.LpSolveStatus.TimeLimit, has_solution=False)

        with patch("shift_optimizer.pulp_solver.optimize", side_effect=solve_stage):
            result = run_solver(SolveRequest(problem=p, time_limit_seconds=3), solve)
        self.assertEqual(phases, 2)
        self.assertEqual(result.status, "feasible")
        self.assertEqual([row.employee_id for row in result.assignments], ["first"])
        self.assertIsNone(result.gap)
        self.assertEqual(result.report.issues, [])

    def test_solver_time_limit_without_incumbent_is_unknown(self):
        stats = pulp.LpSolveStats(status=pulp.LpSolveStatus.TimeLimit, has_solution=False)
        with patch("shift_optimizer.pulp_solver.optimize", return_value=stats):
            result = run_solver(SolveRequest(problem=self.choice_problem(), time_limit_seconds=3), solve)
        self.assertEqual(result.status, "no_solution")
        self.assertEqual(result.assignments, [])

    def test_joint_staffing_shortage_is_diagnosed_without_relaxing_the_actual_result(self):
        p = problem(days=1, employees=[employee("first")],
                    coverage=[Coverage(date=START, shift_id=shift, minimum=1, maximum=1)
                              for shift in ("day", "night")])
        result = run_solver(SolveRequest(problem=p, time_limit_seconds=3), solve)
        self.assertEqual(result.status, "infeasible")
        self.assertEqual(result.assignments, [])
        self.assertIsNone(result.report)
        self.assertEqual([issue.code for issue in result.diagnostics], ["staffing_review"])

    def test_fixed_unavailable_shift_is_explained(self):
        p = self.choice_problem(
            locks=[Assignment(employee_id="first", date=START, shift_id="day")],
            preferences=[Preference(employee_id="first", date=START, kind="unavailable", weight=1)],
        )
        result = run_solver(SolveRequest(problem=p, time_limit_seconds=3), solve)
        self.assertEqual(result.status, "infeasible")
        self.assertTrue(any(row.code == "unavailable" and row.employee_id == "first" for row in result.diagnostics))

    def test_zero_eligible_variables_can_be_feasible(self):
        p = problem(days=1, employees=[employee("first")],
                    preferences=[Preference(employee_id="first", date=START, kind="unavailable", weight=1)])
        result = run_solver(SolveRequest(problem=p, time_limit_seconds=3), solve)
        self.assertEqual(result.status, "optimal")
        self.assertEqual(result.assignments, [])
        self.assertEqual(result.report.issues, [])

    @staticmethod
    def choice_problem(**changes):
        return problem(
            days=1,
            employees=[employee("first", target=0), employee("second", target=480)],
            coverage=[Coverage(date=START, shift_id="day", minimum=1, maximum=1),
                      Coverage(date=START, shift_id="night", minimum=0, maximum=0)],
            **changes,
        )
