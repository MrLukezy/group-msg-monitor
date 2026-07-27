"""QQ 群消息实时监控服务入口。"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from logging.handlers import RotatingFileHandler
from typing import Any

from app.config import Settings, load_settings
from app.filters import is_allowed_group, matched_keywords
from app.handlers.alert_handler import AlertHandler
from app.handlers.log_handler import LogHandler
from app.handlers.store_handler import StoreHandler
from app.models import try_parse_group_message
from app.onebot_client import OneBotClient


def setup_logging(settings: Settings) -> None:
    log_dir = settings.log_path
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "monitor.log"

    root = logging.getLogger()
    root.setLevel(settings.log_level.upper())
    root.handlers.clear()

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)


class MonitorApp:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.log_handler = LogHandler()
        self.store_handler = (
            StoreHandler(settings.sqlite_path) if settings.storage_enabled else None
        )
        self.alert_handler = (
            AlertHandler(settings.alert_webhook_url) if settings.alert_enabled else None
        )
        self.client = OneBotClient(
            ws_url=settings.onebot_ws_url,
            access_token=settings.onebot_access_token,
            on_event=self.on_event,
            reconnect_min_delay=settings.reconnect_min_delay,
            reconnect_max_delay=settings.reconnect_max_delay,
        )

    async def on_event(self, raw: dict[str, Any]) -> None:
        event = try_parse_group_message(raw)
        if event is None:
            return
        if not is_allowed_group(event, self.settings.allowed_groups):
            logging.getLogger(__name__).debug(
                "忽略非白名单群: %s", event.group_id_str
            )
            return

        if self.settings.monitor_log_all:
            await self.log_handler.handle(event)
            if self.store_handler is not None:
                await self.store_handler.handle(event)

        keywords = matched_keywords(event, self.settings.monitor_keywords)
        if keywords and self.alert_handler is not None:
            if not self.settings.monitor_log_all:
                # 仅告警模式时，命中关键词也写一条日志，便于排查
                await self.log_handler.handle(event)
            await self.alert_handler.handle(event, keywords)

    async def run(self) -> None:
        logger = logging.getLogger(__name__)
        if not self.settings.allowed_groups:
            logger.error("未配置 MONITOR_GROUP_IDS / monitor.group_ids，退出")
            return
        if not self.settings.onebot_access_token or self.settings.onebot_access_token.startswith(
            "CHANGE_ME"
        ):
            logger.warning(
                "Access Token 未修改为强随机串，存在安全风险；请尽快在 .env / config.yaml 中更换"
            )

        logger.info(
            "监控启动 | groups=%s | storage=%s | alert=%s | ws=%s",
            sorted(self.settings.allowed_groups),
            self.settings.storage_enabled,
            self.settings.alert_enabled,
            self.settings.onebot_ws_url,
        )
        await self.client.run_forever()


async def _amain() -> None:
    settings = load_settings()
    setup_logging(settings)
    app = MonitorApp(settings)

    loop = asyncio.get_running_loop()

    def _request_stop() -> None:
        logging.getLogger(__name__).info("收到停止信号，正在退出…")
        app.client.stop()

    if sys.platform == "win32":
        # Windows 下 SIGTERM 可能不可用；用 CTRL_C 即可
        signal.signal(signal.SIGINT, lambda *_: _request_stop())
        try:
            signal.signal(signal.SIGTERM, lambda *_: _request_stop())
        except (AttributeError, ValueError, OSError):
            pass
    else:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _request_stop)

    await app.run()


def main() -> None:
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
