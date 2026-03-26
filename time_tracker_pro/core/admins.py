from __future__ import annotations

import os
from typing import List, Optional


ADMIN_EMAILS_ENV = "ADMIN_EMAILS"


def get_admin_emails() -> List[str]:
    raw = os.getenv(ADMIN_EMAILS_ENV, "")
    return [email.strip().lower() for email in raw.split(",") if email.strip()]


def is_admin_email(email: Optional[str]) -> bool:
    if not email:
        return False
    return email.strip().lower() in set(get_admin_emails())
