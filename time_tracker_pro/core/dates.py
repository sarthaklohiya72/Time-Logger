from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional, Tuple


def parse_period_param(raw_period: Optional[str]) -> str:
    allowed = {"day", "week", "month"}
    if raw_period in allowed:
        return raw_period
    return "day"


def parse_date_param(raw_date: Optional[str]):
    if not raw_date:
        return datetime.now().date()
    try:
        return datetime.strptime(raw_date, "%Y-%m-%d").date()
    except ValueError:
        return datetime.now().date()


def get_period_range(selected_date, period: str) -> Tuple:
    if period == "week":
        idx = (selected_date.weekday() + 1) % 7
        start_date = selected_date - timedelta(days=idx)
        end_date = start_date + timedelta(days=6)
        return start_date, end_date
    if period == "month":
        start_date = selected_date.replace(day=1)
        if start_date.month == 12:
            end_date = start_date.replace(year=start_date.year + 1, month=1) - timedelta(days=1)
        else:
            end_date = start_date.replace(month=start_date.month + 1) - timedelta(days=1)
        return start_date, end_date
    return selected_date, selected_date
