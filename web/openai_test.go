package main

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestOpenAIProposalUsesStructuredResponseAndValidatesIDs(t *testing.T) {
	p, err := demoFile()
	if err != nil {
		t.Fatal(err)
	}
	const phrase = "12月12日の夜勤にリーダーを最低1人"
	transport := roundTripFunc(func(r *http.Request) (*http.Response, error) {
		w := httptest.NewRecorder()
		if r.Header.Get("Authorization") != "Bearer test-token" || r.Method != http.MethodPost {
			t.Error("missing authenticated POST")
		}
		var request struct {
			Input string         `json:"input"`
			Text  map[string]any `json:"text"`
			Store bool           `json:"store"`
		}
		if err := json.NewDecoder(r.Body).Decode(&request); err != nil {
			t.Error(err)
		}
		if format, ok := request.Text["format"].(map[string]any); !ok || format["strict"] != true ||
			request.Store || !strings.Contains(request.Input, phrase) ||
			strings.Contains(request.Input, "preferences") || strings.Contains(request.Input, "baseline") {
			t.Error("unexpected OpenAI request payload")
		}
		output, _ := json.Marshal(map[string]any{
			"status": "proposal", "question": nil, "source": phrase,
			"rules": []map[string]any{{
				"type": "min_skill_coverage", "date": "2026-12-12", "shift_id": "night",
				"skill_id": "leader", "employee_id": nil, "value": 1, "all_dates": false, "all_shifts": false,
			}},
		})
		jsonResponse(w, 200, map[string]any{
			"status": "completed", "output": []any{map[string]any{
				"type": "message", "content": []any{map[string]any{"type": "output_text", "text": string(output)}},
			}},
		})
		return w.Result(), nil
	})

	client := openAIProposer{apiKey: "test-token", model: "test", endpoint: "https://openai.test/v1/responses",
		http: &http.Client{Transport: transport}}
	proposal, err := client.propose(context.Background(), p, phrase)
	if err != nil || proposal.Status != "proposal" || proposal.Rule == nil ||
		proposal.Rule.SkillID != "leader" || *proposal.Rule.Value != 1 {
		t.Fatalf("unexpected proposal: %+v, %v", proposal, err)
	}
	proposal.Rule.SkillID = "made-up"
	checked, err := checkedProposal(p, phrase, proposal)
	if err != nil || checked.Status != "clarify" || checked.Rule != nil {
		t.Fatalf("invalid ID not rejected: %+v, %v", checked, err)
	}
}

func TestRuleProposalDoesNotChangeRevision(t *testing.T) {
	s, _ := testServer(t)
	s.proposer = staticProposal{}
	request(t, s.handler(), "POST", "/api/plan/proposals",
		map[string]any{"revision": 1, "text": "条件を具体化したい"}, 200, nil)
	plan, _, err := readPlan(context.Background(), s.store)
	if err != nil || plan.Revision != 1 {
		t.Fatal("a proposal must not change the current plan")
	}
}

func TestCoverageProposalWithNoShiftExplainsHowToSpecifyIt(t *testing.T) {
	p, err := demoFile()
	if err != nil {
		t.Fatal(err)
	}
	minimum := 2
	rule := Rule{Type: "min_skill_coverage", Date: "2026-12-12", SkillID: "leader", Value: &minimum}
	checked, err := checkedProposal(p, "12月12日にリーダーを常に2人", Proposal{Status: "proposal", Rule: &rule})
	if err != nil || checked.Status != "clarify" || checked.Rule != nil ||
		checked.Question != "勤務枠を指定してください。" {
		t.Fatalf("missing shift did not produce a specific clarification: %+v, %v", checked, err)
	}
	if strings.Contains(checked.Question, "入力を確認してください") {
		t.Fatalf("clarification repeated a generic instruction: %q", checked.Question)
	}
}

func TestRuleValidationReportsMissingAndUnexpectedFields(t *testing.T) {
	p, err := demoFile()
	if err != nil {
		t.Fatal(err)
	}
	if err := p.checkRule(Rule{Type: "min_coverage", ShiftID: "night"}); err == nil ||
		err.Error() != "対象日と最低人数を指定してください" {
		t.Fatalf("wrong error for missing coverage fields: %v", err)
	}
	value := 2
	if err := p.checkRule(Rule{Type: "fixed", Date: "2026-12-12", ShiftID: "off", EmployeeID: p.Employees[0].ID, Value: &value}); err == nil || err.Error() != "この種類の条件では数値の指定を外してください" {
		t.Fatalf("wrong error for unexpected fixed rule field: %v", err)
	}
}

func TestOpenAIProposalAcceptsSeveralRulesAndPreservesScope(t *testing.T) {
	p, err := demoFile()
	if err != nil {
		t.Fatal(err)
	}
	two, four := 2, 4
	rules := []Rule{
		{Type: "min_skill_coverage", Date: "2026-12-12", AllShifts: true, SkillID: "leader", Value: &two},
		{Type: "min_coverage", Date: "2026-12-12", ShiftID: "night", Value: &four},
	}
	checked, err := checkedProposal(p, "12月12日の全勤務枠にリーダー2人、夜勤は最低4人", Proposal{Status: "proposal", Rules: rules})
	if err != nil || checked.Status != "proposal" || checked.Rule != nil || len(checked.Rules) != 2 || !checked.Rules[0].AllShifts {
		t.Fatalf("scoped conditions were not preserved: %+v, %v", checked, err)
	}
	rules[1].AllShifts = true
	checked, err = checkedProposal(p, "複数の勤務条件", Proposal{Status: "proposal", Rules: rules})
	if err != nil || checked.Status != "clarify" || len(checked.Rules) != 0 || !strings.Contains(checked.Question, "2件目") {
		t.Fatalf("invalid condition should hold the entire proposal: %+v, %v", checked, err)
	}
}

func TestTimeCoverageProposalValidatesRangeAndScope(t *testing.T) {
	p, err := demoFile()
	if err != nil {
		t.Fatal(err)
	}
	value := 2
	rule := Rule{Type: "min_time_coverage", AllDates: true, SkillID: "leader",
		StartTime: "22:00", EndTime: "06:00", Value: &value}
	const phrase = "毎日22時から翌6時にリーダーを最低2人"
	proposal, err := checkedProposal(p, phrase, Proposal{Status: "proposal", Rule: &rule})
	if err != nil || proposal.Status != "proposal" || proposal.Rule == nil || *proposal.Rule != rule {
		t.Fatalf("unexpected time coverage proposal: %+v, %v", proposal, err)
	}
	rule.EndTime = "24:00"
	if err := p.checkRule(rule); err == nil {
		t.Fatal("invalid clock time was accepted")
	}
	rule.EndTime = "06:00"
	rule.ShiftID = "night"
	if err := p.checkRule(rule); err == nil {
		t.Fatal("a time coverage rule must not be tied to one shift")
	}
}

type staticProposal struct{}

func (staticProposal) propose(context.Context, Problem, string) (Proposal, error) {
	return clarification("どの日付を指定しますか？"), nil
}

type roundTripFunc func(*http.Request) (*http.Response, error)

func (fn roundTripFunc) RoundTrip(r *http.Request) (*http.Response, error) {
	return fn(r)
}
