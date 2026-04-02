from __future__ import annotations

from typing import Optional

from ..db import get_db_connection


def get_app_setting(db_name: str, key: str) -> Optional[str]:
    conn = get_db_connection(db_name)
    row = conn.execute(
        "SELECT value FROM app_settings WHERE key = ?",
        (key,),
    ).fetchone()
    conn.close()
    if not row:
        return None
    return row["value"]


def upsert_app_setting(db_name: str, key: str, value: Optional[str]) -> None:
    conn = get_db_connection(db_name)
    conn.execute(
        """
        INSERT INTO app_settings (key, value)
        VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET
            value = excluded.value
        """,
        (key, value),
    )
    conn.commit()
    conn.close()
