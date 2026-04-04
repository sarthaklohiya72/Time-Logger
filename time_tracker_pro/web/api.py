from __future__ import annotations

import csv
import logging
import os
import re
from datetime import datetime, timedelta
from urllib.parse import urlparse
from io import BytesIO, StringIO
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
from flask import Blueprint, current_app, g, jsonify, request, send_file

from ..core.constants import GRAPH_TAG_MAP
from ..core.dates import get_period_range, parse_date_param, parse_period_param
from ..core.tags import filter_special_tags, primary_special_tag
from ..db import get_db_connection
from ..repositories.logs import fetch_local_data
from ..repositories.users import get_user_count, get_user_by_id
from ..core.rows import row_value
from ..services.import_csv import import_csv_content
from ..services.sync import sync_cloud_data, sync_status_payload
from .decorators import api_or_login_required, resolve_request_user_id, token_is_valid
from .utils import get_current_user_id


bp = Blueprint("api", __name__)

logger = logging.getLogger(__name__)

SearchExpr = Tuple[str, Any]
SEARCH_TOKEN_RE = re.compile(r'"[^"]*"|\'[^\']*\'|\(|\)|,|\bAND\b|\bOR\b|\bNOT\b|[^\s()]+', re.IGNORECASE)


def parse_graph_search(raw: str) -> Optional[SearchExpr]:
    cleaned = re.sub(r"\s+", " ", (raw or "").strip())
    if not cleaned:
        return None
    tokens: List[Tuple[str, Optional[str]]] = []
    for match in SEARCH_TOKEN_RE.findall(cleaned):
        token = match.strip()
        if not token:
            continue
        if token == ",":
            tokens.append(("OR", None))
            continue
        if token in {"(", ")"}:
            tokens.append((token, None))
            continue
        upper = token.upper()
        if upper in {"AND", "OR", "NOT"}:
            tokens.append((upper, None))
            continue
        if token[0] in {'"', "'"} and token[-1] == token[0]:
            token = token[1:-1]
        term = token.strip().lower()
        if term:
            tokens.append(("TERM", term))
    if not tokens:
        return None

    index = 0

    def peek() -> Optional[Tuple[str, Optional[str]]]:
        return tokens[index] if index < len(tokens) else None

    def consume() -> Optional[Tuple[str, Optional[str]]]:
        nonlocal index
        token = peek()
        if token is not None:
            index += 1
        return token

    def parse_primary() -> Optional[SearchExpr]:
        token = peek()
        if not token:
            return None
        if token[0] == "TERM":
            consume()
            return ("term", token[1] or "")
        if token[0] == "(":
            consume()
            expr = parse_or()
            if peek() and peek()[0] == ")":
                consume()
            return expr
        return None

    def parse_not() -> Optional[SearchExpr]:
        token = peek()
        if token and token[0] == "NOT":
            consume()
            expr = parse_not()
            return ("not", expr) if expr else None
        return parse_primary()

    def parse_and() -> Optional[SearchExpr]:
        left = parse_not()
        if not left:
            return None
        while True:
            token = peek()
            if not token:
                break
            if token[0] == "AND":
                consume()
                right = parse_not()
                if not right:
                    break
                left = ("and", left, right)
                continue
            if token[0] in {"TERM", "(", "NOT"}:
                right = parse_not()
                if not right:
                    break
                left = ("and", left, right)
                continue
            break
        return left

    def parse_or() -> Optional[SearchExpr]:
        left = parse_and()
        if not left:
            return None
        while True:
            token = peek()
            if token and token[0] == "OR":
                consume()
                right = parse_and()
                if not right:
                    break
                left = ("or", left, right)
                continue
            break
        return left

    return parse_or()


def graph_term_matches(row: pd.Series, term: str) -> bool:
    clean = str(term or "").strip().lower()
    if not clean:
        return False
    if clean == "important":
        return bool(row.get("important"))
    if clean == "urgent":
        return bool(row.get("urgent"))

    def normalize_text(value: Any) -> str:
        return re.sub(r"\s+", " ", str(value or "").strip().lower())

    def split_terms(value: Any) -> List[str]:
        clean_value = normalize_text(value)
        if not clean_value:
            return []
        return [part for part in re.split(r"[^a-z0-9]+", clean_value) if part]

    def split_tags(value: Any) -> List[str]:
        clean_value = normalize_text(value)
        if not clean_value:
            return []
        return [part.strip() for part in clean_value.split(",") if part.strip()]

    tags = set()
    for value in [row.get("tag"), row.get("raw_tag"), row.get("primary_tag")]:
        tags.update(split_tags(value))
    special_tags = row.get("special_tags")
    if isinstance(special_tags, list):
        tags.update([normalize_text(tag) for tag in special_tags if tag])

    if clean in tags:
        return True

    task_value = normalize_text(row.get("task"))
    date_value = normalize_text(row.get("date"))
    if clean == task_value or clean == date_value:
        return True

    tokens = set(split_terms(task_value))
    tokens.update(split_terms(date_value))
    for tag in tags:
        tokens.update(split_terms(tag))
    return clean in tokens


def graph_row_matches(row: pd.Series, expr: Optional[SearchExpr]) -> bool:
    if not expr:
        return True
    op = expr[0]
    if op == "term":
        return graph_term_matches(row, expr[1])
    if op == "not":
        return not graph_row_matches(row, expr[1]) if expr[1] else True
    if op == "and":
        return graph_row_matches(row, expr[1]) and graph_row_matches(row, expr[2])
    if op == "or":
        return graph_row_matches(row, expr[1]) or graph_row_matches(row, expr[2])
    return True


