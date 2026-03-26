from __future__ import annotations

import os
from datetime import timedelta
from typing import Any, Dict, List

import pandas as pd
from flask import Blueprint, current_app, make_response, redirect, render_template, request, url_for

from ..core.admins import is_admin_email
from ..core.dates import get_period_range, parse_date_param, parse_period_param
from ..core.rows import display_name, row_value
from ..core.tags import primary_special_tag
from ..repositories.logs import fetch_local_data
from ..repositories.settings import get_user_settings, upsert_user_settings
from ..repositories.users import get_user_by_id, get_user_count
from ..services.matrix import get_matrix_stats
from ..services.sync import sync_cloud_data
from .decorators import login_required
from .utils import get_current_user_id


bp = Blueprint("main", __name__)


@bp.route("/settings", methods=["GET", "POST"], endpoint="settings")
@login_required
def settings():
    db_name = current_app.config["DB_NAME"]
    user_id = int(get_current_user_id() or 0)
    current = get_user_settings(db_name, user_id)

    if request.method == "POST":
        sheety_endpoint = request.form.get("sheety_endpoint")
        sheety_token = request.form.get("sheety_token")
        upsert_user_settings(db_name, user_id, sheety_endpoint, sheety_token)
        return redirect(url_for("main.dashboard"))

    user_row = get_user_by_id(db_name, user_id)
    is_admin = bool(
        user_row
        and (
            (row_value(user_row, "role") or "user") == "admin"
            or is_admin_email(row_value(user_row, "email"))
        )
    )

    allow_env_fallback = get_user_count(db_name) <= 1
    env_sheety = (os.getenv("SHEETY_ENDPOINT") or "").strip() if allow_env_fallback else ""

    current_user = {
        "id": user_id,
        "display_name": display_name(user_row),
        "role": (row_value(user_row, "role") if user_row else "user"),
    }

    return render_template(
        "settings.html",
        settings=current,
        current_user=current_user,
        env_sheety=env_sheety,
        is_admin=is_admin,
    )


@bp.route("/", endpoint="dashboard")
@login_required
def dashboard():
    db_name = current_app.config["DB_NAME"]
    user_id = int(get_current_user_id() or 0)

    sync_cloud_data(db_name, user_id)

    date_str = request.args.get("date")
    raw_period = request.args.get("period", "day")
    period = parse_period_param(raw_period)
    selected_date = parse_date_param(date_str)

    df = fetch_local_data(db_name, user_id)
    if df.empty:
        try:
            sync_cloud_data(db_name, user_id, force=True)
            df = fetch_local_data(db_name, user_id)
        except Exception:
            pass

    start_date, end_date = get_period_range(selected_date, period)
    period_df = df[(df["date"] >= start_date) & (df["date"] <= end_date)] if not df.empty else pd.DataFrame()

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
        tag_counts = tag_df.groupby("primary_tag")["duration"].sum().sort_values(ascending=False)
        tag_labels = list(tag_counts.index)
        tag_data = [round(float(x) / 60.0, 1) for x in tag_counts.values]

    if period == "day":
        period_label = selected_date.strftime("%B %Y")
    elif period == "week":
        period_label = f"{start_of_week.strftime('%b %d')} - {(start_of_week + timedelta(days=6)).strftime('%b %d, %Y')}"
    else:
        period_label = selected_date.strftime("%B %Y")

    user_row = get_user_by_id(db_name, user_id)
    is_admin = bool(
        user_row
        and (
            (row_value(user_row, "role") or "user") == "admin"
            or is_admin_email(row_value(user_row, "email"))
        )
    )
    current_user = {
        "id": user_id,
        "display_name": display_name(user_row),
        "role": (row_value(user_row, "role") if user_row else "user"),
        "is_admin": is_admin,
    }

    resp = make_response(
        render_template(
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
            current_user=current_user,
            is_admin=is_admin,
        )
    )

    expected = os.getenv("TIME_TRACKER_API_TOKEN")
    if expected and get_user_count(db_name) <= 1:
        try:
            resp.set_cookie("tt_token", expected, httponly=True, samesite="Lax")
        except Exception:
            pass

    return resp


@bp.route("/graphs", endpoint="graphs")
@login_required
def graphs():
    db_name = current_app.config["DB_NAME"]
    user_id = int(get_current_user_id() or 0)
    user_row = get_user_by_id(db_name, user_id)
    current_user = {
        "id": user_id,
        "display_name": display_name(user_row),
        "role": (row_value(user_row, "role") if user_row else "user"),
    }
    is_admin = bool(
        user_row
        and (
            (row_value(user_row, "role") or "user") == "admin"
            or is_admin_email(row_value(user_row, "email"))
        )
    )
    return render_template("graphs.html", current_user=current_user, is_admin=is_admin)
