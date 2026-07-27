"""关键词命中后的 Webhook 告警。"""

from __future__ import annotations

import logging
from datetime import datetime

import aiohttp

from app.models import GroupMessageEvent

logger = logging.getLogger(__name__)


class AlertHandler:
    def __init__(self, webhook_url: str) -> None:
        self.webhook_url = webhook_url.strip()

    async def handle(self, event: GroupMessageEvent, keywords: list[str]) -> None:
        if not self.webhook_url:
            logger.warning("告警已启用但未配置 webhook_url，跳过")
            return

        ts = datetime.fromtimestamp(event.time).strftime("%Y-%m-%d %H:%M:%S") if event.time else "-"
        text = (
            f"【QQ群关键词告警】\n"
            f"时间: {ts}\n"
            f"群号: {event.group_id_str}\n"
            f"发送者: {event.display_name} ({event.user_id})\n"
            f"关键词: {', '.join(keywords)}\n"
            f"内容: {event.message_summary}"
        )
        payload = self._build_payload(text)

        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(self.webhook_url, json=payload) as resp:
                    body = await resp.text()
                    if resp.status >= 400:
                        logger.error("Webhook 失败 status=%s body=%s", resp.status, body[:300])
                    else:
                        logger.info("Webhook 已发送 keywords=%s", keywords)
        except Exception:
            logger.exception("Webhook 请求异常")

    @staticmethod
    def _build_payload(text: str) -> dict:
        # 兼容常见机器人 Webhook：飞书 / 钉钉 / 通用 text
        return {
            "msg_type": "text",
            "content": {"text": text},
            "msgtype": "text",
            "text": {"content": text},
            "message": text,
        }
