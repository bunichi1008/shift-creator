package main

import (
	"cmp"
	"context"
	"errors"
	"slices"
	"time"
)

var legacySampleNames = map[string]string{
	"e01": "青木 遥", "e02": "石井 凛", "e03": "上田 陸", "e04": "遠藤 澪",
	"e05": "大野 葵", "e06": "片山 湊", "e07": "木村 結衣", "e08": "小林 朔",
	"e09": "斉藤 陽菜", "e10": "佐藤 翔", "e11": "高橋 美月", "e12": "田中 奏",
	"e13": "伊藤 翼", "e14": "井上 菫", "e15": "今井 悠", "e16": "岩田 紬",
	"e17": "岡田 蓮", "e18": "加藤 杏", "e19": "川口 颯", "e20": "栗田 華",
	"e21": "近藤 陽", "e22": "清水 碧", "e23": "鈴木 柊", "e24": "中村 結",
}

func planDocument(ctx context.Context) (collection, id string) {
	if workspace := demoWorkspace(ctx); workspace != "" {
		return demoPlans, workspace
	}
	return plans, currentPlanID
}

func readPlan(ctx context.Context, store store) (Plan, string, error) {
	var plan Plan
	collection, id := planDocument(ctx)
	version, err := store.read(ctx, collection, id, &plan)
	return plan, version, err
}

func (s *server) loadPlan(ctx context.Context) (Plan, string, error) {
	for range 5 {
		plan, version, err := readPlan(ctx, s.store)
		if err != nil || !s.renameLegacySampleRoster(plan.Problem.Employees) {
			return plan, version, err
		}
		plan.SampleRoster = true
		collection, id := planDocument(ctx)
		// Only display names change, so jobs for this revision remain adoptable.
		err = s.store.write(ctx, collection, id, version, plan)
		if err == nil {
			return readPlan(ctx, s.store)
		}
		if !errors.Is(err, errConflict) {
			return Plan{}, "", err
		}
	}
	return Plan{}, "", errBusy
}

func (s *server) ensurePlan(ctx context.Context) error {
	_, _, err := readPlan(ctx, s.store)
	if !errors.Is(err, errNotFound) {
		return err
	}
	initial := Plan{Revision: 1, UpdatedAt: s.now().UTC(), Problem: s.demo}
	collection, id := planDocument(ctx)
	err = s.store.write(ctx, collection, id, "", initial)
	if errors.Is(err, errConflict) {
		return nil
	}
	return err
}

func (s *server) updatePlan(ctx context.Context, revision int, update func(*Problem) error) (Plan, error) {
	plan, version, err := s.loadPlan(ctx)
	if err != nil {
		return Plan{}, err
	}
	if plan.Revision != revision {
		return Plan{}, errConflict
	}
	if err := update(&plan.Problem); err != nil {
		return Plan{}, err
	}
	s.renameLegacySampleRoster(plan.Problem.Employees)
	plan.Revision++
	plan.UpdatedAt = s.now().UTC()
	plan.SampleRoster = s.isSampleRoster(plan.Problem)
	collection, id := planDocument(ctx)
	return plan, s.store.write(ctx, collection, id, version, plan)
}

func (s *server) isSampleRoster(p Problem) bool {
	if len(p.Employees) == 0 || len(p.Employees) != len(s.demo.Employees) {
		return false
	}
	return rosterMatchesNames(p.Employees, s.sampleNames())
}

func (s *server) sampleNames() map[string]string {
	names := make(map[string]string, len(s.demo.Employees))
	for _, employee := range s.demo.Employees {
		names[employee.ID] = employee.Name
	}
	return names
}

func rosterMatchesNames(employees []Employee, names map[string]string) bool {
	if len(employees) != len(names) {
		return false
	}
	for _, employee := range employees {
		if name, exists := names[employee.ID]; !exists || name != employee.Name {
			return false
		}
	}
	return true
}

func (s *server) renameLegacySampleRoster(employees []Employee) bool {
	if !rosterMatchesNames(employees, legacySampleNames) || len(employees) != len(s.demo.Employees) {
		return false
	}
	names := s.sampleNames()
	for _, employee := range employees {
		if names[employee.ID] == "" {
			return false
		}
	}
	changed := false
	for i := range employees {
		name := names[employees[i].ID]
		if employees[i].Name != name {
			employees[i].Name = name
			changed = true
		}
	}
	return changed
}

func changeCell(p *Problem, cell Assignment, locked bool) error {
	if !p.validEmployee(cell.EmployeeID) || !p.validDate(cell.Date) ||
		!(p.validShift(cell.ShiftID) || cell.ShiftID == "off") {
		return inputError("職員、対象日、勤務種別を確認してください")
	}
	match := func(other Assignment) bool {
		return other.EmployeeID == cell.EmployeeID && other.Date == cell.Date
	}
	cell.BreakStarts = nil
	for _, previous := range p.Baseline {
		if match(previous) && previous.ShiftID == cell.ShiftID {
			cell.BreakStarts = previous.BreakStarts
			break
		}
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
