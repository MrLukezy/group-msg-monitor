"""OneBot 11 事件模型（宽松解析，兼容各协议端字段差异）。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Sender(BaseModel):
    model_config = ConfigDict(extra="ignore")

    user_id: int | str | None = None
    nickname: str | None = None
    card: str | None = None
    role: str | None = None


class GroupMessageEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")

    post_type: str
    message_type: str
    group_id: int | str
    user_id: int | str | None = None
    message_id: int | str | None = None
    raw_message: str = ""
    message: Any = None
    sender: Sender | None = None
    time: int | None = None
    self_id: int | str | None = None

    @property
    def group_id_str(self) -> str:
        return str(self.group_id)

    @property
    def display_name(self) -> str:
        if self.sender is None:
            return str(self.user_id or "unknown")
        return self.sender.card or self.sender.nickname or str(self.user_id or "unknown")

    @property
    def text(self) -> str:
        if self.raw_message:
            return self.raw_message
        if isinstance(self.message, str):
            return self.message
        if isinstance(self.message, list):
            parts: list[str] = []
            for seg in self.message:
                if not isinstance(seg, dict):
                    continue
                seg_type = seg.get("type")
                data = seg.get("data") or {}
                if seg_type == "text":
                    parts.append(str(data.get("text", "")))
                elif seg_type == "image":
                    parts.append("[图片]")
                elif seg_type == "face":
                    parts.append("[表情]")
                elif seg_type == "at":
                    parts.append(f"[@{data.get('qq', '')}]")
                elif seg_type == "file":
                    parts.append(f"[文件:{data.get('name', '')}]")
                elif seg_type == "record":
                    parts.append("[语音]")
                elif seg_type == "video":
                    parts.append("[视频]")
                else:
                    parts.append(f"[{seg_type}]")
            return "".join(parts)
        return ""

    @property
    def message_summary(self) -> str:
        text = self.text.strip()
        if text:
            return text
        return "[非文本消息]"


def try_parse_group_message(event: dict[str, Any]) -> GroupMessageEvent | None:
    if event.get("post_type") != "message":
        return None
    if event.get("message_type") != "group":
        return None
    return GroupMessageEvent.model_validate(event)
