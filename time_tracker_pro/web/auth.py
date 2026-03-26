from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional

from flask import Blueprint, current_app, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from ..core.admins import is_admin_email
from ..core.rows import display_name, row_value
from ..repositories.auth import create_user
from ..repositories.bootstrap import adopt_orphan_logs
from ..repositories.users import (
    get_user_by_email,
    get_user_by_id,
    get_user_by_public_id,
    get_user_by_username_or_id_or_email,
    get_user_count,
)
from ..repositories.settings import get_user_settings
from ..services.verification import has_active_verification, send_verification_email, verify_email_code
from ..services.welcome_email import send_welcome_email
from .utils import safe_next_url


bp = Blueprint("auth", __name__)


@bp.route("/login", methods=["GET", "POST"], endpoint="login")
def login():
    if session.get("user_id") is not None:
        return redirect(url_for("main.dashboard"))

    db_name = current_app.config["DB_NAME"]
    next_url = safe_next_url(request.args.get("next"))
    error: Optional[str] = None
    show_verify = False

    if request.method == "POST":
        identifier = (request.form.get("identifier") or request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        remember = bool(request.form.get("remember"))
        next_url = safe_next_url(request.form.get("next")) or next_url

        if not identifier or not password:
            error = "Username, email, or user ID and password are required."
        else:
            user = get_user_by_username_or_id_or_email(db_name, identifier)
            pw_hash = row_value(user, "password_hash") or ""
            if not user or not check_password_hash(pw_hash, password):
                error = "Invalid credentials."
            elif not int(row_value(user, "is_verified") or 0):
                session.clear()
                session["pending_user_id"] = int(row_value(user, "id"))
                session["pending_remember"] = remember

                if send_verification_email(db_name, user):
                    session["verification_success"] = "Verification code sent."
                else:
                    session["verification_error"] = "Unable to send email. Check SMTP settings."

                return redirect(url_for("auth.verify_email", next=next_url or ""))
            else:
                session.clear()
                session["user_id"] = int(row_value(user, "id"))
                session.permanent = remember

                settings = get_user_settings(db_name, int(row_value(user, "id")))
                multi_user = get_user_count(db_name) > 1
                if multi_user:
                    if not settings.get("sheety_endpoint"):
                        return redirect(url_for("main.settings"))
                else:
                    if not (settings.get("sheety_endpoint") or (current_app.config.get("SHEETY_ENDPOINT") or "") or os.getenv("SHEETY_ENDPOINT")):
                        return redirect(url_for("main.settings"))

                return redirect(next_url or url_for("main.dashboard"))

    if request.args.get("verify"):
        show_verify = True

    return render_template("login.html", error=error, next=next_url or "", show_verify=show_verify)


@bp.route("/register", methods=["GET", "POST"], endpoint="register")
def register():
    if session.get("user_id") is not None:
        return redirect(url_for("main.dashboard"))

    db_name = current_app.config["DB_NAME"]
    error: Optional[str] = None

    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        email = (request.form.get("email") or "").strip().lower()
        public_user_id = (request.form.get("user_id") or "").strip()
        password = request.form.get("password") or ""
        remember = bool(request.form.get("remember"))

        if not name or not email or not public_user_id or not password:
            error = "Name, email, user ID, and password are required."
        elif "@" not in email or "." not in email:
            error = "Please provide a valid email address."
        elif get_user_by_public_id(db_name, public_user_id) or get_user_by_username_or_id_or_email(db_name, public_user_id):
            error = "That user ID is already taken."
        elif get_user_by_email(db_name, email):
            error = "An account with that email already exists."
        else:
            created_at = datetime.now(timezone.utc).isoformat()
            password_hash = generate_password_hash(password)
            role = "admin" if is_admin_email(email) else "user"
            existing_users = get_user_count(db_name)

            new_user_id = create_user(
                db_name,
                username=public_user_id,
                user_id=public_user_id,
                name=name,
                email=email,
                password_hash=password_hash,
                role=role,
                is_verified=0,
                created_at=created_at,
            )

            if int(existing_users) == 0:
                adopt_orphan_logs(db_name, new_user_id)

            user = get_user_by_id(db_name, new_user_id)
            if user:
                send_verification_email(db_name, user)

            session.clear()
            session["pending_user_id"] = new_user_id
            session["pending_remember"] = remember
            return redirect(url_for("auth.verify_email"))

    return render_template("register.html", error=error)


@bp.route("/logout", endpoint="logout")
def logout():
    session.clear()
    resp = redirect(url_for("auth.login"))
    try:
        resp.set_cookie("tt_token", "", expires=0)
    except Exception:
        pass
    return resp


@bp.route("/verify-email", methods=["GET", "POST"], endpoint="verify_email")
def verify_email():
    db_name = current_app.config["DB_NAME"]
    pending_id = session.get("pending_user_id")
    if not pending_id:
        return redirect(url_for("auth.login"))

    user = get_user_by_id(db_name, int(pending_id))
    if not user:
        session.pop("pending_user_id", None)
        return redirect(url_for("auth.register"))

    error: Optional[str] = session.pop("verification_error", None)
    success: Optional[str] = session.pop("verification_success", None)

    if request.method == "POST":
        if request.form.get("action") == "resend":
            if send_verification_email(db_name, user):
                success = "Verification code resent."
            else:
                error = "Unable to send email. Check SMTP settings."
        else:
            code = (request.form.get("code") or "").strip()
            if not code:
                error = "Please enter the verification code."
            else:
                ok, message = verify_email_code(db_name, int(user["id"]), code)
                if ok:
                    send_welcome_email(user)
                    session.pop("pending_user_id", None)
                    remember = bool(session.pop("pending_remember", False))
                    session["user_id"] = int(user["id"])
                    session.permanent = remember
                    return redirect(url_for("main.settings"))
                error = message
    elif not has_active_verification(db_name, int(user["id"])):
        if send_verification_email(db_name, user):
            success = "Verification code sent."
        else:
            error = "Unable to send email. Check SMTP settings."

    return render_template(
        "verify_email.html",
        error=error,
        success=success,
        email=row_value(user, "email"),
        name=display_name(user),
    )
