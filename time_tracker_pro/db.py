from __future__ import annotations

import os
import sqlite3


def get_db_connection(db_name: str) -> sqlite3.Connection:
    try:
        parent = os.path.dirname(db_name)
        if parent:
            os.makedirs(parent, exist_ok=True)
    except Exception:
        pass
    conn = sqlite3.connect(db_name)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_name: str) -> None:
    conn = get_db_connection(db_name)

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            user_id TEXT UNIQUE,
            name TEXT,
            email TEXT UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT DEFAULT 'user',
            is_verified INTEGER DEFAULT 0,
            created_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_settings (
            user_id INTEGER PRIMARY KEY,
            sheety_endpoint TEXT,
            sheety_token TEXT,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            start_date TEXT,
            start_time TEXT,
            end_date TEXT,
            end_time TEXT,
            task TEXT,
            duration INTEGER,
            tags TEXT,
            urg INTEGER,
            imp INTEGER,
            user_id INTEGER DEFAULT 0
        )
        """
    )

    cols = [row["name"] for row in conn.execute("PRAGMA table_info(logs)").fetchall()]
    if "user_id" not in cols:
        conn.execute("ALTER TABLE logs ADD COLUMN user_id INTEGER DEFAULT 0")
        conn.execute("UPDATE logs SET user_id = 0 WHERE user_id IS NULL")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_user_start ON logs(user_id, start_date, start_time)")

    user_cols = [row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()]
    if "user_id" not in user_cols:
        conn.execute("ALTER TABLE users ADD COLUMN user_id TEXT")
    if "name" not in user_cols:
        conn.execute("ALTER TABLE users ADD COLUMN name TEXT")
    if "email" not in user_cols:
        conn.execute("ALTER TABLE users ADD COLUMN email TEXT")
    if "role" not in user_cols:
        conn.execute("ALTER TABLE users ADD COLUMN role TEXT DEFAULT 'user'")
    if "is_verified" not in user_cols:
        conn.execute("ALTER TABLE users ADD COLUMN is_verified INTEGER DEFAULT 0")
        conn.execute("UPDATE users SET is_verified = 1 WHERE is_verified IS NULL")

    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_user_id ON users(user_id)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users(email)")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS email_verifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            code_hash TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            attempts INTEGER DEFAULT 0,
            verified_at TEXT,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
        """
    )

    conn.commit()
    conn.close()
