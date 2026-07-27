"""多通道消息接入（QQ OneBot / 微信本地库 / Telegram 用户 session）。"""

from app.channels.ids import channel_of_group_id, make_group_id, parse_group_id

__all__ = ["channel_of_group_id", "make_group_id", "parse_group_id"]
