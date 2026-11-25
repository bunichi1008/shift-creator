package main

import (
	"context"
	"crypto/sha256"
	"errors"
	"fmt"
	"log/slog"
	"slices"
	"strings"
	"time"
)

const (
	plans     = "shift-plans"
	jobs      = "shift-jobs"
	demoPlans = "shift-demo-plans"
	demoJobs  = "shift-demo-jobs"

	currentPlanID = "current"
	maxAttempts   = 3
	leaseDuration = 90 * time.Second
)

var (
	errProcessing = errors.New("job has an active lease or retry delay")
	errBusy       = errors.New("job service not available")
)

type jobQueue interface {
	enqueue(context.Context, string) error
}

type jobService struct {
	store     store
	optimizer optimizer
	queue     jobQueue
	timeLimit int
	clock     func() time.Time
}

func (s jobService) get(ctx context.Context, id string) (Job, string, error) {
	var job Job
	version, err := s.store.read(ctx, jobCollection(id), id, &job)
	return job, version, err
}

func jobCollection(id string) string {
	if strings.Contains(id, "-") {
		return demoJobs
	}
	return jobs
}

func (s jobService) save(ctx context.Context, job Job, version string) error {
	return s.store.write(ctx, jobCollection(job.ID), job.ID, version, job)
}

func (s jobService) submit(ctx context.Context, revision int, key string) (Job, error) {
	if key == "" || len(key) > 100 || strings.TrimSpace(key) != key {
		return Job{}, inputError("有効な冪等性キーが必要です")
	}
	hash := sha256.Sum256([]byte(key))
	id := fmt.Sprintf("%x", hash[:16])
	if workspace := demoWorkspace(ctx); workspace != "" {
		id = workspace + "-" + id
	}
	job, _, err := s.get(ctx, id)
	if err == nil {
		if job.PlanRevision != revision {
			return Job{}, errConflict
		}
		s.tryDispatch(ctx, job)
		return job, nil
	}
	if !errors.Is(err, errNotFound) {
		return Job{}, err
	}

	plan, _, err := readPlan(ctx, s.store)
	if err != nil {
		return Job{}, err
	}
	if plan.Revision != revision {
		return Job{}, errConflict
	}
	now := s.clock().UTC()
	job = Job{
		ID: id, PlanRevision: revision, State: "queued",
		CreatedAt: now, UpdatedAt: now, Snapshot: plan.Problem,
	}
	if err := s.save(ctx, job, ""); err != nil {
		if !errors.Is(err, errConflict) {
			return Job{}, err
		}
		job, _, err = s.get(ctx, id)
		if err != nil {
			return Job{}, err
		}
		if job.PlanRevision != revision {
			return Job{}, errConflict
		}
	}
	s.tryDispatch(ctx, job)
	return job, nil
}

func (s jobService) tryDispatch(ctx context.Context, job Job) {
	if s.queue == nil || job.State != "queued" || job.Dispatched {
		return
	}
	dispatchCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	if err := s.queue.enqueue(dispatchCtx, job.ID); err != nil {
		slog.Warn("job is saved and awaiting queue delivery", "job", job.ID, "error", err)
		return
	}
	current, version, err := s.get(dispatchCtx, job.ID)
	if err == nil && current.State == "queued" && !current.Dispatched {
		current.Dispatched = true
		err = s.save(dispatchCtx, current, version)
	}
	if err != nil && !errors.Is(err, errConflict) {
		slog.Warn("could not mark queued job as dispatched", "job", job.ID, "error", err)
	}
}

