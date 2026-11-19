package main

import (
	"context"
	"errors"
	"net/http"
	"regexp"
)

var jobIDPattern = regexp.MustCompile("^[0-9a-f]{32}$")

func (s *server) listJobs(w http.ResponseWriter, r *http.Request) {
	jobs, err := s.jobs.list(r.Context())
	if err != nil {
		respondError(w, err)
		return
	}
	views := make([]JobView, 0, min(len(jobs), 15))
	for _, job := range jobs[:cap(views)] {
		views = append(views, job.view())
	}
	jsonResponse(w, 200, views)
}

func (s *server) submitJob(w http.ResponseWriter, r *http.Request) {
	type request struct {
		Revision       int    `json:"revision"`
		IdempotencyKey string `json:"idempotency_key"`
	}
	handleJSON(w, r, 202, func(body request) (any, error) {
		job, err := s.jobs.submit(r.Context(), body.Revision, body.IdempotencyKey)
		if err != nil {
			return nil, err
		}
		w.Header().Set("Location", "/api/jobs/"+job.ID)
		return job.view(), nil
	})
}

func (s *server) lookupJob(ctx context.Context, id string) (Job, error) {
	if !jobIDPattern.MatchString(id) {
		return Job{}, errNotFound
	}
	job, _, err := s.jobs.get(ctx, id)
	return job, err
}

func (s *server) getJob(w http.ResponseWriter, r *http.Request) {
	job, err := s.lookupJob(r.Context(), r.PathValue("id"))
	if err != nil {
		respondError(w, err)
		return
	}
	s.jobs.tryDispatch(r.Context(), job)
	jsonResponse(w, 200, job.view())
}

func (s *server) adoptJob(w http.ResponseWriter, r *http.Request) {
	handleJSON(w, r, 200, func(body revisionRequest) (any, error) {
		job, err := s.lookupJob(r.Context(), r.PathValue("id"))
		if err != nil {
			return nil, err
		}
		if job.PlanRevision != body.Revision {
			return nil, errConflict
		}
		if job.Result == nil || !job.Result.adoptable() {
			return nil, inputError("必須条件の検査を通過した勤務表を選択してください")
		}
		return s.updatePlan(r.Context(), body.Revision, func(p *Problem) error {
			p.Baseline = append([]Assignment{}, job.Result.Assignments...)
			return nil
		})
	})
}

func (s *server) runTask(w http.ResponseWriter, r *http.Request) {
	type request struct {
		JobID string `json:"job_id"`
	}
	handleJSON(w, r, 200, func(body request) (any, error) {
		if !jobIDPattern.MatchString(body.JobID) {
			return nil, inputError("invalid job ID")
		}
		err := s.jobs.process(r.Context(), body.JobID)
		if errors.Is(err, errConflict) {
			err = errBusy
		}
		return map[string]bool{"processed": err == nil}, err
	})
}

func (s *server) redrive(w http.ResponseWriter, r *http.Request) {
	items, err := s.jobs.list(r.Context())
	if err != nil {
		respondError(w, err)
		return
	}
	for _, item := range items {
		s.jobs.tryDispatch(r.Context(), item)
	}
	jsonResponse(w, 200, map[string]bool{"checked": true})
}
