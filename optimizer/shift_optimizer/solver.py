from __future__ import annotations

import importlib
import json
from collections.abc import Callable
from pathlib import Path
from time import monotonic

from .models import Assignment, SolveRequest, SolveResult
from .validation import fixed_diagnostics, staffing_diagnostics, validate

Solver = Callable[[SolveRequest], SolveResult]


def load_solver(backend: str, demo_path: Path) -> Solver:
    if backend == "none":
        return unavailable_solver
    if backend == "demo":
        demo = json.loads(demo_path.read_text(encoding="utf-8"))
        rows = [Assignment.model_validate(row) for row in demo["assignments"]]
        return lambda request: demo_solver(request, rows)

    if backend == "pulp":
        backend = "shift_optimizer.pulp_solver:solve"
    module_name, separator, attribute = backend.partition(":")
    if not separator or not module_name or not attribute:
        raise ValueError("OPTIMIZER_SOLVER must be pulp, demo, none, or module:function")
    solve = getattr(importlib.import_module(module_name), attribute)
    if not callable(solve):
        raise TypeError("solver entry point must be callable")
    return solve


def unavailable_solver(_request: SolveRequest) -> SolveResult:
    return SolveResult(status="solver_unavailable", solver="none", message="最適化エンジンを接続してください")


def demo_solver(request: SolveRequest, rows: list[Assignment]) -> SolveResult:
    problem = request.problem
    employee_ids = {employee.id for employee in problem.employees}
    shift_ids = {shift.id for shift in problem.shifts}
    candidate = [row for row in rows if problem.in_period(row.date)
                 and row.employee_id in employee_ids and row.shift_id in shift_ids]
    if not candidate:
        return SolveResult(status="solver_unavailable", solver="demo",
                           message="この入力に対応するサンプル勤務表はありません")
    return SolveResult(status="demo_valid", solver="demo", assignments=candidate,
                       message="あらかじめ用意したサンプル勤務表です")


def run_solver(request: SolveRequest, solver: Solver) -> SolveResult:
    started = monotonic()
    result = SolveResult.model_validate(solver(request))
    result.elapsed_ms = round((monotonic() - started) * 1000)
    diagnostics = staffing_diagnostics(request.problem)
    if result.status == "infeasible":
        diagnostics.extend(fixed_diagnostics(request.problem))
    result.diagnostics = [*diagnostics, *result.diagnostics][:3100]
    if result.status in {"optimal", "feasible", "demo_valid", "demo_invalid"}:
        result.report = validate(request.problem, result.assignments)
        if result.report.issues:
            if result.status.startswith("demo_"):
                result.status = "demo_invalid"
                result.message = "サンプル勤務表が現在の条件に違反しています。表示された箇所を確認できます"
            else:
                result.status = "error"
                result.message = "ソルバーが返した割当が必須条件に違反しています"
        elif result.status.startswith("demo_"):
            result.status = "demo_valid"
    else:
        result.assignments = []
        result.report = None
    return result
