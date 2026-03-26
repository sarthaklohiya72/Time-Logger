from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List

import pandas as pd

from ..db import get_db_connection
from ..core.tags import filter_special_tags


def fetch_local_data(db_name: str, user_id: int) -> pd.DataFrame:
    conn = get_db_connection(db_name)
    df = pd.read_sql_query(
        "SELECT * FROM logs WHERE user_id = ? ORDER BY start_date ASC, start_time ASC",
        conn,
        params=(int(user_id),),
    )
    conn.close()
    if df.empty:
        return pd.DataFrame()

    final_rows: List[Dict[str, Any]] = []
    for _, row in df.iterrows():
        start = pd.to_datetime(f"{row['start_date']} {row['start_time']}")
        end = pd.to_datetime(f"{row['end_date']} {row['end_time']}")
        special_tags = filter_special_tags(row["tags"])
        tag_value = ", ".join(special_tags) if special_tags else "Waste"
        primary_tag = special_tags[0] if special_tags else "Waste"
        final_rows.append(
            {
                "id": row["id"],
                "date": start.date(),
                "start_time": start.strftime("%I:%M %p"),
                "end_time": end.strftime("%I:%M %p"),
                "start_datetime": start,
                "end_datetime": end,
                "task": row["task"],
                "duration": row["duration"],
                "tag": tag_value,
                "primary_tag": primary_tag,
                "special_tags": special_tags,
                "urgent": bool(row["urg"]),
                "important": bool(row["imp"]),
            }
        )
    return pd.DataFrame(final_rows)


def replace_logs_for_user(db_name: str, user_id: int, final_rows: List[Dict[str, Any]]) -> int:
    if not final_rows:
        return 0

    conn = get_db_connection(db_name)
    inserted_count = 0
    try:
        conn.execute("DELETE FROM logs WHERE user_id = ?", (int(user_id),))

        for p in final_rows:
            duration = int((p["end_dt"] - p["start_dt"]).total_seconds() / 60)
            if duration <= 0:
                continue
            conn.execute(
                """
                INSERT INTO logs (
                    start_date, start_time, end_date, end_time,
                    task, duration, tags, urg, imp, user_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    p["start_dt"].strftime("%Y-%m-%d"),
                    p["start_dt"].strftime("%H:%M:%S"),
                    p["end_dt"].strftime("%Y-%m-%d"),
                    p["end_dt"].strftime("%H:%M:%S"),
                    p["task"],
                    duration,
                    p["tag"],
                    1 if p.get("urg") else 0,
                    1 if p.get("imp") else 0,
                    int(user_id),
                ),
            )
            inserted_count += 1

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return inserted_count
