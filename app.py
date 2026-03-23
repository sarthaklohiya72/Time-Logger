from __future__ import annotations

import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple
from flask import send_file
import pandas as pd
import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, make_response
from io import StringIO

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

DB_NAME = "productivity.db"
SHEETY_ENDPOINT_ENV = "SHEETY_ENDPOINT"
API_AUTH_TOKEN_ENV = "TIME_TRACKER_API_TOKEN"
SYNC_INTERVAL_SECONDS = int(os.getenv("SYNC_INTERVAL_SECONDS", "300"))
SYNC_FAIL_COOLDOWN_SECONDS = int(os.getenv("SYNC_FAIL_COOLDOWN_SECONDS", "1800"))
SPECIAL_TAGS = {"Work", "Necessity", "Soul", "Rest", "Waste"}

_LAST_SYNC_TS: Optional[datetime] = None
_LAST_SYNC_FAIL_TS: Optional[datetime] = None

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

app.jinja_env.filters["human_hours"] = human_hours

def normalize_tag(raw: Optional[str]) -> str:
    s = str(raw or "").strip()
    if not s:
        return ""
    parts = re.split(r"[,.]", s)
    cleaned_parts: List[str] = []
    for part in parts:
        cleaned = re.sub(r"[^A-Za-z\s]+$", "", part)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        if not cleaned:
            continue
        if cleaned.lower() in {"undefined", "null", "none", "nan", "urg", "urgent", "imp", "important"}:
            continue
        cleaned = cleaned.title()
        if cleaned and cleaned not in cleaned_parts:
            cleaned_parts.append(cleaned)
    return ", ".join(cleaned_parts)


def filter_special_tags(raw: Optional[str]) -> List[str]:
    normalized = normalize_tag(raw)
    if not normalized:
        return []
    parts = [p.strip() for p in normalized.split(",") if p.strip()]
    return [p for p in parts if p in SPECIAL_TAGS]


def primary_special_tag(raw: Optional[str]) -> str:
    tags = filter_special_tags(raw)
    return tags[0] if tags else "Waste"

