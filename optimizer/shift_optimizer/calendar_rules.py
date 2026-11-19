from datetime import date, timedelta

from .models import Limits, Shift, clock_minutes

DAY = timedelta(days=1)


def interval(day: date, shift: Shift) -> tuple[int, int]:
    start = clock_minutes(shift.start)
    end = clock_minutes(shift.end)
    origin = day.toordinal() * 1440
    return origin + start, origin + end + (1440 if end < start else 0)


def conflict_reason(limits: Limits, first_day: date, first_shift: Shift, second_day: date, second_shift: Shift) -> str:
    if first_day == second_day:
        return "勤務開始日が重複しています"
    if first_day > second_day:
        first_day, first_shift, second_day, second_shift = second_day, second_shift, first_day, first_shift
    _, end = interval(first_day, first_shift)
    start, _ = interval(second_day, second_shift)
    if start - end < limits.min_rest_minutes:
        return "勤務間休息が不足しています"
    if limits.no_next_day_after_night and first_shift.id == limits.night_shift_id and second_day == first_day + DAY:
        return "夜勤翌日に新しい勤務が始まっています"
    return ""
