from __future__ import annotations

from app.llm.service import (
    apply_image_placeholders,
    format_transcript_with_image_placeholders,
    replace_cq_images_with_placeholder,
    rows_contain_cq_images,
)


def test_replace_cq_images_with_placeholder() -> None:
    raw = '看这张 [CQ:image,file=media/g1/a.jpg,url=https://example.com/a.jpg] 报错'
    out = replace_cq_images_with_placeholder(raw)
    assert "[图片]" in out
    assert "[CQ:image" not in out
    assert "报错" in out


def test_rows_contain_and_apply_placeholders() -> None:
    rows = [
        {"message_id": "1", "content": "纯文字"},
        {
            "message_id": "2",
            "content": "[CQ:image,file=media/g1/b.jpg]",
            "sender_name": "A",
            "event_time": 1,
        },
    ]
    assert rows_contain_cq_images(rows) is True
    changed = apply_image_placeholders(rows)
    assert changed == 1
    assert rows[1]["content"] == "[图片]"
    assert rows_contain_cq_images(rows) is False


def test_format_transcript_with_image_placeholders_does_not_mutate() -> None:
    cq = "[CQ:image,file=media/g1/c.jpg]"
    rows = [
        {
            "message_id": "9",
            "content": f"截图 {cq}",
            "sender_name": "B",
            "event_time": 1700000000,
        }
    ]
    text = format_transcript_with_image_placeholders(rows)
    assert "[图片]" in text
    assert "[CQ:image" not in text
    assert cq in rows[0]["content"]
