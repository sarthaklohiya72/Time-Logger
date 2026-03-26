from __future__ import annotations

import sqlite3
from typing import Optional

from ..db import get_db_connection


def get_user_count(db_name: str) -> int:
    conn = get_db_connection(db_name)
    row = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()
    conn.close()
    return int(row["c"] if row else 0)


def get_user_by_id(db_name: str, user_id: int) -> Optional[sqlite3.Row]:
    conn = get_db_connection(db_name)
    row = conn.execute(
        "SELECT * FROM users WHERE id = ?",
        (int(user_id),),
    ).fetchone()
    conn.close()
    return row


def get_user_by_public_id(db_name: str, public_id: str) -> Optional[sqlite3.Row]:
    value = (public_id or "").strip()
    if not value:
        return None
    conn = get_db_connection(db_name)
    row = conn.execute(
        "SELECT * FROM users WHERE lower(user_id) = lower(?)",
        (value,),
    ).fetchone()
    conn.close()
    return row


def get_user_by_email(db_name: str, email: str) -> Optional[sqlite3.Row]:
    value = (email or "").strip()
    if not value:
        return None
    conn = get_db_connection(db_name)
    row = conn.execute(
        "SELECT * FROM users WHERE lower(email) = lower(?)",
        (value,),
    ).fetchone()
    conn.close()
    return row


def get_user_by_username_or_id_or_email(db_name: str, identifier: str) -> Optional[sqlite3.Row]:
    value = (identifier or "").strip()
    if not value:
        return None
    conn = get_db_connection(db_name)
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


def update_user_role(db_name: str, user_id: int, role: str) -> None:
    conn = get_db_connection(db_name)
    conn.execute("UPDATE users SET role = ? WHERE id = ?", (role, int(user_id)))
    conn.commit()
    conn.close()


def mark_user_verified(db_name: str, user_id: int) -> None:
    conn = get_db_connection(db_name)
    conn.execute("UPDATE users SET is_verified = 1 WHERE id = ?", (int(user_id),))
    conn.commit()
    conn.close()
