"""End-to-end smoke test with the demo or PuLP backend running."""
import csv
import io
import json
import os
import time
from copy import deepcopy
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

ORIGIN = os.environ.get("SMOKE_WEB_URL", "http://127.0.0.1:18080")


SOLVER = os.environ.get("SMOKE_SOLVER", "demo")
SOLUTION_STATUSES = {"optimal", "feasible"} if SOLVER == "pulp" else {"demo_valid"}


def call(path, method="GET", body=None, status=200, raw=False):
    data = None if body is None else json.dumps(body).encode()
    headers = {"Content-Type": "application/json"} if data else {}
    request = Request(ORIGIN + path, data, headers, method=method)
    try:
        response = urlopen(request, timeout=8)
    except HTTPError as error:
        response = error
    with response:
        assert response.status == status, (response.status, response.read())
        return response.read() if raw else json.load(response)


def solve(revision):
    job = call("/api/jobs", "POST", {"revision": revision, "idempotency_key": str(uuid4())}, 202)
    for _ in range(210):
        job = call("/api/jobs/" + job["id"])
        if job["state"] in ("completed", "failed"):
            assert job["state"] == "completed", job
            return job
        time.sleep(0.2)
    raise TimeoutError(f"Job {job['id']} is still {job['state']}")


def check_imports(revision):
    invalid = "date,shift_id,minimum,maximum\n2026-12-01,day,5,4\n"
    call("/api/plan/import", "POST", {"revision": revision, "kind": "coverage", "csv": invalid}, 422)
    assert call("/api/plan")["plan"]["revision"] == revision
    preferences = "employee_id,date,kind,shift_id,weight\ne01,2026-12-01,prefer_off,,2\n"
    saved = call("/api/plan/import", "POST", {"revision": revision, "kind": "preferences", "csv": preferences})
    assert saved["problem"]["preferences"] == [
        {"employee_id": "e01", "date": "2026-12-01", "kind": "prefer_off", "weight": 2}
    ]
    print("Scenario passed: rejected CSV preserves the revision; valid preferences are saved")
    return saved["revision"]


def check_break_scenarios(original, revision):
    problem = deepcopy(original)
    day = next(shift for shift in problem["shifts"] if shift["id"] == "day")
    fixed_break = {"start": "12:00", "end": "12:30"}
    day.update(paid_minutes=450, breaks=[fixed_break])
    employees = problem["employees"][:2]
    for employee in employees:
        employee.update(target_minutes=450, max_minutes=480, target_nights=0)
    date = problem["start_date"]
    problem.update(
        days=1, employees=employees, shifts=[day], history=[], locks=[], baseline=[], preferences=[],
        limits={"max_consecutive": 5, "min_rest_minutes": 660, "no_next_day_after_night": False},
        coverage=[{"date": date, "shift_id": "day", "minimum": 2, "maximum": 2}],
        rules=[{"id": "lunch", "type": "min_time_coverage", "date": date,
                "start_time": "12:00", "end_time": "13:00", "value": 1}],
    )
    plan = call("/api/plan", "PUT", {"revision": revision, "problem": problem})
    assert not plan.get("sample_roster")
    result = solve(plan["revision"])
    assert result["result"]["status"] == "infeasible"
    assert any(i.get("start_time") == "12:00" and i.get("end_time") == "12:30"
               for i in result["result"]["diagnostics"])
    call("/api/jobs/" + result["id"] + "/adopt", "POST", {"revision": plan["revision"]}, 422)

    fixed_break["latest_start"] = "12:30"
    plan = call("/api/plan", "PUT", {"revision": plan["revision"], "problem": problem})
    result = solve(plan["revision"])
    assignments = result["result"]["assignments"]
    assert result["result"]["status"] in SOLUTION_STATUSES
    assert result["result"]["report"]["issues"] == []
    assert sorted(row["break_starts"] for row in assignments) == [["12:00"], ["12:30"]]
    adopted_breaks = call("/api/jobs/" + result["id"] + "/adopt", "POST", {"revision": plan["revision"]})
    assert adopted_breaks["problem"]["baseline"] == assignments
    assert call("/api/validation")["report"]["issues"] == []
    kept = call("/api/plan/cell", "PATCH", {
        "revision": adopted_breaks["revision"], "cell": assignments[0], "locked": True,
    })
    assert kept["problem"]["baseline"] == assignments
    print("Scenario passed: infeasible fixed breaks, feasible staggered breaks, adoption and lock")
    return kept["revision"]


