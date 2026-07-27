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
        # 优先结构化 message，避免 raw_message 里超长 CQ（图片 url / raw json）污染展示与 LLM
        if isinstance(self.message, list):
            parts: list[str] = []
            for seg in self.message:
                if not isinstance(seg, dict):
                    continue
                seg_type = seg.get("type")
                data = seg.get("data") or {}
                if seg_type == "text":
                    parts.append(str(data.get("text", "")))
                elif seg_type in ("image", "mface"):
                    url = data.get("url") or data.get("file") or ""
                    if isinstance(url, str) and url.startswith("http"):
                        parts.append(f"[CQ:image,url={url}]")
                    else:
                        parts.append("[图片]")
                elif seg_type == "face":
                    face_id = data.get("id", "")
                    parts.append(f"[CQ:face,id={face_id}]" if face_id != "" else "[表情]")
                elif seg_type == "reply":
                    rid = data.get("id") or data.get("seq") or ""
                    rtext = data.get("text") or data.get("content") or ""
                    if rtext:
                        parts.append(f"[CQ:reply,id={rid},text={rtext}]")
                    else:
                        parts.append(f"[CQ:reply,id={rid}]")
                elif seg_type == "at":
                    qq = data.get("qq", "")
                    name = data.get("name") or ""
                    if name:
                        parts.append(f"[CQ:at,qq={qq},name={name}]")
                    else:
                        parts.append(f"[CQ:at,qq={qq}]")
                elif seg_type == "file":
                    parts.append(f"[文件:{data.get('name', '')}]")
                elif seg_type == "record":
                    parts.append("[语音]")
                elif seg_type == "video":
                    parts.append("[视频]")
                elif seg_type in ("json", "xml", "forward"):
                    parts.append("[卡片消息]" if seg_type != "forward" else "[合并转发]")
                elif seg_type == "share":
                    url = data.get("url") or ""
                    parts.append(url or "[分享]")
                else:
                    parts.append(f"[{seg_type}]")
            return "".join(parts)
        if isinstance(self.message, str) and self.message.strip():
            return self.message
        if self.raw_message:
            return self.raw_message
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
