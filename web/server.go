package main

import (
	"context"
	"crypto/rand"
	"crypto/subtle"
	"embed"
	"encoding/csv"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"log/slog"
	"mime"
	"net/http"
	"net/url"
	"slices"
	"strings"
	"time"
)

//go:embed static
var assets embed.FS

type server struct {
	store      store
	jobs       jobService
	opt        optimizer
	demo       Problem
	now        func() time.Time
	adminToken string
	taskToken  string
	proposer   ruleProposer
}

type ruleProposer interface {
	propose(context.Context, Problem, string) (Proposal, error)
}

type Proposal struct {
	Status   string `json:"status"`
	Question string `json:"question,omitempty"`
	Source   string `json:"source,omitempty"`
	Rule     *Rule  `json:"rule,omitempty"`
	Rules    []Rule `json:"rules,omitempty"`
}

func (s *server) handler() http.Handler {
	mux := http.NewServeMux()
	health := func(w http.ResponseWriter, _ *http.Request) {
		jsonResponse(w, 200, map[string]string{"status": "ok"})
	}
	mux.HandleFunc("GET /health", health)
	mux.HandleFunc("GET /healthz", health)
	mux.HandleFunc("POST /internal/jobs/run", s.task(s.runTask))
	mux.HandleFunc("POST /internal/jobs/redrive", s.task(s.redrive))
	mux.HandleFunc("GET /api/plan", s.admin(s.getPlan))
	mux.HandleFunc("PUT /api/plan", s.admin(s.replacePlan))
	mux.HandleFunc("POST /api/plan/import", s.admin(s.importPlan))
	mux.HandleFunc("POST /api/plan/reset", s.admin(s.resetPlan))
	mux.HandleFunc("PATCH /api/plan/cell", s.admin(s.editCell))
	mux.HandleFunc("POST /api/plan/rules", s.admin(s.addRule))
	mux.HandleFunc("PUT /api/plan/rules/{id}", s.admin(s.editRule))
	mux.HandleFunc("DELETE /api/plan/rules/{id}", s.admin(s.deleteRule))
	mux.HandleFunc("GET /api/plan.csv", s.admin(s.exportPlan))
	mux.HandleFunc("POST /api/plan/proposals", s.admin(s.proposeRule))
	mux.HandleFunc("GET /api/validation", s.admin(s.validatePlan))
	mux.HandleFunc("GET /api/jobs", s.admin(s.listJobs))
	mux.HandleFunc("POST /api/jobs", s.admin(s.submitJob))
	mux.HandleFunc("GET /api/jobs/{id}", s.admin(s.getJob))
	mux.HandleFunc("POST /api/jobs/{id}/adopt", s.admin(s.adoptJob))
	static, err := fs.Sub(assets, "static")
	if err != nil {
		panic(err)
	}
	mux.HandleFunc("GET /", s.admin(http.FileServer(http.FS(static)).ServeHTTP))
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Content-Type-Options", "nosniff")
		w.Header().Set("Referrer-Policy", "no-referrer")
		w.Header().Set("Content-Security-Policy", "default-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
		if strings.HasPrefix(r.URL.Path, "/api/") {
			w.Header().Set("Cache-Control", "no-store")
		}
		mux.ServeHTTP(w, r)
	})
}

func (s *server) admin(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if s.adminToken != "" {
			user, password, ok := r.BasicAuth()
			if !ok || user != "admin" || subtle.ConstantTimeCompare([]byte(password), []byte(s.adminToken)) != 1 {
				w.Header().Set("WWW-Authenticate", "Basic realm=\"Shift Creator\", charset=\"UTF-8\"")
				jsonResponse(w, 401, map[string]string{"error": "管理者としてログインしてください"})
				return
			}
		}
		if r.Method != http.MethodGet && r.Method != http.MethodHead {
			origin := r.Header.Get("Origin")
			source, err := url.Parse(origin)
			if r.Header.Get("Sec-Fetch-Site") == "cross-site" ||
				(origin != "" && (err != nil || source.Host != r.Host ||
					(source.Scheme != "http" && source.Scheme != "https"))) {
				jsonResponse(w, 403, map[string]string{"error": "別のサイトから更新できません"})
				return
			}
		}
		next(w, r)
	}
}

func (s *server) task(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if s.taskToken == "" {
			http.NotFound(w, r)
			return
		}
		if subtle.ConstantTimeCompare([]byte(r.Header.Get("X-Shift-Task-Token")), []byte(s.taskToken)) != 1 {
			jsonResponse(w, 401, map[string]string{"error": "task authentication required"})
			return
		}
		next(w, r)
	}
}

func jsonResponse(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	if err := json.NewEncoder(w).Encode(value); err != nil {
		slog.Debug("write JSON response", "error", err)
	}
}

