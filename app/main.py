"""群消息实时监控服务入口（QQ / 微信 / Telegram）。"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time
from logging.handlers import RotatingFileHandler
from typing import Any

from app.channels.telegram import TelegramUserAdapter
from app.channels.feature_flags import WECHAT_CHANNEL_ENABLED
from app.config import Settings, load_settings
from app.filters import matched_keywords
from app.handlers.alert_handler import AlertHandler
from app.handlers.log_handler import LogHandler
from app.handlers.store_handler import StoreHandler
from app.history_sync import pull_enabled_groups_history
from app.llm.service import run_group_summary
from app.models import GroupMessageEvent, try_parse_group_message
from app.onebot_client import OneBotClient, build_ws_url
from app.settings_store import (
    ROOT_DIR,
    enabled_group_ids,
    list_group_configs,
    load_app_settings,
    load_group_config,
)


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


def acquire_singleton_lock() -> Any:
    """确保同时只有一个监控进程。"""
    import atexit

    lock_path = ROOT_DIR / "data" / "monitor.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "a+", encoding="utf-8")
    try:
        if fh.tell() == 0:
            fh.write("0")
            fh.flush()
        fh.seek(0)
        if sys.platform == "win32":
            import msvcrt

            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as e:
                fh.close()
                raise SystemExit("监控服务已在运行（检测到 monitor.lock）") from e
        else:
            import fcntl

            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as e:
                fh.close()
                raise SystemExit("监控服务已在运行（检测到 monitor.lock）") from e
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
    except SystemExit:
        raise
    except Exception:
        fh.close()
        raise

    def _release() -> None:
        try:
            if sys.platform == "win32":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            fh.close()
        except Exception:
            pass

    atexit.register(_release)
    return fh


class MonitorApp:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.log_handler = LogHandler()
        self.store_handler = (
            StoreHandler(settings.sqlite_path) if settings.storage_enabled else None
        )
        self._alert_cache: dict[str, AlertHandler] = {}
        self._llm_last_run: dict[str, float] = {}
        self.qq_client: OneBotClient | None = None
        self.tg_adapter: TelegramUserAdapter | None = None
        self.wx_adapter: WechatLocalAdapter | None = None

    def _allowed(self) -> set[str]:
        ids = enabled_group_ids()
        if ids:
            return ids
        return self.settings.allowed_groups

    async def handle_group_event(self, event: GroupMessageEvent) -> None:
        gcfg = load_group_config(event.group_id_str)
        if gcfg.blocked or not gcfg.enabled:
            return

        if gcfg.basic.log_all:
            await self.log_handler.handle(event)
        if gcfg.basic.storage_enabled and self.store_handler is not None:
            await self.store_handler.handle(event)

        km = gcfg.keyword_monitor
        if km.enabled and km.keywords:
            hits = matched_keywords(event, km.keywords)
            if hits:
                if not gcfg.basic.log_all:
                    await self.log_handler.handle(event)
                if km.alert_enabled and km.webhook_url:
                    alert = self._alert_cache.get(km.webhook_url)
                    if alert is None:
                        alert = AlertHandler(km.webhook_url)
                        self._alert_cache[km.webhook_url] = alert
                    await alert.handle(event, hits)

    async def on_onebot_event(self, raw: dict[str, Any]) -> None:
        event = try_parse_group_message(raw)
        if event is None:
            return
        await self.handle_group_event(event)

    async def llm_scheduler_loop(self) -> None:
        logger = logging.getLogger(__name__)
        while True:
            try:
                for cfg in list_group_configs():
                    if cfg.blocked or not cfg.enabled or not cfg.llm_monitor.enabled:
                        continue
                    every = int(cfg.llm_monitor.every_minutes or 60)
                    every = max(1, every)
                    last = self._llm_last_run.get(cfg.group_id, 0)
                    if time.time() - last < every * 60:
                        continue
                    self._llm_last_run[cfg.group_id] = time.time()
                    try:
                        result = await run_group_summary(cfg.group_id, job_type="schedule")
                        logger.info(
                            "LLM 定时总结 group=%s status=%s msg=%s reason=%s",
                            cfg.group_id,
                            result.get("status"),
                            result.get("msg_count"),
                            result.get("reason") or "",
                        )
                    except Exception:
                        logger.exception("LLM 定时总结失败 group=%s", cfg.group_id)
            except Exception:
                logger.exception("LLM scheduler 异常")
            await asyncio.sleep(15)

    def stop(self) -> None:
        if self.qq_client is not None:
            self.qq_client.stop()
        if self.tg_adapter is not None:
            self.tg_adapter.stop()
        if self.wx_adapter is not None:
            self.wx_adapter.stop()

    async def run(self) -> None:
        logger = logging.getLogger(__name__)
        allowed = self._allowed()
        if not allowed:
            logger.warning("当前无启用监控群；可在桌面端群列表中启用")

        app_settings = load_app_settings()
        ch = app_settings.channels
        tasks: list[Any] = [self.llm_scheduler_loop()]

        # QQ / OneBot
        if ch.qq.bound:
            ws = app_settings.onebot_ws_url or self.settings.onebot_ws_url
            token = app_settings.onebot_access_token or self.settings.onebot_access_token
            self.qq_client = OneBotClient(
                ws_url=ws,
                access_token=token,
                on_event=self.on_onebot_event,
                reconnect_min_delay=self.settings.reconnect_min_delay,
                reconnect_max_delay=self.settings.reconnect_max_delay,
            )
            self.qq_client.ws_url = build_ws_url(ws, token)
            self.qq_client.access_token = token
            tasks.append(self.qq_client.run_forever())
            logger.info("通道 QQ 已启用 | ws=%s", ws)

            async def _bootstrap_history() -> None:
                try:
                    result = await pull_enabled_groups_history(count=200)
                    logger.info("启动补拉历史完成: %s", result)
                except Exception:
                    logger.exception("启动补拉历史失败")

            tasks.append(_bootstrap_history())
        else:
            logger.info("通道 QQ 未绑定，跳过 OneBot")

        # Telegram 用户 session
        if ch.telegram.bound:
            if ch.telegram.api_id and ch.telegram.api_hash:
                try:
                    self.tg_adapter = TelegramUserAdapter(
                        api_id=ch.telegram.api_id,
                        api_hash=ch.telegram.api_hash,
                        on_message=self.handle_group_event,
                    )
                    tasks.append(self.tg_adapter.run_forever())
                    logger.info(
                        "通道 Telegram 已启用 | user=%s",
                        ch.telegram.label or "(session)",
                    )
                except Exception:
                    logger.exception("Telegram 用户适配器启动失败")
            else:
                logger.warning("Telegram 已绑定但缺少 api_id / api_hash")

        # WeChat local（功能开关关闭时一律不启动）
        if WECHAT_CHANNEL_ENABLED and ch.wechat.bound:
            from app.channels.wechat import WechatLocalAdapter

            if not ch.wechat.data_dir and not ch.wechat.decrypted_dir:
                logger.warning("微信已绑定但未配置 data_dir / decrypted_dir")
            else:
                self.wx_adapter = WechatLocalAdapter(
                    data_dir=ch.wechat.data_dir,
                    decrypted_dir=ch.wechat.decrypted_dir,
                    keys_path=ch.wechat.keys_path,
                    on_message=self.handle_group_event,
                    poll_seconds=ch.wechat.poll_seconds,
                )
                tasks.append(self.wx_adapter.run_forever())
                logger.info(
                    "通道微信已启用 | data_dir=%s",
                    ch.wechat.data_dir or ch.wechat.decrypted_dir,
                )
        elif ch.wechat.bound and not WECHAT_CHANNEL_ENABLED:
            logger.info("微信通道已屏蔽，忽略绑定配置")

        if len(tasks) == 1:
            logger.warning("未绑定任何消息通道；请在总配置中绑定 QQ / Telegram")

        logger.info(
            "监听启动 | 启用监听群=%s",
            sorted(allowed) if allowed else ["(未启用任何群)"],
        )
        await asyncio.gather(*tasks)


async def _amain() -> None:
    settings = load_settings()
    setup_logging(settings)
    app = MonitorApp(settings)

    def _request_stop() -> None:
        logging.getLogger(__name__).info("收到停止信号，正在退出…")
        app.stop()

    if sys.platform == "win32":
        signal.signal(signal.SIGINT, lambda *_: _request_stop())
        try:
            signal.signal(signal.SIGTERM, lambda *_: _request_stop())
        except (AttributeError, ValueError, OSError):
            pass
    else:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _request_stop)

    await app.run()


def main() -> None:
    try:
        acquire_singleton_lock()
        asyncio.run(_amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
