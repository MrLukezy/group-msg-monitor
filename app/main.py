"""QQ 群消息实时监控服务入口。"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time
from logging.handlers import RotatingFileHandler
from typing import Any

from app.config import Settings, load_settings
from app.filters import matched_keywords
from app.handlers.alert_handler import AlertHandler
from app.handlers.log_handler import LogHandler
from app.handlers.store_handler import StoreHandler
from app.history_sync import pull_enabled_groups_history
from app.llm.service import run_group_summary
from app.models import try_parse_group_message
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
        # Windows msvcrt.locking 要求文件至少有对应字节
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
        self.client = OneBotClient(
            ws_url=settings.onebot_ws_url,
            access_token=settings.onebot_access_token,
            on_event=self.on_event,
            reconnect_min_delay=settings.reconnect_min_delay,
            reconnect_max_delay=settings.reconnect_max_delay,
        )

    def _allowed(self) -> set[str]:
        ids = enabled_group_ids()
        if ids:
            return ids
        return self.settings.allowed_groups

    async def on_event(self, raw: dict[str, Any]) -> None:
        event = try_parse_group_message(raw)
        if event is None:
            return

        gcfg = load_group_config(event.group_id_str)

        # 仅处理已启用监听、且未屏蔽的群
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

    async def llm_scheduler_loop(self) -> None:
        logger = logging.getLogger(__name__)
        while True:
            try:
                for cfg in list_group_configs():
                    if cfg.blocked or not cfg.enabled or not cfg.llm_monitor.enabled:
                        continue
                    # 允许 1 分钟级调度；配置非法时回退到 60
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

    async def run(self) -> None:
        logger = logging.getLogger(__name__)
        allowed = self._allowed()
        if not allowed:
            logger.warning("当前无启用监控群；可在桌面端群列表中启用")

        app_settings = load_app_settings()
        ws = app_settings.onebot_ws_url or self.settings.onebot_ws_url
        token = app_settings.onebot_access_token or self.settings.onebot_access_token
        self.client.ws_url = build_ws_url(ws, token)
        self.client.access_token = token

        logger.info(
            "监听启动 | 启用监听群=%s | ws=%s",
            sorted(allowed) if allowed else ["(未启用任何群)"],
            ws,
        )

        async def _bootstrap_history() -> None:
            try:
                result = await pull_enabled_groups_history(count=80)
                logger.info("启动补拉历史完成: %s", result)
            except Exception:
                logger.exception("启动补拉历史失败")

        await asyncio.gather(
            self.client.run_forever(),
            self.llm_scheduler_loop(),
            _bootstrap_history(),
        )


async def _amain() -> None:
    settings = load_settings()
    setup_logging(settings)
    app = MonitorApp(settings)

    def _request_stop() -> None:
        logging.getLogger(__name__).info("收到停止信号，正在退出…")
        app.client.stop()

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
