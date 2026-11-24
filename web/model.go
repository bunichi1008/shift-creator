package main

import (
	"fmt"
	"slices"
	"strings"
	"time"
)

const schemaVersion = 1
const maxPlanRules = 120

var allowedWeekdays = []string{"mon", "tue", "wed", "thu", "fri", "sat", "sun"}

type Employee struct {
	ID            string   `json:"id"`
	Name          string   `json:"name"`
	Skills        []string `json:"skills"`
	TargetMinutes int      `json:"target_minutes"`
	MaxMinutes    int      `json:"max_minutes"`
	TargetNights  int      `json:"target_nights"`
}

type ShiftBreak struct {
	Start       string `json:"start"`
	End         string `json:"end"`
	LatestStart string `json:"latest_start,omitempty"`
}

type Shift struct {
	ID          string       `json:"id"`
	Name        string       `json:"name"`
	Start       string       `json:"start"`
	End         string       `json:"end"`
	PaidMinutes int          `json:"paid_minutes"`
	Breaks      []ShiftBreak `json:"breaks,omitempty"`
}

type Coverage struct {
	Date    string `json:"date"`
	ShiftID string `json:"shift_id"`
	Minimum int    `json:"minimum"`
	Maximum int    `json:"maximum"`
}

type Assignment struct {
	EmployeeID  string   `json:"employee_id"`
	Date        string   `json:"date"`
	ShiftID     string   `json:"shift_id"`
	BreakStarts []string `json:"break_starts,omitempty"`
}

type Preference struct {
	EmployeeID string `json:"employee_id"`
	Date       string `json:"date"`
	Kind       string `json:"kind"`
	ShiftID    string `json:"shift_id,omitempty"`
	Weight     int    `json:"weight"`
}

type Rule struct {
	ID         string   `json:"id,omitempty"`
	Type       string   `json:"type"`
	Date       string   `json:"date,omitempty"`
	Dates      []string `json:"dates,omitempty"`
	Weekdays   []string `json:"weekdays,omitempty"`
	EmployeeID string   `json:"employee_id,omitempty"`
	ShiftID    string   `json:"shift_id,omitempty"`
	AllDates   bool     `json:"all_dates,omitempty"`
	AllShifts  bool     `json:"all_shifts,omitempty"`
	SkillID    string   `json:"skill_id,omitempty"`
	StartTime  string   `json:"start_time,omitempty"`
	EndTime    string   `json:"end_time,omitempty"`
	Value      *int     `json:"value,omitempty"`
}

type Limits struct {
	MaxConsecutive      int    `json:"max_consecutive"`
	MinRestMinutes      int    `json:"min_rest_minutes"`
	NightShiftID        string `json:"night_shift_id,omitempty"`
	NoNextDayAfterNight bool   `json:"no_next_day_after_night"`
}

type Optimization struct {
	Order                *[]string `json:"order,omitempty"`
	NightDeviationWeight *int      `json:"night_deviation_weight,omitempty"`
}

type Problem struct {
	SchemaVersion    int           `json:"schema_version"`
	StartDate        string        `json:"start_date"`
	Days             int           `json:"days"`
	Employees        []Employee    `json:"employees"`
	Shifts           []Shift       `json:"shifts"`
	HistoryShifts    []Shift       `json:"history_shifts,omitempty"`
	Coverage         []Coverage    `json:"coverage"`
	UseShiftCoverage *bool         `json:"use_shift_coverage,omitempty"`
	Preferences      []Preference  `json:"preferences"`
	Rules            []Rule        `json:"rules"`
	Locks            []Assignment  `json:"locks"`
	Baseline         []Assignment  `json:"baseline"`
	History          []Assignment  `json:"history"`
	Limits           Limits        `json:"limits"`
	Optimization     *Optimization `json:"optimization,omitempty"`
}

func (p Problem) validDate(value string) bool {
	start, startErr := time.Parse(time.DateOnly, p.StartDate)
	day, dayErr := time.Parse(time.DateOnly, value)
	return startErr == nil && dayErr == nil && !day.Before(start) && day.Before(start.AddDate(0, 0, p.Days))
}

func (p Problem) validEmployee(id string) bool {
	return slices.ContainsFunc(p.Employees, func(e Employee) bool { return e.ID == id })
}

func (p Problem) validShift(id string) bool {
	return slices.ContainsFunc(p.Shifts, func(s Shift) bool { return s.ID == id })
}

func (p Problem) validSkill(id string) bool {
	return slices.ContainsFunc(p.Employees, func(e Employee) bool { return slices.Contains(e.Skills, id) })
}

