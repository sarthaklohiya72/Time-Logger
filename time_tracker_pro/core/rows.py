from __future__ import annotations

import sqlite3
from typing import Optional


def row_value(row: Optional[sqlite3.Row], key: str) -> Optional[str]:
    if not row:
        return None
    return row[key] if key in row.keys() else None


def display_name(user: Optional[sqlite3.Row]) -> str:
    if not user:
        return ""
    return (
        (user["name"] if "name" in user.keys() else None)
        or (user["user_id"] if "user_id" in user.keys() else None)
        or user["username"]
        or ""
    )