@bp.route("/api/graph-data", endpoint="graph_data")
@api_or_login_required
def graph_data():
    db_name = current_app.config["DB_NAME"]
    user_id = int(getattr(g, "user_id", 0) or 0)

    metric = (request.args.get("metric") or "total").strip().lower()
    search = (request.args.get("search") or "").strip()
    include_tasks = (request.args.get("include_tasks") or "").strip().lower() in {"1", "true", "yes"}
    raw_exclude = (request.args.get("exclude") or "").strip()
    raw_days = request.args.get("days") or "30"
    try:
        days = max(1, min(int(raw_days), 365))
    except Exception:
        days = 30

    end_date = parse_date_param(request.args.get("end"))
    start_date = end_date - timedelta(days=days - 1)

    df = fetch_local_data(db_name, user_id)
    if df.empty:
        labels = [(start_date + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)]
        return jsonify({"labels": labels, "values": [0 for _ in labels], "total_hours": 0, "avg_hours": 0, "max_hours": 0})

    if "primary_tag" not in df.columns:
        df["primary_tag"] = df["tag"].apply(primary_special_tag)

    min_complete_minutes = 10 * 60
    latest_total = df.loc[df["date"] == end_date, "duration"].sum()
    if latest_total < min_complete_minutes:
        end_date = end_date - timedelta(days=1)
        start_date = end_date - timedelta(days=days - 1)

    df = df[(df["date"] >= start_date) & (df["date"] <= end_date)]
    if df.empty:
        labels = [(start_date + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)]
        return jsonify({"labels": labels, "values": [0 for _ in labels], "total_hours": 0, "avg_hours": 0, "max_hours": 0})

    if metric in GRAPH_TAG_MAP:
        df = df[df["primary_tag"] == GRAPH_TAG_MAP[metric]]
    elif metric == "important":
        df = df[df["important"]]
    elif metric == "urgent":
        df = df[df["urgent"]]
    elif metric == "q1":
        df = df[(df["important"]) & (df["urgent"])]
    elif metric == "q2":
        df = df[(df["important"]) & (~df["urgent"])]
    elif metric == "q3":
        df = df[(df["urgent"]) & (~df["important"])]
    elif metric == "q4":
        df = df[(~df["urgent"]) & (~df["important"])]

    if search:
        search_expr = parse_graph_search(search)
        if search_expr:
            df = df[df.apply(lambda row: graph_row_matches(row, search_expr), axis=1)]

    if raw_exclude:
        try:
            excluded_ids = {
                int(item)
                for item in re.split(r"[\s,]+", raw_exclude)
                if item and str(item).strip().isdigit()
            }
        except Exception:
            excluded_ids = set()
        if excluded_ids:
            df = df[~df["id"].isin(excluded_ids)]

    daily = df.groupby("date")["duration"].sum() if not df.empty else {}
    labels: List[str] = []
    values: List[float] = []
    for i in range(days):
        day = start_date + timedelta(days=i)
        labels.append(day.strftime("%Y-%m-%d"))
        minutes = float(daily.get(day, 0) if hasattr(daily, "get") else 0)
        values.append(round(minutes / 60.0, 2))

    total_hours = round(sum(values), 2)
    first_logged_index = None
    for idx, value in enumerate(values):
        if value > 0:
            first_logged_index = idx
            break
    if first_logged_index is None:
        avg_hours = 0
    else:
        active_days = max(1, days - first_logged_index)
        avg_hours = round(total_hours / active_days, 2)
    max_hours = round(max(values) if values else 0, 2)

    tasks: List[Dict[str, Any]] = []
    if include_tasks and not df.empty:
        for _, row in df.iterrows():
            minutes = int(row.get("duration") or 0)
            tasks.append(
                {
                    "id": int(row.get("id") or 0),
                    "sheety_id": (int(row.get("sheety_id")) if row.get("sheety_id") is not None else None),
                    "task": row.get("task") or "",
                    "date": str(row.get("date") or ""),
                    "start_time": row.get("start_time") or "",
                    "end_time": row.get("end_time") or "",
                    "duration_minutes": minutes,
                    "duration_hours": round(minutes / 60.0, 2),
                    "tag": row.get("tag") or "",
                    "raw_tag": row.get("raw_tag") or "",
                    "important": bool(row.get("important")),
                    "urgent": bool(row.get("urgent")),
                }
            )

    return jsonify(
        {
            "labels": labels,
            "values": values,
            "total_hours": total_hours,
            "avg_hours": avg_hours,
            "max_hours": max_hours,
            "tasks": tasks,
        }
    )


