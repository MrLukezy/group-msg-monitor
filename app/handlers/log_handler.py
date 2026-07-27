"""控制台 + 文件日志 Handler。"""

from __future__ import annotations

import logging
from datetime import datetime

from app.models import GroupMessageEvent

logger = logging.getLogger(__name__)


class LogHandler:
    async def handle(self, event: GroupMessageEvent) -> None:
        ts = datetime.fromtimestamp(event.time).strftime("%Y-%m-%d %H:%M:%S") if event.time else "-"
        line = (
            f"[{ts}] group={event.group_id_str} "
            f"user={event.user_id}({event.display_name}) "
            f"msg_id={event.message_id} "
            f"text={event.message_summary}"
        )
        logger.info(line)
