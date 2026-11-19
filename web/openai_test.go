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
			"rule": map[string]any{
				"type": "min_skill_coverage", "date": "2026-12-12", "shift_id": "night",
				"skill_id": "leader", "employee_id": nil, "value": 1,
			},
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

type staticProposal struct{}

func (staticProposal) propose(context.Context, Problem, string) (Proposal, error) {
	return clarification("どの日付を指定しますか？"), nil
}

type roundTripFunc func(*http.Request) (*http.Response, error)

func (fn roundTripFunc) RoundTrip(r *http.Request) (*http.Response, error) {
	return fn(r)
}
