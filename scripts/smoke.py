"""End-to-end smoke test with the demo or PuLP backend running."""
import json
import os
import time
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


for _ in range(80):
    try:
        call("/healthz")
        break
    except URLError:
        time.sleep(0.2)
else:
    raise RuntimeError("Web service did not start")

assert "勤務表" in call("/", raw=True).decode()
assert len(call("/app.js", raw=True)) > 10000
initial = call("/api/plan")["plan"]
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
assert content.startswith("employee_id,employee_name,date,shift_id,locked")
assert ",off,true" in content
print(f"Smoke test passed ({SOLVER}): form rules, async jobs, fixed cells, validation, stale adoption, and CSV")
