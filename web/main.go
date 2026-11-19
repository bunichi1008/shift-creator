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
	client := &http.Client{Timeout: 72 * time.Second}
	opt := optimizerClient{url: optimizerURL, http: client}
	var persistence store = &fileStore{dir: env("DATA_DIR", "../.shift-creator")}
	s := server{store: persistence, demo: demo, now: time.Now, opt: opt, adminToken: os.Getenv("DEMO_ADMIN_TOKEN")}
	s.jobs = jobService{store: persistence, optimizer: opt, timeLimit: timeout, clock: time.Now}
	port := env("PORT", "8080")
	localAddress := env("LISTEN_ADDR", "127.0.0.1:"+port)
	initCtx, cancel := context.WithTimeout(ctx, 15*time.Second)
	defer cancel()
	if err := s.ensurePlan(initCtx); err != nil {
		return fmt.Errorf("initialize workspace: %w", err)
	}
	go s.jobs.runLocal(ctx)
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
