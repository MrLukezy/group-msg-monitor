"""群消息实时监控服务入口。"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time
from logging.handlers import RotatingFileHandler
from typing import Any

from app.channels.feature_flags import TELEGRAM_CHANNEL_ENABLED, WECHAT_CHANNEL_ENABLED, GEWECHAT_CHANNEL_ENABLED
from app.config import Settings, load_settings
from app.filters import matched_keywords
from app.handlers.alert_handler import AlertHandler
from app.handlers.log_handler import LogHandler
from app.handlers.store_handler import StoreHandler
from app.llm.service import record_llm_failure, run_group_summary
from app.models import GroupMessageEvent, try_parse_group_message
from app.onebot_client import OneBotClient, build_ws_url
from app.settings_store import (
    DATA_DIR,
    ROOT_DIR,
    enabled_group_ids,
    list_group_configs,
    load_app_settings,
    load_group_config,
    resolve_llm_timing,
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

    lock_path = DATA_DIR / "monitor.lock"
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
        self._llm_queue: asyncio.Queue[str] = asyncio.Queue()
        self._llm_pending: set[str] = set()
        self._stop_event = asyncio.Event()
        self._stop_file = DATA_DIR / "monitor.stop"
        self._store_stop_task: asyncio.Task[None] | None = None
        self._received_count = 0
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self.qq_client: OneBotClient | None = None
        self.qq_passive = None
        self.tg_adapter: Any | None = None
        self.wx_adapter: WechatLocalAdapter | None = None
        self.gewe_adapter: Any | None = None

    def _allowed(self) -> set[str]:
        ids = enabled_group_ids()
        if ids:
            return ids
        return self.settings.allowed_groups

    def _spawn_background(self, coro: Any, *, label: str) -> None:
        task = asyncio.create_task(coro, name=label)
        self._background_tasks.add(task)

        def _done(done: asyncio.Task[Any]) -> None:
            self._background_tasks.discard(done)
            if not done.cancelled() and done.exception() is not None:
                logging.getLogger(__name__).error(
                    "后台任务失败 task=%s error=%s",
                    label,
                    done.exception(),
                )

        task.add_done_callback(_done)

    async def handle_group_event(self, event: GroupMessageEvent) -> None:
        gcfg = load_group_config(event.group_id_str)
        if self.store_handler is not None:
            await self.store_handler.enqueue(
                event,
                live=gcfg.enabled,
                persist=gcfg.enabled and gcfg.basic.storage_enabled,
            )
        if not gcfg.enabled:
            return

        if gcfg.basic.log_all:
            await self.log_handler.handle(event)

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
                    self._spawn_background(
                        alert.handle(event, hits),
                        label=f"keyword-alert-{event.group_id_str}",
                    )

    async def on_onebot_event(self, raw: dict[str, Any]) -> None:
        started = time.perf_counter()
        event = try_parse_group_message(raw)
        if event is None:
            return
        await self.handle_group_event(event)
        self._received_count += 1
        elapsed_ms = (time.perf_counter() - started) * 1000
        if elapsed_ms >= 50 or self._received_count % 500 == 0:
            logging.getLogger(__name__).info(
                "OneBot 消息接收处理 group=%s elapsed_ms=%.1f total=%s",
                event.group_id_str,
                elapsed_ms,
                self._received_count,
            )

    async def llm_scheduler_loop(self) -> None:
        logger = logging.getLogger(__name__)
        while not self._stop_event.is_set():
            try:
                global_llm = load_app_settings().llm
                for cfg in list_group_configs():
                    if not cfg.enabled or not cfg.llm_monitor.enabled:
                        continue
                    every, _, _ = resolve_llm_timing(global_llm, cfg.llm_monitor)
                    last = self._llm_last_run.get(cfg.group_id, 0)
                    if time.time() - last < every * 60:
                        continue
                    self._llm_last_run[cfg.group_id] = time.time()
                    if cfg.group_id not in self._llm_pending:
                        self._llm_pending.add(cfg.group_id)
                        await self._llm_queue.put(cfg.group_id)
                        logger.info("LLM 定时任务已入后台队列 group=%s", cfg.group_id)
            except Exception:
                logger.exception("LLM scheduler 异常")
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=15)
            except asyncio.TimeoutError:
                pass

    @staticmethod
    def _run_llm_job(group_id: str) -> dict[str, Any]:
        return asyncio.run(run_group_summary(group_id, job_type="schedule"))

    async def llm_worker_loop(self) -> None:
        logger = logging.getLogger(__name__)
        while not self._stop_event.is_set() or not self._llm_queue.empty():
            try:
                group_id = await asyncio.wait_for(self._llm_queue.get(), timeout=1)
            except asyncio.TimeoutError:
                continue
            started = time.perf_counter()
            try:
                result = await asyncio.to_thread(self._run_llm_job, group_id)
                logger.info(
                    "LLM 后台任务完成 group=%s status=%s msg=%s elapsed=%.1fs",
                    group_id,
                    result.get("status"),
                    result.get("msg_count"),
                    time.perf_counter() - started,
                )
            except Exception as e:
                logger.exception("LLM 后台任务失败 group=%s", group_id)
                record_llm_failure(
                    group_id,
                    job_type="schedule",
                    exc=e,
                    stage="scheduler_worker",
                )
            finally:
                self._llm_pending.discard(group_id)
                self._llm_queue.task_done()

    async def stop_file_watcher(self) -> None:
        while not self._stop_event.is_set():
            if self._stop_file.exists():
                try:
                    self._stop_file.unlink()
                except OSError:
                    pass
                logging.getLogger(__name__).info("收到桌面端优雅停止请求")
                self.stop()
                return
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=0.2)
            except asyncio.TimeoutError:
                pass

    def stop(self) -> None:
        self._stop_event.set()
        if self.qq_client is not None:
            self.qq_client.stop()
        if self.qq_passive is not None:
            self.qq_passive.stop()
        if self.tg_adapter is not None:
            self.tg_adapter.stop()
        if self.wx_adapter is not None:
            self.wx_adapter.stop()
        if self.gewe_adapter is not None:
            self.gewe_adapter.stop()
        if self.store_handler is not None and self._store_stop_task is None:
            try:
                self._store_stop_task = asyncio.get_running_loop().create_task(
                    self.store_handler.stop(), name="message-store-drain"
                )
            except RuntimeError:
                pass

    async def run(self) -> None:
        logger = logging.getLogger(__name__)
        allowed = self._allowed()
        if not allowed:
            logger.warning("当前无启用监控群；可在桌面端群列表中启用")

        app_settings = load_app_settings()
        ch = app_settings.channels
        try:
            self._stop_file.unlink(missing_ok=True)
        except OSError:
            pass
        if self.store_handler is not None:
            await self.store_handler.start()
        tasks: list[Any] = [
            self.llm_scheduler_loop(),
            self.llm_worker_loop(),
            self.stop_file_watcher(),
        ]

        # QQ：OneBot 完整监听，或官方 QQ 被动采集（通知 + UIA）。
        if ch.qq.bound:
            if ch.qq.mode == "passive":
                from app.channels.qq_passive import QqPassiveAdapter

                self.qq_passive = QqPassiveAdapter(
                    on_message=self.handle_group_event,
                    poll_seconds=ch.qq.poll_seconds,
                    group_name_map=ch.qq.group_name_map,
                )
                tasks.append(self.qq_passive.run_forever())
                logger.info(
                    "通道 QQ 已启用 | mode=passive poll=%.1fs map=%s",
                    ch.qq.poll_seconds,
                    len(ch.qq.group_name_map),
                )
            else:
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
                logger.info("通道 QQ 已启用 | mode=onebot ws=%s", ws)

        else:
            logger.info("通道 QQ 未绑定，跳过 QQ 采集")

        # Telegram 暂未支持；功能开关恢复后才加载适配器。
        if TELEGRAM_CHANNEL_ENABLED and ch.telegram.bound:
            from app.channels.telegram import TelegramUserAdapter

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

        # GeWeChat（iPad 协议，只收不发）
        if GEWECHAT_CHANNEL_ENABLED and ch.gewechat.bound:
            from app.channels.gewechat import GeWeChatAdapter

            if not ch.gewechat.base_url or not ch.gewechat.app_id:
                logger.warning("GeWeChat 已绑定但缺少 base_url / app_id")
            else:
                self.gewe_adapter = GeWeChatAdapter(
                    base_url=ch.gewechat.base_url,
                    token=ch.gewechat.token,
                    app_id=ch.gewechat.app_id,
                    on_message=self.handle_group_event,
                    callback_host=ch.gewechat.callback_host,
                    callback_port=ch.gewechat.callback_port,
                    wxid=ch.gewechat.wxid,
                )
                tasks.append(self.gewe_adapter.run_forever())
                logger.info(
                    "通道 GeWeChat 已启用 | app_id=%s label=%s",
                    ch.gewechat.app_id,
                    ch.gewechat.label or ch.gewechat.wxid or "",
                )
        elif ch.gewechat.bound and not GEWECHAT_CHANNEL_ENABLED:
            logger.info("GeWeChat 通道已关闭，忽略绑定配置")

        if len(tasks) == 3:
            logger.warning("未绑定任何消息通道；请在总配置中绑定 QQ")

        logger.info(
            "监听启动 | 启用监听群=%s",
            sorted(allowed) if allowed else ["(未启用任何群)"],
        )
        try:
            await asyncio.gather(*tasks)
        finally:
            self._stop_event.set()
            if self._store_stop_task is not None:
                await self._store_stop_task
            elif self.store_handler is not None:
                await self.store_handler.stop()


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