func (s jobService) process(ctx context.Context, id string) error {
	job, version, err := s.get(ctx, id)
	if err != nil {
		return err
	}
	now := s.clock().UTC()
	if job.State == "completed" || job.State == "failed" {
		return nil
	}
	if job.LeaseUntil.After(now) || job.RetryAfter.After(now) {
		return errProcessing
	}
	if job.Attempt >= maxAttempts {
		job.State, job.Error, job.UpdatedAt = "failed", "実行を再試行できませんでした", now
		return s.save(ctx, job, version)
	}
	job.State, job.UpdatedAt, job.LeaseUntil = "running", now, now.Add(leaseDuration)
	job.Attempt++
	if err := s.save(ctx, job, version); err != nil {
		return err
	}

	runCtx, cancel := context.WithTimeout(ctx, time.Duration(s.timeLimit+8)*time.Second)
	result, solveErr := s.optimizer.solve(runCtx, SolveRequest{
		Problem: job.Snapshot, TimeLimitSeconds: s.timeLimit,
	})
	cancel()
	if solveErr != nil {
		slog.Warn("optimizer job failed", "job", id, "attempt", job.Attempt, "error", solveErr)
	}
	saveCtx, saveCancel := context.WithTimeout(context.WithoutCancel(ctx), 5*time.Second)
	defer saveCancel()
	current, version, err := s.get(saveCtx, id)
	if err != nil {
		return err
	}
	if current.Attempt != job.Attempt || current.State != "running" {
		return errConflict
	}
	current.UpdatedAt = s.clock().UTC()
	current.LeaseUntil = time.Time{}
	current.RetryAfter = time.Time{}
	if solveErr == nil {
		current.State, current.Result, current.Error = "completed", &result, ""
	} else {
		var invalid inputError
		if current.Attempt >= maxAttempts || errors.As(solveErr, &invalid) {
			current.State, current.Error = "failed", "最適化サービスで処理できませんでした"
			if invalid != "" {
				current.Error = invalid.Error()
			}
		} else {
			current.State, current.RetryAfter = "queued", current.UpdatedAt.Add(2*time.Second)
		}
	}
	if err := s.save(saveCtx, current, version); err != nil {
		return err
	}
	if current.State == "queued" {
		return errBusy
	}
	return nil
}

func (s jobService) list(ctx context.Context) ([]Job, error) {
	if workspace := demoWorkspace(ctx); workspace != "" {
		return s.listJobs(ctx, demoJobs, workspace+"-")
	}
	return s.listJobs(ctx, jobs, "")
}

func (s jobService) listJobs(ctx context.Context, collection, prefix string) ([]Job, error) {
	ids, err := s.store.listIDs(ctx, collection, prefix)
	if err != nil {
		return nil, err
	}
	result := make([]Job, 0, len(ids))
	for _, id := range ids {
		job, _, err := s.get(ctx, id)
		if errors.Is(err, errNotFound) {
			continue
		}
		if err != nil {
			return nil, err
		}
		result = append(result, job)
	}
	slices.SortFunc(result, func(a, b Job) int { return b.CreatedAt.Compare(a.CreatedAt) })
	return result, nil
}

func (s jobService) allJobs(ctx context.Context) ([]Job, error) {
	shared, err := s.listJobs(ctx, jobs, "")
	if err != nil {
		return nil, err
	}
	personal, err := s.listJobs(ctx, demoJobs, "")
	if err != nil {
		return nil, err
	}
	result := append(shared, personal...)
	slices.SortFunc(result, func(a, b Job) int { return b.CreatedAt.Compare(a.CreatedAt) })
	return result, nil
}

func (s jobService) runLocal(ctx context.Context) {
	ticker := time.NewTicker(time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			result, err := s.allJobs(ctx)
			if err != nil {
				slog.Error("list local jobs", "error", err)
				continue
			}
			for _, job := range slices.Backward(result) {
				now := s.clock().UTC()
				due := !job.LeaseUntil.After(now) && !job.RetryAfter.After(now)
				if due && (job.State == "queued" || job.State == "running") && ctx.Err() == nil {
					if err := s.process(ctx, job.ID); err != nil &&
						!errors.Is(err, errBusy) && !errors.Is(err, errProcessing) && !errors.Is(err, errConflict) {
						slog.Error("process local job", "job", job.ID, "error", err)
					}
					break
				}
			}
		}
	}
}
