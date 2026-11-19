package main

import (
	"cmp"
	"context"
	"errors"
	"slices"
	"time"
)

func readPlan(ctx context.Context, store store) (Plan, string, error) {
	var plan Plan
	version, err := store.read(ctx, plans, currentPlanID, &plan)
	return plan, version, err
}

func (s *server) ensurePlan(ctx context.Context) error {
	_, _, err := readPlan(ctx, s.store)
	if !errors.Is(err, errNotFound) {
		return err
	}
	initial := Plan{Revision: 1, UpdatedAt: s.now().UTC(), Problem: s.demo}
	err = s.store.write(ctx, plans, currentPlanID, "", initial)
	if errors.Is(err, errConflict) {
		return nil
	}
	return err
}

func (s *server) updatePlan(ctx context.Context, revision int, update func(*Problem) error) (Plan, error) {
	plan, version, err := readPlan(ctx, s.store)
	if err != nil {
		return Plan{}, err
	}
	if plan.Revision != revision {
		return Plan{}, errConflict
	}
	if err := update(&plan.Problem); err != nil {
		return Plan{}, err
	}
	plan.Revision++
	plan.UpdatedAt = s.now().UTC()
	return plan, s.store.write(ctx, plans, currentPlanID, version, plan)
}

func changeCell(p *Problem, cell Assignment, locked bool) error {
	if !p.validEmployee(cell.EmployeeID) || !p.validDate(cell.Date) ||
		!(p.validShift(cell.ShiftID) || cell.ShiftID == "off") {
		return inputError("職員、対象日、勤務種別を確認してください")
	}
	match := func(other Assignment) bool {
		return other.EmployeeID == cell.EmployeeID && other.Date == cell.Date
	}
	p.Baseline = slices.DeleteFunc(p.Baseline, match)
	p.Locks = slices.DeleteFunc(p.Locks, match)
	if cell.ShiftID != "off" {
		p.Baseline = append(p.Baseline, cell)
	}
	if locked {
		p.Locks = append(p.Locks, cell)
	}
	slices.SortFunc(p.Baseline, compareAssignments)
	slices.SortFunc(p.Locks, compareAssignments)
	return nil
}

func compareAssignments(a, b Assignment) int {
	return cmp.Or(cmp.Compare(a.Date, b.Date), cmp.Compare(a.EmployeeID, b.EmployeeID))
}

func dates(p Problem) []string {
	start, _ := time.Parse(time.DateOnly, p.StartDate)
	result := make([]string, p.Days)
	for i := range p.Days {
		result[i] = start.AddDate(0, 0, i).Format(time.DateOnly)
	}
	return result
}