@bp.route("/api/tasks/<int:task_id>", methods=["PUT"], endpoint="update_task")
@api_or_login_required
def update_task(task_id: int):
    from ..services.sheety_failover import SheetyFailoverService

    db_name = current_app.config["DB_NAME"]
    user_id = int(getattr(g, "user_id", 0) or 0)

    data = request.get_json(silent=True) or {}

    conn = get_db_connection(db_name)
    row = conn.execute(
        "SELECT * FROM logs WHERE id = ? AND user_id = ?",
        (int(task_id), int(user_id)),
    ).fetchone()
    if not row:
        row = conn.execute(
            "SELECT * FROM logs WHERE sheety_id = ? AND user_id = ? ORDER BY id DESC LIMIT 1",
            (int(task_id), int(user_id)),
        ).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Task not found"}), 404

    row_id = int(row["id"])

    sheety_id = row["sheety_id"]
    if sheety_id is None:
        conn.close()
        return jsonify({"error": "Task is not linked to a Sheety row"}), 400

    task_name = (data.get("task") or row["task"] or "").strip()
    if not task_name:
        conn.close()
        return jsonify({"error": "Task name is required"}), 400

    duration_minutes = None
    if data.get("duration_minutes") is not None:
        duration_minutes = int(float(data.get("duration_minutes") or 0))
    elif data.get("duration") is not None:
        duration_minutes = int(round(float(data.get("duration") or 0) * 60))
    else:
        duration_minutes = int(row["duration"] or 0)

    if duration_minutes <= 0:
        conn.close()
        return jsonify({"error": "Duration must be greater than 0"}), 400

    tag_raw = (data.get("tag") or row["tags"] or "").strip()
    urgent = bool(data.get("urgent") if "urgent" in data else row["urg"])
    important = bool(data.get("important") if "important" in data else row["imp"])

    start_date = row["start_date"]
    start_time = row["start_time"]

    def parse_datetime(date_value: str, time_value: str) -> Optional[datetime]:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                return datetime.strptime(f"{date_value} {time_value}", fmt)
            except ValueError:
                continue
        return None

    start_dt = parse_datetime(start_date, start_time)
    if start_dt is None:
        conn.close()
        return jsonify({"error": "Invalid start time format"}), 400

    end_dt = start_dt + timedelta(minutes=duration_minutes)
    if end_dt <= start_dt:
        conn.close()
        return jsonify({"error": "Duration must be greater than 0"}), 400
    end_date = end_dt.strftime("%Y-%m-%d")
    end_time = end_dt.strftime("%H:%M:%S")

    def format_time(dt: datetime) -> str:
        return dt.strftime("%I:%M%p").lstrip("0").lower()

    def format_date(dt: datetime) -> str:
        return dt.strftime("%d/%m/%Y")

    def build_log_payload(
        task_label: str,
        tag_value_raw: str,
        is_urgent: bool,
        is_important: bool,
        start_value: datetime,
        end_value: datetime,
    ) -> Tuple[Dict[str, str], str]:
        tags_list = filter_special_tags(tag_value_raw)
        meta_tokens: List[str] = []
        if tags_list:
            meta_tokens.extend([tag.lower() for tag in tags_list])
        if is_urgent:
            meta_tokens.append("urgent")
        if is_important:
            meta_tokens.append("important")

        log_entry = f"{format_date(start_value)} {format_time(start_value)} {format_time(end_value)} {task_label}".strip()
        if meta_tokens:
            log_entry = f"{log_entry}. {' '.join(meta_tokens)}"

        logged_time = end_value.isoformat()
        tag_value = ", ".join(tags_list) if tags_list else "Waste"
        return {"logEntry": log_entry, "loggedTime": logged_time}, tag_value

    def extract_sheety_id(response_data: Any, sheet_key: str) -> Optional[int]:
        if not isinstance(response_data, dict):
            return None
        candidate = response_data.get(sheet_key)
        if isinstance(candidate, dict) and candidate.get("id") is not None:
            try:
                return int(candidate.get("id"))
            except (TypeError, ValueError):
                return None
        if response_data.get("id") is not None:
            try:
                return int(response_data.get("id"))
            except (TypeError, ValueError):
                return None
        for value in response_data.values():
            if isinstance(value, dict) and value.get("id") is not None:
                try:
                    return int(value.get("id"))
                except (TypeError, ValueError):
                    return None
        return None

    log_payload, tag_value = build_log_payload(task_name, tag_raw, urgent, important, start_dt, end_dt)

    overlap_updates: List[Dict[str, Any]] = []
    overlap_deletes: List[Dict[str, int]] = []
    overlap_inserts: List[Dict[str, Any]] = []

    other_rows = conn.execute(
        "SELECT * FROM logs WHERE user_id = ? AND id != ? ORDER BY start_date ASC, start_time ASC",
        (int(user_id), row_id),
    ).fetchall()

    def record_overlap_update(target_row, new_start: datetime, new_end: datetime) -> None:
        duration = int((new_end - new_start).total_seconds() / 60)
        if duration <= 0:
            overlap_deletes.append(
                {"id": int(target_row["id"]), "sheety_id": int(target_row["sheety_id"])}
            )
            return
        overlap_updates.append(
            {
                "id": int(target_row["id"]),
                "sheety_id": int(target_row["sheety_id"]),
                "start_dt": new_start,
                "end_dt": new_end,
                "duration": duration,
                "task": target_row["task"],
                "tag_raw": target_row["tags"] or "",
                "urgent": bool(target_row["urg"]),
                "important": bool(target_row["imp"]),
            }
        )

    def record_overlap_insert(target_row, new_start: datetime, new_end: datetime) -> None:
        duration = int((new_end - new_start).total_seconds() / 60)
        if duration <= 0:
            return
        overlap_inserts.append(
            {
                "start_dt": new_start,
                "end_dt": new_end,
                "duration": duration,
                "task": target_row["task"],
                "tag_raw": target_row["tags"] or "",
                "urgent": bool(target_row["urg"]),
                "important": bool(target_row["imp"]),
            }
        )

    for other in other_rows:
        other_sheety_id = other["sheety_id"]
        if other_sheety_id is None:
            conn.close()
            return jsonify({"error": "Overlapping tasks must be linked to Sheety before editing."}), 400
        other_start = parse_datetime(other["start_date"], other["start_time"])
        other_end = parse_datetime(other["end_date"], other["end_time"])
        if other_start is None or other_end is None:
            conn.close()
            return jsonify({"error": "Invalid time format on overlapping task."}), 400
        if other_end <= start_dt or other_start >= end_dt:
            continue
        before = other_start < start_dt
        after = other_end > end_dt
        if before and after:
            record_overlap_update(other, other_start, start_dt)
            record_overlap_insert(other, end_dt, other_end)
        elif before and not after:
            record_overlap_update(other, other_start, start_dt)
        elif after and not before:
            record_overlap_update(other, end_dt, other_end)
        else:
            overlap_deletes.append({"id": int(other["id"]), "sheety_id": int(other_sheety_id)})

    from ..repositories.sheety_accounts import get_active_api_account

    sheet_name = "sheet1"
    active_account = get_active_api_account(db_name, user_id)
    if active_account:
        base_url = row_value(active_account, "api_base_url") or ""
        parsed = urlparse(base_url)
        path = parsed.path.rstrip("/") if parsed.path else ""
        if path:
            sheet_name = path.split("/")[-1] or sheet_name

    payload = {sheet_name: log_payload}

    service = SheetyFailoverService(db_name, user_id)
    success, _, error = service.make_request("PUT", str(sheety_id), payload)
    if not success:
        conn.close()
        return jsonify({"error": error or "Failed to update task in Sheety"}), 502

    for update in overlap_updates:
        update_payload, _ = build_log_payload(
            update["task"],
            update["tag_raw"],
            update["urgent"],
            update["important"],
            update["start_dt"],
            update["end_dt"],
        )
        success, _, error = service.make_request("PUT", str(update["sheety_id"]), {sheet_name: update_payload})
        if not success:
            conn.close()
            return jsonify({"error": error or "Failed to update overlapping task in Sheety"}), 502

    for deleted in overlap_deletes:
        success, _, error = service.make_request("DELETE", str(deleted["sheety_id"]), None)
        if not success:
            conn.close()
            return jsonify({"error": error or "Failed to delete overlapping task in Sheety"}), 502

    for insert in overlap_inserts:
        insert_payload, insert_tag_value = build_log_payload(
            insert["task"],
            insert["tag_raw"],
            insert["urgent"],
            insert["important"],
            insert["start_dt"],
            insert["end_dt"],
        )
        success, data, error = service.make_request("POST", "", {sheet_name: insert_payload})
        if not success:
            conn.close()
            return jsonify({"error": error or "Failed to create split task in Sheety"}), 502
        new_sheety_id = extract_sheety_id(data, sheet_name)
        if new_sheety_id is None:
            conn.close()
            return jsonify({"error": "Sheety did not return a new row id."}), 502
        insert["sheety_id"] = int(new_sheety_id)
        insert["tag_value"] = insert_tag_value

    try:
        conn.execute(
            """
            UPDATE logs
            SET task = ?, duration = ?, tags = ?, urg = ?, imp = ?, end_date = ?, end_time = ?
            WHERE id = ? AND user_id = ?
            """,
            (
                task_name,
                duration_minutes,
                tag_value,
                1 if urgent else 0,
                1 if important else 0,
                end_date,
                end_time,
                row_id,
                int(user_id),
            ),
        )

        for update in overlap_updates:
            conn.execute(
                """
                UPDATE logs
                SET start_date = ?, start_time = ?, end_date = ?, end_time = ?, duration = ?
                WHERE id = ? AND user_id = ?
                """,
                (
                    update["start_dt"].strftime("%Y-%m-%d"),
                    update["start_dt"].strftime("%H:%M:%S"),
                    update["end_dt"].strftime("%Y-%m-%d"),
                    update["end_dt"].strftime("%H:%M:%S"),
                    update["duration"],
                    update["id"],
                    int(user_id),
                ),
            )

        for deleted in overlap_deletes:
            conn.execute(
                "DELETE FROM logs WHERE sheety_id = ? AND user_id = ?",
                (deleted["sheety_id"], int(user_id)),
            )

        for insert in overlap_inserts:
            conn.execute(
                """
                INSERT INTO logs (
                    sheety_id, start_date, start_time, end_date, end_time,
                    task, duration, tags, urg, imp, user_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    insert["sheety_id"],
                    insert["start_dt"].strftime("%Y-%m-%d"),
                    insert["start_dt"].strftime("%H:%M:%S"),
                    insert["end_dt"].strftime("%Y-%m-%d"),
                    insert["end_dt"].strftime("%H:%M:%S"),
                    insert["task"],
                    insert["duration"],
                    insert["tag_value"],
                    1 if insert["urgent"] else 0,
                    1 if insert["important"] else 0,
                    int(user_id),
                ),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        raise
    conn.close()

    response_payload = {
        "success": True,
        "task": {
            "id": row_id,
            "sheety_id": int(sheety_id),
            "task": task_name,
            "start_time": start_dt.strftime("%I:%M %p"),
            "end_time": end_dt.strftime("%I:%M %p"),
            "date": start_dt.strftime("%Y-%m-%d"),
            "duration": round(duration_minutes / 60.0, 2),
            "tag": tag_value,
            "urgent": urgent,
            "important": important,
        },
    }
    failover = service.get_failover_notification()
    if failover:
        response_payload["failover"] = failover
    return jsonify(response_payload)


@bp.route("/api/tasks", methods=["POST"], endpoint="create_task")
@api_or_login_required
def create_task():
    from ..services.sheety_failover import SheetyFailoverService
    from ..repositories.sheety_accounts import get_active_api_account

    db_name = current_app.config["DB_NAME"]
    user_id = int(getattr(g, "user_id", 0) or 0)

    data = request.get_json(silent=True) or {}

    task_name = (data.get("task") or "").strip()
    if not task_name:
        return jsonify({"error": "Task name is required"}), 400

    date_value = (data.get("date") or "").strip()
    start_time_value = (data.get("start_time") or data.get("start") or "").strip()
    if not date_value:
        return jsonify({"error": "Date is required"}), 400
    if not start_time_value:
        return jsonify({"error": "Start time is required"}), 400

    duration_minutes = None
    if data.get("duration_minutes") is not None:
        duration_minutes = int(float(data.get("duration_minutes") or 0))
    elif data.get("duration") is not None:
        duration_minutes = int(round(float(data.get("duration") or 0) * 60))

    if duration_minutes is None or duration_minutes <= 0:
        return jsonify({"error": "Duration must be greater than 0"}), 400

    tag_raw = (data.get("tag") or "").strip()
    urgent = bool(data.get("urgent"))
    important = bool(data.get("important"))

    def parse_datetime(date_str: str, time_str: str) -> Optional[datetime]:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                return datetime.strptime(f"{date_str} {time_str}", fmt)
            except ValueError:
                continue
        return None

    start_dt = parse_datetime(date_value, start_time_value)
    if start_dt is None:
        return jsonify({"error": "Invalid start time format"}), 400

    end_dt = start_dt + timedelta(minutes=duration_minutes)
    if end_dt <= start_dt:
        return jsonify({"error": "Duration must be greater than 0"}), 400

    conn = get_db_connection(db_name)
    overlap_rows = conn.execute(
        "SELECT id, sheety_id, task, start_date, start_time, end_date, end_time FROM logs WHERE user_id = ?",
        (int(user_id),),
    ).fetchall()
    overlaps: List[Dict[str, Any]] = []
    for row in overlap_rows:
        other_start = parse_datetime(row["start_date"], row["start_time"])
        other_end = parse_datetime(row["end_date"], row["end_time"])
        if other_start is None or other_end is None:
            continue
        if other_end <= start_dt or other_start >= end_dt:
            continue
        overlaps.append(
            {
                "id": int(row["id"]),
                "sheety_id": (int(row["sheety_id"]) if row["sheety_id"] is not None else None),
                "task": row["task"],
                "start_time": other_start.strftime("%I:%M %p"),
                "end_time": other_end.strftime("%I:%M %p"),
                "date": other_start.strftime("%Y-%m-%d"),
            }
        )
    if overlaps:
        conn.close()
        return (
            jsonify(
                {
                    "error": "Task overlaps existing logs.",
                    "overlaps": overlaps,
                }
            ),
            409,
        )

    def format_time(dt: datetime) -> str:
        return dt.strftime("%I:%M%p").lstrip("0").lower()

    def format_date(dt: datetime) -> str:
        return dt.strftime("%d/%m/%Y")

    def build_log_payload(
        task_label: str,
        tag_value_raw: str,
        is_urgent: bool,
        is_important: bool,
        start_value: datetime,
        end_value: datetime,
    ) -> Tuple[Dict[str, str], str]:
        tags_list = filter_special_tags(tag_value_raw)
        meta_tokens: List[str] = []
        if tags_list:
            meta_tokens.extend([tag.lower() for tag in tags_list])
        if is_urgent:
            meta_tokens.append("urgent")
        if is_important:
            meta_tokens.append("important")

        log_entry = f"{format_date(start_value)} {format_time(start_value)} {format_time(end_value)} {task_label}".strip()
        if meta_tokens:
            log_entry = f"{log_entry}. {' '.join(meta_tokens)}"

        logged_time = end_value.isoformat()
        tag_value = ", ".join(tags_list) if tags_list else "Waste"
        return {"logEntry": log_entry, "loggedTime": logged_time}, tag_value

    def extract_sheety_id(response_data: Any, sheet_key: str) -> Optional[int]:
        if not isinstance(response_data, dict):
            return None
        candidate = response_data.get(sheet_key)
        if isinstance(candidate, dict) and candidate.get("id") is not None:
            try:
                return int(candidate.get("id"))
            except (TypeError, ValueError):
                return None
        if response_data.get("id") is not None:
            try:
                return int(response_data.get("id"))
            except (TypeError, ValueError):
                return None
        for value in response_data.values():
            if isinstance(value, dict) and value.get("id") is not None:
                try:
                    return int(value.get("id"))
                except (TypeError, ValueError):
                    return None
        return None

    log_payload, tag_value = build_log_payload(task_name, tag_raw, urgent, important, start_dt, end_dt)

    sheet_name = "sheet1"
    active_account = get_active_api_account(db_name, user_id)
    if active_account:
        base_url = row_value(active_account, "api_base_url") or ""
        parsed = urlparse(base_url)
        path = parsed.path.rstrip("/") if parsed.path else ""
        if path:
            sheet_name = path.split("/")[-1] or sheet_name

    service = SheetyFailoverService(db_name, user_id)
    success, data, error = service.make_request("POST", "", {sheet_name: log_payload})
    if not success:
        conn.close()
        return jsonify({"error": error or "Failed to create task in Sheety"}), 502

    sheety_id = extract_sheety_id(data, sheet_name)
    if sheety_id is None:
        return jsonify({"error": "Sheety did not return a new row id."}), 502

    try:
        cursor = conn.execute(
            """
            INSERT INTO logs (
                sheety_id, start_date, start_time, end_date, end_time,
                task, duration, tags, urg, imp, user_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(sheety_id),
                start_dt.strftime("%Y-%m-%d"),
                start_dt.strftime("%H:%M:%S"),
                end_dt.strftime("%Y-%m-%d"),
                end_dt.strftime("%H:%M:%S"),
                task_name,
                duration_minutes,
                tag_value,
                1 if urgent else 0,
                1 if important else 0,
                int(user_id),
            ),
        )
        row_id = int(cursor.lastrowid)
        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        raise
    conn.close()

    response_payload = {
        "success": True,
        "task": {
            "id": row_id,
            "sheety_id": int(sheety_id),
            "task": task_name,
            "start_time": start_dt.strftime("%I:%M %p"),
            "end_time": end_dt.strftime("%I:%M %p"),
            "date": start_dt.strftime("%Y-%m-%d"),
            "duration": round(duration_minutes / 60.0, 2),
            "tag": tag_value,
            "urgent": urgent,
            "important": important,
        },
    }
    failover = service.get_failover_notification()
    if failover:
        response_payload["failover"] = failover
    return jsonify(response_payload)


