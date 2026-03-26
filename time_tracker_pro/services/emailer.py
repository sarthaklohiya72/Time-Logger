from __future__ import annotations

import logging
import os
import re
import smtplib
import socket
from email.message import EmailMessage
from typing import List, Optional

import requests


logger = logging.getLogger(__name__)

SMTP_HOST_ENV = "SMTP_HOST"
SMTP_PORT_ENV = "SMTP_PORT"
SMTP_USER_ENV = "SMTP_USER"
SMTP_PASSWORD_ENV = "SMTP_PASSWORD"
SMTP_SENDER_ENV = "SMTP_SENDER"
SMTP_USE_TLS_ENV = "SMTP_USE_TLS"
BREVO_API_KEY_ENV = "BREVO_API_KEY"
EMAIL_MODE_ENV = "EMAIL_MODE"


def send_email(to_email: str, subject: str, body: str, html_body: Optional[str] = None) -> bool:
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
