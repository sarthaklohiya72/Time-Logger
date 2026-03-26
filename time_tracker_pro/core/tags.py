from __future__ import annotations

import re
from typing import List, Optional

SPECIAL_TAGS = {"Work", "Necessity", "Soul", "Rest", "Waste"}


def normalize_tag(raw: Optional[str]) -> str:
    s = str(raw or "").strip()
    if not s:
        return ""
    parts = re.split(r"[,.]", s)
    cleaned_parts: List[str] = []
    for part in parts:
        cleaned = re.sub(r"[^A-Za-z\s]+$", "", part)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        if not cleaned:
            continue
        if cleaned.lower() in {"undefined", "null", "none", "nan", "urg", "urgent", "imp", "important"}:
            continue
        cleaned = cleaned.title()
        if cleaned and cleaned not in cleaned_parts:
            cleaned_parts.append(cleaned)
    return ", ".join(cleaned_parts)


def filter_special_tags(raw: Optional[str]) -> List[str]:
    normalized = normalize_tag(raw)
    if not normalized:
        return []
    parts = [p.strip() for p in normalized.split(",") if p.strip()]
    return [p for p in parts if p in SPECIAL_TAGS]


def primary_special_tag(raw: Optional[str]) -> str:
    tags = filter_special_tags(raw)
    return tags[0] if tags else "Waste"