@bp.route("/api/tasks/<int:task_id>", methods=["DELETE"], endpoint="delete_task")
@api_or_login_required
def delete_task(task_id: int):
    from ..services.sheety_failover import SheetyFailoverService
    from ..repositories.sheety_accounts import get_active_api_account

    db_name = current_app.config["DB_NAME"]
    user_id = int(getattr(g, "user_id", 0) or 0)

    conn = get_db_connection(db_name)
    row = conn.execute(
        "SELECT * FROM logs WHERE id = ? AND user_id = ?",
        (int(task_id), int(user_id)),
    ).fetchone()
    if not row:
        row = conn.execute(
            "SELECT * FROM logs WHERE sheety_id = ? AND user_id = ? ORDER BY id DESC LIMIT 1",
            (int(task_id), int(user_id)),
        ).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Task not found"}), 404

    row_id = int(row["id"])
    sheety_id = row["sheety_id"]
    if sheety_id is None:
        conn.close()
        return jsonify({"error": "Task is not linked to a Sheety row"}), 400

    def parse_datetime(date_value: str, time_value: str) -> Optional[datetime]:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                return datetime.strptime(f"{date_value} {time_value}", fmt)
            except ValueError:
                continue
        return None

    deleted_start = parse_datetime(row["start_date"], row["start_time"])
    deleted_end = parse_datetime(row["end_date"], row["end_time"])

    other_rows = conn.execute(
        "SELECT * FROM logs WHERE user_id = ? AND id != ? ORDER BY start_date ASC, start_time ASC",
        (int(user_id), row_id),
    ).fetchall()

    next_row = None
    next_start = None
    next_end = None
    if deleted_end is not None:
        for candidate in other_rows:
            candidate_start = parse_datetime(candidate["start_date"], candidate["start_time"])
            if candidate_start is None:
                continue
            if candidate_start >= deleted_end:
                next_row = candidate
                next_start = candidate_start
                next_end = parse_datetime(candidate["end_date"], candidate["end_time"])
                break

    empty_window = None
    if deleted_start is not None and deleted_end is not None and deleted_end > deleted_start:
        overlaps = False
        for candidate in other_rows:
            candidate_start = parse_datetime(candidate["start_date"], candidate["start_time"])
            candidate_end = parse_datetime(candidate["end_date"], candidate["end_time"])
            if candidate_start is None or candidate_end is None:
                continue
            if candidate_end <= deleted_start or candidate_start >= deleted_end:
                continue
            overlaps = True
            break
        if not overlaps:
            empty_window = {
                "start": deleted_start.isoformat(),
                "end": deleted_end.isoformat(),
                "date": deleted_start.strftime("%Y-%m-%d"),
                "start_label": deleted_start.strftime("%I:%M %p"),
                "end_label": deleted_end.strftime("%I:%M %p"),
            }

    def format_time(dt: datetime) -> str:
        return dt.strftime("%I:%M%p").lstrip("0").lower()

    def format_date(dt: datetime) -> str:
        return dt.strftime("%d/%m/%Y")

    def build_log_payload(
        task_label: str,
        tag_value_raw: str,
        is_urgent: bool,
        is_important: bool,
        start_value: datetime,
        end_value: datetime,
    ) -> Tuple[Dict[str, str], str]:
        tags_list = filter_special_tags(tag_value_raw)
        meta_tokens: List[str] = []
        if tags_list:
            meta_tokens.extend([tag.lower() for tag in tags_list])
        if is_urgent:
            meta_tokens.append("urgent")
        if is_important:
            meta_tokens.append("important")

        log_entry = f"{format_date(start_value)} {format_time(start_value)} {format_time(end_value)} {task_label}".strip()
        if meta_tokens:
            log_entry = f"{log_entry}. {' '.join(meta_tokens)}"

        logged_time = end_value.isoformat()
        tag_value = ", ".join(tags_list) if tags_list else "Waste"
        return {"logEntry": log_entry, "loggedTime": logged_time}, tag_value

    service = SheetyFailoverService(db_name, user_id)

    adjusted_next = False
    if (
        next_row
        and next_start is not None
        and next_end is not None
        and deleted_end is not None
        and next_start == deleted_end
        and next_row["sheety_id"] is not None
    ):
        sheet_name = "sheet1"
        active_account = get_active_api_account(db_name, user_id)
        if active_account:
            base_url = row_value(active_account, "api_base_url") or ""
            parsed = urlparse(base_url)
            path = parsed.path.rstrip("/") if parsed.path else ""
            if path:
                sheet_name = path.split("/")[-1] or sheet_name

        next_payload, _ = build_log_payload(
            next_row["task"],
            next_row["tags"] or "",
            bool(next_row["urg"]),
            bool(next_row["imp"]),
            next_start,
            next_end,
        )
        success, _, error = service.make_request("PUT", str(next_row["sheety_id"]), {sheet_name: next_payload})
        if not success:
            conn.close()
            return jsonify({"error": error or "Failed to update the next task in Sheety"}), 502
        adjusted_next = True

    success, _, error = service.make_request("DELETE", str(sheety_id), None)
    if not success:
        conn.close()
        return jsonify({"error": error or "Failed to delete task in Sheety"}), 502

    conn.execute(
            "DELETE FROM logs WHERE sheety_id = ? AND user_id = ?",
            (sheety_id, int(user_id)),
        )
    conn.commit()
    conn.close()

    response_payload = {"success": True, "adjusted_next": adjusted_next}
    if empty_window:
        response_payload["empty_window"] = empty_window
    failover = service.get_failover_notification()
    if failover:
        response_payload["failover"] = failover
    return jsonify(response_payload)


