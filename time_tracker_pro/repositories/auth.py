from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import sqlite3

from ..db import get_db_connection


def create_user(
    db_name: str,
    username: str,
    user_id: str,
    name: str,
    email: str,
    password_hash: str,
    role: str,
    is_verified: int = 0,
    created_at: Optional[str] = None,
) -> int:
    created_at_value = created_at or datetime.now(timezone.utc).isoformat()
    conn = get_db_connection(db_name)
    cur = conn.execute(
        """
        INSERT INTO users (username, user_id, name, email, password_hash, role, is_verified, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (username, user_id, name, email, password_hash, role, int(is_verified), created_at_value),
    )
    new_user_id = int(cur.lastrowid)
    conn.commit()
    conn.close()
    return new_user_id
