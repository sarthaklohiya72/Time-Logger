from __future__ import annotations

from typing import Dict, Optional

from ..db import get_db_connection


def get_user_settings(db_name: str, user_id: int) -> Dict[str, Optional[str]]:
    conn = get_db_connection(db_name)
    row = conn.execute(
        "SELECT sheety_endpoint, sheety_token FROM user_settings WHERE user_id = ?",
        (int(user_id),),
    ).fetchone()
    conn.close()
    if not row:
        return {"sheety_endpoint": None, "sheety_token": None}
    return {"sheety_endpoint": row["sheety_endpoint"], "sheety_token": row["sheety_token"]}


def upsert_user_settings(db_name: str, user_id: int, sheety_endpoint: Optional[str], sheety_token: Optional[str]) -> None:
    endpoint = (sheety_endpoint or "").strip() or None
    token = (sheety_token or "").strip() or None
    conn = get_db_connection(db_name)
    conn.execute(
        """
        INSERT INTO user_settings (user_id, sheety_endpoint, sheety_token)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            sheety_endpoint = excluded.sheety_endpoint,
            sheety_token = excluded.sheety_token
        """,
        (int(user_id), endpoint, token),
    )
    conn.commit()
    conn.close()