func (p Problem) checkRule(rule Rule) error {
	var date, employee, shift, skill bool
	var min, max int
	valueLabel := "数値"
	coverage := rule.Type == "min_skill_coverage" || rule.Type == "min_coverage"
	timeCoverage := rule.Type == "min_time_coverage"
	if rule.AllShifts && !coverage {
		return inputError("全勤務枠の指定は勤務別の人数条件で使えます")
	}
	if rule.AllDates && !coverage && !timeCoverage {
		return inputError("全日の指定は人数条件で使えます")
	}
	switch rule.Type {
	case "min_skill_coverage", "min_coverage":
		date, shift, skill, min, max = !rule.AllDates, !rule.AllShifts, rule.Type == "min_skill_coverage", 1, 100
		valueLabel = "最低人数"
	case "min_time_coverage":
		date, skill, min, max = !rule.AllDates, rule.SkillID != "", 1, 100
		valueLabel = "最低人数"
	case "fixed":
		date, employee, shift = true, true, true
	case "unavailable":
		date, employee, shift = true, true, rule.ShiftID != ""
	case "available_only":
		employee = true
	case "prefer_off":
		date, employee, min, max = true, true, 1, 100
		valueLabel = "希望の重み"
	case "max_consecutive":
		employee, min, max = rule.EmployeeID != "", 1, 31
		valueLabel = "連勤の上限日数"
	case "max_work_minutes":
		employee, max = true, 44640
		valueLabel = "勤務時間の上限（分）"
	default:
		return inputError("未対応の条件です")
	}
	fields := []struct {
		label   string
		expects bool
		present bool
	}{
		{"対象日", date, rule.Date != ""},
		{"職員", employee, rule.EmployeeID != ""},
		{"勤務枠", shift, rule.ShiftID != ""},
		{"資格", skill, rule.SkillID != ""},
		{"開始時刻", timeCoverage, rule.StartTime != ""},
		{"終了時刻", timeCoverage, rule.EndTime != ""},
		{valueLabel, max != 0, rule.Value != nil},
	}
	var missing, unexpected []string
	for _, field := range fields {
		if field.expects && !field.present {
			missing = append(missing, field.label)
		}
		if !field.expects && field.present {
			unexpected = append(unexpected, field.label)
		}
	}
	if len(missing) > 0 {
		return inputError(strings.Join(missing, "と") + "を指定してください")
	}
	if len(unexpected) > 0 {
		return inputError("この種類の条件では" + strings.Join(unexpected, "と") + "の指定を外してください")
	}
	if date && !p.validDate(rule.Date) {
		return inputError("対象期間の日付を指定してください")
	}
	if employee && !p.validEmployee(rule.EmployeeID) {
		return inputError("職員を確認してください")
	}
	if shift && !p.validShift(rule.ShiftID) && !(rule.Type == "fixed" && rule.ShiftID == "off") {
		return inputError("勤務種別を確認してください")
	}
	if skill && !p.validSkill(rule.SkillID) {
		return inputError("資格を確認してください")
	}
	if rule.Type == "available_only" {
		if err := p.checkAvailableDays(rule.Dates, rule.Weekdays); err != nil {
			return err
		}
	} else if len(rule.Dates) != 0 || len(rule.Weekdays) != 0 {
		return inputError("複数の日付・曜日は出勤可能日の条件で指定してください")
	}
	if timeCoverage {
		for _, value := range []string{rule.StartTime, rule.EndTime} {
			clock, err := time.Parse("15:04", value)
			if err != nil || clock.Format("15:04") != value {
				return inputError("開始・終了時刻を HH:MM で指定してください")
			}
		}
		if rule.StartTime == rule.EndTime && rule.StartTime != "00:00" {
			return inputError("開始と終了は別の時刻を指定してください")
		}
	}
	if rule.Value != nil && (*rule.Value < min || *rule.Value > max) {
		return inputError(fmt.Sprintf("数値は %d〜%d の範囲で指定してください", min, max))
	}
	return nil
}

func (p Problem) checkAvailableDays(dates, weekdays []string) error {
	if len(dates) == 0 && len(weekdays) == 0 {
		return inputError("出勤できる日付または曜日を選んでください")
	}
	seen := make(map[string]bool, len(dates)+len(weekdays))
	for _, date := range dates {
		if !p.validDate(date) || seen[date] {
			return inputError("対象期間内の出勤可能日を重複なく選んでください")
		}
		seen[date] = true
	}
	for _, weekday := range weekdays {
		if !slices.Contains(allowedWeekdays, weekday) || seen[weekday] {
			return inputError("出勤可能な曜日を重複なく選んでください")
		}
		seen[weekday] = true
	}
	return nil
}

func checkStaffRuleUniqueness(rules []Rule) error {
	seen := make(map[[3]string]bool)
	for _, rule := range rules {
		switch rule.Type {
		case "available_only", "prefer_off":
			key := [3]string{rule.Type, rule.EmployeeID, rule.Date}
			if seen[key] {
				label := "この職員の出勤可能日"
				if rule.Type == "prefer_off" {
					label = "この職員の同じ日の休み希望"
				}
				return inputError(label + "は登録済みです。既存の条件を編集してください")
			}
			seen[key] = true
		}
	}
	return nil
}

