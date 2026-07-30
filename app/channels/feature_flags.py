"""暂未开放通道的功能开关。"""

from __future__ import annotations

# 恢复微信通道时改为 True，并同步打开桌面端 UI。
WECHAT_CHANNEL_ENABLED = False
TELEGRAM_CHANNEL_ENABLED = False

WECHAT_DISABLED_MESSAGE = (
    "微信通道已暂时屏蔽：Windows 微信 ≥4.1.10 无法稳定提取本地库密钥，"
    "相关绑定与监听已停用。"
)

TELEGRAM_DISABLED_MESSAGE = "Telegram 通道暂不支持，相关绑定与监听已停用。"