@bp.route("/api/tasks", endpoint="get_tasks")
@api_or_login_required
def get_tasks():
    db_name = current_app.config["DB_NAME"]
    user_id = int(getattr(g, "user_id", 0) or 0)

    selected_date = parse_date_param(request.args.get("date"))
    period = parse_period_param(request.args.get("period", "day"))
    filter_type = request.args.get("filter", "all")
    tag_name = request.args.get("tag", "")

    df = fetch_local_data(db_name, user_id)
    if df.empty:
        try:
            sync_cloud_data(db_name, user_id, force=True)
            df = fetch_local_data(db_name, user_id)
        except Exception as exc:
            logger.warning(
                "API tasks sync fallback failed user_id=%s error=%s",
                int(user_id),
                exc,
            )

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
    period_df = df[(df["date"] >= start_date) & (df["date"] <= end_date)] if not df.empty else pd.DataFrame()

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
    elif filter_type in {"imp", "important"}:
        filtered = period_df[period_df["important"]]
    elif filter_type in {"urg", "urgent"}:
        filtered = period_df[period_df["urgent"]]
    elif filter_type == "imp_and_urg":
        filtered = period_df[(period_df["urgent"]) & (period_df["important"])]
    elif filter_type == "imp_not_urg":
        filtered = period_df[(~period_df["urgent"]) & (period_df["important"])]
    elif filter_type == "urg_not_imp":
        filtered = period_df[(period_df["urgent"]) & (~period_df["important"])]
    elif filter_type == "not_imp" or filter_type == "not_important":
        filtered = period_df[~period_df["important"]]
    elif filter_type == "not_urg" or filter_type == "not_urgent":
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
                    "id": int(row.get("id") or 0),
                    "sheety_id": (int(row.get("sheety_id")) if row.get("sheety_id") is not None else None),
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

    total_hours = float(round(filtered["duration"].sum() / 60.0, 2)) if not filtered.empty else 0.0
    total_minutes = int(filtered["duration"].sum()) if not filtered.empty else 0
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


