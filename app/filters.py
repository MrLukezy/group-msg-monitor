"""群白名单与关键词过滤。"""

from __future__ import annotations

from app.models import GroupMessageEvent


def is_allowed_group(event: GroupMessageEvent, allowed_groups: set[str]) -> bool:
    if not allowed_groups:
        return False
    return event.group_id_str in allowed_groups


def matched_keywords(event: GroupMessageEvent, keywords: list[str]) -> list[str]:
    if not keywords:
        return []
    text = event.text
    if not text:
        return []
    hits: list[str] = []
    for kw in keywords:
        if kw and kw in text:
            hits.append(kw)
    return hits
