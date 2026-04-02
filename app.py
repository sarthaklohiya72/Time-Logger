from __future__ import annotations

_LEGACY_APP_PY = r'''

import logging
import os
import re
import secrets
import smtplib
import socket
import sqlite3
from datetime import datetime, timedelta, timezone
from functools import wraps
from typing import Any, Dict, Iterable, List, Optional, Tuple
from flask import send_file
import pandas as pd
import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, make_response, redirect, session, url_for, g, abort
from email.message import EmailMessage
from io import StringIO
from werkzeug.security import check_password_hash, generate_password_hash

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev-secret-change-me")
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)
if os.getenv("SESSION_COOKIE_SECURE"):
    app.config["SESSION_COOKIE_SECURE"] = True
app.permanent_session_lifetime = timedelta(days=int(os.getenv("SESSION_LIFETIME_DAYS", "30")))

DB_PATH_ENV = "DB_PATH"
DB_NAME = (os.getenv(DB_PATH_ENV) or "productivity.db").strip() or "productivity.db"
SHEETY_ENDPOINT_ENV = "SHEETY_ENDPOINT"
API_AUTH_TOKEN_ENV = "TIME_TRACKER_API_TOKEN"
ADMIN_EMAILS_ENV = "ADMIN_EMAILS"
SMTP_HOST_ENV = "SMTP_HOST"
SMTP_PORT_ENV = "SMTP_PORT"
SMTP_USER_ENV = "SMTP_USER"
SMTP_PASSWORD_ENV = "SMTP_PASSWORD"
SMTP_SENDER_ENV = "SMTP_SENDER"
SMTP_USE_TLS_ENV = "SMTP_USE_TLS"
BREVO_API_KEY_ENV = "BREVO_API_KEY"
EMAIL_MODE_ENV = "EMAIL_MODE"
LOG_VERIFICATION_CODES_ENV = "LOG_VERIFICATION_CODES"
VERIFICATION_CODE_TTL_MINUTES = int(os.getenv("VERIFICATION_CODE_TTL_MINUTES", "10"))
VERIFICATION_MAX_ATTEMPTS = int(os.getenv("VERIFICATION_MAX_ATTEMPTS", "5"))
SYNC_INTERVAL_SECONDS = int(os.getenv("SYNC_INTERVAL_SECONDS", "300"))
SYNC_FAIL_COOLDOWN_SECONDS = int(os.getenv("SYNC_FAIL_COOLDOWN_SECONDS", "1800"))
SPECIAL_TAGS = {"Work", "Necessity", "Soul", "Rest", "Waste"}
GRAPH_TAG_MAP = {
    "work": "Work",
    "necessity": "Necessity",
    "soul": "Soul",
    "rest": "Rest",
    "waste": "Waste",
}

_LAST_SYNC_TS_BY_USER: Dict[int, datetime] = {}
_LAST_SYNC_FAIL_TS_BY_USER: Dict[int, datetime] = {}

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
    try:
        parent = os.path.dirname(DB_NAME)
        if parent:
            os.makedirs(parent, exist_ok=True)
    except Exception:
        pass
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    try:
        conn = get_db_connection()
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                user_id TEXT UNIQUE,
                name TEXT,
                email TEXT UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT DEFAULT 'user',
                is_verified INTEGER DEFAULT 0,
                created_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER PRIMARY KEY,
                sheety_endpoint TEXT,
                sheety_token TEXT,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
            """
        )
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
                imp INTEGER,
                user_id INTEGER DEFAULT 0
            )
            """
        )
        cols = [row["name"] for row in conn.execute("PRAGMA table_info(logs)").fetchall()]
        if "user_id" not in cols:
            conn.execute("ALTER TABLE logs ADD COLUMN user_id INTEGER DEFAULT 0")
            conn.execute("UPDATE logs SET user_id = 0 WHERE user_id IS NULL")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_logs_user_start ON logs(user_id, start_date, start_time)"
        )
        user_cols = [row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()]
        if "user_id" not in user_cols:
            conn.execute("ALTER TABLE users ADD COLUMN user_id TEXT")
        if "name" not in user_cols:
            conn.execute("ALTER TABLE users ADD COLUMN name TEXT")
        if "email" not in user_cols:
            conn.execute("ALTER TABLE users ADD COLUMN email TEXT")
        if "role" not in user_cols:
            conn.execute("ALTER TABLE users ADD COLUMN role TEXT DEFAULT 'user'")
        if "is_verified" not in user_cols:
            conn.execute("ALTER TABLE users ADD COLUMN is_verified INTEGER DEFAULT 0")
            conn.execute("UPDATE users SET is_verified = 1 WHERE is_verified IS NULL")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_user_id ON users(user_id)")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users(email)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS email_verifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                code_hash TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                attempts INTEGER DEFAULT 0,
                verified_at TEXT,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
            """
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        logger.exception("DB initialization error: %s", exc)


def _safe_next_url(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    if raw.startswith("/") and not raw.startswith("//"):
        return raw
    return None


def get_current_user_id() -> Optional[int]:
    uid = session.get("user_id")
    if uid is None:
        return None
    try:
        return int(uid)
    except Exception:
        return None


def _get_user_count() -> int:
    conn = get_db_connection()
    row = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()
    conn.close()
    return int(row["c"] if row else 0)


def _token_is_valid(headers: Dict[str, str]) -> bool:
    expected = os.getenv(API_AUTH_TOKEN_ENV)
    if not expected:
        return False
    allow_cookie_token = _get_user_count() <= 1
    provided = headers.get("X-API-Token") or request.args.get("token")
    if allow_cookie_token and not provided:
        provided = request.cookies.get("tt_token")
    return bool(provided and provided == expected)


def resolve_request_user_id(headers: Dict[str, str]) -> Optional[int]:
    session_uid = get_current_user_id()
    if session_uid is not None:
        user = _get_user_by_id(int(session_uid))
        if not user or not int(_row_value(user, "is_verified") or 0):
            return None
        return int(session_uid)

    if not _token_is_valid(headers):
        return None

    raw_user_id = request.args.get("user_id") or request.headers.get("X-User-Id")
    raw_username = request.args.get("username") or request.headers.get("X-Username")

    if raw_user_id:
        try:
            user = _get_user_by_id(int(raw_user_id))
            if not user or not int(_row_value(user, "is_verified") or 0):
                return None
            return int(user["id"])
        except Exception:
            return None

    if raw_username:
        user = _get_user_by_username(raw_username)
        if not user or not int(_row_value(user, "is_verified") or 0):
            return None
        return int(user["id"])

    conn = get_db_connection()
    row = conn.execute("SELECT id FROM users ORDER BY id ASC LIMIT 1").fetchone()
    count_row = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()
    conn.close()
    if row and int(count_row["c"]) == 1:
        user = _get_user_by_id(int(row["id"]))
        if user and int(_row_value(user, "is_verified") or 0):
            return int(user["id"])
    return None


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        uid = get_current_user_id()
        if uid is not None:
            user = _get_user_by_id(int(uid))
            if user and not int(_row_value(user, "is_verified") or 0):
                session.clear()
                session["pending_user_id"] = int(uid)
                if request.path.startswith("/api/"):
                    return jsonify({"error": "Email not verified"}), 403
                return redirect(url_for("verify_email"))
            return fn(*args, **kwargs)
        if _get_user_count() == 0:
            return redirect(url_for("register"))
        if request.path.startswith("/api/"):
            return jsonify({"error": "Unauthorized"}), 401
        return redirect(url_for("login", next=_safe_next_url(request.full_path or request.path)))

    return wrapper


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        uid = get_current_user_id()
        if uid is None:
            return redirect(url_for("login"))
        user = _get_user_by_id(int(uid))
        if not user or not int(_row_value(user, "is_verified") or 0):
            session.clear()
            session["pending_user_id"] = int(uid)
            return redirect(url_for("verify_email"))
        email = _row_value(user, "email")
        role = _row_value(user, "role") or "user"
        is_admin = role == "admin" or _is_admin_email(email)
        if is_admin and role != "admin":
            _ensure_admin_role(user)
        if not is_admin:
            abort(403)
        return fn(*args, **kwargs)

    return wrapper


def api_or_login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        headers = dict(request.headers)
        uid = resolve_request_user_id(headers)
        if uid is None:
            return jsonify({"error": "Unauthorized"}), 401
        g.user_id = int(uid)
        return fn(*args, **kwargs)

    return wrapper


def _get_user_by_username(username: str) -> Optional[sqlite3.Row]:
    value = (username or "").strip()
    if not value:
        return None
    conn = get_db_connection()
    row = conn.execute(
        """
        SELECT * FROM users
        WHERE lower(username) = lower(?)
           OR lower(user_id) = lower(?)
           OR lower(email) = lower(?)
        """,
        (value, value, value),
    ).fetchone()
    conn.close()
    return row


def _get_user_by_id(user_id: int) -> Optional[sqlite3.Row]:
    conn = get_db_connection()
    row = conn.execute(
        "SELECT * FROM users WHERE id = ?",
        (int(user_id),),
    ).fetchone()
    conn.close()
    return row


def _get_user_by_public_id(public_id: str) -> Optional[sqlite3.Row]:
    value = (public_id or "").strip()
    if not value:
        return None
    conn = get_db_connection()
    row = conn.execute(
        "SELECT * FROM users WHERE lower(user_id) = lower(?)",
        (value,),
    ).fetchone()
    conn.close()
    return row


def _get_user_by_email(email: str) -> Optional[sqlite3.Row]:
    value = (email or "").strip()
    if not value:
        return None
    conn = get_db_connection()
    row = conn.execute(
        "SELECT * FROM users WHERE lower(email) = lower(?)",
        (value,),
    ).fetchone()
    conn.close()
    return row


def _display_name(user: sqlite3.Row) -> str:
    if not user:
        return ""
    return (
        (user["name"] if "name" in user.keys() else None)
        or (user["user_id"] if "user_id" in user.keys() else None)
        or user["username"]
        or ""
    )


def _row_value(row: Optional[sqlite3.Row], key: str) -> Optional[str]:
    if not row:
        return None
    return row[key] if key in row.keys() else None


def _get_admin_emails() -> List[str]:
    raw = os.getenv(ADMIN_EMAILS_ENV, "")
    return [email.strip().lower() for email in raw.split(",") if email.strip()]


def _is_admin_email(email: Optional[str]) -> bool:
    if not email:
        return False
    return email.strip().lower() in set(_get_admin_emails())


def _ensure_admin_role(user: Optional[sqlite3.Row]) -> bool:
    if not user:
        return False
    email = _row_value(user, "email")
    if not _is_admin_email(email):
        return False
    role = _row_value(user, "role") or "user"
    if role == "admin":
        return True
    user_id = _row_value(user, "id")
    if not user_id:
        return True
    conn = get_db_connection()
    conn.execute("UPDATE users SET role = 'admin' WHERE id = ?", (int(user_id),))
    conn.commit()
    conn.close()
    return True


def _send_email(to_email: str, subject: str, body: str, html_body: Optional[str] = None) -> bool:
    email_mode = (os.getenv(EMAIL_MODE_ENV) or "").strip().lower()
    brevo_key = (os.getenv(BREVO_API_KEY_ENV) or "").strip()

    if brevo_key and (email_mode in {"", "auto", "brevo", "brevo_api"}):
        ok = _send_email_via_brevo_api(to_email, subject, body, html_body)
        if ok:
            return True
        if email_mode in {"brevo", "brevo_api"}:
            return False

    host = (os.getenv(SMTP_HOST_ENV) or "").strip()
    port = int((os.getenv(SMTP_PORT_ENV, "0") or "0").strip() or 0)
    user = (os.getenv(SMTP_USER_ENV) or "").strip()
    password = os.getenv(SMTP_PASSWORD_ENV) or ""
    sender = ((os.getenv(SMTP_SENDER_ENV) or "").strip() or user)
    missing: List[str] = []
    if not host:
        missing.append(SMTP_HOST_ENV)
    if not port:
        missing.append(SMTP_PORT_ENV)
    if not sender:
        missing.append(SMTP_SENDER_ENV)
    if missing:
        logger.warning(
            "SMTP not configured (%s); email to %s skipped",
            ",".join(missing),
            to_email,
        )
        return False

    message = EmailMessage()
    message["From"] = sender
    message["To"] = to_email
    message["Subject"] = subject
    message.set_content(body)
    if html_body:
        message.add_alternative(html_body, subtype="html")

    use_tls = str(os.getenv(SMTP_USE_TLS_ENV, "true")).lower() in {"1", "true", "yes"}
    try:
        connect_host = host
        if host and not re.match(r"^\d+\.\d+\.\d+\.\d+$", host):
            try:
                infos = socket.getaddrinfo(host, port, family=socket.AF_INET, type=socket.SOCK_STREAM)
                if infos:
                    connect_host = infos[0][4][0]
            except Exception:
                connect_host = host

        use_ssl = int(port) == 465
        if use_ssl:
            with smtplib.SMTP_SSL(connect_host, port, timeout=10) as smtp:
                smtp.ehlo()
                if user and password:
                    smtp.login(user, password)
                smtp.send_message(message)
        else:
            with smtplib.SMTP(connect_host, port, timeout=10) as smtp:
                smtp.ehlo()
                if use_tls:
                    smtp.starttls()
                    smtp.ehlo()
                if user and password:
                    smtp.login(user, password)
                smtp.send_message(message)
        return True
    except Exception as exc:
        logger.exception(
            "Failed to send email via SMTP host=%s port=%s tls=%s sender=%s user_configured=%s to=%s: %s",
            host,
            port,
            use_tls,
            sender,
            bool(user),
            to_email,
            exc,
        )
        return False


def _send_email_via_brevo_api(to_email: str, subject: str, body: str, html_body: Optional[str] = None) -> bool:
    api_key = (os.getenv(BREVO_API_KEY_ENV) or "").strip()
    sender_email = (os.getenv(SMTP_SENDER_ENV) or os.getenv(SMTP_USER_ENV) or "").strip()
    if not api_key or not sender_email:
        return False

    payload = {
        "sender": {"email": sender_email, "name": "Time Tracker Pro"},
        "to": [{"email": (to_email or "").strip()}],
        "subject": subject,
        "textContent": body,
    }
    if html_body:
        payload["htmlContent"] = html_body

    try:
        resp = requests.post(
            "https://api.brevo.com/v3/smtp/email",
            headers={"api-key": api_key, "content-type": "application/json"},
            json=payload,
            timeout=15,
        )
        if 200 <= int(resp.status_code) < 300:
            return True
        logger.error(
            "Brevo API email failed status=%s to=%s response=%s",
            resp.status_code,
            to_email,
            (resp.text or "")[:500],
        )
        return False
    except Exception as exc:
        logger.exception("Brevo API email exception to=%s: %s", to_email, exc)
        return False


def _create_verification_code(user_id: int) -> str:
    code = f"{secrets.randbelow(1_000_000):06d}"
    code_hash = generate_password_hash(code)
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=VERIFICATION_CODE_TTL_MINUTES)).isoformat()

    conn = get_db_connection()
    conn.execute("DELETE FROM email_verifications WHERE user_id = ?", (int(user_id),))
    conn.execute(
        """
        INSERT INTO email_verifications (user_id, code_hash, expires_at, attempts)
        VALUES (?, ?, ?, 0)
        """,
        (int(user_id), code_hash, expires_at),
    )
    conn.commit()
    conn.close()
    return code


def _has_active_verification(user_id: int) -> bool:
    conn = get_db_connection()
    row = conn.execute(
        "SELECT expires_at, verified_at FROM email_verifications WHERE user_id = ? ORDER BY id DESC LIMIT 1",
        (int(user_id),),
    ).fetchone()
    conn.close()
    if not row:
        return False
    if row["verified_at"]:
        return False
    try:
        expires_at = datetime.fromisoformat(row["expires_at"])
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
    except Exception:
        return False
    return datetime.now(timezone.utc) <= expires_at


def _send_verification_email(user: sqlite3.Row) -> bool:
    if not user or not _row_value(user, "email"):
        return False
    code = _create_verification_code(int(user["id"]))
    greeting = (
        _row_value(user, "name")
        or _row_value(user, "user_id")
        or _row_value(user, "username")
        or "there"
    )
    subject = "Verify your Time Tracker Pro account"
    body = (
        f"Hi {greeting},\n\n"
        f"Your verification code is: {code}\n\n"
        "Enter this code on the verification screen to activate your account.\n\n"
        "If you did not request this, you can ignore this email."
    )
    html_body = f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta name="color-scheme" content="light">
    <meta name="supported-color-schemes" content="light">
</head>
<body style="margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background: #F5EDE1;">
    <table width="100%" cellpadding="0" cellspacing="0" style="background: #F5EDE1; padding: 20px 10px;">
        <tr>
            <td align="center">
                <table width="600" cellpadding="0" cellspacing="0" style="max-width: 600px; width: 100%; background: #FFFFFF; border: 1px solid #D4C5B0; border-radius: 16px; box-shadow: 0 4px 12px rgba(0, 0, 0, 0.08); overflow: hidden;">
                    <tr>
                        <td style="background: #1F2937; padding: 24px 20px; text-align: center;">
                            <h1 style="margin: 0; color: #FCD34D; font-size: 24px; font-weight: 700; letter-spacing: -0.3px;">Time Tracker Pro</h1>
                        </td>
                    </tr>
                    <tr>
                        <td style="padding: 32px 24px;">
                            <h2 style="margin: 0 0 16px 0; color: #1F2937; font-size: 20px; font-weight: 600;">Welcome, {greeting}!</h2>
                            <p style="margin: 0 0 24px 0; color: #374151; font-size: 15px; line-height: 1.5;">Thank you for joining Time Tracker Pro. To activate your account, please use the verification code below:</p>
                            <div style="background: #1F2937; border-radius: 12px; padding: 24px 20px; text-align: center; margin: 24px 0;">
                                <div style="color: #D1D5DB; font-size: 11px; text-transform: uppercase; letter-spacing: 1.2px; margin-bottom: 10px; font-weight: 600;">Your Verification Code</div>
                                <div style="color: #FCD34D; font-size: 36px; font-weight: 700; letter-spacing: 6px; font-family: 'Courier New', Courier, monospace;">{code}</div>
                            </div>
                            <p style="margin: 24px 0 0 0; color: #4B5563; font-size: 14px; line-height: 1.5;">Enter this code on the verification screen to complete your registration. This code will expire in 10 minutes.</p>
                            <div style="margin-top: 32px; padding-top: 24px; border-top: 1px solid #E5E7EB;">
                                <p style="margin: 0; color: #6B7280; font-size: 13px; line-height: 1.4;">If you didn't request this verification, you can safely ignore this email.</p>
                            </div>
                        </td>
                    </tr>
                    <tr>
                        <td style="background: #F9FAFB; padding: 20px 24px; text-align: center; border-top: 1px solid #E5E7EB;">
                            <p style="margin: 0; color: #6B7280; font-size: 12px;">&copy; 2026 Time Tracker Pro. All rights reserved.</p>
                        </td>
                    </tr>
                </table>
            </td>
        </tr>
    </table>
</body>
</html>
    """
    ok = _send_email(_row_value(user, "email") or "", subject, body, html_body)
    if not ok and str(os.getenv(LOG_VERIFICATION_CODES_ENV, "")).lower() in {"1", "true", "yes"}:
        logger.warning(
            "Verification code (email send failed) user_id=%s email=%s code=%s",
            _row_value(user, "id"),
            _row_value(user, "email"),
            code,
        )
    return ok


def _send_welcome_email(user: sqlite3.Row) -> bool:
    if not user or not _row_value(user, "email"):
        return False
    greeting = (
        _row_value(user, "name")
        or _row_value(user, "user_id")
        or _row_value(user, "username")
        or "there"
    )
    subject = "Welcome to Time Tracker Pro"
    body = (
        f"Hi {greeting},\n\n"
        "Welcome to Time Tracker Pro! Your account is verified and ready to use.\n\n"
        "Log in to start tracking your time and insights."
    )
    html_body = f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta name="color-scheme" content="light">
    <meta name="supported-color-schemes" content="light">
</head>
<body style="margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background: #F5EDE1;">
    <table width="100%" cellpadding="0" cellspacing="0" style="background: #F5EDE1; padding: 20px 10px;">
        <tr>
            <td align="center">
                <table width="600" cellpadding="0" cellspacing="0" style="max-width: 600px; width: 100%; background: #FFFFFF; border: 1px solid #D4C5B0; border-radius: 16px; box-shadow: 0 4px 12px rgba(0, 0, 0, 0.08); overflow: hidden;">
                    <tr>
                        <td style="background: #1F2937; padding: 24px 20px; text-align: center;">
                            <h1 style="margin: 0; color: #FCD34D; font-size: 24px; font-weight: 700; letter-spacing: -0.3px;">Time Tracker Pro</h1>
                        </td>
                    </tr>
                    <tr>
                        <td style="padding: 32px 24px;">
                            <h2 style="margin: 0 0 12px 0; color: #1F2937; font-size: 20px; font-weight: 600;">Welcome, {greeting}!</h2>
                            <p style="margin: 0 0 18px 0; color: #374151; font-size: 15px; line-height: 1.5;">Your account is now verified and ready to use.</p>
                            <div style="background: #1F2937; border-radius: 12px; padding: 18px 20px; text-align: center; margin: 18px 0;">
                                <div style="color: #FCD34D; font-size: 15px; font-weight: 600; letter-spacing: 0.4px;">Start tracking your time today</div>
                            </div>
                            <p style="margin: 18px 0 0 0; color: #4B5563; font-size: 14px; line-height: 1.5;">Log in to Time Tracker Pro to capture your focus, tasks, and insights.</p>
                        </td>
                    </tr>
                    <tr>
                        <td style="background: #F9FAFB; padding: 20px 24px; text-align: center; border-top: 1px solid #E5E7EB;">
                            <p style="margin: 0; color: #6B7280; font-size: 12px;">&copy; 2026 Time Tracker Pro. All rights reserved.</p>
                        </td>
                    </tr>
                </table>
            </td>
        </tr>
    </table>
</body>
</html>
    """
    return _send_email(_row_value(user, "email") or "", subject, body, html_body)


def _verify_email_code(user_id: int, code: str) -> Tuple[bool, str]:
    conn = get_db_connection()
    row = conn.execute(
        "SELECT * FROM email_verifications WHERE user_id = ? ORDER BY id DESC LIMIT 1",
        (int(user_id),),
    ).fetchone()
    if not row:
        conn.close()
        return False, "No verification request found. Please resend the code."

    attempts = int(row["attempts"] or 0)
    if attempts >= VERIFICATION_MAX_ATTEMPTS:
        conn.close()
        return False, "Too many attempts. Please resend a new code."

    try:
        expires_at = datetime.fromisoformat(row["expires_at"])
    except Exception:
        expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)

    if datetime.now(timezone.utc) > expires_at:
        conn.close()
        return False, "Verification code expired. Please resend."

    if not check_password_hash(row["code_hash"], (code or "").strip()):
        conn.execute(
            "UPDATE email_verifications SET attempts = ? WHERE id = ?",
            (attempts + 1, int(row["id"])),
        )
        conn.commit()
        conn.close()
        return False, "Invalid verification code."

    verified_at = datetime.now(timezone.utc).isoformat()
    conn.execute("UPDATE users SET is_verified = 1 WHERE id = ?", (int(user_id),))
    conn.execute(
        "UPDATE email_verifications SET verified_at = ? WHERE id = ?",
        (verified_at, int(row["id"])),
    )
    conn.commit()
    conn.close()
    return True, "Your account is now verified."


def get_user_settings(user_id: int) -> Dict[str, Optional[str]]:
    conn = get_db_connection()
    row = conn.execute(
        "SELECT sheety_endpoint, sheety_token FROM user_settings WHERE user_id = ?",
        (int(user_id),),
    ).fetchone()
    conn.close()
    if not row:
        return {"sheety_endpoint": None, "sheety_token": None}
    return {"sheety_endpoint": row["sheety_endpoint"], "sheety_token": row["sheety_token"]}


def upsert_user_settings(user_id: int, sheety_endpoint: Optional[str], sheety_token: Optional[str]) -> None:
    endpoint = (sheety_endpoint or "").strip() or None
    token = (sheety_token or "").strip() or None
    conn = get_db_connection()
    conn.execute(
        """
        INSERT INTO user_settings (user_id, sheety_endpoint, sheety_token)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            sheety_endpoint = excluded.sheety_endpoint,
            sheety_token = excluded.sheety_token
        """,
        (int(user_id), endpoint, token),
    )
    conn.commit()
    conn.close()


# --- CALL THE FUNCTION HERE ---
init_db()
# ------------------------------

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


def _should_sync(user_id: int, now: Optional[datetime] = None) -> bool:
    current = now or datetime.now(timezone.utc)
    last_fail = _LAST_SYNC_FAIL_TS_BY_USER.get(int(user_id))
    if last_fail and (current - last_fail).total_seconds() < SYNC_FAIL_COOLDOWN_SECONDS:
        return False
    last_ok = _LAST_SYNC_TS_BY_USER.get(int(user_id))
    if last_ok is None:
        return True
    return (current - last_ok).total_seconds() > SYNC_INTERVAL_SECONDS


def sync_cloud_data(user_id: int, force: bool = False) -> None:

    if os.getenv("DISABLE_CLOUD_SYNC") and not force:
        logger.info("Cloud sync disabled by env; skipping (force=%s)", force)
        return
    settings = get_user_settings(int(user_id))
    user_url = (settings.get("sheety_endpoint") or "").strip()
    env_url = (os.getenv(SHEETY_ENDPOINT_ENV) or "").strip()
    allow_env_fallback = _get_user_count() <= 1
    url = user_url or (env_url if allow_env_fallback else "")
    if not url:
        if allow_env_fallback:
            logger.info("SHEETY_ENDPOINT not configured; skipping cloud sync")
        else:
            logger.info("Sheety endpoint not set for user_id=%s; multi-user mode requires per-user settings", int(user_id))
        return

    headers: Dict[str, str] = {}
    token = (settings.get("sheety_token") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    now = datetime.now(timezone.utc)
    if not force and not _should_sync(int(user_id), now):
        return

    try:
        response = requests.get(url, headers=headers, timeout=15)
        # Check for 402 Payment Required before raising
        if response.status_code == 402:
            logger.warning("Sheety API returned 402 Payment Required. Skipping sync for extended period. "
                         "Consider upgrading Sheety plan or using alternative sync method.")
            # Set a longer backoff (24 hours) for payment required errors
            _LAST_SYNC_FAIL_TS_BY_USER[int(user_id)] = now - timedelta(hours=23)
            return
        response.raise_for_status()
        data = response.json()
        if "sheet1" not in data:
            logger.warning("Unexpected payload from Sheety endpoint: missing 'sheet1'")
            return
        cloud_df = pd.DataFrame(data["sheet1"])
        if "id" in cloud_df.columns:
            cloud_df = cloud_df.sort_values("id")

        def _row_text(row: pd.Series, keys: Iterable[str]) -> str:
            for key in keys:
                value = row.get(key)
                if value is None or (isinstance(value, float) and pd.isna(value)):
                    continue
                text = str(value).strip()
                if text and text.lower() != "nan":
                    return text
            return ""

        parser = TimeLogParser()
        parsed_rows: List[Dict[str, Any]] = []
        previous_end: Optional[datetime] = None

        for _, row in cloud_df.iterrows():
            col_a = _row_text(
                row,
                (
                    "colA",
                    "startTime",
                    "rawStart",
                    "start_time",
                    "start time",
                ),
            )
            col_b = _row_text(
                row,
                (
                    "colB",
                    "taskDetails",
                    "rawTask",
                    "task",
                    "task_details",
                    "task details",
                ),
            )
            client_now = _row_text(
                row,
                (
                    "loggedTime",
                    "logged_time",
                    "logged time",
                ),
            )

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
        conn.execute("DELETE FROM logs WHERE user_id = ?", (int(user_id),))

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
                    imp,
                    user_id
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
                    1 if p["urg"] else 0,
                    1 if p["imp"] else 0,
                    int(user_id),
                ),
            )

        conn.commit()
        conn.close()
        _LAST_SYNC_TS_BY_USER[int(user_id)] = now
        logger.info("Sync complete: strict end times enforced for %s rows", len(final_rows))
    except requests.RequestException as exc:
        _LAST_SYNC_FAIL_TS_BY_USER[int(user_id)] = now
        # Handle 402 Payment Required specially - don't spam logs
        if hasattr(exc, 'response') and exc.response is not None and exc.response.status_code == 402:
            # For 402 errors, extend the backoff period significantly
            # This prevents constant retry attempts when Sheety quota is exhausted
            logger.warning("Sheety API returned 402 Payment Required. Skipping sync for extended period. "
                         "Consider upgrading Sheety plan or using alternative sync method.")
            # Set a longer backoff (24 hours) for payment required errors
            _LAST_SYNC_FAIL_TS_BY_USER[int(user_id)] = now - timedelta(hours=23)
            return
        logger.error("Network error during cloud sync: %s", exc)
    except Exception as exc:
        _LAST_SYNC_FAIL_TS_BY_USER[int(user_id)] = now
        logger.exception("Unexpected sync error: %s", exc)


def fetch_local_data(user_id: int) -> pd.DataFrame:
    try:
        conn = get_db_connection()
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
    if get_current_user_id() is not None:
        return True
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


@app.route("/login", methods=["GET", "POST"])
def login():
    if get_current_user_id() is not None:
        return redirect(url_for("dashboard"))

    next_url = _safe_next_url(request.args.get("next"))
    error: Optional[str] = None
    show_verify = False

    if request.method == "POST":
        identifier = (request.form.get("identifier") or request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        remember = bool(request.form.get("remember"))
        next_url = _safe_next_url(request.form.get("next")) or next_url

        if not identifier or not password:
            error = "Username, email, or user ID and password are required."
        else:
            user = _get_user_by_username(identifier)
            if not user or not check_password_hash(_row_value(user, "password_hash"), password):
                error = "Invalid credentials."
            elif not int(_row_value(user, "is_verified") or 0):
                error = "Please verify your email before logging in."
                session["pending_user_id"] = int(_row_value(user, "id"))
                session["pending_remember"] = remember
                if _send_verification_email(user):
                    session["verification_success"] = "Verification code sent."
                else:
                    session["verification_error"] = "Unable to send email. Check SMTP settings."
                return redirect(url_for("verify_email", next=next_url or ""))
            else:
                session.clear()
                session["user_id"] = int(_row_value(user, "id"))
                session.permanent = remember

                settings = get_user_settings(int(_row_value(user, "id")))
                multi_user = _get_user_count() > 1
                if multi_user:
                    if not settings.get("sheety_endpoint"):
                        return redirect(url_for("settings"))
                else:
                    if not (settings.get("sheety_endpoint") or os.getenv(SHEETY_ENDPOINT_ENV)):
                        return redirect(url_for("settings"))

                return redirect(next_url or url_for("dashboard"))

    if request.args.get("verify"):
        show_verify = True

    return render_template("login.html", error=error, next=next_url or "", show_verify=show_verify)


@app.route("/register", methods=["GET", "POST"])
def register():
    if get_current_user_id() is not None:
        return redirect(url_for("dashboard"))

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
        elif _get_user_by_public_id(public_user_id) or _get_user_by_username(public_user_id):
            error = "That user ID is already taken."
        elif _get_user_by_email(email):
            error = "An account with that email already exists."
        else:
            created_at = datetime.now(timezone.utc).isoformat()
            password_hash = generate_password_hash(password)
            role = "admin" if _is_admin_email(email) else "user"
            conn = get_db_connection()
            existing_users = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
            cur = conn.execute(
                """
                INSERT INTO users (username, user_id, name, email, password_hash, role, is_verified, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 0, ?)
                """,
                (public_user_id, public_user_id, name, email, password_hash, role, created_at),
            )
            new_user_id = int(cur.lastrowid)

            if int(existing_users) == 0:
                conn.execute("UPDATE logs SET user_id = ? WHERE user_id = 0", (new_user_id,))

            conn.commit()
            conn.close()

            user = _get_user_by_id(new_user_id)
            _send_verification_email(user)
            session.clear()
            session["pending_user_id"] = new_user_id
            session["pending_remember"] = remember

            return redirect(url_for("verify_email"))

    return render_template("register.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    resp = redirect(url_for("login"))
    try:
        resp.set_cookie("tt_token", "", expires=0)
    except Exception:
        pass
    return resp


@app.route("/verify-email", methods=["GET", "POST"])
def verify_email():
    pending_id = session.get("pending_user_id")
    if not pending_id:
        return redirect(url_for("login"))
    user = _get_user_by_id(int(pending_id))
    if not user:
        session.pop("pending_user_id", None)
        return redirect(url_for("register"))

    error: Optional[str] = session.pop("verification_error", None)
    success: Optional[str] = session.pop("verification_success", None)

    if request.method == "POST":
        if request.form.get("action") == "resend":
            if _send_verification_email(user):
                success = "Verification code resent."
            else:
                error = "Unable to send email. Check SMTP settings."
        else:
            code = (request.form.get("code") or "").strip()
            if not code:
                error = "Please enter the verification code."
            else:
                ok, message = _verify_email_code(int(user["id"]), code)
                if ok:
                    _send_welcome_email(user)
                    session.pop("pending_user_id", None)
                    remember = bool(session.pop("pending_remember", False))
                    session["user_id"] = int(user["id"])
                    session.permanent = remember
                    return redirect(url_for("settings"))
                error = message
    elif not _has_active_verification(int(user["id"])):
        if _send_verification_email(user):
            success = "Verification code sent."
        else:
            error = "Unable to send email. Check SMTP settings."

    return render_template(
        "verify_email.html",
        error=error,
        success=success,
        email=_row_value(user, "email"),
        name=_display_name(user),
    )


@app.route("/admin/users")
@admin_required
def admin_users():
    admin_row = _get_user_by_id(int(get_current_user_id() or 0))
    current_user = {
        "id": int(admin_row["id"]) if admin_row else 0,
        "display_name": _display_name(admin_row),
        "role": (_row_value(admin_row, "role") if admin_row else "admin"),
    }
    conn = get_db_connection()
    users = conn.execute(
        """
        SELECT id, name, user_id, email, role, is_verified, created_at
        FROM users
        ORDER BY created_at DESC
        """
    ).fetchall()
    conn.close()
    return render_template(
        "admin_users.html",
        users=users,
        current_user=current_user,
        error=request.args.get("error"),
        success=request.args.get("success"),
    )


@app.route("/admin/delete-user", methods=["POST"])
@admin_required
def admin_delete_user():
    target_id = (request.form.get("user_id") or "").strip()
    message = (request.form.get("message") or "").strip()
    admin_id = int(get_current_user_id() or 0)

    if not target_id:
        return redirect(url_for("admin_users", error="Missing user selection."))
    try:
        target_id_int = int(target_id)
    except Exception:
        return redirect(url_for("admin_users", error="Invalid user id."))

    if target_id_int == admin_id:
        return redirect(url_for("admin_users", error="You cannot delete your own account."))

    if not message:
        return redirect(url_for("admin_users", error="Please include a message to the user."))

    user = _get_user_by_id(target_id_int)
    if not user:
        return redirect(url_for("admin_users", error="User not found."))
    if (_row_value(user, "role") or "user") == "admin":
        return redirect(url_for("admin_users", error="You cannot delete another admin."))

    conn = get_db_connection()
    conn.execute("DELETE FROM logs WHERE user_id = ?", (target_id_int,))
    conn.execute("DELETE FROM user_settings WHERE user_id = ?", (target_id_int,))
    conn.execute("DELETE FROM email_verifications WHERE user_id = ?", (target_id_int,))
    conn.execute("DELETE FROM users WHERE id = ?", (target_id_int,))
    conn.commit()
    conn.close()

    greeting = _display_name(user) or "there"
    subject = "Your Time Tracker Pro account was removed"
    body = (
        f"Hi {greeting},\n\n"
        "Your Time Tracker Pro account has been removed by an administrator.\n\n"
        "Message from admin:\n"
        f"{message}\n\n"
        "If you believe this is a mistake, please reply to this email."
    )
    html_body = f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta name="color-scheme" content="light">
    <meta name="supported-color-schemes" content="light">
</head>
<body style="margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background: #F5EDE1;">
    <table width="100%" cellpadding="0" cellspacing="0" style="background: #F5EDE1; padding: 20px 10px;">
        <tr>
            <td align="center">
                <table width="600" cellpadding="0" cellspacing="0" style="max-width: 600px; width: 100%; background: #FFFFFF; border: 1px solid #D4C5B0; border-radius: 16px; box-shadow: 0 4px 12px rgba(0, 0, 0, 0.08); overflow: hidden;">
                    <tr>
                        <td style="background: #1F2937; padding: 24px 20px; text-align: center;">
                            <h1 style="margin: 0; color: #FCD34D; font-size: 24px; font-weight: 700; letter-spacing: -0.3px;">Time Tracker Pro</h1>
                        </td>
                    </tr>
                    <tr>
                        <td style="padding: 32px 24px;">
                            <h2 style="margin: 0 0 16px 0; color: #1F2937; font-size: 20px; font-weight: 600;">Account Removal Notice</h2>
                            <p style="margin: 0 0 16px 0; color: #374151; font-size: 15px; line-height: 1.5;">Hi {greeting},</p>
                            <p style="margin: 0 0 24px 0; color: #374151; font-size: 15px; line-height: 1.5;">Your Time Tracker Pro account has been removed by an administrator.</p>
                            <div style="background: #FEF3C7; border-left: 4px solid #D97706; border-radius: 8px; padding: 16px; margin: 24px 0;">
                                <div style="color: #92400E; font-size: 11px; text-transform: uppercase; letter-spacing: 0.8px; margin-bottom: 8px; font-weight: 600;">Message from Administrator</div>
                                <p style="margin: 0; color: #1F2937; font-size: 14px; line-height: 1.5; white-space: pre-wrap;">{message}</p>
                            </div>
                            <div style="margin-top: 32px; padding-top: 24px; border-top: 1px solid #E5E7EB;">
                                <p style="margin: 0; color: #4B5563; font-size: 14px; line-height: 1.5;">If you believe this is a mistake, please reply to this email to contact support.</p>
                            </div>
                        </td>
                    </tr>
                    <tr>
                        <td style="background: #F9FAFB; padding: 20px 24px; text-align: center; border-top: 1px solid #E5E7EB;">
                            <p style="margin: 0; color: #6B7280; font-size: 12px;">&copy; 2026 Time Tracker Pro. All rights reserved.</p>
                        </td>
                    </tr>
                </table>
            </td>
        </tr>
    </table>
</body>
</html>
    """
    email_sent = (
        _send_email(_row_value(user, "email"), subject, body, html_body)
        if _row_value(user, "email")
        else False
    )
    if not email_sent:
        return redirect(url_for("admin_users", success="User deleted. Email failed to send."))

    return redirect(url_for("admin_users", success="User deleted and notified."))


@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    user_id = int(get_current_user_id() or 0)
    current = get_user_settings(user_id)

    if request.method == "POST":
        sheety_endpoint = request.form.get("sheety_endpoint")
        sheety_token = request.form.get("sheety_token")
        upsert_user_settings(user_id, sheety_endpoint, sheety_token)
        return redirect(url_for("dashboard"))

    user_row = _get_user_by_id(user_id)
    current_user = {
        "id": user_id,
        "display_name": _display_name(user_row),
        "role": (_row_value(user_row, "role") if user_row else "user"),
    }
    is_admin = bool(
        user_row
        and (
            (_row_value(user_row, "role") or "user") == "admin"
            or _is_admin_email(_row_value(user_row, "email"))
        )
    )
    allow_env_fallback = _get_user_count() <= 1
    env_sheety = (os.getenv(SHEETY_ENDPOINT_ENV) or "").strip() if allow_env_fallback else ""
    return render_template(
        "settings.html",
        settings=current,
        current_user=current_user,
        env_sheety=env_sheety,
        is_admin=is_admin,
    )


@app.route("/")
@login_required
def dashboard():
    user_id = int(get_current_user_id() or 0)
    sync_cloud_data(user_id)
    date_str = request.args.get("date")
    raw_period = request.args.get("period", "day")
    period = parse_period_param(raw_period)

    selected_date = parse_date_param(date_str)

    df = fetch_local_data(user_id)
    if df.empty:
        try:
            sync_cloud_data(user_id, force=True)
            df = fetch_local_data(user_id)
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

    user_row = _get_user_by_id(user_id)
    is_admin = bool(
        user_row
        and (
            (_row_value(user_row, "role") or "user") == "admin"
            or _is_admin_email(_row_value(user_row, "email"))
        )
    )
    current_user = {
        "id": user_id,
        "display_name": _display_name(user_row),
        "role": (_row_value(user_row, "role") if user_row else "user"),
        "is_admin": is_admin,
    }

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
        current_user=current_user,
        is_admin=is_admin,
    ))
    expected = os.getenv(API_AUTH_TOKEN_ENV)
    if expected and _get_user_count() <= 1:
        try:
            resp.set_cookie("tt_token", expected, httponly=True, samesite="Lax")
        except Exception:
            pass
    return resp


@app.route("/graphs")
@login_required
def graphs():
    user_id = int(get_current_user_id() or 0)
    user_row = _get_user_by_id(user_id)
    current_user = {
        "id": user_id,
        "display_name": _display_name(user_row),
        "role": (_row_value(user_row, "role") if user_row else "user"),
    }
    is_admin = bool(
        user_row
        and (
            (_row_value(user_row, "role") or "user") == "admin"
            or _is_admin_email(_row_value(user_row, "email"))
        )
    )
    return render_template("graphs.html", current_user=current_user, is_admin=is_admin)


@app.route("/api/graph-data")
@api_or_login_required
def graph_data():
    user_id = int(getattr(g, "user_id", 0) or 0)
    metric = (request.args.get("metric") or "total").strip().lower()
    raw_days = request.args.get("days") or "30"
    try:
        days = max(1, min(int(raw_days), 365))
    except Exception:
        days = 30

    end_date = parse_date_param(request.args.get("end"))
    start_date = end_date - timedelta(days=days - 1)

    df = fetch_local_data(user_id)
    if df.empty:
        labels = [
            (start_date + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)
        ]
        return jsonify(
            {
                "labels": labels,
                "values": [0 for _ in labels],
                "total_hours": 0,
                "avg_hours": 0,
                "max_hours": 0,
            }
        )

    if "primary_tag" not in df.columns:
        df["primary_tag"] = df["tag"].apply(primary_special_tag)

    min_complete_minutes = 10 * 60
    latest_total = df.loc[df["date"] == end_date, "duration"].sum()
    if latest_total < min_complete_minutes:
        end_date = end_date - timedelta(days=1)
        start_date = end_date - timedelta(days=days - 1)

    df = df[(df["date"] >= start_date) & (df["date"] <= end_date)]
    if df.empty:
        labels = [
            (start_date + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)
        ]
        return jsonify(
            {
                "labels": labels,
                "values": [0 for _ in labels],
                "total_hours": 0,
                "avg_hours": 0,
                "max_hours": 0,
            }
        )

    if metric in GRAPH_TAG_MAP:
        tag_name = GRAPH_TAG_MAP[metric]
        df = df[df["primary_tag"] == tag_name]
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
    labels = []
    values = []
    for i in range(days):
        day = start_date + timedelta(days=i)
        labels.append(day.strftime("%Y-%m-%d"))
        minutes = float(daily.get(day, 0) if hasattr(daily, "get") else 0)
        values.append(round(minutes / 60.0, 2))

    total_hours = round(sum(values), 2)
    avg_hours = round(total_hours / days, 2) if days else 0
    max_hours = round(max(values) if values else 0, 2)

    return jsonify(
        {
            "labels": labels,
            "values": values,
            "total_hours": total_hours,
            "avg_hours": avg_hours,
            "max_hours": max_hours,
        }
    )


@app.route("/api/tasks")
@api_or_login_required
def get_tasks():
    user_id = int(getattr(g, "user_id", 0) or 0)

    date_str = request.args.get("date")
    raw_period = request.args.get("period", "day")
    period = parse_period_param(raw_period)
    filter_type = request.args.get("filter", "all")
    tag_name = request.args.get("tag", "")

    selected_date = parse_date_param(date_str)

    df = fetch_local_data(user_id)
    if df.empty:
        try:
            sync_cloud_data(user_id, force=True)
            df = fetch_local_data(user_id)
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
@api_or_login_required
def get_tags():
    user_id = int(getattr(g, "user_id", 0) or 0)

    date_str = request.args.get("date")
    raw_period = request.args.get("period", "day")
    period = parse_period_param(raw_period)

    selected_date = parse_date_param(date_str)

    df = fetch_local_data(user_id)
    if df.empty:
        try:
            sync_cloud_data(user_id, force=True)
            df = fetch_local_data(user_id)
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
        headers = dict(request.headers)
        if _get_user_count() > 1:
            return "Disabled in multi-user mode", 403
        if not _token_is_valid(headers):
            return "Unauthorized", 401
        resolved_user_id = resolve_request_user_id(headers)
        if resolved_user_id is not None:
            try:
                sync_cloud_data(int(resolved_user_id))
            except Exception:
                pass
        return send_file(DB_NAME, as_attachment=True)
    except Exception as e:
        return f"Error downloading DB: {e}"

@app.route("/sync-status")
def sync_status():
    try:
        headers = dict(request.headers)
        user_id = get_current_user_id()
        if user_id is None and _token_is_valid(headers):
            user_id = resolve_request_user_id(headers)
        if user_id is not None:
            settings = get_user_settings(int(user_id))
            allow_env_fallback = _get_user_count() <= 1
            status = {
                "sheety_configured": bool((settings.get("sheety_endpoint") or "").strip() or ((os.getenv(SHEETY_ENDPOINT_ENV) or "").strip() if allow_env_fallback else "")),
                "disable_cloud_sync": bool(os.getenv("DISABLE_CLOUD_SYNC")),
                "last_sync": _LAST_SYNC_TS_BY_USER.get(int(user_id)).isoformat() if _LAST_SYNC_TS_BY_USER.get(int(user_id)) else None,
                "last_sync_fail": _LAST_SYNC_FAIL_TS_BY_USER.get(int(user_id)).isoformat() if _LAST_SYNC_FAIL_TS_BY_USER.get(int(user_id)) else None,
                "sync_interval_seconds": SYNC_INTERVAL_SECONDS,
                "fail_cooldown_seconds": SYNC_FAIL_COOLDOWN_SECONDS,
                "db_exists": os.path.exists(DB_NAME),
            }
            return jsonify(status)
        status = {
            "sheety_configured": bool(os.getenv(SHEETY_ENDPOINT_ENV)),
            "disable_cloud_sync": bool(os.getenv("DISABLE_CLOUD_SYNC")),
            "last_sync": None,
            "last_sync_fail": None,
            "sync_interval_seconds": SYNC_INTERVAL_SECONDS,
            "fail_cooldown_seconds": SYNC_FAIL_COOLDOWN_SECONDS,
            "db_exists": os.path.exists(DB_NAME),
        }
        return jsonify(status)
    except Exception as exc:
        logger.exception("Sync status error: %s", exc)
        return jsonify({"error": str(exc)}), 500

@app.route("/sync-now")
@api_or_login_required
def sync_now():
    try:
        user_id = int(getattr(g, "user_id", 0) or 0)
        sync_cloud_data(user_id, force=True)
        return jsonify({"status": "success", "message": "Synced latest data from cloud"})
    except Exception as exc:
        logger.exception("Sync now error: %s", exc)
        return jsonify({"status": "error", "message": str(exc)}), 500


@app.route("/hard-reset")
@api_or_login_required
def hard_reset():
    try:
        user_id = int(getattr(g, "user_id", 0) or 0)
        sync_cloud_data(user_id, force=True)
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
@api_or_login_required
def import_csv():
    """Import data from CSV format (Google Sheets export)"""
    user_id = int(getattr(g, "user_id", 0) or 0)
    
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
        
        # Save to database (replace only the current user's rows)
        conn = get_db_connection()
        conn.execute("DELETE FROM logs WHERE user_id = ?", (int(user_id),))
        
        inserted_count = 0
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
                    1 if p["urg"] else 0,
                    1 if p["imp"] else 0,
                    int(user_id),
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

'''

from time_tracker_pro import create_app as _create_app

app = _create_app()


if __name__ == '__main__':
    # host='0.0.0.0' allows access from other devices on the network
    app.run(debug=True, host='0.0.0.0', port=8080)
