from __future__ import annotations

from datetime import datetime
from io import StringIO
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from ..repositories.logs import replace_logs_for_user
from .parser import TimeLogParser


def import_csv_content(db_name: str, user_id: int, csv_content: str) -> int:
    df = pd.read_csv(StringIO(csv_content))

    col_a = None
    col_b = None
    logged_time_col = None

    for col in df.columns:
        col_lower = col.lower().strip()
        if "start" in col_lower or col_lower == "cola":
            col_a = col
        elif "task" in col_lower or col_lower == "colb" or "raw" in col_lower:
            col_b = col
        elif "logged" in col_lower or "time" in col_lower:
            logged_time_col = col

    if col_b is None:
        for col in df.columns:
            if col.lower() not in ["logged time", "raw start"]:
                col_b = col
                break

    if col_b is None:
        raise ValueError("Could not identify task column in CSV")

    parser = TimeLogParser()
    parsed_rows: List[Dict[str, Any]] = []
    previous_end: Optional[datetime] = None

    for _, row in df.iterrows():
        col_a_val = str(row.get(col_a, "") or "") if col_a else ""
        col_b_val = str(row.get(col_b, "") or "")
        client_now = str(row.get(logged_time_col, "")) if logged_time_col else None

        if not col_b_val or col_b_val.strip() == "":
            continue

        parsed = parser.parse_row(col_a_val, col_b_val, client_now, previous_end)
        parsed_rows.append(parsed)
        previous_end = parsed["end_dt"]

    for i in range(1, len(parsed_rows)):
        current = parsed_rows[i]
        prev = parsed_rows[i - 1]
        if current["start_dt"] < prev["end_dt"]:
            parsed_rows[i - 1]["end_dt"] = current["start_dt"]

    final_rows: List[Dict[str, Any]] = []
    for p in parsed_rows:
        if p["start_dt"].date() < p["end_dt"].date():
            midnight = datetime.combine(p["end_dt"].date(), datetime.min.time())
            part1 = p.copy()
            part1["end_dt"] = midnight
            part2 = p.copy()
            part2["start_dt"] = midnight
            final_rows.append(part1)
            final_rows.append(part2)
        else:
            final_rows.append(p)

    return replace_logs_for_user(db_name, int(user_id), final_rows)
