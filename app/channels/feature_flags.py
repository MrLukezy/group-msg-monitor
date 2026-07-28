"""通道功能开关。微信本地库在 Windows ≥4.1.10 暂无稳定取 key 方案，默认关闭。"""

from __future__ import annotations

# 恢复微信通道时改为 True，并同步打开桌面端 UI。
WECHAT_CHANNEL_ENABLED = False

WECHAT_DISABLED_MESSAGE = (
    "微信通道已暂时屏蔽：Windows 微信 ≥4.1.10 无法稳定提取本地库密钥，"
    "相关绑定与监听已停用。"
)
