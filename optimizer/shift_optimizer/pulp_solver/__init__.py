from __future__ import annotations

from math import isfinite
from time import monotonic

import pulp

from ..models import Issue, Problem, SolveRequest, SolveResult
from .coverage import add_coverage
from .model import RosterModel
from .objectives import build_objectives, scores
from .work_rules import add_work_rules

SOLVER = "PuLP / HiGHS"
OPTIMAL = pulp.LpSolveStatus.Optimal
INFEASIBLE = pulp.LpSolveStatus.Infeasible
FAILED = {
    pulp.LpSolveStatus.Unbounded, pulp.LpSolveStatus.Undefined, pulp.LpSolveStatus.NumericalError,
}


def solve(request: SolveRequest) -> SolveResult:
    deadline = monotonic() + request.time_limit_seconds
    problem = request.problem
    break_choices = sum(len(options) for shift in problem.shifts for options in shift.flexible_break_options())
    if break_choices * problem.days * len(problem.employees) > 250_000:
        return SolveResult(status="no_solution", solver=SOLVER,
                           message="休憩の開始候補が多すぎます。休憩の開始時刻の範囲を狭めてください")
    roster = RosterModel(request.problem)
    add_work_rules(roster)
    add_coverage(roster)
    objectives = build_objectives(roster)
    stages = [(name, expression) for name, expression in objectives.items() if len(expression)]
    if not stages:
        stages = [next(iter(objectives.items()))]

    candidate = None
    for index, (name, expression) in enumerate(stages):
        remaining = deadline - monotonic()
        if remaining <= 0:
            if candidate:
                candidate.best_bound = candidate.gap = None
            break

        roster.mip.setObjective(expression)
        stats = optimize(roster, remaining, warm_start=candidate is not None)
        if stats.has_solution and stats.status not in FAILED | {INFEASIBLE}:
            values = scores(roster, objectives)
            bound = finite_bound(stats)
            gap = max(0.0, values[name] - bound) / max(1, abs(values[name])) if bound is not None else None
            candidate = SolveResult(
                status="feasible", solver=SOLVER, message="実行可能な勤務表です。全段階の最適性は未確認です",
                assignments=roster.extract(), objective=values, best_bound=bound, gap=gap,
            )
        if stats.status == OPTIMAL and stats.has_solution:
            if index == len(stages) - 1:
                candidate.status = "optimal"
                candidate.message = "指定した優先順位で最適な勤務表です"
                candidate.best_bound, candidate.gap = candidate.objective[name], 0.0
                return candidate
            if len(expression):
                roster.add("proven_objective", expression == round(pulp.value(expression)))
            continue

        if candidate:
            # A bound from the previous stage does not apply to this stage.
            if not stats.has_solution or stats.status in FAILED | {INFEASIBLE}:
                candidate.best_bound = candidate.gap = None
            break
        if stats.status == INFEASIBLE:
            return SolveResult(
                status="infeasible", solver=SOLVER, message="必須条件を同時に満たす勤務表はありません",
                diagnostics=diagnose_staffing(request.problem, deadline),
            )
        if stats.status in FAILED or stats.status == OPTIMAL:
            return SolveResult(status="error", solver=SOLVER, message="ソルバーが有効な結果を返しませんでした")
        break

    return candidate or SolveResult(
        status="no_solution", solver=SOLVER, message="制限内に勤務表の候補を取得できませんでした",
    )


def optimize(roster: RosterModel, seconds: float, *, warm_start: bool = False) -> pulp.LpSolveStats:
    solver = pulp.HiGHS(msg=False, threads=1, timeLimit=seconds, gapRel=0, gapAbs=0, warmStart=warm_start)
    return roster.mip.solve(solver)


def finite_bound(stats: pulp.LpSolveStats) -> float | None:
    if stats.best_bound is not None and isfinite(stats.best_bound):
        return stats.best_bound
    return None


def diagnose_staffing(problem: Problem, deadline: float) -> list[Issue]:
    if deadline - monotonic() <= 0.2 or not (
        any(row.minimum for row in problem.shift_staffing)
        or any(rule.type in {"min_coverage", "min_skill_coverage", "min_time_coverage"} for rule in problem.rules)
    ):
        return []
    roster = RosterModel(problem)
    add_work_rules(roster)
    shortages = add_coverage(roster, relax_minimums=True)
    remaining = deadline - monotonic()
    if remaining <= 0:
        return []
    roster.mip.setObjective(pulp.lpSum(row.variable for row in shortages))
    stats = optimize(roster, remaining)
    if not stats.has_solution or stats.status in FAILED | {INFEASIBLE}:
        return []
    result = []
    for row in shortages:
        shortage = round(row.variable.varValue or 0)
        if shortage > 0:
            staff = f"資格 {row.skill_id} の職員" if row.skill_id else "職員"
            result.append(Issue(
                code="staffing_review", **row.scope,
                message=f"見直し候補の1つです。休息・固定・人員上限を保った診断では{staff}が{shortage}人不足しました",
            ))
            if len(result) == 3100:
                break
    return result
