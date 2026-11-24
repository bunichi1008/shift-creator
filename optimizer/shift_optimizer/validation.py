from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date

from .calendar_rules import DAY, conflict_reason
from .models import (
    Assignment, Cell, EmployeeMetrics, Issue, Metrics, Problem, Report, Shift,
)
from .time_staffing import time_staffing_segments


def incompatible(problem: Problem, shifts: dict[str, Shift], first: Assignment, second: Assignment) -> str:
    return conflict_reason(problem.limits, first.date, shifts[first.shift_id], second.date, shifts[second.shift_id])


def validate(problem: Problem, assignments: list[Assignment]) -> Report:
    employees = {e.id: e for e in problem.employees}
    shifts = {s.id: s for s in problem.shifts}
    issues: list[Issue] = []

    def issue(code: str, message: str, **attributes) -> None:
        issues.append(Issue(code=code, message=message, **attributes))

    def cell_issue(code: str, message: str, row: Assignment, **attributes) -> None:
        issue(code, message, employee_id=row.employee_id, date=row.date, shift_id=row.shift_id, **attributes)

    assigned: dict[Cell, str] = {}
    by_employee: dict[str, list[Assignment]] = defaultdict(list)
    by_slot: dict[tuple[date, str], list[str]] = defaultdict(list)
    for row in problem.history:
        by_employee[row.employee_id].append(row)
        by_slot[row.date, row.shift_id].append(row.employee_id)
    for row in assignments:
        if row.employee_id not in employees or row.shift_id not in shifts or not problem.in_period(row.date):
            cell_issue("invalid_assignment", "職員・日付・勤務枠を確認してください", row)
            continue
        if row.cell in assigned:
            cell_issue("duplicate_assignment", "1日に2件以上の勤務が入っています", row)
            continue
        assigned[row.cell] = row.shift_id
        by_slot[row.date, row.shift_id].append(row.employee_id)
        by_employee[row.employee_id].append(row)

    covered = 0
    for row in problem.shift_staffing:
        count = len(by_slot[row.date, row.shift_id])
        if row.minimum <= count <= row.maximum:
            covered += 1
        else:
            issue("coverage", f"{count}人の配置です。必要人数は{row.minimum}〜{row.maximum}人です",
                  date=row.date, shift_id=row.shift_id)

    preference_total = preference_met = weighted_violations = 0
    for preference in problem.preferences:
        item = assigned.get((preference.employee_id, preference.date))
        if preference.kind == "unavailable":
            if item and (not preference.shift_id or item == preference.shift_id):
                issue("unavailable", "勤務不可の枠に配置されています", employee_id=preference.employee_id,
                      date=preference.date, shift_id=item)
            continue
        preference_total += 1
        met = item is None if preference.kind == "prefer_off" else item == preference.shift_id
        preference_met += int(met)
        if not met:
            weighted_violations += preference.weight

    for lock in problem.locks:
        if assigned.get(lock.cell, "off") != lock.shift_id:
            cell_issue("locked_assignment", "固定した勤務または休みと一致していません", lock)

    for rule, day, shift in problem.coverage_rule_slots():
        listed = by_slot[day, shift]
        count = sum(rule.skill_id is None or rule.skill_id in employees[key].skills for key in listed)
        if count < rule.value:
            label = f"資格 {rule.skill_id} の職員" if rule.skill_id else "職員"
            issue(rule.type, f"{label}が{rule.value - count}人不足しています（{count}/{rule.value}）",
                  date=day, shift_id=shift, rule_id=rule.id)

    covered_time = total_time = time_shortages = 0
    for segment in time_staffing_segments(problem):
        total_time += 1
        rule = segment.rule
        on_duty = {key for slot in segment.slots for key in by_slot[slot]
                   if rule.skill_id is None or rule.skill_id in employees[key].skills}
        count = len(on_duty)
        if count >= rule.value:
            covered_time += 1
        else:
            time_shortages += 1
            if time_shortages > 3100:
                continue
            label = f"資格 {rule.skill_id} の職員" if rule.skill_id else "職員"
            issue(rule.type, f"{label}が{rule.value - count}人不足しています（{count}/{rule.value}）",
                  **segment.issue_scope())
    if time_shortages > 3100:
        issue("min_time_coverage", f"ほかに{time_shortages - 3100}件の時間帯で人数が不足しています")

    employee_metrics = []
    limits_by_employee = defaultdict(list)
    for rule in problem.rules:
        if rule.type in {"max_consecutive", "max_work_minutes"}:
            targets = [rule.employee_id] if rule.employee_id else employees
            for employee_id in targets:
                limits_by_employee[employee_id].append(rule)
            continue
        if rule.type in {"fixed", "unavailable"}:
            attrs = dict(date=rule.date, employee_id=rule.employee_id, shift_id=rule.shift_id, rule_id=rule.id)
            item = assigned.get((rule.employee_id, rule.date), "off")
            if rule.type == "fixed" and item != rule.shift_id:
                issue(rule.type, "固定した勤務または休みと一致していません", **attrs)
            if rule.type == "unavailable" and item != "off" and rule.shift_id in (None, item):
                issue(rule.type, "勤務不可の枠に配置されています", **attrs)

    for employee in problem.employees:
        ordered = sorted(by_employee[employee.id], key=lambda row: row.date)
        minutes = nights = run = peak = 0
        prior = None
        peak_day = None
        for row in ordered:
            current_period = problem.in_period(row.date)
            if current_period:
                minutes += shifts[row.shift_id].paid_minutes
                nights += row.shift_id == problem.limits.night_shift_id
                if prior and (reason := incompatible(problem, shifts, prior, row)):
                    cell_issue("rest", reason, row)
            run = run + 1 if prior and row.date == prior.date + DAY else 1
            if current_period and run > peak:
                peak, peak_day = run, row.date
            prior = row

        if minutes > employee.max_minutes:
            issue("max_work_minutes", f"勤務時間が上限を{minutes - employee.max_minutes}分超えています",
                  employee_id=employee.id)
        if peak > problem.limits.max_consecutive:
            issue("max_consecutive", f"{peak}連勤です。上限は{problem.limits.max_consecutive}日です",
                  employee_id=employee.id, date=peak_day)
        for rule in limits_by_employee[employee.id]:
            actual = peak if rule.type == "max_consecutive" else minutes
            if actual > rule.value:
                unit = "日" if rule.type == "max_consecutive" else "分"
                issue(rule.type, f"上限の{rule.value}{unit}を超えています（{actual}{unit}）",
                      employee_id=employee.id, rule_id=rule.id,
                      date=peak_day if rule.type == "max_consecutive" else None)
        employee_metrics.append(EmployeeMetrics(
            employee_id=employee.id, minutes=minutes, nights=nights,
            minutes_deviation=abs(minutes - employee.target_minutes),
            nights_deviation=abs(nights - employee.target_nights),
        ))

    baseline = {row.cell: row.shift_id for row in problem.baseline}
    changed = sum(baseline.get(cell) != assigned.get(cell) for cell in baseline.keys() | assigned.keys())
    return Report(issues=issues, metrics=Metrics(
        preference_total=preference_total, preference_met=preference_met,
        weighted_preference_violations=weighted_violations, covered_slots=covered,
        total_slots=len(problem.shift_staffing), covered_time_segments=covered_time, total_time_segments=total_time,
        changed_cells=changed if baseline else None,
        employees=employee_metrics,
    ))


