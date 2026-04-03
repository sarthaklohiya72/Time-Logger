from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

import pandas as pd
import requests

from ..core.tags import filter_special_tags
from ..repositories.logs import replace_logs_for_user
from ..repositories.settings import get_user_settings
from ..repositories.sheety_accounts import get_user_api_accounts
from ..repositories.users import get_user_count
from .parser import TimeLogParser


logger = logging.getLogger(__name__)

SHEETY_ENDPOINT_ENV = "SHEETY_ENDPOINT"
SYNC_INTERVAL_SECONDS = int(os.getenv("SYNC_INTERVAL_SECONDS", "300"))
SYNC_FAIL_COOLDOWN_SECONDS = int(os.getenv("SYNC_FAIL_COOLDOWN_SECONDS", "1800"))

_LAST_SYNC_TS_BY_USER: Dict[int, datetime] = {}
_LAST_SYNC_FAIL_TS_BY_USER: Dict[int, datetime] = {}


def should_sync(user_id: int, now: Optional[datetime] = None) -> bool:
    current = now or datetime.now(timezone.utc)
    last_fail = _LAST_SYNC_FAIL_TS_BY_USER.get(int(user_id))
    if last_fail and (current - last_fail).total_seconds() < SYNC_FAIL_COOLDOWN_SECONDS:
        return False
    last_ok = _LAST_SYNC_TS_BY_USER.get(int(user_id))
    if last_ok is None:
        return True
    return (current - last_ok).total_seconds() > SYNC_INTERVAL_SECONDS


def sync_cloud_data(db_name: str, user_id: int, force: bool = False) -> Optional[Dict[str, str]]:
    if os.getenv("DISABLE_CLOUD_SYNC") and not force:
        logger.info("Cloud sync disabled by env; skipping (force=%s)", force)
        return None

    now = datetime.now(timezone.utc)
    if not force and not should_sync(int(user_id), now):
        return None

    failover_notice: Optional[Dict[str, str]] = None

    try:
        payload: Optional[Dict[str, Any]] = None
        accounts = get_user_api_accounts(db_name, int(user_id))
        if accounts:
            from .sheety_failover import SheetyFailoverService

            service = SheetyFailoverService(db_name, int(user_id))
            success, data, error = service.make_request("GET")
            failover_notice = service.get_failover_notification()
            if not success:
                _LAST_SYNC_FAIL_TS_BY_USER[int(user_id)] = now
                logger.warning("Sheety sync failed user_id=%s error=%s", int(user_id), error)
                return failover_notice
            if isinstance(data, dict):
                payload = data
        else:
            settings = get_user_settings(db_name, int(user_id))
            user_url = (settings.get("sheety_endpoint") or "").strip()
            env_url = (os.getenv(SHEETY_ENDPOINT_ENV) or "").strip()
            allow_env_fallback = get_user_count(db_name) <= 1
            url = user_url or (env_url if allow_env_fallback else "")
            if not url:
                return failover_notice

            headers: Dict[str, str] = {}
            token = (settings.get("sheety_token") or "").strip()
            if token:
                headers["Authorization"] = f"Bearer {token}"

            response = requests.get(url, headers=headers, timeout=15)
            if response.status_code == 402:
                _LAST_SYNC_FAIL_TS_BY_USER[int(user_id)] = now - timedelta(hours=23)
                return failover_notice
            response.raise_for_status()
            payload = response.json()

        if not isinstance(payload, dict) or not payload:
            return failover_notice

        sheet_key = "sheet1" if "sheet1" in payload else next(iter(payload.keys()), None)
        if not sheet_key:
            return failover_notice

        cloud_df = pd.DataFrame(payload.get(sheet_key) or [])
        if "id" in cloud_df.columns:
            cloud_df = cloud_df.sort_values("id")

        def row_text(row: pd.Series, keys: Iterable[str]) -> str:
            for key in keys:
                value = row.get(key)
                if value is None or (isinstance(value, float) and pd.isna(value)):
                    continue
                text = str(value).strip()
                if text and text.lower() != "nan":
                    return text
            return ""

        parser = TimeLogParser()
        parsed_rows: List[Dict[str, Any]] = []
        previous_end: Optional[datetime] = None

        for _, row in cloud_df.iterrows():
            sheety_id = row.get("id")
            if isinstance(sheety_id, float) and pd.isna(sheety_id):
                sheety_id = None
            elif sheety_id is not None:
                try:
                    sheety_id = int(sheety_id)
                except (TypeError, ValueError):
                    sheety_id = None
            log_entry = row_text(
                row,
                (
                    "logEntry",
                    "log_entry",
                    "log entry",
                    "entry",
                    "taskDetails",
                    "task",
                    "task_details",
                    "task details",
                    "rawTask",
                    "raw_task",
                    "colB",
                ),
            )
            client_now = row_text(
                row,
                (
                    "loggedTime",
                    "logged_time",
                    "logged time",
                ),
            )

            try:
                parsed = parser.parse_row(log_entry, client_now, previous_end)
            except Exception as exc:
                logger.warning(
                    "Skipping invalid row during cloud sync user_id=%s logged_time=%s log_entry=%r error=%s",
                    int(user_id),
                    client_now,
                    log_entry,
                    exc,
                )
                continue

            if sheety_id is not None:
                parsed["sheety_id"] = sheety_id
            parsed_rows.append(parsed)
            previous_end = parsed["end_dt"]

        for i in range(1, len(parsed_rows)):
            current = parsed_rows[i]
            prev = parsed_rows[i - 1]
            if current["start_dt"] < prev["end_dt"]:
                parsed_rows[i - 1]["end_dt"] = current["start_dt"]

        final_rows: List[Dict[str, Any]] = []
        for p in parsed_rows:
            if p["start_dt"].date() < p["end_dt"].date():
                midnight = datetime.combine(p["end_dt"].date(), datetime.min.time())
                part1 = p.copy()
                part1["end_dt"] = midnight
                part2 = p.copy()
                part2["start_dt"] = midnight
                final_rows.append(part1)
                final_rows.append(part2)
            else:
                final_rows.append(p)

        replace_logs_for_user(db_name, int(user_id), final_rows)
        _LAST_SYNC_TS_BY_USER[int(user_id)] = now
        return failover_notice
    except requests.RequestException as exc:
        _LAST_SYNC_FAIL_TS_BY_USER[int(user_id)] = now
        if hasattr(exc, "response") and exc.response is not None and exc.response.status_code == 402:
            _LAST_SYNC_FAIL_TS_BY_USER[int(user_id)] = now - timedelta(hours=23)
            return failover_notice
        logger.error("Network error during cloud sync: %s", exc)
    except Exception as exc:
        _LAST_SYNC_FAIL_TS_BY_USER[int(user_id)] = now
        logger.exception("Unexpected sync error: %s", exc)
    return failover_notice


