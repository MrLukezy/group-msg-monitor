"""多通道消息接入（QQ OneBot / 微信本地库 / Telegram 用户 session）。"""

from app.channels.feature_flags import WECHAT_CHANNEL_ENABLED, WECHAT_DISABLED_MESSAGE
from app.channels.ids import channel_of_group_id, make_group_id, parse_group_id

__all__ = [
    "WECHAT_CHANNEL_ENABLED",
    "WECHAT_DISABLED_MESSAGE",
    "channel_of_group_id",
    "make_group_id",
    "parse_group_id",
]
