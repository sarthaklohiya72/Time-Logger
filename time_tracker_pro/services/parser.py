from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple

import pandas as pd
from dateutil import parser as date_parser


class TimeLogParser:
    def __init__(self) -> None:
        self.time_pattern = re.compile(
            r"^(\d{1,2})(?:[:\s]?(\d{2}))?\s*([ap]m)?\s*",
            re.IGNORECASE,
        )
        self.time_token_pattern = re.compile(
            r"^(\d{1,2})(?:[:\s]?(\d{2}))?\s*([ap]m)?$",
            re.IGNORECASE,
        )
        self.date_token_pattern = re.compile(r"^(\d{1,2})[./-](\d{1,2})(?:[./-](\d{2,4}))?$")

    def _infer_12h_time(self, hour: int, minute: int, ref_date: datetime) -> Tuple[int, int]:
        ref_minutes = ref_date.hour * 60 + ref_date.minute
        if hour == 12:
            candidates = [(0, minute), (12, minute)]
        else:
            candidates = [(hour, minute), (hour + 12, minute)]

        best = candidates[0]
        best_delta = 24 * 60
        for h, m in candidates:
            cand_minutes = h * 60 + m
            delta = ref_minutes - cand_minutes
            if delta < 0:
                delta += 24 * 60
            if delta < best_delta:
                best_delta = delta
                best = (h, m)
        return best

    def _month_from_token(self, token: str) -> Optional[int]:
        normalized = (token or "").strip().lower().strip(".,")
        if not normalized:
            return None
        if normalized.isdigit():
            month = int(normalized)
            return month if 1 <= month <= 12 else None
        months = {
            "jan": 1,
            "january": 1,
            "feb": 2,
            "february": 2,
            "mar": 3,
            "march": 3,
            "apr": 4,
            "april": 4,
            "may": 5,
            "jun": 6,
            "june": 6,
            "jul": 7,
            "july": 7,
            "aug": 8,
            "august": 8,
            "sep": 9,
            "sept": 9,
            "september": 9,
            "oct": 10,
            "october": 10,
            "nov": 11,
            "november": 11,
            "dec": 12,
            "december": 12,
        }
        return months.get(normalized)

    def _parse_split_date(self, tokens: list[str], idx: int, ref_date: datetime) -> tuple[Optional[datetime.date], int]:
        if idx >= len(tokens) or idx + 1 >= len(tokens):
            return None, 0
        day_token = (tokens[idx] or "").strip().strip(".,")
        if not re.fullmatch(r"\d{1,2}", day_token):
            return None, 0
        day = int(day_token)
        if not (1 <= day <= 31):
            return None, 0
        month = self._month_from_token(tokens[idx + 1])
        if month is None:
            return None, 0

        consumed = 2
        year = ref_date.year
        if idx + 2 < len(tokens):
            year_token = (tokens[idx + 2] or "").strip().strip(".,")
            if re.fullmatch(r"\d{2,4}", year_token):
                year = int(year_token)
                if len(year_token) == 2:
                    year += 2000
                consumed = 3
        if consumed == 2 and month > ref_date.month + 1:
            year -= 1
        try:
            return datetime(year, month, day).date(), consumed
        except ValueError:
            return None, 0

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

    def _parse_time_token(self, token: str, ref_date: datetime) -> Optional[Tuple[int, int]]:
        raw = (token or "").strip().lower().strip(".,")
        if not raw:
            return None

        if re.fullmatch(r"\d{3,4}", raw):
            if len(raw) == 3:
                hour = int(raw[0])
                minute = int(raw[1:])
            else:
                hour = int(raw[:2])
                minute = int(raw[2:])
            if hour > 23 or minute > 59:
                return None
            return hour, minute

        if re.fullmatch(r"\d{1,2}\.\d{2}([ap]m)?", raw, re.IGNORECASE):
            raw = raw.replace(".", ":", 1)

        m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?([ap]m)?", raw)
        if not m:
            return None

        hour = int(m.group(1))
        minute = int(m.group(2) or 0)
        ampm = (m.group(3) or "").lower() or None

        if minute > 59:
            return None

        if ampm:
            if not (1 <= hour <= 12):
                return None
            if ampm == "pm" and hour < 12:
                hour += 12
            if ampm == "am" and hour == 12:
                hour = 0
            return hour, minute

        if hour > 23:
            return None

        if m.group(2) is None:
            return hour, minute

        if hour >= 13:
            return hour, minute
        inferred_hour, inferred_minute = self._infer_12h_time(hour, minute, ref_date)
        return inferred_hour, inferred_minute

    def _parse_date_token(self, token: str, ref_date: datetime) -> Optional[datetime.date]:
        match = self.date_token_pattern.match(token)
        if not match:
            if re.search(r"[ap]m", token, re.IGNORECASE) or ":" in token:
                return None
            if re.search(r"[A-Za-z]", token) or re.search(r"[./-]", token) or re.fullmatch(r"\d{4}", token):
                try:
                    parsed = date_parser.parse(token, default=ref_date, dayfirst=True, fuzzy=False)
                    return parsed.date()
                except Exception:
                    return None
            return None
        day = int(match.group(1))
        month = int(match.group(2))
        year_str = match.group(3)
        if year_str:
            year = int(year_str)
            if len(year_str) == 2:
                year += 2000
        else:
            year = ref_date.year
            if month > ref_date.month + 1:
                year -= 1
        try:
            return datetime(year, month, day).date()
        except ValueError:
            return None

    def _combine_dt(self, date_value: datetime.date, time_value: Tuple[int, int]) -> datetime:
        base = datetime.combine(date_value, datetime.min.time())
        return base.replace(hour=time_value[0], minute=time_value[1], second=0, microsecond=0)

    def _is_time_after(self, t1: Tuple[int, int], t2: Tuple[int, int]) -> bool:
        return (t1[0], t1[1]) > (t2[0], t2[1])

    def parse_row(
        self,
        log_entry: str,
        client_now_str: str,
        previous_end_dt: Optional[datetime],
    ) -> Dict[str, Any]:
        client_now: datetime
        if client_now_str:
            try:
                client_now = datetime.strptime(str(client_now_str).strip(), "%Y-%m-%d %H:%M:%S")
            except Exception:
                try:
                    client_now = pd.to_datetime(client_now_str)
                    if pd.isna(client_now):
                        raise ValueError
                except Exception:
                    client_now = datetime.now()
        else:
            client_now = datetime.now()

        if log_entry is None or (isinstance(log_entry, float) and pd.isna(log_entry)):
            raw_entry = ""
        else:
            raw_entry = str(log_entry)

        tokens = raw_entry.strip().split()
        elements = []
        dot_positions = []
        consumed = 0

        def _ampm_normalized(token: str) -> Optional[str]:
            normalized = token.strip().lower().strip(".,")
            if normalized in {"am", "a.m", "a.m.", "a"}:
                return "am"
            if normalized in {"pm", "p.m", "p.m.", "p"}:
                return "pm"
            return None

        leading_date_has_comma = False
        i = 0
        while i < len(tokens):
            token = tokens[i]
            trailing_dot = token.endswith(".")
            cleaned = token.rstrip(".").rstrip(",")

            time_val = self._parse_time_token(cleaned, client_now)
            consumed_extra = 0
            ampm = None
            if time_val is not None:
                if i + 2 < len(tokens) and time_val[1] == 0 and re.fullmatch(r"\d{2}", tokens[i + 1].strip().strip(".,")):
                    ampm2 = _ampm_normalized(tokens[i + 2])
                    if ampm2:
                        minute_token = tokens[i + 1].strip().strip(".,")
                        merged = f"{cleaned}:{minute_token}{ampm2}"
                        merged_time = self._parse_time_token(merged, client_now)
                        if merged_time is not None:
                            time_val = merged_time
                            consumed_extra = 2
                            trailing_dot = (
                                trailing_dot
                                or tokens[i + 1].endswith(".")
                                or tokens[i + 2].endswith(".")
                            )
                if i + 1 < len(tokens):
                    ampm = _ampm_normalized(tokens[i + 1])
                if ampm and not re.search(r"[ap]m\.?$", cleaned, re.IGNORECASE):
                    merged = f"{cleaned}{ampm}"
                    merged_time = self._parse_time_token(merged, client_now)
                    if merged_time is not None:
                        time_val = merged_time
                        if consumed_extra == 0:
                            consumed_extra = 1
                        trailing_dot = trailing_dot or tokens[i + 1].endswith(".")

            date_val = self._parse_date_token(cleaned, client_now)
            if i == 0 and date_val and token.endswith(","):
                leading_date_has_comma = True
            if time_val:
                elements.append(("time", time_val))
                dot_positions.append(trailing_dot)
            elif date_val:
                elements.append(("date", date_val))
                dot_positions.append(trailing_dot)
            else:
                break
            consumed += 1 + consumed_extra
            i += 1 + consumed_extra
            if len(elements) >= 4:
                break

        if i == 0:
            i = consumed

        remaining_tokens = tokens[consumed:]
        explicit_logged_dt = None
        if (
            leading_date_has_comma
            and len(elements) >= 2
            and elements[0][0] == "date"
            and elements[1][0] == "time"
            and not any(kind == "date" for kind, _ in elements[2:])
        ):
            explicit_logged_dt = self._combine_dt(elements[0][1], elements[1][1])
            client_now = explicit_logged_dt
            elements = elements[2:]
            dot_positions = dot_positions[2:]

        dot_after_first = dot_positions[0] if dot_positions else False
        dot_after_last = dot_positions[-1] if dot_positions else False
        if remaining_tokens and remaining_tokens[0].startswith("."):
            dot_after_last = True
            remaining_tokens[0] = remaining_tokens[0].lstrip(".")
            if not remaining_tokens[0]:
                remaining_tokens = remaining_tokens[1:]

        raw_text = " ".join(remaining_tokens).strip() or "Unspecified"
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

        def fallback_start(end_dt: datetime) -> datetime:
            return previous_end_dt or end_dt

        current_date = client_now.date()
        start_dt = previous_end_dt or client_now
        end_dt = client_now
        has_explicit_logged_time = explicit_logged_dt is not None

        if len(elements) == 0:
            start_dt = previous_end_dt or client_now
            end_dt = client_now
        elif len(elements) == 1 and elements[0][0] == "time":
            t1 = elements[0][1]
            if has_explicit_logged_time:
                start_dt = self._combine_dt(current_date, t1)
                if start_dt > client_now:
                    start_dt = start_dt - timedelta(days=1)
                end_dt = client_now
            elif dot_after_last:
                start_dt = self._combine_dt(current_date, t1)
                if start_dt > client_now:
                    start_dt = start_dt - timedelta(days=1)
                end_dt = client_now
            else:
                end_dt = self._combine_dt(current_date, t1)
                if end_dt > client_now:
                    end_dt = end_dt - timedelta(days=1)
                start_dt = fallback_start(end_dt)
        elif len(elements) == 2:
            times = [val for kind, val in elements if kind == "time"]
            dates = [val for kind, val in elements if kind == "date"]
            if len(times) == 1 and len(dates) == 1:
                t1 = times[0]
                d1 = dates[0]
                if dot_after_last:
                    start_dt = self._combine_dt(d1, t1)
                    end_dt = client_now
                else:
                    end_dt = self._combine_dt(d1, t1)
                    start_dt = fallback_start(end_dt)
            elif len(times) == 2:
                t1, t2 = times[0], times[1]
                start_date = current_date - timedelta(days=1) if self._is_time_after(t1, t2) else current_date
                start_dt = self._combine_dt(start_date, t1)
                end_dt = self._combine_dt(current_date, t2)
        elif len(elements) == 3:
            if elements[0][0] == "date" and sum(1 for kind, _ in elements if kind == "time") == 2:
                d1 = elements[0][1]
                times = [val for kind, val in elements if kind == "time"]
                t1, t2 = times[0], times[1]
                if dot_after_first:
                    start_dt = self._combine_dt(d1, t1)
                    end_dt = self._combine_dt(current_date, t2)
                else:
                    end_date = d1 + timedelta(days=1) if self._is_time_after(t1, t2) else d1
                    start_dt = self._combine_dt(d1, t1)
                    end_dt = self._combine_dt(end_date, t2)
        elif len(elements) == 4:
            if (
                elements[0][0] == "date"
                and elements[1][0] == "time"
                and elements[2][0] == "date"
                and elements[3][0] == "time"
            ):
                start_dt = self._combine_dt(elements[0][1], elements[1][1])
                end_dt = self._combine_dt(elements[2][1], elements[3][1])

        if previous_end_dt is None and start_dt == client_now and end_dt != client_now:
            start_dt = end_dt

        return {
            "start_dt": start_dt,
            "end_dt": end_dt,
            "task": task_name,
            "tag": tag,
            "urg": is_urg,
            "imp": is_imp,
        }
