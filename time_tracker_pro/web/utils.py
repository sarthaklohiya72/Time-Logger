from __future__ import annotations

import hashlib
from typing import Optional

from flask import request, session


def _ua_hash(value: str) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


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
    expected = session.get("ua_hash")
    current = _ua_hash(request.headers.get("User-Agent") or "")
    if not expected or expected != current:
        session.clear()
        return None
    try:
        return int(uid)
    except Exception:
        return None
