from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional, Tuple

from flask import Blueprint, current_app, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from ..core.admins import is_admin_email
from ..core.rows import display_name, row_value
from ..db import get_db_connection
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
from .decorators import login_required
from .utils import safe_next_url


bp = Blueprint("auth", __name__)

logger = logging.getLogger(__name__)

_PROFILE_OTP_PURPOSES = {"change_password", "change_username"}


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
        action = (request.form.get("action") or "").strip().lower()

        if action == "login_otp":
            if not identifier:
                error = "Please enter your username, email, or user ID."
            else:
                user = get_user_by_username_or_id_or_email(db_name, identifier)
                if not user:
                    error = "No account found with that identifier."
                else:
                    session.clear()
                    session["pending_user_id"] = int(row_value(user, "id"))
                    session["pending_remember"] = remember
                    session["pending_verification_purpose"] = "login_otp"
                    if send_verification_email(db_name, user, purpose="login_otp"):
                        target_email = row_value(user, "email") or ""
                        session["verification_success"] = (
                            f"Verification code sent to {target_email}." if target_email else "Verification code sent."
                        )
                    else:
                        session["verification_error"] = "Unable to send email. Check SMTP settings."
                    return redirect(url_for("auth.verify_email", next=next_url or ""))
        elif not identifier or not password:
            error = "Username, email, or user ID and password are required."
        else:
            user = get_user_by_username_or_id_or_email(db_name, identifier)
            if not user:
                error = "Invalid credentials."
            else:
                pw_hash = row_value(user, "password_hash") or ""
                if not check_password_hash(pw_hash, password):
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
                        if not (
                            settings.get("sheety_endpoint")
                            or (current_app.config.get("SHEETY_ENDPOINT") or "")
                            or os.getenv("SHEETY_ENDPOINT")
                        ):
                            return redirect(url_for("main.settings"))

                    return redirect(next_url or url_for("main.dashboard"))

    if request.args.get("verify"):
        show_verify = True

    return render_template("login.html", error=error, next=next_url or "", show_verify=show_verify)


@bp.route("/forgot-password", methods=["GET", "POST"], endpoint="forgot_password")
def forgot_password():
    if session.get("user_id") is not None:
        return redirect(url_for("main.settings"))

    db_name = current_app.config["DB_NAME"]
    error: Optional[str] = None
    success: Optional[str] = None

    if request.method == "POST":
        identifier = (request.form.get("identifier") or "").strip()
        if not identifier:
            error = "Please enter your email or user ID."
        else:
            if "@" in identifier:
                user = get_user_by_email(db_name, identifier)
            else:
                user = get_user_by_public_id(db_name, identifier)
            if not user:
                error = "No account found with that email or user ID."
            else:
                session.clear()
                session["pending_user_id"] = int(row_value(user, "id"))
                session["pending_verification_purpose"] = "forgot_password"
                if send_verification_email(db_name, user, purpose="forgot_password"):
                    target_email = row_value(user, "email") or ""
                    session["verification_success"] = (
                        f"Verification code sent to {target_email}." if target_email else "Verification code sent."
                    )
                    return redirect(url_for("auth.verify_email"))
                error = "Unable to send email. Check SMTP settings."

    return render_template("forgot_password.html", error=error, success=success)


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
    except Exception as exc:
        logger.warning("Failed to clear tt_token cookie error=%s", exc)
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
    purpose = session.get("pending_verification_purpose") or "verify_email"
    next_url = safe_next_url(request.args.get("next"))
    title = "Verify your email"
    subtitle = "We sent a 6-digit code to"
    if purpose == "forgot_password":
        title = "Reset your password"
        subtitle = "We sent a 6-digit code to"
    if purpose == "login_otp":
        title = "Log in with OTP"
        subtitle = "We sent a 6-digit code to"

    if request.method == "POST":
        if request.form.get("action") == "resend":
            if send_verification_email(db_name, user, purpose=purpose):
                success = "Verification code resent."
            else:
                error = "Unable to send email. Check SMTP settings."
        else:
            code = (request.form.get("code") or "").strip()
            if not code:
                error = "Please enter the verification code."
            else:
                ok, message = verify_email_code(
                    db_name,
                    int(user["id"]),
                    code,
                    purpose=purpose,
                    mark_verified=(purpose == "verify_email"),
                )
                if ok:
                    session.pop("pending_user_id", None)
                    session.pop("pending_verification_purpose", None)
                    if purpose == "verify_email":
                        send_welcome_email(user)
                        remember = bool(session.pop("pending_remember", False))
                        session["user_id"] = int(user["id"])
                        session.permanent = remember
                        return redirect(url_for("main.settings"))
                    if purpose == "login_otp":
                        remember = bool(session.pop("pending_remember", False))
                        session["user_id"] = int(user["id"])
                        session.permanent = remember
                        session["otp_authenticated"] = True
                        session["password_reset_user_id"] = int(user["id"])
                        return redirect(url_for("auth.reset_password"))
                    if purpose == "forgot_password":
                        session["otp_authenticated"] = True
                    session["password_reset_user_id"] = int(user["id"])
                    return redirect(url_for("auth.reset_password"))
                error = message
    elif not has_active_verification(db_name, int(user["id"]), purpose=purpose):
        if send_verification_email(db_name, user, purpose=purpose):
            success = "Verification code sent."
        else:
            error = "Unable to send email. Check SMTP settings."

    return render_template(
        "verify_email.html",
        error=error,
        success=success,
        email=row_value(user, "email"),
        name=display_name(user),
        title=title,
        subtitle=subtitle,
        purpose=purpose,
    )


