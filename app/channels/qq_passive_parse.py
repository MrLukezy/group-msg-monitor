"""官方 QQ 被动采集：通知 / UIA 文本解析与群映射（纯逻辑，可单测）。"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from typing import Any


QQ_APP_HINTS = (
    "qq",
    "tencent",
    "com.tencent.qq",
    "qqnt",
)

# 常见 QQ Toast 形态：
#   title: 群名 / 好友昵称
#   body:  "张三: 你好" / "张三：你好" / "你好" / "[图片]"
# 注意：必须要求冒号后有空格，或使用中文冒号，避免误拆 https://
SENDER_SPLIT_RE = re.compile(
    r"^\s*([^:：\n/]{1,32}?)\s*(?:：|: )\s*(.+)\s*$",
    re.DOTALL,
)
IMAGE_HINT_RE = re.compile(r"\[图片\]|\[Image\]|发送了一张图片|分享了一张图片", re.I)


@dataclass(frozen=True)
class ParsedPassiveMessage:
    group_name: str
    sender_name: str
    text: str
    has_image: bool
    source: str  # notification | uia
    raw_title: str = ""
    raw_body: str = ""
    notification_id: str = ""
    observed_at: float = 0.0


def is_qq_notification_app(app_id: str | None, app_name: str | None = None) -> bool:
    blob = f"{app_id or ''} {app_name or ''}".strip().lower()
    if not blob:
        return False
    return any(h in blob for h in QQ_APP_HINTS)


def stable_group_id(group_name: str, mapping: dict[str, str] | None = None) -> str:
    """将群名映射为稳定 group_id。

    - 若 mapping 提供真实 QQ 群号，优先使用；
    - 否则生成 `qqp:<hash>`，与 OneBot 数字群号隔离。
    """
    name = (group_name or "").strip()
    if not name:
        return "qqp:unknown"
    if mapping:
        mapped = (mapping.get(name) or "").strip()
        if mapped:
            return mapped
    digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:16]
    return f"qqp:{digest}"


def message_id_for(
    *,
    group_id: str,
    sender_name: str,
    text: str,
    observed_at: float,
    notification_id: str = "",
    bucket_seconds: int = 8,
) -> str:
    if notification_id:
        digest = hashlib.sha1(f"n:{notification_id}".encode("utf-8")).hexdigest()[:20]
        return f"passive-n-{digest}"
    bucket = int(observed_at // max(1, bucket_seconds))
    payload = f"{group_id}|{sender_name}|{text.strip()}|{bucket}"
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:20]
    return f"passive-{digest}"


def parse_notification_payload(
    *,
    title: str,
    body: str,
    app_id: str = "",
    app_name: str = "",
    notification_id: str = "",
    observed_at: float | None = None,
    require_qq_app: bool = True,
) -> ParsedPassiveMessage | None:
    if require_qq_app and not is_qq_notification_app(app_id, app_name):
        return None
    group_name = (title or "").strip()
    body_text = (body or "").strip()
    if not group_name and not body_text:
        return None
    if not group_name:
        # 某些 Toast 只有正文；无法可靠识别群，降级为「未知会话」
        group_name = "未知会话"

    sender_name = "未知"
    text = body_text
    m = SENDER_SPLIT_RE.match(body_text)
    if m:
        sender_name = m.group(1).strip() or "未知"
        text = m.group(2).strip()
    has_image = bool(IMAGE_HINT_RE.search(text) or IMAGE_HINT_RE.search(body_text))
    if has_image and not text:
        text = "[图片]"
    if not text:
        text = "[非文本消息]"
    return ParsedPassiveMessage(
        group_name=group_name,
        sender_name=sender_name,
        text=text,
        has_image=has_image,
        source="notification",
        raw_title=title or "",
        raw_body=body or "",
        notification_id=str(notification_id or ""),
        observed_at=float(observed_at if observed_at is not None else time.time()),
    )


def parse_uia_message(
    *,
    group_name: str,
    sender_name: str = "",
    text: str = "",
    observed_at: float | None = None,
) -> ParsedPassiveMessage | None:
    g = (group_name or "").strip()
    t = (text or "").strip()
    if not g or not t:
        return None
    s = (sender_name or "").strip() or "未知"
    has_image = bool(IMAGE_HINT_RE.search(t))
    return ParsedPassiveMessage(
        group_name=g,
        sender_name=s,
        text=t if t else ("[图片]" if has_image else "[非文本消息]"),
        has_image=has_image,
        source="uia",
        raw_title=g,
        raw_body=t,
        observed_at=float(observed_at if observed_at is not None else time.time()),
    )


def dedupe_key(parsed: ParsedPassiveMessage, group_id: str) -> str:
    return message_id_for(
        group_id=group_id,
        sender_name=parsed.sender_name,
        text=parsed.text,
        observed_at=parsed.observed_at,
        notification_id=parsed.notification_id,
    )


# 固化实测样例，供单测与探针对照
FIXTURE_NOTIFICATIONS: list[dict[str, Any]] = [
    {
        "app_id": "QQ",
        "app_name": "QQ",
        "notification_id": "toast-1",
        "title": "产品讨论群",
        "body": "Alice: 今晚发版吗？",
        "expect": {
            "group_name": "产品讨论群",
            "sender_name": "Alice",
            "text": "今晚发版吗？",
            "has_image": False,
        },
    },
    {
        "app_id": "com.tencent.qq",
        "app_name": "QQ",
        "notification_id": "toast-2",
        "title": "周末约饭",
        "body": "Bob：[图片]",
        "expect": {
            "group_name": "周末约饭",
            "sender_name": "Bob",
            "text": "[图片]",
            "has_image": True,
        },
    },
    {
        "app_id": "Microsoft.Windows.Explorer",
        "app_name": "文件资源管理器",
        "notification_id": "toast-x",
        "title": "下载完成",
        "body": "file.zip",
        "expect": None,
    },
    {
        "app_id": "QQ",
        "app_name": "QQ",
        "notification_id": "toast-3",
        "title": "技术分享",
        "body": "看这个链接 https://github.com/example/repo",
        "expect": {
            "group_name": "技术分享",
            "sender_name": "未知",
            "text": "看这个链接 https://github.com/example/repo",
            "has_image": False,
        },
    },
]
