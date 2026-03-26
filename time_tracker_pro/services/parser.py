from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple

import pandas as pd


class TimeLogParser:
    def __init__(self) -> None:
        self.time_pattern = re.compile(
            r"^(\d{1,2})(?:[:\s]?(\d{2}))?\s*([ap]m)?\s*",
            re.IGNORECASE,
        )

    def parse_time_string(self, text_str: str, ref_date: datetime) -> Tuple[Optional[datetime], str]:
        if not isinstance(text_str, str) or not text_str:
            return None, text_str
        match = self.time_pattern.match(text_str)
        if match:
            hour, minute = int(match.group(1)), int(match.group(2) or 0)
            ampm = match.group(3).lower() if match.group(3) else None
            if ampm == "pm" and hour < 12:
                hour += 12
            if ampm == "am" and hour == 12:
                hour = 0
            dt = ref_date.replace(hour=hour, minute=minute, second=0, microsecond=0)
            return dt, text_str[match.end() :].strip()
        return None, text_str

    def parse_row(
        self,
        col_a: str,
        col_b: str,
        client_now_str: str,
        previous_end_dt: Optional[datetime],
    ) -> Dict[str, Any]:
        try:
            client_now = pd.to_datetime(client_now_str)
        except Exception:
            client_now = datetime.now()

        explicit_time_a, _ = self.parse_time_string(col_a, client_now)
        explicit_time_b, remaining_text = self.parse_time_string(col_b, client_now)

        raw_text = remaining_text if remaining_text else (col_b if col_b else "Unspecified")
        task_name, tag = raw_text.strip(), ""
        is_urg, is_imp = False, False

        tags = []

        def apply_token(raw_token: str) -> None:
            nonlocal is_urg, is_imp
            token = re.sub(r"[^A-Za-z]+", "", raw_token).lower()
            if not token:
                return
            if token in {"urg", "urgent"}:
                is_urg = True
                return
            if token in {"imp", "important"}:
                is_imp = True
                return
            if token in {"work", "necessity", "soul", "rest", "waste"}:
                canonical = token.title()
                if canonical not in tags:
                    tags.append(canonical)
                return

        if "." in raw_text:
            before, after = raw_text.split(".", 1)
            task_name = before.strip() or task_name
            meta = after.strip()
            if meta:
                for tok in re.split(r"[\s,]+", meta):
                    apply_token(tok)
        else:
            for tok in re.split(r"\s+", raw_text.strip()):
                apply_token(tok)

        tag = ", ".join(tags) if tags else "Waste"

        end_dt = explicit_time_b if explicit_time_b else client_now

        if end_dt > client_now + timedelta(hours=1):
            end_dt -= timedelta(days=1)

        start_dt: Optional[datetime] = None

        if explicit_time_a:
            start_dt = explicit_time_a

        if not start_dt:
            if previous_end_dt:
                gap_hours = (end_dt - previous_end_dt).total_seconds() / 3600
                if gap_hours > 16:
                    start_dt = end_dt - timedelta(minutes=30)
                else:
                    start_dt = previous_end_dt
            else:
                start_dt = end_dt - timedelta(minutes=30)

        return {
            "start_dt": start_dt,
            "end_dt": end_dt,
            "task": task_name,
            "tag": tag,
            "urg": is_urg,
            "imp": is_imp,
        }