def staffing_diagnostics(problem: Problem) -> list[Issue]:
    """Prove simple staffing shortages using unavailable and fixed assignments."""
    shifts = {s.id: s for s in problem.shifts}
    upper = {(row.date, row.shift_id): row.maximum for row in problem.shift_staffing}
    fixed = defaultdict(list)
    forbidden = problem.unavailable_slots()
    for row in [*problem.history, *problem.fixed_slots()]:
        fixed[row.cell].append(row.shift_id)
    fixed_by_employee = defaultdict(list)
    for (employee_id, day), items in fixed.items():
        for shift_id in set(items) - {"off"}:
            fixed_by_employee[employee_id].append(Assignment(employee_id=employee_id, date=day, shift_id=shift_id))

    requirements = [(r.date, r.shift_id, r.minimum, None, None) for r in problem.shift_staffing if r.minimum]
    requirements.extend((day, shift, rule.value, rule.skill_id, rule.id)
                        for rule, day, shift in problem.coverage_rule_slots())
    result = []
    for day, shift_id, minimum, skill, rule_id in requirements:
        exclusions = Counter()
        available = 0
        for employee in problem.employees:
            if skill and skill not in employee.skills:
                continue
            cell = employee.id, day
            if forbidden[cell] & {None, shift_id}:
                exclusions["勤務不可"] += 1
            elif set(fixed[cell]) - {shift_id}:
                exclusions["同日の固定勤務・休み"] += 1
            else:
                candidate = Assignment(employee_id=employee.id, date=day, shift_id=shift_id)
                if any(incompatible(problem, shifts, fixed_row, candidate)
                       for fixed_row in fixed_by_employee[employee.id] if 0 < abs((fixed_row.date - day).days) <= 2):
                    exclusions["固定勤務との休息・夜勤翌日ルール"] += 1
                else:
                    available += 1
        maximum = min(available, upper.get((day, shift_id), available))
        if maximum < minimum:
            explanation = "、".join(f"{name} {count}人" for name, count in exclusions.items())
            reason = f"（{explanation}）" if explanation else ""
            label = f"資格 {skill} の候補" if skill else "配置可能な候補"
            result.append(Issue(code="staffing_shortage", date=day, shift_id=shift_id, rule_id=rule_id,
                                message=f"{label}は最大{maximum}人、必要{minimum}人です{reason}"))
    return result


def fixed_diagnostics(problem: Problem) -> list[Issue]:
    """Detect irreversible conflicts already present in fixed working cells."""
    fixed = {(row.cell, row.shift_id): row for row in problem.fixed_slots() if row.shift_id != "off"}
    if not fixed:
        return []
    report = validate(problem, list(fixed.values()))
    codes = {
        "duplicate_assignment", "unavailable", "fixed", "locked_assignment",
        "rest", "max_consecutive", "max_work_minutes",
    }
    return [
        issue.model_copy(update={"message": f"固定勤務の組み合わせで違反が発生します。{issue.message}"})
        for issue in report.issues if issue.code in codes
    ]
