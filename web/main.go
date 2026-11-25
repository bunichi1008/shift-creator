package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"
)

func env(name, fallback string) string {
	if value := os.Getenv(name); value != "" {
		return value
	}
	return fallback
}

func demoFile() (Problem, error) {
	path := env("DEMO_DATA_PATH", "../contracts/demo.json")
	data, err := os.ReadFile(path)
	if err != nil && os.Getenv("DEMO_DATA_PATH") == "" {
		data, err = os.ReadFile(filepath.Join("contracts", "demo.json"))
	}
	if err != nil {
		return Problem{}, err
	}
	var demo struct {
		Problem Problem `json:"problem"`
	}
	if err := json.Unmarshal(data, &demo); err != nil {
		return Problem{}, err
	}
	if demo.Problem.SchemaVersion != schemaVersion || demo.Problem.Days < 1 || demo.Problem.Days > 31 {
		return Problem{}, errors.New("invalid demo contract")
	}
	return demo.Problem, nil
}

func run(ctx context.Context) error {
	demo, err := demoFile()
	if err != nil {
		return fmt.Errorf("load sample data: %w", err)
	}
	timeout, err := strconv.Atoi(env("SOLVER_TIME_LIMIT_SECONDS", "30"))
	if err != nil || timeout < 1 || timeout > 60 {
		return errors.New("SOLVER_TIME_LIMIT_SECONDS must be between 1 and 60")
	}
	optimizerURL := strings.TrimRight(env("OPTIMIZER_URL", "http://127.0.0.1:8001"), "/")
	parsed, err := url.Parse(optimizerURL)
	if err != nil || parsed.Host == "" || (parsed.Scheme != "http" && parsed.Scheme != "https") ||
		parsed.User != nil || parsed.Path != "" || parsed.RawQuery != "" || parsed.Fragment != "" {
		return errors.New("OPTIMIZER_URL must be the optimizer HTTP origin")
	}
	client := &http.Client{Timeout: 72 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error {
		return http.ErrUseLastResponse
	}}
	opt := optimizerClient{url: optimizerURL, http: client}
	var persistence store = &fileStore{dir: env("DATA_DIR", "../.shift-creator")}
	adminToken := os.Getenv("DEMO_ADMIN_TOKEN")
	publicDemo, err := strconv.ParseBool(env("DEMO_PUBLIC", "false"))
	if err != nil {
		return errors.New("DEMO_PUBLIC must be true or false")
	}
	var queue jobQueue
	var taskToken string
	project := os.Getenv("GCP_PROJECT")
	if project != "" {
		persistence, queue, opt.token, taskToken, err = cloudBackends(project, optimizerURL, adminToken, publicDemo, client)
		if err != nil {
			return err
		}
	} else if os.Getenv("K_SERVICE") != "" {
		return errors.New("GCP_PROJECT is required when running the web service on Cloud Run")
	}
	s := server{
		store: persistence, demo: demo, now: time.Now, opt: opt, adminToken: adminToken, taskToken: taskToken,
		publicDemo: publicDemo, secureSession: project != "",
		publicOrigins: strings.Split(os.Getenv("DEMO_PUBLIC_ORIGINS"), ","),
	}
	s.jobs = jobService{store: persistence, optimizer: opt, queue: queue, timeLimit: timeout, clock: time.Now}
	if apiKey := os.Getenv("OPENAI_API_KEY"); apiKey != "" && !publicDemo {
		s.proposer = openAIProposer{apiKey: apiKey, model: env("OPENAI_MODEL", "gpt-4o-mini"),
			endpoint: openAIEndpoint, http: client}
	}
	port := env("PORT", "8080")
	localAddress := env("LISTEN_ADDR", "127.0.0.1:"+port)
	if !publicDemo {
		initCtx, cancel := context.WithTimeout(ctx, 15*time.Second)
		err := s.ensurePlan(initCtx)
		cancel()
		if err != nil {
			return fmt.Errorf("initialize workspace: %w", err)
		}
	}
	if queue == nil {
		go s.jobs.runLocal(ctx)
	} else {
		localAddress = ":" + port
	}
	httpServer := &http.Server{
		Addr: localAddress, Handler: s.handler(), ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout: 15 * time.Second, WriteTimeout: 85 * time.Second, IdleTimeout: 60 * time.Second,
	}
	go func() {
		<-ctx.Done()
		shutdown, cancel := context.WithTimeout(context.Background(), 8*time.Second)
		defer cancel()
		_ = httpServer.Shutdown(shutdown)
	}()
	slog.Info("Shift Creator listening", "address", httpServer.Addr)
	if err := httpServer.ListenAndServe(); !errors.Is(err, http.ErrServerClosed) {
		return err
	}
	return nil
}

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	if err := run(ctx); err != nil {
		slog.Error("start Shift Creator", "error", err)
		os.Exit(1)
	}
}
