from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

from werkzeug.security import check_password_hash, generate_password_hash

from ..db import get_db_connection
from ..core.rows import display_name, row_value
from .emailer import send_email


logger = logging.getLogger(__name__)

LOG_VERIFICATION_CODES_ENV = "LOG_VERIFICATION_CODES"
VERIFICATION_CODE_TTL_MINUTES = int(os.getenv("VERIFICATION_CODE_TTL_MINUTES", "10"))
VERIFICATION_MAX_ATTEMPTS = int(os.getenv("VERIFICATION_MAX_ATTEMPTS", "5"))


def create_verification_code(db_name: str, user_id: int, purpose: str = "verify_email") -> str:
    code = f"{secrets.randbelow(1_000_000):06d}"
    code_hash = generate_password_hash(code)
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=VERIFICATION_CODE_TTL_MINUTES)).isoformat()

    conn = get_db_connection(db_name)
    conn.execute(
        "DELETE FROM email_verifications WHERE user_id = ? AND purpose = ?",
        (int(user_id), purpose),
    )
    conn.execute(
        """
        INSERT INTO email_verifications (user_id, code_hash, expires_at, attempts, purpose)
        VALUES (?, ?, ?, 0, ?)
        """,
        (int(user_id), code_hash, expires_at, purpose),
    )
    conn.commit()
    conn.close()
    return code


def has_active_verification(db_name: str, user_id: int, purpose: str = "verify_email") -> bool:
    conn = get_db_connection(db_name)
    row = conn.execute(
        """
        SELECT expires_at, verified_at FROM email_verifications
        WHERE user_id = ? AND purpose = ?
        ORDER BY id DESC LIMIT 1
        """,
        (int(user_id), purpose),
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


def _verification_copy(purpose: str) -> Tuple[str, str]:
    copy_map = {
        "verify_email": (
            "Verify your Time Tracker Pro account",
            "Enter this code to activate your account.",
        ),
        "forgot_password": (
            "Reset your Time Tracker Pro password",
            "Enter this code to reset your password.",
        ),
        "login_otp": (
            "Your Time Tracker Pro login code",
            "Enter this code to log in securely.",
        ),
        "change_password": (
            "Confirm your password change",
            "Enter this code to confirm your password change.",
        ),
        "change_username": (
            "Confirm your username change",
            "Enter this code to confirm your username update.",
        ),
        "change_email_new": (
            "Confirm your new email address",
            "Enter this code to confirm your new email address.",
        ),
        "change_email_old": (
            "Confirm your email change",
            "Enter this code to confirm the change from your current email address.",
        ),
    }
    return copy_map.get(purpose, copy_map["verify_email"])


def send_verification_email(
    db_name: str,
    user_row,
    purpose: str = "verify_email",
    email_override: Optional[str] = None,
) -> bool:
    target_email = (email_override or row_value(user_row, "email") or "").strip()
    if not user_row or not target_email:
        return False

    code = create_verification_code(db_name, int(user_row["id"]), purpose)
    greeting = display_name(user_row) or "there"
    subject, instruction = _verification_copy(purpose)

    body = (
        f"Hi {greeting},\n\n"
        f"Your verification code is: {code}\n\n"
        f"{instruction}\n\n"
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
                            <h2 style="margin: 0 0 16px 0; color: #1F2937; font-size: 20px; font-weight: 600;">Hi {greeting},</h2>
                            <p style="margin: 0 0 24px 0; color: #374151; font-size: 15px; line-height: 1.5;">{instruction}</p>
                            <div style="background: #1F2937; border-radius: 12px; padding: 24px 20px; text-align: center; margin: 24px 0;">
                                <div style="color: #D1D5DB; font-size: 11px; text-transform: uppercase; letter-spacing: 1.2px; margin-bottom: 10px; font-weight: 600;">Your Verification Code</div>
                                <div style="color: #FCD34D; font-size: 36px; font-weight: 700; letter-spacing: 6px; font-family: 'Courier New', Courier, monospace;">{code}</div>
                            </div>
                            <p style="margin: 24px 0 0 0; color: #4B5563; font-size: 14px; line-height: 1.5;">This code will expire in {VERIFICATION_CODE_TTL_MINUTES} minutes.</p>
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

    ok = send_email(target_email, subject, body, html_body)
    if not ok and str(os.getenv(LOG_VERIFICATION_CODES_ENV, "")).lower() in {"1", "true", "yes"}:
        logger.warning(
            "Verification code (email send failed) user_id=%s email=%s code=%s",
            row_value(user_row, "id"),
            target_email,
            code,
        )
    return ok


def verify_email_code(
    db_name: str,
    user_id: int,
    code: str,
    purpose: str = "verify_email",
    mark_verified: bool = False,
) -> Tuple[bool, str]:
    conn = get_db_connection(db_name)
    row = conn.execute(
        """
        SELECT * FROM email_verifications
        WHERE user_id = ? AND purpose = ?
        ORDER BY id DESC LIMIT 1
        """,
        (int(user_id), purpose),
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
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
    except Exception:
        expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)

    if datetime.now(timezone.utc) > expires_at:
        conn.close()
        return False, "Verification code expired. Please resend."

    if not check_password_hash(row["code_hash"], (code or "").strip()):
        conn.execute("UPDATE email_verifications SET attempts = ? WHERE id = ?", (attempts + 1, int(row["id"])))
        conn.commit()
        conn.close()
        return False, "Invalid verification code."

    verified_at = datetime.now(timezone.utc).isoformat()
    if mark_verified:
        conn.execute("UPDATE users SET is_verified = 1 WHERE id = ?", (int(user_id),))
    conn.execute(
        "UPDATE email_verifications SET verified_at = ? WHERE id = ?",
        (verified_at, int(row["id"])),
    )
    conn.commit()
    conn.close()
    if mark_verified or purpose == "verify_email":
        return True, "Your account is now verified."
    return True, "Verification successful."
