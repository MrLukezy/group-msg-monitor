"""跨通道群 ID 约定：QQ 保持纯数字兼容；微信 wx:；GeWeChat gw:；Telegram tg:。"""

from __future__ import annotations


def make_group_id(channel: str, raw_id: str | int) -> str:
    ch = (channel or "qq").strip().lower()
    rid = str(raw_id).strip()
    if ch in ("qq", ""):
        return rid
    if ch in ("qq_passive", "qqpassive", "qqp"):
        if rid.startswith("qqp:"):
            return rid
        return f"qqp:{rid}"
    if ch in ("wechat", "wx"):
        if rid.startswith("wx:"):
            return rid
        return f"wx:{rid}"
    if ch in ("gewechat", "gewe", "gw"):
        if rid.startswith("gw:"):
            return rid
        if rid.startswith("gewechat:") or rid.startswith("gewe:"):
            return f"gw:{rid.split(':', 1)[1]}"
        return f"gw:{rid}"
    if ch in ("telegram", "tg"):
        if rid.startswith("tg:"):
            return rid
        return f"tg:{rid}"
    return f"{ch}:{rid}"


def parse_group_id(group_id: str) -> tuple[str, str]:
    gid = str(group_id or "").strip()
    if gid.startswith("qqp:"):
        return "qq", gid
    if gid.startswith("wx:"):
        return "wechat", gid[3:]
    if gid.startswith("wechat:"):
        return "wechat", gid.split(":", 1)[1]
    if gid.startswith("gw:"):
        return "gewechat", gid[3:]
    if gid.startswith("gewechat:") or gid.startswith("gewe:"):
        return "gewechat", gid.split(":", 1)[1]
    if gid.startswith("tg:"):
        return "telegram", gid[3:]
    if gid.startswith("telegram:"):
        return "telegram", gid.split(":", 1)[1]
    return "qq", gid


def channel_of_group_id(group_id: str) -> str:
    return parse_group_id(group_id)[0]