def get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    try:
        conn = get_db_connection()
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                start_date TEXT,
                start_time TEXT,
                end_date TEXT,
                end_time TEXT,
                task TEXT,
                duration INTEGER,
                tags TEXT,
                urg INTEGER,
                imp INTEGER
            )
            """
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        logger.exception("DB initialization error: %s", exc)


# --- CALL THE FUNCTION HERE ---
init_db()
# ------------------------------

class TimeLogParser:
    def __init__(self) -> None:
        self.time_pattern = re.compile(
            r"^(\d{1,2})(?:[:\s]?(\d{2}))?\s*([ap]m)?\s*",
            re.IGNORECASE,
        )


class TimeLogParser:
    def __init__(self) -> None:
        self.time_pattern = re.compile(
            r"^(\d{1,2})(?:[:\s]?(\d{2}))?\s*([ap]m)?\s*",
            re.IGNORECASE,
        )

    def parse_time_string(
        self, text_str: str, ref_date: datetime
    ) -> Tuple[Optional[datetime], str]:
        if not isinstance(text_str, str) or not text_str:
            return None, text_str
        match = self.time_pattern.match(text_str)
        if match:
            hour, minute = int(match.group(1)), int(match.group(2) or 0)
            ampm = match.group(3).lower() if match.group(3) else None
            if ampm == "pm" and hour < 12:
                hour += 12
            if ampm == "am" and hour == 12:
                hour = 0
            dt = ref_date.replace(
                hour=hour,
                minute=minute,
                second=0,
                microsecond=0,
            )
            return dt, text_str[match.end() :].strip()
        return None, text_str

    def parse_row(
        self,
        col_a: str,
        col_b: str,
        client_now_str: str,
        previous_end_dt: Optional[datetime],
    ) -> Dict[str, Any]:
        try:
            client_now = pd.to_datetime(client_now_str)
        except Exception:
            client_now = datetime.now()

        explicit_time_a, _ = self.parse_time_string(col_a, client_now)
        explicit_time_b, remaining_text = self.parse_time_string(col_b, client_now)

        raw_text = remaining_text if remaining_text else (col_b if col_b else "Unspecified")
        task_name, tag = raw_text.strip(), ""
        is_urg, is_imp = False, False

        tags: List[str] = []

        def apply_token(raw_token: str) -> None:
            nonlocal is_urg, is_imp
            token = re.sub(r"[^A-Za-z]+", "", raw_token).lower()
            if not token:
                return
            if token in {"urg", "urgent"}:
                is_urg = True
                return
            if token in {"imp", "important"}:
                is_imp = True
                return
            if token in {"work", "necessity", "soul", "rest", "waste"}:
                canonical = token.title()
                if canonical not in tags:
                    tags.append(canonical)
                return
            return

        if "." in raw_text:
            before, after = raw_text.split(".", 1)
            task_name = before.strip() or task_name
            meta = after.strip()
            if meta:
                for tok in re.split(r"[\s,]+", meta):
                    apply_token(tok)
        else:
            for tok in re.split(r"\s+", raw_text.strip()):
                apply_token(tok)

        tag = ", ".join(tags) if tags else "Waste"

        end_dt = explicit_time_b if explicit_time_b else client_now

        if end_dt > client_now + timedelta(hours=1):
            end_dt -= timedelta(days=1)

        start_dt: Optional[datetime] = None

        if explicit_time_a:
            start_dt = explicit_time_a

        if not start_dt:
            if previous_end_dt:
                gap_hours = (end_dt - previous_end_dt).total_seconds() / 3600
                if gap_hours > 16:
                    start_dt = end_dt - timedelta(minutes=30)
                else:
                    start_dt = previous_end_dt
            else:
                start_dt = end_dt - timedelta(minutes=30)

        return {
            "start_dt": start_dt,
            "end_dt": end_dt,
            "task": task_name,
            "tag": tag,
            "urg": is_urg,
            "imp": is_imp,
        }


def _should_sync(now: Optional[datetime] = None) -> bool:
    global _LAST_SYNC_TS, _LAST_SYNC_FAIL_TS
    current = now or datetime.now(timezone.utc)
    if _LAST_SYNC_FAIL_TS and (current - _LAST_SYNC_FAIL_TS).total_seconds() < SYNC_FAIL_COOLDOWN_SECONDS:
        return False
    if _LAST_SYNC_TS is None:
        return True
    return (current - _LAST_SYNC_TS).total_seconds() > SYNC_INTERVAL_SECONDS


def sync_cloud_data(force: bool = False) -> None:
    global _LAST_SYNC_TS, _LAST_SYNC_FAIL_TS

    if os.getenv("DISABLE_CLOUD_SYNC") and not force:
        logger.info("Cloud sync disabled by env; skipping (force=%s)", force)
        return
    url = os.getenv(SHEETY_ENDPOINT_ENV)
    if not url:
        logger.info("SHEETY_ENDPOINT not configured; skipping cloud sync")
        return

    now = datetime.now(timezone.utc)
    if not force and not _should_sync(now):
        return

    try:
        response = requests.get(url, timeout=15)
        # Check for 402 Payment Required before raising
        if response.status_code == 402:
            logger.warning("Sheety API returned 402 Payment Required. Skipping sync for extended period. "
                         "Consider upgrading Sheety plan or using alternative sync method.")
            # Set a longer backoff (24 hours) for payment required errors
            _LAST_SYNC_FAIL_TS = now - timedelta(hours=23)  # Will allow retry after 24 hours
            return
        response.raise_for_status()
        data = response.json()
        if "sheet1" not in data:
            logger.warning("Unexpected payload from Sheety endpoint: missing 'sheet1'")
            return
        cloud_df = pd.DataFrame(data["sheet1"])
        if "id" in cloud_df.columns:
            cloud_df = cloud_df.sort_values("id")

        parser = TimeLogParser()
        parsed_rows: List[Dict[str, Any]] = []
        previous_end: Optional[datetime] = None

        for _, row in cloud_df.iterrows():
            col_a = str(row.get("colA", "") or "")
            col_b = str(row.get("colB", "") or str(row.get("rawTask", "")))
            client_now = row.get("loggedTime")

            parsed = parser.parse_row(col_a, col_b, client_now, previous_end)
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

        conn = get_db_connection()
        conn.execute("DROP TABLE IF EXISTS logs")
        conn.execute(
            """
            CREATE TABLE logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                start_date TEXT,
                start_time TEXT,
                end_date TEXT,
                end_time TEXT,
                task TEXT,
                duration INTEGER,
                tags TEXT,
                urg INTEGER,
                imp INTEGER
            )
            """
        )

        for p in final_rows:
            duration = int((p["end_dt"] - p["start_dt"]).total_seconds() / 60)
            if duration <= 0:
                continue

            conn.execute(
                """
                INSERT INTO logs (
                    start_date,
                    start_time,
                    end_date,
                    end_time,
                    task,
                    duration,
                    tags,
                    urg,
                    imp
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    p["start_dt"].strftime("%Y-%m-%d"),
                    p["start_dt"].strftime("%H:%M:%S"),
                    p["end_dt"].strftime("%Y-%m-%d"),
                    p["end_dt"].strftime("%H:%M:%S"),
                    p["task"],
                    duration,
                    p["tag"], 
                    1 if p["urg"] else 0,
                    1 if p["imp"] else 0,
                ),
            )

        conn.commit()
        conn.close()
        _LAST_SYNC_TS = now
        logger.info("Sync complete: strict end times enforced for %s rows", len(final_rows))
    except requests.RequestException as exc:
        _LAST_SYNC_FAIL_TS = now
        # Handle 402 Payment Required specially - don't spam logs
        if hasattr(exc, 'response') and exc.response is not None and exc.response.status_code == 402:
            # For 402 errors, extend the backoff period significantly
            # This prevents constant retry attempts when Sheety quota is exhausted
            logger.warning("Sheety API returned 402 Payment Required. Skipping sync for extended period. "
                         "Consider upgrading Sheety plan or using alternative sync method.")
            # Set a longer backoff (24 hours) for payment required errors
            _LAST_SYNC_FAIL_TS = now - timedelta(hours=23)  # Will allow retry after 24 hours
            return
        logger.error("Network error during cloud sync: %s", exc)
    except Exception as exc:
        _LAST_SYNC_FAIL_TS = now
        logger.exception("Unexpected sync error: %s", exc)