@bp.route("/api/tags", endpoint="get_tags")
@api_or_login_required
def get_tags():
    db_name = current_app.config["DB_NAME"]
    user_id = int(getattr(g, "user_id", 0) or 0)

    selected_date = parse_date_param(request.args.get("date"))
    period = parse_period_param(request.args.get("period", "day"))

    df = fetch_local_data(db_name, user_id)
    if df.empty:
        try:
            sync_cloud_data(db_name, user_id, force=True)
            df = fetch_local_data(db_name, user_id)
        except Exception as exc:
            logger.warning(
                "API tags sync fallback failed user_id=%s error=%s",
                int(user_id),
                exc,
            )

    start_date, end_date = get_period_range(selected_date, period)
    period_df = df[(df["date"] >= start_date) & (df["date"] <= end_date)] if not df.empty else pd.DataFrame()

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
        tag_stats.append({"name": tag, "hours": round(total_minutes / 60.0, 2), "minutes": total_minutes, "count": int(len(group))})

    tag_stats.sort(key=lambda x: x["hours"], reverse=True)
    return jsonify({"tags": tag_stats})


@bp.route("/api/import-csv", methods=["POST"], endpoint="import_csv")
@api_or_login_required
def import_csv():
    db_name = current_app.config["DB_NAME"]
    user_id = int(getattr(g, "user_id", 0) or 0)

    try:
        data = request.get_json(silent=True) or {}
        csv_content = data.get("csv_data")
        if not csv_content:
            return jsonify({"status": "error", "message": "Missing 'csv_data' field"}), 400

        inserted_count = import_csv_content(db_name, int(user_id), str(csv_content))
        return jsonify(
            {
                "status": "success",
                "message": f"Successfully imported {inserted_count} tasks from CSV",
                "count": inserted_count,
            }
        )
    except Exception as exc:
        return jsonify({"status": "error", "message": str(exc)}), 500