for _ in range(80):
    try:
        assert call("/health") == {"status": "ok"}
        break
    except URLError:
        time.sleep(0.2)
else:
    raise RuntimeError("Web service did not start")

assert "勤務表" in call("/", raw=True).decode()
assert len(call("/app.js", raw=True)) > 10000
initial = call("/api/plan")["plan"]
assert initial["sample_roster"]
rule = {
    "type": "min_skill_coverage", "date": "2026-12-12", "shift_id": "night", "skill_id": "leader", "value": 1
}
plan = call("/api/plan/rules", "POST", {"revision": initial["revision"], "rule": rule}, 201)
first = solve(plan["revision"])
assert first["result"]["status"] in SOLUTION_STATUSES, first
assert first["result"]["report"]["issues"] == []
assert first["result"]["report"]["metrics"]["covered_slots"] == 124
adopted = call("/api/jobs/" + first["id"] + "/adopt", "POST", {"revision": plan["revision"]})
call("/api/jobs/" + first["id"] + "/adopt", "POST", {"revision": adopted["revision"]}, 409)
assigned = adopted["problem"]["baseline"][0]
locked = call("/api/plan/cell", "PATCH", {
    "revision": adopted["revision"],
    "cell": {**assigned, "shift_id": "off"},
    "locked": True,
})
check = call("/api/validation")
assert any(issue["code"] == "coverage" for issue in check["report"]["issues"])
second = solve(locked["revision"])
if SOLVER == "pulp":
    assert second["result"]["status"] in SOLUTION_STATUSES, second
    assert second["result"]["report"]["issues"] == []
    assert second["result"]["report"]["metrics"]["changed_cells"] > 0
    assert not any(row["employee_id"] == assigned["employee_id"] and row["date"] == assigned["date"]
                   for row in second["result"]["assignments"])
    replanned = call("/api/jobs/" + second["id"] + "/adopt", "POST", {"revision": locked["revision"]})
    impossible = call("/api/plan/rules", "POST", {
        "revision": replanned["revision"], "rule": {**rule, "value": 25},
    }, 201)
    failed = solve(impossible["revision"])
    assert failed["result"]["status"] == "infeasible", failed
    assert any(issue["code"] == "staffing_shortage" for issue in failed["result"]["diagnostics"])
    call("/api/jobs/" + failed["id"] + "/adopt", "POST", {"revision": impossible["revision"]}, 422)
else:
    assert second["result"]["status"] == "demo_invalid", second
    assert any(issue["code"] == "locked_assignment" for issue in second["result"]["report"]["issues"])
    call("/api/jobs/" + second["id"] + "/adopt", "POST", {"revision": locked["revision"]}, 422)
content = call("/api/plan.csv", raw=True).decode("utf-8-sig")
rows = list(csv.DictReader(io.StringIO(content)))
assert len(rows) == 24 * 31
assert any(row["shift_id"] == "off" and row["locked"] == "true" for row in rows)
if SOLVER == "pulp":
    revision = check_imports(impossible["revision"])
    revision = check_break_scenarios(initial["problem"], revision)
    restored = call("/api/plan/reset", "POST", {"revision": revision})
    assert restored["sample_roster"] and restored["problem"] == initial["problem"]
    print("Scenario passed: sample reset restores the original input and empty baseline")
print(f"Smoke test passed ({SOLVER}): form rules, async jobs, fixed cells, validation, stale adoption, and CSV")