func parseJSON(w http.ResponseWriter, r *http.Request, dst any) error {
	contentType, _, err := mime.ParseMediaType(r.Header.Get("Content-Type"))
	if err != nil || contentType != "application/json" {
		return inputError("Content-Type に application/json を指定してください")
	}
	r.Body = http.MaxBytesReader(w, r.Body, 640<<10)
	decoder := json.NewDecoder(r.Body)
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(dst); err != nil {
		return inputError("JSON の形式・項目・サイズを確認してください")
	}
	if err := decoder.Decode(new(any)); err != io.EOF {
		return inputError("JSON は1件だけ送信してください")
	}
	return nil
}

func respondError(w http.ResponseWriter, err error) {
	var invalid inputError
	status, message := http.StatusInternalServerError, "処理できませんでした"
	switch {
	case errors.As(err, &invalid):
		status, message = 422, invalid.Error()
	case errors.Is(err, errConflict):
		status, message = 409, "入力が更新されています。最新版を読み込んでやり直してください"
	case errors.Is(err, errNotFound):
		status, message = 404, "対象が見つかりません"
	case errors.Is(err, errBusy), errors.Is(err, errProcessing), errors.Is(err, context.DeadlineExceeded):
		status, message = 503, "処理を再試行してください"
	default:
		slog.Error("handle request", "error", err)
	}
	jsonResponse(w, status, map[string]string{"error": message})
}

func handleJSON[T any](w http.ResponseWriter, r *http.Request, status int, handle func(T) (any, error)) {
	var body T
	if err := parseJSON(w, r, &body); err != nil {
		respondError(w, err)
		return
	}
	respond(w, status, handle, body)
}

func respond[T any](w http.ResponseWriter, status int, handle func(T) (any, error), request T) {
	value, err := handle(request)
	if err != nil {
		respondError(w, err)
		return
	}
	jsonResponse(w, status, value)
}

func (s *server) getPlan(w http.ResponseWriter, r *http.Request) {
	plan, _, err := readPlan(r.Context(), s.store)
	if err != nil {
		respondError(w, err)
		return
	}
	plan.SampleRoster = s.isSampleRoster(plan.Problem)
	jsonResponse(w, 200, struct {
		Plan   Plan `json:"plan"`
		HasLLM bool `json:"has_llm"`
	}{plan, s.proposer != nil})
}

func (s *server) replacePlan(w http.ResponseWriter, r *http.Request) {
	type request struct {
		Revision int     `json:"revision"`
		Problem  Problem `json:"problem"`
	}
	handleJSON(w, r, 200, func(body request) (any, error) {
		return s.updatePlan(r.Context(), body.Revision, func(p *Problem) error {
			req := ValidationRequest{Problem: body.Problem, Assignments: body.Problem.Baseline}
			if _, err := s.opt.validate(r.Context(), req); err != nil {
				return err
			}
			*p = body.Problem
			return nil
		})
	})
}

func (s *server) importPlan(w http.ResponseWriter, r *http.Request) {
	type request struct {
		Revision int    `json:"revision"`
		Kind     string `json:"kind"`
		CSV      string `json:"csv"`
	}
	handleJSON(w, r, 200, func(body request) (any, error) {
		return s.updatePlan(r.Context(), body.Revision, func(p *Problem) error {
			if err := importCSV(p, body.Kind, body.CSV); err != nil {
				return err
			}
			_, err := s.opt.validate(r.Context(), ValidationRequest{Problem: *p, Assignments: p.Baseline})
			return err
		})
	})
}

type revisionRequest struct {
	Revision int `json:"revision"`
}

func (s *server) resetPlan(w http.ResponseWriter, r *http.Request) {
	handleJSON(w, r, 200, func(body revisionRequest) (any, error) {
		return s.updatePlan(r.Context(), body.Revision, func(p *Problem) error {
			*p = s.demo
			return nil
		})
	})
}

func (s *server) editCell(w http.ResponseWriter, r *http.Request) {
	type request struct {
		Revision int        `json:"revision"`
		Cell     Assignment `json:"cell"`
		Locked   bool       `json:"locked"`
	}
	handleJSON(w, r, 200, func(body request) (any, error) {
		return s.updatePlan(r.Context(), body.Revision, func(p *Problem) error {
			return changeCell(p, body.Cell, body.Locked)
		})
	})
}

func randomID() (string, error) {
	var data [12]byte
	if _, err := rand.Read(data[:]); err != nil {
		return "", err
	}
	return hex.EncodeToString(data[:]), nil
}