@bp.route("/reset-password", methods=["GET", "POST"], endpoint="reset_password")
def reset_password():
    db_name = current_app.config["DB_NAME"]
    reset_user_id = session.get("password_reset_user_id")
    if not reset_user_id:
        return redirect(url_for("auth.login"))

    user = get_user_by_id(db_name, int(reset_user_id))
    if not user:
        session.pop("password_reset_user_id", None)
        return redirect(url_for("auth.login"))

    error: Optional[str] = None
    success: Optional[str] = None
    if request.method == "POST":
        new_password = request.form.get("password") or ""
        confirm_password = request.form.get("confirm_password") or ""
        if not new_password or len(new_password) < 6:
            error = "Password must be at least 6 characters."
        elif new_password != confirm_password:
            error = "Passwords do not match."
        else:
            conn = get_db_connection(db_name)
            conn.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (generate_password_hash(new_password), int(user["id"])),
            )
            conn.commit()
            conn.close()
            session.pop("password_reset_user_id", None)
            session.pop("otp_authenticated", None)
            session["user_id"] = int(user["id"])
            session.permanent = True
            success = "Password updated."
            return redirect(url_for("main.settings"))

    return render_template("reset_password.html", error=error, success=success)


@bp.route("/profile/request-otp", methods=["POST"], endpoint="profile_request_otp")
@login_required
def profile_request_otp():
    db_name = current_app.config["DB_NAME"]
    user_id = int(session.get("user_id") or 0)
    user = get_user_by_id(db_name, user_id)
    if not user:
        session["profile_error"] = "Unable to load your profile."
        return redirect(url_for("main.settings"))

    purpose = (request.form.get("purpose") or "").strip()
    if purpose == "change_email":
        new_email = (request.form.get("new_email") or "").strip().lower()
        if not new_email or "@" not in new_email:
            session["profile_error"] = "Please provide a valid new email address."
            return redirect(url_for("main.settings"))
        if (row_value(user, "email") or "").strip().lower() == new_email:
            session["profile_error"] = "New email matches your current email."
            return redirect(url_for("main.settings"))
        existing = get_user_by_email(db_name, new_email)
        if existing and int(existing["id"]) != int(user["id"]):
            session["profile_error"] = "That email is already in use."
            return redirect(url_for("main.settings"))

        session["pending_email_new"] = new_email
        sent_old = send_verification_email(db_name, user, purpose="change_email_old")
        sent_new = send_verification_email(
            db_name,
            user,
            purpose="change_email_new",
            email_override=new_email,
        )
        if sent_old and sent_new:
            session["profile_success"] = "Verification codes sent to both emails."
        else:
            session["profile_error"] = "Unable to send verification emails. Check SMTP settings."
        return redirect(url_for("main.settings"))

    if purpose not in _PROFILE_OTP_PURPOSES:
        session["profile_error"] = "Invalid verification request."
        return redirect(url_for("main.settings"))

    if send_verification_email(db_name, user, purpose=purpose):
        session["profile_success"] = "Verification code sent."
    else:
        session["profile_error"] = "Unable to send email. Check SMTP settings."
    return redirect(url_for("main.settings"))


