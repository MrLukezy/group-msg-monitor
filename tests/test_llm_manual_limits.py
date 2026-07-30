from __future__ import annotations

from app.llm.service import limit_recent_rows


def _row(index: int, content: str = "消息") -> dict[str, object]:
    return {
        "message_id": str(index),
        "event_time": index,
        "sender_name": "测试用户",
        "content": content,
    }


def test_limit_recent_rows_keeps_latest_messages() -> None:
    rows = [_row(i) for i in range(150)]

    limited, dropped = limit_recent_rows(rows, max_messages=100, max_chars=100000)

    assert len(limited) == 100
    assert dropped == 50
    assert limited[0]["message_id"] == "50"
    assert limited[-1]["message_id"] == "149"


def test_limit_recent_rows_respects_character_budget() -> None:
    rows = [_row(i, "文" * 100) for i in range(10)]

    limited, dropped = limit_recent_rows(rows, max_messages=10, max_chars=400)

    assert 1 <= len(limited) < len(rows)
    assert dropped == len(rows) - len(limited)
    assert limited[-1]["message_id"] == "9"
