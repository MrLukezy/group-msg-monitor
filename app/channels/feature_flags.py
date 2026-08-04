"""暂未开放通道的功能开关。"""

from __future__ import annotations

# 恢复微信本地库通道时改为 True，并同步打开桌面端 UI。
WECHAT_CHANNEL_ENABLED = False
TELEGRAM_CHANNEL_ENABLED = False
# GeWeChat（iPad 协议）扫码登录 + 只读群监听。
# 自建 GeWe 取码常失败（创建设备失败），入口暂时屏蔽。
GEWECHAT_CHANNEL_ENABLED = False

WECHAT_DISABLED_MESSAGE = (
    "微信通道已暂时屏蔽：Windows 微信 ≥4.1.10 无法稳定提取本地库密钥，"
    "相关绑定与监听已停用。"
)

TELEGRAM_DISABLED_MESSAGE = "Telegram 通道暂不支持，相关绑定与监听已停用。"

GEWECHAT_DISABLED_MESSAGE = (
    "微信 · GeWeChat 通道已暂时屏蔽：自建服务取码不稳定（创建设备失败），"
    "相关绑定与监听已停用。"
)
