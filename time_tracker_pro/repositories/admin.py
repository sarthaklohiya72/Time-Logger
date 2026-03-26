from __future__ import annotations

from typing import List, Optional

import sqlite3

from ..db import get_db_connection


def list_users(db_name: str) -> List[sqlite3.Row]:
    conn = get_db_connection(db_name)
    users = conn.execute(
        """
        SELECT id, name, user_id, email, role, is_verified, created_at
        FROM users
        ORDER BY created_at DESC
        """
    ).fetchall()
    conn.close()
    return list(users or [])


def delete_user_cascade(db_name: str, target_id: int) -> None:
    conn = get_db_connection(db_name)
    conn.execute("DELETE FROM logs WHERE user_id = ?", (int(target_id),))
    conn.execute("DELETE FROM user_settings WHERE user_id = ?", (int(target_id),))
    conn.execute("DELETE FROM email_verifications WHERE user_id = ?", (int(target_id),))
    conn.execute("DELETE FROM users WHERE id = ?", (int(target_id),))
    conn.commit()
    conn.close()
