"""从 OneBot 拉取群历史消息并落库（补齐实时推送缺口）。"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import websockets

from app.handlers.store_handler import StoreHandler
from app.llm.service import sqlite_path
from app.models import try_parse_group_message
from app.onebot_client import build_ws_url
from app.settings_store import load_app_settings

logger = logging.getLogger(__name__)


async def _call(
    ws: websockets.ClientConnection,
    action: str,
    params: dict[str, Any],
    echo: str,
    *,
    timeout: float = 20,
) -> dict[str, Any]:
    await ws.send(json.dumps({"action": action, "params": params, "echo": echo}))
    while True:
        raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
        data = json.loads(raw)
        if isinstance(data, dict) and data.get("echo") == echo:
            return data


def _extract_messages(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("data")
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in ("messages", "message"):
            arr = data.get(key)
            if isinstance(arr, list):
                return [x for x in arr if isinstance(x, dict)]
    return []


async def pull_group_history(group_id: str, count: int = 100) -> dict[str, Any]:
    """拉取最近 count 条群消息并写入 SQLite，返回统计。"""
    from app.channels.ids import channel_of_group_id

    if channel_of_group_id(group_id) != "qq":
        return {
            "ok": False,
            "inserted": 0,
            "skipped": 0,
            "message": "仅 QQ / OneBot 群支持主动拉取历史；微信与 Telegram 依赖实时监听",
        }
    settings = load_app_settings()
    if not settings.channels.qq.bound:
        raise RuntimeError("QQ 通道未绑定")
    ws_url = build_ws_url(settings.onebot_ws_url, settings.onebot_access_token)
    count = max(1, min(int(count or 100), 200))
    store = StoreHandler(sqlite_path())

    async with websockets.connect(
        ws_url,
        open_timeout=8,
        max_size=50 * 1024 * 1024,
    ) as ws:
        resp = await _call(
            ws,
            "get_group_msg_history",
            {"group_id": int(group_id) if str(group_id).isdigit() else group_id, "count": count},
            f"hist-{group_id}",
        )

    if resp.get("status") != "ok" and resp.get("retcode") not in (0, None):
        raise RuntimeError(
            f"拉取历史失败: status={resp.get('status')} retcode={resp.get('retcode')} "
            f"{resp.get('message') or resp.get('wording') or ''}"
        )

    messages = _extract_messages(resp)
    inserted = 0
    skipped = 0
    for raw in messages:
        # 历史消息通常已是 OneBot message 结构，补齐必要字段
        event_raw = dict(raw)
        event_raw.setdefault("post_type", "message")
        event_raw.setdefault("message_type", "group")
        event_raw.setdefault("group_id", group_id)
        event = try_parse_group_message(event_raw)
        if event is None:
            skipped += 1
            continue
        changed = await store.handle_upsert(event)
        if changed:
            inserted += 1
        else:
            skipped += 1

    return {
        "ok": True,
        "groupId": str(group_id),
        "fetched": len(messages),
        "inserted": inserted,
        "skipped": skipped,
    }


async def pull_enabled_groups_history(count: int = 50) -> dict[str, Any]:
    from app.channels.ids import channel_of_group_id
    from app.settings_store import list_group_configs

    results = []
    for cfg in list_group_configs():
        if cfg.blocked or not cfg.enabled:
            continue
        if (cfg.channel or channel_of_group_id(cfg.group_id)) != "qq":
            continue
        try:
            results.append(await pull_group_history(cfg.group_id, count=count))
        except Exception as e:
            logger.exception("启动补拉历史失败 group=%s", cfg.group_id)
            results.append({"ok": False, "groupId": cfg.group_id, "error": str(e)})
    return {"ok": True, "results": results}
