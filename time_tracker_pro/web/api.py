from __future__ import annotations

import csv
import logging
import os
from datetime import timedelta
from io import BytesIO, StringIO
from typing import Any, Dict, List, Optional

import pandas as pd
from flask import Blueprint, current_app, g, jsonify, request, send_file

from ..core.constants import GRAPH_TAG_MAP
from ..core.dates import get_period_range, parse_date_param, parse_period_param
from ..core.tags import filter_special_tags, primary_special_tag
from ..repositories.logs import fetch_local_data
from ..repositories.users import get_user_count
from ..services.import_csv import import_csv_content
from ..services.sync import sync_cloud_data, sync_status_payload
from .decorators import api_or_login_required, resolve_request_user_id, token_is_valid
from .utils import get_current_user_id


bp = Blueprint("api", __name__)

logger = logging.getLogger(__name__)


@bp.route("/api/graph-data", endpoint="graph_data")
@api_or_login_required
def graph_data():
    db_name = current_app.config["DB_NAME"]
    user_id = int(getattr(g, "user_id", 0) or 0)

    metric = (request.args.get("metric") or "total").strip().lower()
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

    daily = df.groupby("date")["duration"].sum() if not df.empty else {}
    labels: List[str] = []
    values: List[float] = []
    for i in range(days):
        day = start_date + timedelta(days=i)
        labels.append(day.strftime("%Y-%m-%d"))
        minutes = float(daily.get(day, 0) if hasattr(daily, "get") else 0)
        values.append(round(minutes / 60.0, 2))

    total_hours = round(sum(values), 2)
    avg_hours = round(total_hours / days, 2) if days else 0
    max_hours = round(max(values) if values else 0, 2)

    return jsonify({"labels": labels, "values": values, "total_hours": total_hours, "avg_hours": avg_hours, "max_hours": max_hours})


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
        sync_cloud_data(db_name, user_id, force=True)
        return jsonify({"status": "success", "message": "Synced latest data from cloud"})
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
