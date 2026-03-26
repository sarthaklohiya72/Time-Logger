from __future__ import annotations


def human_hours(h: float) -> str:
    total = max(0, int(round(float(h) * 60)))
    hrs = total // 60
    mins = total % 60
    if hrs == 0:
        return f"{mins} minutes"
    if mins == 0:
        return f"{hrs} hours"
    if mins == 30:
        return f"{hrs}.5 hours"
    return f"{hrs} hours {mins} minutes"
