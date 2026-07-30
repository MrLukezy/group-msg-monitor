"""OneBot WebSocket 客户端：鉴权、心跳忽略、断线指数退避重连。"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlencode, urlparse, urlunparse

import websockets

logger = logging.getLogger(__name__)

EventHandler = Callable[[dict[str, Any]], Awaitable[None]]


def build_ws_url(base_url: str, access_token: str) -> str:
    if not access_token:
        return base_url
    parsed = urlparse(base_url)
    query = parsed.query
    token_q = urlencode({"access_token": access_token})
    if query:
        query = f"{query}&{token_q}"
    else:
        query = token_q
    return urlunparse(parsed._replace(query=query))


class OneBotClient:
    def __init__(
        self,
        ws_url: str,
        access_token: str,
        on_event: EventHandler,
        *,
        reconnect_min_delay: float = 1.0,
        reconnect_max_delay: float = 60.0,
    ) -> None:
        self.ws_url = build_ws_url(ws_url, access_token)
        self.access_token = access_token
        self.on_event = on_event
        self.reconnect_min_delay = reconnect_min_delay
        self.reconnect_max_delay = reconnect_max_delay
        self._stop = asyncio.Event()
        self._ws: Any | None = None

    def stop(self) -> None:
        self._stop.set()
        if self._ws is not None:
            try:
                asyncio.get_running_loop().create_task(self._ws.close())
            except RuntimeError:
                pass

    async def run_forever(self) -> None:
        delay = self.reconnect_min_delay
        while not self._stop.is_set():
            try:
                await self._connect_once()
                delay = self.reconnect_min_delay
            except asyncio.CancelledError:
                raise
            except ConnectionRefusedError:
                logger.error(
                    "OneBot 连接被拒绝（%s），%.1fs 后重连。请确认 NapCat WS 已启动且端口正确",
                    self._safe_url_for_log(),
                    delay,
                )
            except OSError as exc:
                # WinError 1225 等常包装在 OSError 中
                logger.error(
                    "OneBot 连接失败: %s；目标=%s；%.1fs 后重连",
                    exc,
                    self._safe_url_for_log(),
                    delay,
                )
            except Exception:
                logger.exception("OneBot 连接异常，%.1fs 后重连")
            if self._stop.is_set():
                break
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
                break
            except TimeoutError:
                pass
            delay = min(delay * 2, self.reconnect_max_delay)

    async def _connect_once(self) -> None:
        headers: dict[str, str] = {}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"

        logger.info("正在连接 OneBot: %s", self._safe_url_for_log())
        async with websockets.connect(
            self.ws_url,
            additional_headers=headers,
            ping_interval=20,
            ping_timeout=20,
            max_size=50 * 1024 * 1024,
        ) as ws:
            self._ws = ws
            try:
                logger.info("OneBot WebSocket 已连接")
                async for raw in ws:
                    if self._stop.is_set():
                        break
                    await self._handle_raw(raw)
            finally:
                self._ws = None

    def _safe_url_for_log(self) -> str:
        parsed = urlparse(self.ws_url)
        if not parsed.query:
            return self.ws_url
        return urlunparse(parsed._replace(query="***"))

    async def _handle_raw(self, raw: str | bytes) -> None:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("收到非 JSON 消息，已忽略")
            return

        if not isinstance(event, dict):
            return

        # OneBot 心跳 / 元事件：不进入业务
        if event.get("post_type") == "meta_event":
            meta_type = event.get("meta_event_type")
            if meta_type == "heartbeat":
                logger.debug("heartbeat")
            elif meta_type == "lifecycle":
                logger.info("lifecycle: %s", event.get("sub_type"))
            return

        try:
            await self.on_event(event)
        except Exception:
            logger.exception("处理事件失败: %s", event.get("post_type"))