@bp.route("/profile/update", methods=["POST"], endpoint="update_profile")
@login_required
def update_profile():
    db_name = current_app.config["DB_NAME"]
    user_id = int(session.get("user_id") or 0)
    user = get_user_by_id(db_name, user_id)
    if not user:
        session["profile_error"] = "Unable to load your profile."
        return redirect(url_for("main.settings"))

    action = (request.form.get("action") or "").strip()
    current_password = request.form.get("current_password") or ""
    otp_code = (request.form.get("otp_code") or "").strip()

    def confirm_with_password_or_otp(purpose: str) -> Tuple[bool, str]:
        if current_password:
            pw_hash = row_value(user, "password_hash") or ""
            if check_password_hash(pw_hash, current_password):
                return True, ""
        if otp_code:
            return verify_email_code(db_name, int(user["id"]), otp_code, purpose=purpose)
        return False, "Provide your current password or a verification code."

    if action == "update_username":
        new_username = (request.form.get("new_username") or "").strip()
        if not new_username:
            session["profile_error"] = "Please enter a new username."
            return redirect(url_for("main.settings"))
        existing = get_user_by_username_or_id_or_email(db_name, new_username)
        if existing and int(existing["id"]) != int(user["id"]):
            session["profile_error"] = "That username is already taken."
            return redirect(url_for("main.settings"))
        ok, message = confirm_with_password_or_otp("change_username")
        if not ok:
            session["profile_error"] = message
            return redirect(url_for("main.settings"))
        conn = get_db_connection(db_name)
        conn.execute(
            "UPDATE users SET username = ?, user_id = ? WHERE id = ?",
            (new_username, new_username, int(user["id"])),
        )
        conn.commit()
        conn.close()
        session["profile_success"] = "Username updated."
        return redirect(url_for("main.settings"))

    if action == "update_password":
        new_password = request.form.get("new_password") or ""
        confirm_password = request.form.get("confirm_password") or ""
        if not new_password or len(new_password) < 6:
            session["profile_error"] = "Password must be at least 6 characters."
            return redirect(url_for("main.settings"))
        if new_password != confirm_password:
            session["profile_error"] = "Passwords do not match."
            return redirect(url_for("main.settings"))
        ok = False
        message = ""
        if session.get("otp_authenticated"):
            ok = True
        else:
            ok, message = confirm_with_password_or_otp("change_password")
        if not ok:
            session["profile_error"] = message
            return redirect(url_for("main.settings"))
        conn = get_db_connection(db_name)
        conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (generate_password_hash(new_password), int(user["id"])),
        )
        conn.commit()
        conn.close()
        session.pop("otp_authenticated", None)
        session["profile_success"] = "Password updated."
        return redirect(url_for("main.settings"))

    if action == "update_email":
        new_email = (request.form.get("new_email") or "").strip().lower()
        old_code = (request.form.get("old_email_code") or "").strip()
        new_code = (request.form.get("new_email_code") or "").strip()
        pending_email = (session.get("pending_email_new") or "").strip().lower()
        if not new_email or "@" not in new_email:
            session["profile_error"] = "Please provide a valid new email address."
            return redirect(url_for("main.settings"))
        if not pending_email or pending_email != new_email:
            session["profile_error"] = "Request a new verification code for this email first."
            return redirect(url_for("main.settings"))
        if not old_code or not new_code:
            session["profile_error"] = "Enter both verification codes."
            return redirect(url_for("main.settings"))
        existing = get_user_by_email(db_name, new_email)
        if existing and int(existing["id"]) != int(user["id"]):
            session["profile_error"] = "That email is already in use."
            return redirect(url_for("main.settings"))

        ok_old, message_old = verify_email_code(
            db_name,
            int(user["id"]),
            old_code,
            purpose="change_email_old",
        )
        if not ok_old:
            session["profile_error"] = message_old
            return redirect(url_for("main.settings"))
        ok_new, message_new = verify_email_code(
            db_name,
            int(user["id"]),
            new_code,
            purpose="change_email_new",
        )
        if not ok_new:
            session["profile_error"] = message_new
            return redirect(url_for("main.settings"))

        conn = get_db_connection(db_name)
        conn.execute(
            "UPDATE users SET email = ? WHERE id = ?",
            (new_email, int(user["id"])),
        )
        conn.commit()
        conn.close()
        session.pop("pending_email_new", None)
        session["profile_success"] = "Email updated."
        return redirect(url_for("main.settings"))

    session["profile_error"] = "Unknown profile update action."
    return redirect(url_for("main.settings"))
