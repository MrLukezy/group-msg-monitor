from __future__ import annotations

import asyncio

from app.channels.qq_passive import QqPassiveAdapter
from app.channels.qq_passive_parse import parse_notification_payload, stable_group_id
from app.models import GroupMessageEvent


def test_adapter_emits_and_dedupes(monkeypatch) -> None:
    events: list[GroupMessageEvent] = []

    async def on_message(ev: GroupMessageEvent) -> None:
        events.append(ev)

    adapter = QqPassiveAdapter(on_message=on_message, poll_seconds=0.8)

    parsed = parse_notification_payload(
        title="测试群",
        body="小明: 你好",
        app_id="QQ",
        notification_id="n-dup",
        observed_at=1000,
    )
    assert parsed is not None

    async def run() -> None:
        await adapter._emit(parsed)
        await adapter._emit(parsed)  # duplicate notification id

    asyncio.run(run())
    assert len(events) == 1
    assert str(events[0].group_id).startswith("qqp:") or events[0].group_id == stable_group_id("测试群")
    assert events[0].self_id == "qq-passive"
    assert "你好" in events[0].text


def test_bind_payload_mode_normalize() -> None:
    from app.settings_store import QqChannelSettings

    assert QqChannelSettings(mode="safe").mode == "passive"
    assert QqChannelSettings(mode="ONEBOT").mode == "onebot"