def fetch_local_data() -> pd.DataFrame:
    try:
        conn = get_db_connection()
        df = pd.read_sql_query(
            "SELECT * FROM logs ORDER BY start_date ASC, start_time ASC",
            conn,
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
    except Exception as exc:
        logger.exception("Fetch error: %s", exc)
        return pd.DataFrame()


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


def parse_period_param(raw_period: Optional[str]) -> str:
    allowed = {"day", "week", "month"}
    if raw_period in allowed:
        return raw_period
    return "day"


def parse_date_param(raw_date: Optional[str]) -> datetime.date:
    if not raw_date:
        return datetime.now().date()
    try:
        return datetime.strptime(raw_date, "%Y-%m-%d").date()
    except ValueError:
        logger.warning("Invalid date '%s', falling back to today", raw_date)
        return datetime.now().date()


def get_period_range(selected_date: datetime.date, period: str) -> Tuple[datetime.date, datetime.date]:
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


def require_api_auth(headers: Dict[str, str]) -> bool:
    expected = os.getenv(API_AUTH_TOKEN_ENV)
    if not expected:
        return True
    provided = headers.get("X-API-Token") or request.args.get("token")
    if not provided:
        provided = request.cookies.get("tt_token")
    if provided != expected:
        logger.warning("Unauthorized API access attempt")
        return False
    return True


@app.route("/")
def dashboard():
    sync_cloud_data()
    date_str = request.args.get("date")
    raw_period = request.args.get("period", "day")
    period = parse_period_param(raw_period)

    selected_date = parse_date_param(date_str)

    df = fetch_local_data()
    if df.empty:
        try:
            sync_cloud_data(force=True)
            df = fetch_local_data()
        except Exception as exc:
            logger.exception("Backfill sync failed: %s", exc)

    start_date, end_date = get_period_range(selected_date, period)

    period_df = (
        df[(df["date"] >= start_date) & (df["date"] <= end_date)]
        if not df.empty
        else pd.DataFrame()
    )
    matrix = get_matrix_stats(period_df)

    idx = (selected_date.weekday() + 1) % 7
    start_of_week = selected_date - timedelta(days=idx)
    week_days: List[Dict[str, Any]] = []
    for i in range(7):
        day = start_of_week + timedelta(days=i)
        day_data = df[df["date"] == day] if not df.empty else pd.DataFrame()
        day_hours = round(day_data["duration"].sum() / 60.0, 1) if not day_data.empty else 0.0
        week_days.append(
            {
                "date_obj": day,
                "day_name": day.strftime("%a")[0],
                "day_num": day.day,
                "full_str": day.strftime("%Y-%m-%d"),
                "hours": day_hours,
                "is_selected": day == selected_date,
            }
        )

    week_total = sum(day["hours"] for day in week_days)

    tag_labels: List[str] = []
    tag_data: List[float] = []
    if not period_df.empty:
        tag_df = period_df.copy()
        if "primary_tag" not in tag_df.columns:
            tag_df["primary_tag"] = tag_df["tag"].apply(primary_special_tag)
        tag_counts = (
            tag_df.groupby("primary_tag")["duration"]
            .sum()
            .sort_values(ascending=False)
        )
        tag_labels = list(tag_counts.index)
        tag_data = [round(float(x) / 60.0, 1) for x in tag_counts.values]

    if period == "day":
        period_label = selected_date.strftime("%B %Y")
    elif period == "week":
        period_label = (
            f"{start_of_week.strftime('%b %d')} - "
            f"{(start_of_week + timedelta(days=6)).strftime('%b %d, %Y')}"
        )
    else:
        period_label = selected_date.strftime("%B %Y")

    resp = make_response(render_template(
        "dashboard.html",
        matrix=matrix,
        tags={"labels": tag_labels, "data": tag_data},
        week_days=week_days,
        week_total=round(week_total, 1),
        current_month=period_label,
        selected_date=selected_date,
        period=period,
        start_date=start_date,
        end_date=end_date,
    ))
    expected = os.getenv(API_AUTH_TOKEN_ENV)
    if expected:
        try:
            resp.set_cookie("tt_token", expected, httponly=True, samesite="Lax")
        except Exception:
            pass
    return resp


@app.route("/api/tasks")
def get_tasks():
    if not require_api_auth(dict(request.headers)):
        return jsonify({"error": "Unauthorized"}), 401

    date_str = request.args.get("date")
    raw_period = request.args.get("period", "day")
    period = parse_period_param(raw_period)
    filter_type = request.args.get("filter", "all")
    tag_name = request.args.get("tag", "")

    selected_date = parse_date_param(date_str)

    df = fetch_local_data()
    if df.empty:
        try:
            sync_cloud_data(force=True)
            df = fetch_local_data()
        except Exception as exc:
            logger.exception("Backfill sync failed (tasks): %s", exc)

    if df.empty:
        return jsonify(
            {
                "tasks": [],
                "total_hours": 0.0,
                "total_minutes": 0,
                "period_minutes": 0,
                "period_matrix_minutes": {
                    "q1": 0,
                    "q2": 0,
                    "q3": 0,
                    "q4": 0,
                    "important": 0,
                    "not_important": 0,
                    "urgent": 0,
                    "not_urgent": 0,
                    "total": 0,
                },
                "count": 0,
            }
        )

    start_date, end_date = get_period_range(selected_date, period)

    period_df = (
        df[(df["date"] >= start_date) & (df["date"] <= end_date)]
        if not df.empty
        else pd.DataFrame()
    )

    if period_df.empty:
        filtered = period_df
    elif filter_type == "q1":
        filtered = period_df[(period_df["urgent"]) & (period_df["important"])]
    elif filter_type == "q2":
        filtered = period_df[(~period_df["urgent"]) & (period_df["important"])]
    elif filter_type == "q3":
        filtered = period_df[(period_df["urgent"]) & (~period_df["important"])]
    elif filter_type == "q4":
        filtered = period_df[(~period_df["urgent"]) & (~period_df["important"])]
    elif filter_type == "imp":
        filtered = period_df[period_df["important"]]
    elif filter_type == "urg":
        filtered = period_df[period_df["urgent"]]
    elif filter_type == "imp_and_urg":
        filtered = period_df[(period_df["urgent"]) & (period_df["important"])]
    elif filter_type == "imp_not_urg":
        filtered = period_df[(~period_df["urgent"]) & (period_df["important"])]
    elif filter_type == "urg_not_imp":
        filtered = period_df[(period_df["urgent"]) & (~period_df["important"])]
    elif filter_type == "not_imp":
        filtered = period_df[~period_df["important"]]
    elif filter_type == "important":
        filtered = period_df[period_df["important"]]
    elif filter_type == "not_important":
        filtered = period_df[~period_df["important"]]
    elif filter_type == "urgent":
        filtered = period_df[period_df["urgent"]]
    elif filter_type == "not_urgent":
        filtered = period_df[~period_df["urgent"]]
    elif filter_type == "tag" and tag_name:
        tag_key = primary_special_tag(tag_name)
        if "special_tags" in period_df.columns:
            filtered = period_df[period_df["special_tags"].apply(lambda tags: tag_key in (tags or []))]
        else:
            filtered = period_df[period_df["tag"].apply(lambda t: tag_key in filter_special_tags(t))]
    else:
        filtered = period_df

    tasks: List[Dict[str, Any]] = []
    if not filtered.empty:
        for _, row in filtered.iterrows():
            tasks.append(
                {
                    "task": row["task"],
                    "start_time": row["start_time"],
                    "end_time": row["end_time"],
                    "date": str(row["date"]),
                    "duration": float(round(row["duration"] / 60.0, 2)),
                    "tag": row["tag"],
                    "urgent": bool(row["urgent"]),
                    "important": bool(row["important"]),
                }
            )

    if not filtered.empty:
        total_hours = float(round(filtered["duration"].sum() / 60.0, 2))
        total_minutes = int(filtered["duration"].sum())
    else:
        total_hours = 0.0
        total_minutes = 0
    period_minutes = int(period_df["duration"].sum()) if not period_df.empty else 0

    if not period_df.empty:
        q2_min = int(period_df[(~period_df["urgent"]) & (period_df["important"])]["duration"].sum())
        q1_min = int(period_df[(period_df["urgent"]) & (period_df["important"])]["duration"].sum())
        q3_min = int(period_df[(period_df["urgent"]) & (~period_df["important"])]["duration"].sum())
        q4_min = int(period_df[(~period_df["urgent"]) & (~period_df["important"])]["duration"].sum())
        imp_min = int(period_df[period_df["important"]]["duration"].sum())
        not_imp_min = int(period_df[~period_df["important"]]["duration"].sum())
        urg_min = int(period_df[period_df["urgent"]]["duration"].sum())
        not_urg_min = int(period_df[~period_df["urgent"]]["duration"].sum())
        period_matrix_minutes = {
            "q1": q1_min,
            "q2": q2_min,
            "q3": q3_min,
            "q4": q4_min,
            "important": imp_min,
            "not_important": not_imp_min,
            "urgent": urg_min,
            "not_urgent": not_urg_min,
            "total": int(period_df["duration"].sum()),
        }
    else:
        period_matrix_minutes = {
            "q1": 0,
            "q2": 0,
            "q3": 0,
            "q4": 0,
            "important": 0,
            "not_important": 0,
            "urgent": 0,
            "not_urgent": 0,
            "total": 0,
        }

    return jsonify(
        {
            "tasks": tasks,
            "total_hours": total_hours,
            "total_minutes": total_minutes,
            "period_minutes": period_minutes,
            "period_matrix_minutes": period_matrix_minutes,
            "count": len(tasks),
        }
    )


@app.route("/api/tags")
def get_tags():
    if not require_api_auth(dict(request.headers)):
        return jsonify({"error": "Unauthorized"}), 401

    date_str = request.args.get("date")
    raw_period = request.args.get("period", "day")
    period = parse_period_param(raw_period)

    selected_date = parse_date_param(date_str)

    df = fetch_local_data()
    if df.empty:
        try:
            sync_cloud_data(force=True)
            df = fetch_local_data()
        except Exception as exc:
            logger.exception("Backfill sync failed (tags): %s", exc)

    start_date, end_date = get_period_range(selected_date, period)

    period_df = (
        df[(df["date"] >= start_date) & (df["date"] <= end_date)]
        if not df.empty
        else pd.DataFrame()
    )

    if period_df.empty:
        return jsonify({"tags": []})

    tag_df = period_df.copy()
    if "primary_tag" not in tag_df.columns:
        tag_df["primary_tag"] = tag_df["tag"].apply(primary_special_tag)

    tag_stats: List[Dict[str, Any]] = []
    for tag, group in tag_df.groupby("primary_tag"):
        if not tag:
            continue
        total_minutes = int(group["duration"].sum())
        tag_stats.append(
            {
                "name": tag,
                "hours": round(total_minutes / 60.0, 2),
                "minutes": total_minutes,
                "count": int(len(group)),
            }
        )

    tag_stats.sort(key=lambda x: x["hours"], reverse=True)
    return jsonify({"tags": tag_stats})


@app.route('/download-db')
def download_db():
    """
    Downloads the live production database file.
    """
    try:
        # Ensures the data is fresh before downloading
        sync_cloud_data()
        return send_file(DB_NAME, as_attachment=True)
    except Exception as e:
        return f"Error downloading DB: {e}"

@app.route("/sync-status")
def sync_status():
    try:
        status = {
            "sheety_configured": bool(os.getenv(SHEETY_ENDPOINT_ENV)),
            "disable_cloud_sync": bool(os.getenv("DISABLE_CLOUD_SYNC")),
            "last_sync": _LAST_SYNC_TS.isoformat() if _LAST_SYNC_TS else None,
            "last_sync_fail": _LAST_SYNC_FAIL_TS.isoformat() if _LAST_SYNC_FAIL_TS else None,
            "sync_interval_seconds": SYNC_INTERVAL_SECONDS,
            "fail_cooldown_seconds": SYNC_FAIL_COOLDOWN_SECONDS,
            "db_exists": os.path.exists(DB_NAME),
        }
        return jsonify(status)
    except Exception as exc:
        logger.exception("Sync status error: %s", exc)
        return jsonify({"error": str(exc)}), 500

@app.route("/sync-now")
def sync_now():
    if not require_api_auth(dict(request.headers)):
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
    try:
        sync_cloud_data(force=True)
        return jsonify({"status": "success", "message": "Synced latest data from cloud"})
    except Exception as exc:
        logger.exception("Sync now error: %s", exc)
        return jsonify({"status": "error", "message": str(exc)}), 500


@app.route("/hard-reset")
def hard_reset():
    if not require_api_auth(dict(request.headers)):
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
    try:
        sync_cloud_data(force=True)
        return jsonify(
            {
                "status": "success",
                "message": "Database has been completely wiped and rebuilt from Google Sheets.",
            }
        )
    except Exception as exc:
        logger.exception("Hard reset error: %s", exc)
        return jsonify({"status": "error", "message": str(exc)}), 500


@app.route("/api/import-csv", methods=["POST"])
def import_csv():
    """Import data from CSV format (Google Sheets export)"""
    if not require_api_auth(dict(request.headers)):
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
    
    try:
        data = request.get_json()
        if not data or "csv_data" not in data:
            return jsonify({"status": "error", "message": "Missing 'csv_data' field"}), 400
        
        csv_content = data["csv_data"]
        # Parse CSV
        df = pd.read_csv(StringIO(csv_content))
        
        # Handle different column name formats
        # Google Sheets export might have: "Logged Time", "Raw Start", "Raw Task"
        # Or: "colA", "colB", "loggedTime"
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
            # Try to find any column that looks like task data
            for col in df.columns:
                if col.lower() not in ["logged time", "raw start"]:
                    col_b = col
                    break
        
        if col_b is None:
            return jsonify({"status": "error", "message": "Could not identify task column in CSV"}), 400
        
        # Process rows
        parser = TimeLogParser()
        parsed_rows: List[Dict[str, Any]] = []
        previous_end: Optional[datetime] = None
        
        for _, row in df.iterrows():
            col_a_val = str(row.get(col_a, "") or "") if col_a else ""
            col_b_val = str(row.get(col_b, "") or "")
            client_now = str(row.get(logged_time_col, "")) if logged_time_col else None
            
            if not col_b_val or col_b_val.strip() == "":
                continue
            
            try:
                parsed = parser.parse_row(col_a_val, col_b_val, client_now, previous_end)
                parsed_rows.append(parsed)
                previous_end = parsed["end_dt"]
            except Exception as e:
                logger.warning("Failed to parse row: %s - %s", row, e)
                continue
        
        # Trim overlaps
        for i in range(1, len(parsed_rows)):
            current = parsed_rows[i]
            prev = parsed_rows[i - 1]
            if current["start_dt"] < prev["end_dt"]:
                parsed_rows[i - 1]["end_dt"] = current["start_dt"]
        
        # Split midnight crossovers
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
        
        # Save to database
        conn = get_db_connection()
        conn.execute("DROP TABLE IF EXISTS logs")
        conn.execute(
            """
            CREATE TABLE logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                start_date TEXT,
                start_time TEXT,
                end_date TEXT,
                end_time TEXT,
                task TEXT,
                duration INTEGER,
                tags TEXT,
                urg INTEGER,
                imp INTEGER
            )
            """
        )
        
        inserted_count = 0
        for p in final_rows:
            duration = int((p["end_dt"] - p["start_dt"]).total_seconds() / 60)
            if duration <= 0:
                continue
            
            conn.execute(
                """
                INSERT INTO logs (
                    start_date, start_time, end_date, end_time,
                    task, duration, tags, urg, imp
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    p["start_dt"].strftime("%Y-%m-%d"),
                    p["start_dt"].strftime("%H:%M:%S"),
                    p["end_dt"].strftime("%Y-%m-%d"),
                    p["end_dt"].strftime("%H:%M:%S"),
                    p["task"],
                    duration,
                    p["tag"],
                    1 if p["urg"] else 0,
                    1 if p["imp"] else 0,
                ),
            )
            inserted_count += 1
        
        conn.commit()
        conn.close()
        
        logger.info("CSV import complete: %s rows imported", inserted_count)
        return jsonify({
            "status": "success",
            "message": f"Successfully imported {inserted_count} tasks from CSV",
            "count": inserted_count
        })
        
    except Exception as exc:
        logger.exception("CSV import error: %s", exc)
        return jsonify({"status": "error", "message": str(exc)}), 500


if __name__ == '__main__':
    init_db()
    # host='0.0.0.0' allows access from other devices on the network
    app.run(debug=True, host='0.0.0.0', port=5001)