def sync_status_payload(db_name: str, user_id: Optional[int]) -> Dict[str, Any]:
    allow_env_fallback = get_user_count(db_name) <= 1
    if user_id is not None:
        settings = get_user_settings(db_name, int(user_id))
        accounts = get_user_api_accounts(db_name, int(user_id))
        configured = bool(
            accounts
            or (settings.get("sheety_endpoint") or "").strip()
            or ((os.getenv(SHEETY_ENDPOINT_ENV) or "").strip() if allow_env_fallback else "")
        )
        return {
            "sheety_configured": configured,
            "disable_cloud_sync": bool(os.getenv("DISABLE_CLOUD_SYNC")),
            "last_sync": _LAST_SYNC_TS_BY_USER.get(int(user_id)).isoformat() if _LAST_SYNC_TS_BY_USER.get(int(user_id)) else None,
            "last_sync_fail": _LAST_SYNC_FAIL_TS_BY_USER.get(int(user_id)).isoformat() if _LAST_SYNC_FAIL_TS_BY_USER.get(int(user_id)) else None,
            "sync_interval_seconds": SYNC_INTERVAL_SECONDS,
            "fail_cooldown_seconds": SYNC_FAIL_COOLDOWN_SECONDS,
            "db_exists": os.path.exists(db_name),
        }

    return {
        "sheety_configured": bool(os.getenv(SHEETY_ENDPOINT_ENV)),
        "disable_cloud_sync": bool(os.getenv("DISABLE_CLOUD_SYNC")),
        "last_sync": None,
        "last_sync_fail": None,
        "sync_interval_seconds": SYNC_INTERVAL_SECONDS,
        "fail_cooldown_seconds": SYNC_FAIL_COOLDOWN_SECONDS,
        "db_exists": os.path.exists(db_name),
    }
