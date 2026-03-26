from __future__ import annotations

from typing import Optional

from flask import session


def safe_next_url(raw: Optional[str]) -> Optional[str]:
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
