"""多通道消息接入（QQ OneBot / GeWeChat / 微信本地库 / Telegram）。"""

from app.channels.feature_flags import (
    GEWECHAT_CHANNEL_ENABLED,
    GEWECHAT_DISABLED_MESSAGE,
    WECHAT_CHANNEL_ENABLED,
    WECHAT_DISABLED_MESSAGE,
)
from app.channels.ids import channel_of_group_id, make_group_id, parse_group_id

__all__ = [
    "GEWECHAT_CHANNEL_ENABLED",
    "GEWECHAT_DISABLED_MESSAGE",
    "WECHAT_CHANNEL_ENABLED",
    "WECHAT_DISABLED_MESSAGE",
    "channel_of_group_id",
    "make_group_id",
    "parse_group_id",
]
