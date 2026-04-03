from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from ..db import get_db_connection


logger = logging.getLogger(__name__)


def get_user_api_accounts(db_name: str, user_id: int):
    """Get all API accounts for a user, ordered by priority."""
    conn = get_db_connection(db_name)
    cursor = conn.execute(
        """
        SELECT id, user_id, account_email, api_base_url, api_token, priority,
               is_active, last_tested, last_success, failure_count, created_at
        FROM sheety_api_accounts
        WHERE user_id = ?
        ORDER BY priority ASC
        """,
        (user_id,)
    )
    accounts = cursor.fetchall()
    conn.close()
    return accounts


def get_active_api_account(db_name: str, user_id: int):
    """Get the currently active API account for a user."""
    conn = get_db_connection(db_name)
    cursor = conn.execute(
        """
        SELECT id, user_id, account_email, api_base_url, api_token, priority,
               is_active, last_tested, last_success, failure_count, created_at
        FROM sheety_api_accounts
        WHERE user_id = ? AND is_active = 1
        ORDER BY priority ASC
        LIMIT 1
        """,
        (user_id,)
    )
    account = cursor.fetchone()
    conn.close()
    return account


def get_api_account_by_id(db_name: str, account_id: int, user_id: int):
    """Get a specific API account by ID, ensuring it belongs to the user."""
    conn = get_db_connection(db_name)
    cursor = conn.execute(
        """
        SELECT id, user_id, account_email, api_base_url, api_token, priority,
               is_active, last_tested, last_success, failure_count, created_at
        FROM sheety_api_accounts
        WHERE id = ? AND user_id = ?
        """,
        (account_id, user_id)
    )
    account = cursor.fetchone()
    conn.close()
    return account


def create_api_account(
    db_name: str,
    user_id: int,
    account_email: str,
    api_base_url: str,
    api_token: Optional[str] = None
) -> int:
    """Create a new API account for a user."""
    conn = get_db_connection(db_name)
    
    # Get the next priority number
    cursor = conn.execute(
        "SELECT COALESCE(MAX(priority), 0) + 1 FROM sheety_api_accounts WHERE user_id = ?",
        (user_id,)
    )
    next_priority = cursor.fetchone()[0]
    
    # If this is the first account, make it active
    cursor = conn.execute(
        "SELECT COUNT(*) FROM sheety_api_accounts WHERE user_id = ?",
        (user_id,)
    )
    is_first = cursor.fetchone()[0] == 0
    
    created_at = datetime.now(timezone.utc).isoformat()
    
    cursor = conn.execute(
        """
        INSERT INTO sheety_api_accounts (user_id, account_email, api_base_url, api_token, priority, is_active, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (user_id, account_email, api_base_url, api_token, next_priority, 1 if is_first else 0, created_at)
    )
    account_id = cursor.lastrowid
    conn.commit()
    conn.close()
    
    return account_id


def delete_api_account(db_name: str, account_id: int, user_id: int) -> bool:
    """Delete an API account."""
    conn = get_db_connection(db_name)
    
    # Check if this is the active account
    cursor = conn.execute(
        "SELECT is_active, priority FROM sheety_api_accounts WHERE id = ? AND user_id = ?",
        (account_id, user_id)
    )
    account = cursor.fetchone()
    
    if not account:
        conn.close()
        return False
    
    was_active = account[0]
    deleted_priority = account[1]
    
    # Delete the account
    conn.execute("DELETE FROM sheety_api_accounts WHERE id = ? AND user_id = ?", (account_id, user_id))
    
    # Reorder priorities
    conn.execute(
        "UPDATE sheety_api_accounts SET priority = priority - 1 WHERE user_id = ? AND priority > ?",
        (user_id, deleted_priority)
    )
    
    # If this was active, activate the next one
    if was_active:
        cursor = conn.execute(
            "SELECT id FROM sheety_api_accounts WHERE user_id = ? ORDER BY priority ASC LIMIT 1",
            (user_id,)
        )
        next_account = cursor.fetchone()
        if next_account:
            conn.execute("UPDATE sheety_api_accounts SET is_active = 1 WHERE id = ?", (next_account[0],))
    
    conn.commit()
    conn.close()
    return True


def set_active_account(db_name: str, account_id: int, user_id: int) -> bool:
    """Set a specific account as active."""
    conn = get_db_connection(db_name)
    
    # Deactivate all accounts for this user
    conn.execute("UPDATE sheety_api_accounts SET is_active = 0 WHERE user_id = ?", (user_id,))
    
    # Activate the specified account
    cursor = conn.execute(
        "UPDATE sheety_api_accounts SET is_active = 1 WHERE id = ? AND user_id = ?",
        (account_id, user_id)
    )
    
    success = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return success


def update_account_test_result(
    db_name: str,
    account_id: int,
    success: bool,
    user_id: Optional[int] = None
) -> None:
    """Update the test result for an account."""
    conn = get_db_connection(db_name)
    now = datetime.now(timezone.utc).isoformat()
    
    if success:
        conn.execute(
            """
            UPDATE sheety_api_accounts
            SET last_tested = ?, last_success = ?, failure_count = 0
            WHERE id = ?
            """,
            (now, now, account_id)
        )
    else:
        conn.execute(
            """
            UPDATE sheety_api_accounts
            SET last_tested = ?, failure_count = failure_count + 1
            WHERE id = ?
            """,
            (now, account_id)
        )
    
    conn.commit()
    conn.close()


def get_next_fallback_account(db_name: str, user_id: int, current_priority: int):
    """Get the next account to try after the current one fails."""
    conn = get_db_connection(db_name)
    cursor = conn.execute(
        """
        SELECT id, user_id, account_email, api_base_url, api_token, priority,
               is_active, last_tested, last_success, failure_count, created_at
        FROM sheety_api_accounts
        WHERE user_id = ? AND priority > ?
        ORDER BY priority ASC
        LIMIT 1
        """,
        (user_id, current_priority)
    )
    account = cursor.fetchone()
    
    # If no account with higher priority, wrap around to the first one
    if not account:
        cursor = conn.execute(
            """
            SELECT id, user_id, account_email, api_base_url, api_token, priority,
                   is_active, last_tested, last_success, failure_count, created_at
            FROM sheety_api_accounts
            WHERE user_id = ?
            ORDER BY priority ASC
            LIMIT 1
            """,
            (user_id,)
        )
        account = cursor.fetchone()
    
    conn.close()
    return account
