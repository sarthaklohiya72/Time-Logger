from __future__ import annotations

from ..db import get_db_connection


def adopt_orphan_logs(db_name: str, new_user_id: int) -> None:
    conn = get_db_connection(db_name)
    conn.execute("UPDATE logs SET user_id = ? WHERE user_id = 0", (int(new_user_id),))
    conn.commit()
    conn.close()
