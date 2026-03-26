from __future__ import annotations

from ..core.rows import display_name, row_value
from .emailer import send_email


def send_welcome_email(user_row) -> bool:
    if not user_row or not row_value(user_row, "email"):
        return False

    greeting = display_name(user_row) or "there"
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

    return send_email(row_value(user_row, "email") or "", subject, body, html_body)
