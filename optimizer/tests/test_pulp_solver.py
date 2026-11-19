import unittest
from unittest.mock import patch
from datetime import date, timedelta

import pulp

from shift_optimizer.models import Assignment, Coverage, Employee, Limits, Preference, Problem, Rule, Shift, SolveRequest
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