@bp.route("/api/export-csv", endpoint="export_csv")
@api_or_login_required
def export_csv():
    db_name = current_app.config["DB_NAME"]
    user_id = int(getattr(g, "user_id", 0) or 0)

    df = fetch_local_data(db_name, user_id)
    headers = [
        "start date",
        "start time",
        "end date",
        "end time",
        "task",
        "duration",
        "special tags",
        "urgent",
        "important",
    ]
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(headers)

    if not df.empty:
        for _, row in df.iterrows():
            start_dt = row.get("start_datetime")
            end_dt = row.get("end_datetime")
            if pd.isna(start_dt) or pd.isna(end_dt):
                continue
            duration_hours = round(float(row.get("duration", 0) or 0) / 60.0, 2)
            special_tags = row.get("special_tags")
            if isinstance(special_tags, list):
                tags_value = ", ".join([t for t in special_tags if t])
            else:
                tags_value = ""
            writer.writerow(
                [
                    start_dt.strftime("%Y-%m-%d"),
                    start_dt.strftime("%I:%M %p"),
                    end_dt.strftime("%Y-%m-%d"),
                    end_dt.strftime("%I:%M %p"),
                    row.get("task", ""),
                    duration_hours,
                    tags_value,
                    "Yes" if row.get("urgent") else "No",
                    "Yes" if row.get("important") else "No",
                ]
            )

    buffer = BytesIO(output.getvalue().encode("utf-8"))
    return send_file(
        buffer,
        mimetype="text/csv",
        as_attachment=True,
        download_name="time-tracker-export.csv",
    )


