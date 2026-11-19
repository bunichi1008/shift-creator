import json
import unittest
from datetime import date, timedelta
from pathlib import Path

import httpx

from shift_optimizer.api import create_app
from shift_optimizer.models import Assignment, Problem, Rule, SolveRequest, SolveResult
from shift_optimizer.solver import load_solver, run_solver, unavailable_solver
from shift_optimizer.validation import staffing_diagnostics, validate

CONTRACTS = Path(__file__).resolve().parents[2] / "contracts"
DEMO = CONTRACTS / "demo.json"


class OptimizerTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        demo = json.loads(DEMO.read_text(encoding="utf-8"))
        self.request = SolveRequest(problem=Problem.model_validate(demo["problem"]), time_limit_seconds=30)
        self.assignments = [Assignment.model_validate(row) for row in demo["assignments"]]

    def test_fixture_contract_and_independent_checks(self):
        result = run_solver(self.request, load_solver("demo", DEMO))
        self.assertEqual(result.status, "demo_valid", result.model_dump_json())
        self.assertEqual(result.report.metrics.covered_slots, 124)
        self.assertEqual(result.report.metrics.preference_met, 1)
        self.assertIsNone(result.report.metrics.changed_cells)
        for name, model in (("request", SolveRequest), ("result", SolveResult)):
            schema = json.loads((CONTRACTS / f"solve-{name}.schema.json").read_text())
            self.assertEqual(schema, model.model_json_schema())

    def test_solver_false_success_does_not_produce_adoptable_result(self):
        result = run_solver(self.request, lambda req: SolveResult(
            status="optimal", solver="test", message="invalid", assignments=[]
        ))
        self.assertEqual(result.status, "error")
        self.assertTrue(result.report.issues)

    def test_calendar_boundary_and_fixed_based_staffing_diagnostic(self):
        start = self.request.problem.start_date
        prior = next(row for row in self.request.problem.history
                     if row.date == date(2026, 11, 30) and row.shift_id == "night")
        late = Assignment(employee_id=prior.employee_id, date=start, shift_id="late")
        report = validate(self.request.problem, [*self.assignments, late])
        self.assertTrue(any(issue.code == "rest" and issue.date == start for issue in report.issues))

        problem = self.request.problem
        day = problem.rules[0].date
        problem.locks = [
            Assignment(employee_id=employee.id, date=day - timedelta(days=1), shift_id="night")
            for employee in problem.employees if "leader" in employee.skills
        ]
        shortage = next(issue for issue in staffing_diagnostics(problem) if issue.rule_id == "dec-night-leader")
        self.assertEqual(shortage.date, day)
        self.assertIn("最大0人", shortage.message)

    async def test_api_validates_input_and_uses_explicit_unavailable_status(self):
        transport = httpx.ASGITransport(app=create_app(unavailable_solver))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            body = self.request.model_dump(mode="json")
            result = await client.post("/v1/solve", json=body)
            self.assertEqual(result.json()["status"], "solver_unavailable")
            report = await client.post("/v1/validate", json={
                "problem": body["problem"],
                "assignments": [a.model_dump(mode="json") for a in self.assignments],
            })
            self.assertEqual(report.json()["issues"], [])
            body["problem"]["rules"].append(Rule(id="bad", type="fixed").model_dump(mode="json"))
            self.assertEqual((await client.post("/v1/solve", json=body)).status_code, 422)

    async def test_api_rejects_dates_that_do_not_round_trip_through_go(self):
        transport = httpx.ASGITransport(app=create_app(unavailable_solver))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            body = self.request.model_dump(mode="json")
            body["problem"]["start_date"] = "2026-12-01T00:00:00"
            self.assertEqual((await client.post("/v1/solve", json=body)).status_code, 422)
