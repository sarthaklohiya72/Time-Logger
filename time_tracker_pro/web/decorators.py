from __future__ import annotations

import hmac
import os
from functools import wraps
from typing import Any, Callable, Dict, Optional

from flask import abort, current_app, g, jsonify, redirect, request, session, url_for

from ..core.admins import is_admin_email
from ..core.rows import row_value
from ..repositories.users import get_user_by_id, get_user_count, update_user_role
from .utils import get_current_user_id, safe_next_url


API_AUTH_TOKEN_ENV = "TIME_TRACKER_API_TOKEN"


def token_is_valid(headers: Dict[str, str]) -> bool:
    expected = os.getenv(API_AUTH_TOKEN_ENV) or ""
    if not expected:
        return False
    db_name = current_app.config["DB_NAME"]
    provided = headers.get("X-API-Token") or request.args.get("token") or ""
    return bool(provided and hmac.compare_digest(provided, expected))


def resolve_request_user_id(headers: Dict[str, str]) -> Optional[int]:
    db_name = current_app.config["DB_NAME"]

    session_uid = get_current_user_id()
    if session_uid is not None:
        user = get_user_by_id(db_name, int(session_uid))
        if not user or not int(row_value(user, "is_verified") or 0):
            return None
        return int(session_uid)

    if not token_is_valid(headers):
        return None

    raw_user_id = request.args.get("user_id") or request.headers.get("X-User-Id")
    raw_username = request.args.get("username") or request.headers.get("X-Username")

    if raw_user_id:
        try:
            user = get_user_by_id(db_name, int(raw_user_id))
            if not user or not int(row_value(user, "is_verified") or 0):
                return None
            return int(user["id"])
        except Exception:
            return None

    if raw_username:
        from ..repositories.users import get_user_by_username_or_id_or_email

        user = get_user_by_username_or_id_or_email(db_name, raw_username)
        if not user or not int(row_value(user, "is_verified") or 0):
            return None
        return int(user["id"])

    conn_user = None
    if get_user_count(db_name) == 1:
        from ..db import get_db_connection

        conn = get_db_connection(db_name)
        conn_user = conn.execute("SELECT id FROM users ORDER BY id ASC LIMIT 1").fetchone()
        conn.close()
    if conn_user:
        user = get_user_by_id(db_name, int(conn_user["id"]))
        if user and int(row_value(user, "is_verified") or 0):
            return int(user["id"])
    return None


def login_required(fn: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(fn)
    def wrapper(*args, **kwargs):
        uid = get_current_user_id()
        db_name = current_app.config["DB_NAME"]
        if uid is not None:
            user = get_user_by_id(db_name, int(uid))
            if user and not int(row_value(user, "is_verified") or 0):
                session.clear()
                session["pending_user_id"] = int(uid)
                if request.path.startswith("/api/"):
                    return jsonify({"error": "Email not verified"}), 403
                return redirect(url_for("auth.verify_email"))
            return fn(*args, **kwargs)
        if get_user_count(db_name) == 0:
            return redirect(url_for("auth.register"))
        if request.path.startswith("/api/"):
            return jsonify({"error": "Unauthorized"}), 401
        return redirect(url_for("auth.login", next=safe_next_url(request.full_path or request.path)))

    return wrapper


def admin_required(fn: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(fn)
    def wrapper(*args, **kwargs):
        uid = get_current_user_id()
        if uid is None:
            return redirect(url_for("auth.login"))
        db_name = current_app.config["DB_NAME"]
        user = get_user_by_id(db_name, int(uid))
        if not user or not int(row_value(user, "is_verified") or 0):
            session.clear()
            session["pending_user_id"] = int(uid)
            return redirect(url_for("auth.verify_email"))
        email = row_value(user, "email")
        role = row_value(user, "role") or "user"
        is_admin = role == "admin" or is_admin_email(email)
        if is_admin and role != "admin":
            update_user_role(db_name, int(user["id"]), "admin")
        if not is_admin:
            abort(403)
        return fn(*args, **kwargs)

    return wrapper


def api_or_login_required(fn: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(fn)
    def wrapper(*args, **kwargs):
        headers = dict(request.headers)
        uid = resolve_request_user_id(headers)
        if uid is None:
            return jsonify({"error": "Unauthorized"}), 401
        g.user_id = int(uid)
        return fn(*args, **kwargs)

    return wrapper
