"""期间天数：半开区间 ``[start, end)``、闰年、自然月。

天数一律按订阅时区的本地日历日计算，不按 24 小时整除，避免夏令时把一天算成 23/25 小时。
"""
from __future__ import annotations

import calendar
import datetime as dt


def is_leap(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def days(start: dt.date, end: dt.date) -> int:
    """半开区间天数；end <= start 返回 0（由调用方判原因码）。"""
    delta = (end - start).days
    return delta if delta > 0 else 0


def month_days(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def contains_leap_day(start: dt.date, end: dt.date) -> bool:
    """区间内是否含 2 月 29 日（含端点 start，不含 end）。"""
    for year in range(start.year, end.year + 1):
        if not is_leap(year):
            continue
        leap_day = dt.date(year, 2, 29)
        if start <= leap_day < end:
            return True
    return False


def months_spanned(start: dt.date, end: dt.date) -> int:
    return (end.year - start.year) * 12 + (end.month - start.month) + (1 if end.day else 0)


def add_months(day: dt.date, months: int) -> dt.date:
    """按自然月推进，日号超出目标月长度时夹到月末（1-31 + 1 月 → 2-28/29）。"""
    total = day.year * 12 + (day.month - 1) + int(months)
    year, month = divmod(total, 12)
    month += 1
    last = month_days(year, month)
    return dt.date(year, month, min(day.day, last))


def cycle_bounds(period_start: dt.date, cycle_months: int) -> tuple[dt.date, dt.date]:
    """合同周期 ``[周期起, 周期止)``：起点同期间起，长度按周期月数推。"""
    months = max(int(cycle_months), 1)
    return period_start, add_months(period_start, months)


def cycle_days(period_start: dt.date, cycle_months: int) -> int:
    start, end = cycle_bounds(period_start, cycle_months)
    return days(start, end)


def range_label(start: dt.date, end: dt.date) -> str:
    return f"[{start.isoformat()}, {end.isoformat()})"


def month_label(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def month_bounds(year: int, month: int) -> tuple[dt.date, dt.date]:
    first = dt.date(year, month, 1)
    if month == 12:
        return first, dt.date(year + 1, 1, 1)
    return first, dt.date(year, month + 1, 1)


def overlapping_days(start: dt.date, end: dt.date, lo: dt.date, hi: dt.date) -> int:
    """两个半开区间的重叠天数；不重叠返回 0。"""
    left = max(start, lo)
    right = min(end, hi)
    return days(left, right)