func (p Problem) checkRuleBatch(rules []Rule) error {
	if len(rules) == 0 {
		return inputError("追加する勤務条件を指定してください")
	}
	if len(p.Rules)+len(rules) > maxPlanRules {
		return inputError(fmt.Sprintf("条件の数は%d件までです", maxPlanRules))
	}
	for index, rule := range rules {
		if err := p.checkRule(rule); err != nil {
			if len(rules) == 1 {
				return err
			}
			return inputError(fmt.Sprintf("%d件目は、%s", index+1, err))
		}
	}
	return checkStaffRuleUniqueness(append(slices.Clone(p.Rules), rules...))
}

type Plan struct {
	Revision     int       `json:"revision"`
	UpdatedAt    time.Time `json:"updated_at"`
	Problem      Problem   `json:"problem"`
	SampleRoster bool      `json:"sample_roster,omitempty"`
}

type SolveRequest struct {
	Problem          Problem `json:"problem"`
	TimeLimitSeconds int     `json:"time_limit_seconds"`
}

type ValidationRequest struct {
	Problem     Problem      `json:"problem"`
	Assignments []Assignment `json:"assignments"`
}

type Issue struct {
	Code       string `json:"code"`
	Message    string `json:"message"`
	EmployeeID string `json:"employee_id,omitempty"`
	Date       string `json:"date,omitempty"`
	ShiftID    string `json:"shift_id,omitempty"`
	RuleID     string `json:"rule_id,omitempty"`
	StartTime  string `json:"start_time,omitempty"`
	EndTime    string `json:"end_time,omitempty"`
}

type EmployeeMetrics struct {
	EmployeeID       string `json:"employee_id"`
	Minutes          int    `json:"minutes"`
	Nights           int    `json:"nights"`
	MinutesDeviation int    `json:"minutes_deviation"`
	NightsDeviation  int    `json:"nights_deviation"`
}

type Metrics struct {
	PreferenceTotal              int               `json:"preference_total"`
	PreferenceMet                int               `json:"preference_met"`
	WeightedPreferenceViolations int               `json:"weighted_preference_violations"`
	CoveredSlots                 int               `json:"covered_slots"`
	TotalSlots                   int               `json:"total_slots"`
	CoveredTimeSegments          int               `json:"covered_time_segments"`
	TotalTimeSegments            int               `json:"total_time_segments"`
	ChangedCells                 *int              `json:"changed_cells"`
	Employees                    []EmployeeMetrics `json:"employees"`
}

type Report struct {
	Issues  []Issue `json:"issues"`
	Metrics Metrics `json:"metrics"`
}

type SolveResult struct {
	Status      string             `json:"status"`
	Solver      string             `json:"solver"`
	Message     string             `json:"message"`
	Assignments []Assignment       `json:"assignments"`
	Report      *Report            `json:"report"`
	Diagnostics []Issue            `json:"diagnostics"`
	Objective   map[string]float64 `json:"objective"`
	BestBound   *float64           `json:"best_bound"`
	Gap         *float64           `json:"gap"`
	ElapsedMS   int                `json:"elapsed_ms"`
}

func (r SolveResult) adoptable() bool {
	return (r.Status == "optimal" || r.Status == "feasible" || r.Status == "demo_valid") &&
		r.Report != nil && len(r.Report.Issues) == 0
}

type Job struct {
	ID           string       `json:"id"`
	PlanRevision int          `json:"plan_revision"`
	State        string       `json:"state"`
	CreatedAt    time.Time    `json:"created_at"`
	UpdatedAt    time.Time    `json:"updated_at"`
	Attempt      int          `json:"attempt"`
	Dispatched   bool         `json:"dispatched"`
	LeaseUntil   time.Time    `json:"lease_until"`
	RetryAfter   time.Time    `json:"retry_after"`
	Snapshot     Problem      `json:"snapshot"`
	Result       *SolveResult `json:"result,omitempty"`
	Error        string       `json:"error,omitempty"`
}

type JobView struct {
	ID           string       `json:"id"`
	PlanRevision int          `json:"plan_revision"`
	State        string       `json:"state"`
	CreatedAt    time.Time    `json:"created_at"`
	Result       *SolveResult `json:"result,omitempty"`
	Error        string       `json:"error,omitempty"`
}

func (j Job) view() JobView {
	return JobView{
		ID: j.ID, PlanRevision: j.PlanRevision, State: j.State, CreatedAt: j.CreatedAt,
		Result: j.Result, Error: j.Error,
	}
}

type inputError string

func (e inputError) Error() string { return string(e) }
