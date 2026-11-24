package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

type testOptimizer struct {
	calls int
}

func (s *testOptimizer) solve(_ context.Context, req SolveRequest) (SolveResult, error) {
	s.calls++
	return SolveResult{
		Status: "demo_valid", Solver: "test", Assignments: []Assignment{
			{EmployeeID: req.Problem.Employees[0].ID, Date: req.Problem.StartDate, ShiftID: req.Problem.Shifts[0].ID},
		}, Report: &Report{Issues: []Issue{}},
	}, nil
}

func (*testOptimizer) validate(_ context.Context, req ValidationRequest) (Report, error) {
	if len(req.Problem.Employees) == 0 {
		return Report{}, inputError("missing employees")
	}
	return Report{Issues: []Issue{}}, nil
}

func testServer(t *testing.T) (*server, *testOptimizer) {
	t.Helper()
	demo, err := demoFile()
	if err != nil {
		t.Fatal(err)
	}
	persistence := &fileStore{dir: t.TempDir()}
	opt := &testOptimizer{}
	s := &server{store: persistence, demo: demo, opt: opt, now: time.Now}
	s.jobs = jobService{store: persistence, optimizer: opt, timeLimit: 30, clock: time.Now}
	if err := s.ensurePlan(context.Background()); err != nil {
		t.Fatal(err)
	}
	return s, opt
}

func request(t *testing.T, h http.Handler, method, path string, body any, status int, target any) {
	t.Helper()
	data, err := json.Marshal(body)
	if err != nil {
		t.Fatal(err)
	}
	r := httptest.NewRequest(method, path, bytes.NewReader(data))
	r.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	if w.Code != status {
		t.Fatalf("%s %s: got %d, want %d, body %s", method, path, w.Code, status, w.Body)
	}
	if target != nil {
		if err := json.Unmarshal(w.Body.Bytes(), target); err != nil {
			t.Fatal(err)
		}
	}
}

func TestJobIdempotencyAndStaleAdoption(t *testing.T) {
	s, opt := testServer(t)
	h := s.handler()
	var first, duplicate JobView
	payload := map[string]any{"revision": 1, "idempotency_key": "request-1"}
	request(t, h, "POST", "/api/jobs", payload, 202, &first)
	request(t, h, "POST", "/api/jobs", payload, 202, &duplicate)
	if first.ID != duplicate.ID {
		t.Fatal("duplicate job created")
	}
	for range 2 {
		if err := s.jobs.process(context.Background(), first.ID); err != nil {
			t.Fatal(err)
		}
	}
	if opt.calls != 1 {
		t.Fatalf("solver started %d times", opt.calls)
	}
	request(t, h, "PATCH", "/api/plan/cell", map[string]any{
		"revision": 1, "cell": Assignment{EmployeeID: "e01", Date: "2026-12-01", ShiftID: "off"}, "locked": true,
	}, 200, nil)
	request(t, h, "POST", "/api/jobs/"+first.ID+"/adopt", map[string]int{"revision": 1}, 409, nil)
	request(t, h, "POST", "/api/jobs/"+first.ID+"/adopt", map[string]int{"revision": 2}, 409, nil)
	var plan, check Plan
	request(t, h, "POST", "/api/plan/reset", revisionRequest{Revision: 2}, 200, &plan)
	request(t, h, "POST", "/api/jobs", map[string]any{"revision": plan.Revision, "idempotency_key": "request-2"}, 202, &first)
	if err := s.jobs.process(context.Background(), first.ID); err != nil {
		t.Fatal(err)
	}
	request(t, h, "POST", "/api/jobs/"+first.ID+"/adopt", revisionRequest{Revision: plan.Revision}, 200, &check)
	if len(check.Problem.Baseline) != 1 || check.Revision != plan.Revision+1 {
		t.Fatal("result did not become the new plan revision")
	}
}

func TestFileStoreRejectsStaleWriter(t *testing.T) {
	ctx := context.Background()
	s := &fileStore{dir: t.TempDir()}
	if err := s.write(ctx, plans, currentPlanID, "", "first"); err != nil {
		t.Fatal(err)
	}
	var value string
	version, err := s.read(ctx, plans, currentPlanID, &value)
	if err != nil || value != "first" {
		t.Fatalf("initial version: %q, error: %v", value, err)
	}
	if err := s.write(ctx, plans, currentPlanID, version, "second"); err != nil {
		t.Fatal(err)
	}
	if err := s.write(ctx, plans, currentPlanID, version, "stale"); !errors.Is(err, errConflict) {
		t.Fatalf("expected conflict, received %v", err)
	}
}