@bp.route("/download-db", endpoint="download_db")
def download_db():
    try:
        db_name = current_app.config["DB_NAME"]
        headers = dict(request.headers)
        if get_user_count(db_name) > 1:
            return "Disabled in multi-user mode", 403
        if not token_is_valid(headers):
            return "Unauthorized", 401
        resolved_user_id = resolve_request_user_id(headers)
        if resolved_user_id is not None:
            try:
                sync_cloud_data(db_name, int(resolved_user_id))
            except Exception as exc:
                logger.warning(
                    "DB download sync failed user_id=%s error=%s",
                    int(resolved_user_id),
                    exc,
                )
        return send_file(db_name, as_attachment=True)
    except Exception as exc:
        return f"Error downloading DB: {exc}", 500


@bp.route("/sync-status", endpoint="sync_status")
def sync_status():
    try:
        db_name = current_app.config["DB_NAME"]
        headers = dict(request.headers)
        user_id = get_current_user_id()
        if user_id is None and token_is_valid(headers):
            user_id = resolve_request_user_id(headers)
        return jsonify(sync_status_payload(db_name, int(user_id) if user_id is not None else None))
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@bp.route("/sync-now", endpoint="sync_now")
@api_or_login_required
def sync_now():
    try:
        db_name = current_app.config["DB_NAME"]
        user_id = int(getattr(g, "user_id", 0) or 0)
        failover = sync_cloud_data(db_name, user_id, force=True)
        response = {"status": "success", "message": "Synced latest data from cloud"}
        if failover:
            response["failover"] = failover
        return jsonify(response)
    except Exception as exc:
        return jsonify({"status": "error", "message": str(exc)}), 500


@bp.route("/hard-reset", endpoint="hard_reset")
@api_or_login_required
def hard_reset():
    try:
        db_name = current_app.config["DB_NAME"]
        user_id = int(getattr(g, "user_id", 0) or 0)
        sync_cloud_data(db_name, user_id, force=True)
        return jsonify(
            {
                "status": "success",
                "message": "Database has been completely wiped and rebuilt from Google Sheets.",
            }
        )
    except Exception as exc:
        return jsonify({"status": "error", "message": str(exc)}), 500


@bp.route("/api/sheety-accounts", methods=["POST"], endpoint="create_sheety_account")
@api_or_login_required
def create_sheety_account():
    """Create a new Sheety API account for the user."""
    from ..repositories.sheety_accounts import create_api_account
    
    try:
        db_name = current_app.config["DB_NAME"]
        user_id = int(getattr(g, "user_id", 0) or 0)
        
        data = request.get_json(silent=True) or {}
        account_email = (data.get("account_email") or "").strip()
        api_base_url = (data.get("api_base_url") or "").strip()
        api_token = (data.get("api_token") or "").strip() or None
        
        if not api_base_url:
            return jsonify({"error": "API base URL is required"}), 400
        
        if not account_email:
            user_row = get_user_by_id(db_name, user_id)
            account_email = (row_value(user_row, "email") or "").strip() or "Unknown"
        
        account_id = create_api_account(db_name, user_id, account_email, api_base_url, api_token)
        
        return jsonify({
            "success": True,
            "account_id": account_id,
            "message": "API account created successfully"
        }), 201
    except Exception as exc:
        logger.error(f"Error creating API account: {exc}")
        return jsonify({"error": str(exc)}), 500


@bp.route("/api/sheety-accounts/<int:account_id>", methods=["DELETE"], endpoint="delete_sheety_account")
@api_or_login_required
def delete_sheety_account(account_id: int):
    """Delete a Sheety API account."""
    from ..repositories.sheety_accounts import delete_api_account
    
    try:
        db_name = current_app.config["DB_NAME"]
        user_id = int(getattr(g, "user_id", 0) or 0)
        
        success = delete_api_account(db_name, account_id, user_id)
        
        if success:
            return jsonify({"success": True, "message": "API account deleted"}), 200
        else:
            return jsonify({"error": "Account not found"}), 404
    except Exception as exc:
        logger.error(f"Error deleting API account: {exc}")
        return jsonify({"error": str(exc)}), 500


@bp.route("/api/sheety-accounts/<int:account_id>/test", methods=["GET"], endpoint="test_sheety_account")
@api_or_login_required
def test_sheety_account(account_id: int):
    """Test a Sheety API account connection."""
    from ..services.sheety_failover import SheetyFailoverService
    
    try:
        db_name = current_app.config["DB_NAME"]
        user_id = int(getattr(g, "user_id", 0) or 0)
        
        service = SheetyFailoverService(db_name, user_id)
        success, row_count, error = service.test_connection(account_id)
        
        if success:
            return jsonify({
                "success": True,
                "row_count": row_count,
                "message": f"Connection successful! Found {row_count} rows."
            }), 200
        else:
            return jsonify({
                "success": False,
                "error": error or "Connection test failed"
            }), 200
    except Exception as exc:
        logger.error(f"Error testing API account: {exc}")
        return jsonify({"success": False, "error": str(exc)}), 200
