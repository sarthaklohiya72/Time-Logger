from __future__ import annotations

from typing import Dict

import pandas as pd


def get_matrix_stats(df: pd.DataFrame) -> Dict[str, Dict[str, float]]:
    stats = {
        "q1": {"hours": 0.0, "pct": 0.0},
        "q2": {"hours": 0.0, "pct": 0.0},
        "q3": {"hours": 0.0, "pct": 0.0},
        "q4": {"hours": 0.0, "pct": 0.0},
        "important": {"hours": 0.0, "pct": 0.0},
        "not_important": {"hours": 0.0, "pct": 0.0},
        "urgent": {"hours": 0.0, "pct": 0.0},
        "not_urgent": {"hours": 0.0, "pct": 0.0},
        "total_hours": 0.0,
    }
    if df.empty:
        return stats

    total_min = float(df["duration"].sum() or 1)
    q2 = float(df[(~df["urgent"]) & (df["important"])]["duration"].sum())
    q1 = float(df[(df["urgent"]) & (df["important"])]["duration"].sum())
    q3 = float(df[(df["urgent"]) & (~df["important"])]["duration"].sum())
    q4 = float(df[(~df["urgent"]) & (~df["important"])]["duration"].sum())
    imp = float(df[df["important"]]["duration"].sum())
    not_imp = float(df[~df["important"]]["duration"].sum())
    urg = float(df[df["urgent"]]["duration"].sum())
    not_urg = float(df[~df["urgent"]]["duration"].sum())

    return {
        "q1": {"hours": round(q1 / 60.0, 1), "pct": round((q1 / total_min) * 100.0, 1)},
        "q2": {"hours": round(q2 / 60.0, 1), "pct": round((q2 / total_min) * 100.0, 1)},
        "q3": {"hours": round(q3 / 60.0, 1), "pct": round((q3 / total_min) * 100.0, 1)},
        "q4": {"hours": round(q4 / 60.0, 1), "pct": round((q4 / total_min) * 100.0, 1)},
        "important": {"hours": round(imp / 60.0, 1), "pct": round((imp / total_min) * 100.0, 1)},
        "not_important": {"hours": round(not_imp / 60.0, 1), "pct": round((not_imp / total_min) * 100.0, 1)},
        "urgent": {"hours": round(urg / 60.0, 1), "pct": round((urg / total_min) * 100.0, 1)},
        "not_urgent": {"hours": round(not_urg / 60.0, 1), "pct": round((not_urg / total_min) * 100.0, 1)},
        "total_hours": round(total_min / 60.0, 1),
    }