func TestRuleChecksAndImportDoesNotCommitInvalidInput(t *testing.T) {
	s, _ := testServer(t)
	h := s.handler()
	request(t, h, "POST", "/api/plan/rules", map[string]any{
		"revision": 1,
		"rule": map[string]any{
			"type": "min_skill_coverage", "date": "2026-12-12", "shift_id": "night", "skill_id": "missing", "value": 1,
		},
	}, 422, nil)
	request(t, h, "POST", "/api/plan/import", map[string]any{
		"revision": 1, "kind": "employees",
		"csv": "id,name,skills,target_minutes,max_minutes,target_nights\n",
	}, 422, nil)
	plan, _, err := readPlan(context.Background(), s.store)
	if err != nil || plan.Revision != 1 {
		t.Fatalf("invalid input modified workspace: %v", err)
	}
	if actual := csvValue(" =SUM(A1:A2)"); !strings.HasPrefix(actual, "'") {
		t.Fatal("CSV formula was not escaped")
	}
}

func TestRulesAreSavedTogetherInOneRevision(t *testing.T) {
	s, _ := testServer(t)
	h := s.handler()
	two, four := 2, 4
	rules := []Rule{
		{Type: "min_skill_coverage", Date: "2026-12-12", AllShifts: true, SkillID: "leader", Value: &two},
		{Type: "min_coverage", Date: "2026-12-12", ShiftID: "wrong", Value: &four},
	}
	payload := map[string]any{"revision": 1, "rules": rules}
	request(t, h, "POST", "/api/plan/rules", payload, 422, nil)
	plan, _, err := readPlan(context.Background(), s.store)
	if err != nil || plan.Revision != 1 || len(plan.Problem.Rules) != len(s.demo.Rules) {
		t.Fatalf("failed batch changed the plan: %+v, %v", plan, err)
	}
	rules[1].ShiftID = "night"
	request(t, h, "POST", "/api/plan/rules", payload, 201, &plan)
	stored := plan.Problem.Rules[len(s.demo.Rules):]
	if plan.Revision != 2 || len(stored) != 2 || !stored[0].AllShifts || stored[0].ID == "" || stored[0].ID == stored[1].ID {
		t.Fatalf("batch was not saved as expected: revision %d, %+v", plan.Revision, stored)
	}
}

func TestAvailabilityIsSavedAndEditedWithoutOverlappingRules(t *testing.T) {
	s, _ := testServer(t)
	h := s.handler()
	rule := Rule{Type: "available_only", EmployeeID: "e02", Weekdays: []string{"tue", "sat"}, Dates: []string{"2026-12-10"}}
	var plan Plan
	request(t, h, "POST", "/api/plan/rules", map[string]any{"revision": 1, "rule": rule}, 201, &plan)
	added := plan.Problem.Rules[len(plan.Problem.Rules)-1]
	request(t, h, "POST", "/api/plan/rules", map[string]any{"revision": 2, "rule": rule}, 422, nil)
	rule.Dates = []string{"2026-12-11"}
	request(t, h, "PUT", "/api/plan/rules/"+added.ID, map[string]any{"revision": 2, "rule": rule}, 200, &plan)
	if plan.Revision != 3 || plan.Problem.Rules[len(plan.Problem.Rules)-1].Dates[0] != "2026-12-11" {
		t.Fatalf("availability edit was not saved: %+v", plan.Problem.Rules)
	}
	rule.Dates = []string{"2026-11-30"}
	request(t, h, "PUT", "/api/plan/rules/"+added.ID, map[string]any{"revision": 3, "rule": rule}, 422, nil)
}

func TestAdminOriginAndTaskAuthentication(t *testing.T) {
	s, _ := testServer(t)
	s.adminToken, s.taskToken = "test-admin", "test-task"
	h := s.handler()
	request(t, h, "GET", "/api/plan", nil, 401, nil)
	request(t, h, "POST", "/internal/jobs/redrive", nil, 401, nil)
	r := httptest.NewRequest("POST", "/api/plan/reset", strings.NewReader("{}"))
	r.SetBasicAuth("admin", "test-admin")
	r.Header.Set("Origin", "https://other.invalid")
	r.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	if w.Code != 403 {
		t.Fatalf("expected cross-origin request to be rejected, got %d", w.Code)
	}
}
