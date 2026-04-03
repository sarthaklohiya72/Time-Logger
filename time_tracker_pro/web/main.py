from __future__ import annotations

import base64
import json
import logging
import os
import time
from datetime import timedelta
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd
from flask import Blueprint, current_app, make_response, redirect, render_template, request, send_file, session, url_for

from ..core.admins import is_admin_email
from ..core.dates import get_period_range, parse_date_param, parse_period_param
from ..core.rows import display_name, row_value
from ..core.tags import primary_special_tag
from ..repositories.app_settings import get_app_setting, upsert_app_setting
from ..repositories.logs import fetch_local_data
from ..repositories.settings import get_user_settings, upsert_user_settings
from ..repositories.users import get_user_by_id, get_user_count
from ..services.matrix import get_matrix_stats
from ..services.sync import sync_cloud_data
from .decorators import admin_required, login_required
from .utils import get_current_user_id


bp = Blueprint("main", __name__)

logger = logging.getLogger(__name__)

_DEFAULT_ICON_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIW2P8z8BQDwAF/wJ+q9QKJwAAAABJRU5ErkJggg=="
)


def _icon_storage_path() -> Path:
    icon_dir = Path(current_app.instance_path) / "pwa"
    icon_dir.mkdir(parents=True, exist_ok=True)
    return icon_dir / "app-icon.png"


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
    profile_info = {
        "username": row_value(user_row, "username") if user_row else "",
        "email": row_value(user_row, "email") if user_row else "",
        "user_id": row_value(user_row, "user_id") if user_row else "",
        "name": row_value(user_row, "name") if user_row else "",
    }

    return render_template(
        "settings.html",
        settings=current,
        current_user=current_user,
        profile_info=profile_info,
        env_sheety=env_sheety,
        is_admin=is_admin,
        icon_error=request.args.get("icon_error"),
        icon_success=request.args.get("icon_success"),
        profile_error=session.pop("profile_error", None),
        profile_success=session.pop("profile_success", None),
        pending_email=session.get("pending_email_new"),
    )


@bp.route("/admin/app-icon", methods=["POST"], endpoint="update_app_icon")
@admin_required
def update_app_icon():
    db_name = current_app.config["DB_NAME"]
    file = request.files.get("icon")
    if not file or not file.filename:
        return redirect(url_for("main.settings", icon_error="Please choose a PNG icon to upload."))
    if file.mimetype not in {"image/png", "image/x-png"}:
        return redirect(url_for("main.settings", icon_error="Icon must be a PNG file."))
    data = file.read()
    if not data:
        return redirect(url_for("main.settings", icon_error="Uploaded icon file was empty."))
    if len(data) > 5 * 1024 * 1024:
        return redirect(url_for("main.settings", icon_error="Icon is too large (max 5MB)."))
    icon_path = _icon_storage_path()
    icon_path.write_bytes(data)
    upsert_app_setting(db_name, "app_icon_version", str(int(time.time())))
    return redirect(url_for("main.settings", icon_success="App icon updated. Re-add the app to refresh."))


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
        except Exception as exc:
            logger.warning(
                "Dashboard sync fallback failed user_id=%s error=%s",
                int(user_id),
                exc,
            )

    start_date, end_date = get_period_range(selected_date, period)
    period_df = df[(df["date"] >= start_date) & (df["date"] <= end_date)] if not df.empty else pd.DataFrame()
    avg_start_date = start_date
    avg_end_date = end_date
    if not period_df.empty:
        try:
            parsed_dates = pd.to_datetime(period_df["date"])
            first_date = parsed_dates.min()
            last_date = parsed_dates.max()
            if not pd.isna(first_date):
                first_date = first_date.date()
            if not pd.isna(last_date):
                last_date = last_date.date()
        except Exception:
            first_date = None
            last_date = None
        if first_date and first_date > avg_start_date:
            avg_start_date = first_date
        if last_date and last_date < avg_end_date:
            avg_end_date = last_date
    if avg_end_date < avg_start_date:
        avg_end_date = avg_start_date

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
            avg_start_date=avg_start_date,
            avg_end_date=avg_end_date,
        )
    )

    expected = os.getenv("TIME_TRACKER_API_TOKEN")
    if expected and get_user_count(db_name) <= 1:
        try:
            resp.set_cookie("tt_token", expected, httponly=True, samesite="Lax")
        except Exception as exc:
            logger.warning("Failed to set tt_token cookie error=%s", exc)

    return resp


@bp.route("/manifest.webmanifest", endpoint="manifest")
def manifest():
    db_name = current_app.config["DB_NAME"]
    version = get_app_setting(db_name, "app_icon_version") or "1"
    icon_url = url_for("main.app_icon", v=version)
    payload = {
        "name": "Time Tracker Pro",
        "short_name": "Time Tracker",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "background_color": "#F7F1E6",
        "theme_color": "#F7CF6B",
        "icons": [
            {
                "src": icon_url,
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "any"
            }
        ],
    }
    return current_app.response_class(
        json.dumps(payload),
        mimetype="application/manifest+json",
    )


@bp.route("/app-icon.png", endpoint="app_icon")
def app_icon():
    icon_path = _icon_storage_path()
    if icon_path.exists():
        return send_file(icon_path, mimetype="image/png")
    return send_file(BytesIO(_DEFAULT_ICON_BYTES), mimetype="image/png")


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
