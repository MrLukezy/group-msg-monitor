from __future__ import annotations

from app.channels.qq_passive_parse import (
    FIXTURE_NOTIFICATIONS,
    dedupe_key,
    message_id_for,
    parse_notification_payload,
    parse_uia_message,
    stable_group_id,
)


def test_fixtures_parse() -> None:
    for item in FIXTURE_NOTIFICATIONS:
        parsed = parse_notification_payload(
            title=item["title"],
            body=item["body"],
            app_id=item.get("app_id", ""),
            app_name=item.get("app_name", ""),
            notification_id=item.get("notification_id", ""),
            observed_at=1_700_000_000,
        )
        expect = item.get("expect")
        if expect is None:
            assert parsed is None
            continue
        assert parsed is not None
        assert parsed.group_name == expect["group_name"]
        assert parsed.sender_name == expect["sender_name"]
        assert parsed.text == expect["text"]
        assert parsed.has_image == expect["has_image"]


def test_stable_group_id_and_mapping() -> None:
    a = stable_group_id("产品讨论群")
    b = stable_group_id("产品讨论群")
    assert a == b
    assert a.startswith("qqp:")
    assert stable_group_id("产品讨论群", {"产品讨论群": "123456"}) == "123456"


def test_dedupe_same_notification_id() -> None:
    p1 = parse_notification_payload(
        title="群A",
        body="张三: hello",
        app_id="QQ",
        notification_id="n1",
        observed_at=100,
    )
    p2 = parse_notification_payload(
        title="群A",
        body="张三: hello",
        app_id="QQ",
        notification_id="n1",
        observed_at=999,
    )
    assert p1 and p2
    gid = stable_group_id(p1.group_name)
    assert dedupe_key(p1, gid) == dedupe_key(p2, gid)


def test_uia_and_notification_same_bucket_dedupe() -> None:
    n = parse_notification_payload(
        title="群B",
        body="李四: 在吗",
        app_id="QQ",
        observed_at=1000,
    )
    u = parse_uia_message(group_name="群B", sender_name="李四", text="在吗", observed_at=1003)
    assert n and u
    gid = stable_group_id("群B")
    assert message_id_for(
        group_id=gid,
        sender_name=n.sender_name,
        text=n.text,
        observed_at=n.observed_at,
    ) == message_id_for(
        group_id=gid,
        sender_name=u.sender_name,
        text=u.text,
        observed_at=u.observed_at,
    )


def test_uia_requires_group_and_text() -> None:
    assert parse_uia_message(group_name="", text="hi") is None
    assert parse_uia_message(group_name="群", text="") is None