func (s *server) addRule(w http.ResponseWriter, r *http.Request) {
	type request struct {
		Revision int    `json:"revision"`
		Rule     *Rule  `json:"rule"`
		Rules    []Rule `json:"rules"`
	}
	handleJSON(w, r, 201, func(body request) (any, error) {
		return s.updatePlan(r.Context(), body.Revision, func(p *Problem) error {
			rules := body.Rules
			if body.Rule != nil {
				if body.Rules != nil {
					return inputError("条件を指定する形式を1つ選んでください")
				}
				rules = []Rule{*body.Rule}
			}
			if err := p.checkRuleBatch(rules); err != nil {
				return err
			}
			for index := range rules {
				id, err := randomID()
				if err != nil {
					return err
				}
				rules[index].ID = id
			}
			p.Rules = append(p.Rules, rules...)
			return nil
		})
	})
}

func (s *server) editRule(w http.ResponseWriter, r *http.Request) {
	type request struct {
		Revision int  `json:"revision"`
		Rule     Rule `json:"rule"`
	}
	handleJSON(w, r, 200, func(body request) (any, error) {
		return s.updatePlan(r.Context(), body.Revision, func(p *Problem) error {
			id := r.PathValue("id")
			index := slices.IndexFunc(p.Rules, func(rule Rule) bool { return rule.ID == id })
			if index < 0 {
				return errNotFound
			}
			if err := p.checkRule(body.Rule); err != nil {
				return err
			}
			body.Rule.ID = id
			p.Rules[index] = body.Rule
			return checkStaffRuleUniqueness(p.Rules)
		})
	})
}

func (s *server) deleteRule(w http.ResponseWriter, r *http.Request) {
	handleJSON(w, r, 200, func(body revisionRequest) (any, error) {
		return s.updatePlan(r.Context(), body.Revision, func(p *Problem) error {
			oldCount := len(p.Rules)
			p.Rules = slices.DeleteFunc(p.Rules, func(rule Rule) bool { return rule.ID == r.PathValue("id") })
			if len(p.Rules) == oldCount {
				return errNotFound
			}
			return nil
		})
	})
}

func (s *server) proposeRule(w http.ResponseWriter, r *http.Request) {
	type request struct {
		Revision int    `json:"revision"`
		Text     string `json:"text"`
	}
	handleJSON(w, r, 200, func(body request) (any, error) {
		if s.proposer == nil {
			return nil, inputError("OpenAI API が未設定です。条件フォームを利用できます")
		}
		body.Text = strings.TrimSpace(body.Text)
		if body.Text == "" || len([]rune(body.Text)) > 500 {
			return nil, inputError("条件は1〜500文字で入力してください")
		}
		plan, _, err := readPlan(r.Context(), s.store)
		if err != nil {
			return nil, err
		}
		if plan.Revision != body.Revision {
			return nil, errConflict
		}
		return s.proposer.propose(r.Context(), plan.Problem, body.Text)
	})
}

func (s *server) validatePlan(w http.ResponseWriter, r *http.Request) {
	plan, _, err := readPlan(r.Context(), s.store)
	if err != nil {
		respondError(w, err)
		return
	}
	report, err := s.opt.validate(r.Context(), ValidationRequest{Problem: plan.Problem, Assignments: plan.Problem.Baseline})
	if err != nil {
		respondError(w, err)
		return
	}
	jsonResponse(w, 200, struct {
		Revision int    `json:"revision"`
		Report   Report `json:"report"`
	}{plan.Revision, report})
}

func (s *server) exportPlan(w http.ResponseWriter, r *http.Request) {
	plan, _, err := readPlan(r.Context(), s.store)
	if err != nil {
		respondError(w, err)
		return
	}
	p := plan.Problem
	assigned := make(map[[2]string]string)
	for _, item := range p.Baseline {
		assigned[[2]string{item.EmployeeID, item.Date}] = item.ShiftID
	}
	locked := make(map[[2]string]string)
	for _, item := range p.Locks {
		locked[[2]string{item.EmployeeID, item.Date}] = item.ShiftID
	}
	w.Header().Set("Content-Type", "text/csv; charset=utf-8")
	w.Header().Set("Content-Disposition", fmt.Sprintf("attachment; filename=\"shift-%s-v%d.csv\"", p.StartDate, plan.Revision))
	_, _ = io.WriteString(w, "\ufeff")
	writer := csv.NewWriter(w)
	_ = writer.Write([]string{"employee_id", "employee_name", "date", "shift_id", "locked"})
	for _, employee := range p.Employees {
		for _, date := range dates(p) {
			cell := [2]string{employee.ID, date}
			shift := assigned[cell]
			if shift == "" {
				shift = "off"
			}
			_ = writer.Write([]string{csvValue(employee.ID), csvValue(employee.Name), date,
				csvValue(shift), fmt.Sprint(locked[cell] != "")})
		}
	}
	writer.Flush()
}
