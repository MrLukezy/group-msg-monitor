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

# 单次 OneBot 请求条数上限（多数实现支持到 20~100；NapCat 常可到 100+）
_PAGE_SIZE = 100
_MAX_TOTAL = 500


async def _call(
    ws: websockets.ClientConnection,
    action: str,
    params: dict[str, Any],
    echo: str,
    *,
    timeout: float = 25,
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


def _message_seq(msg: dict[str, Any]) -> int | None:
    for key in ("message_seq", "message_id", "real_id", "id"):
        v = msg.get(key)
        if v is None:
            continue
        try:
            n = int(v)
            if n > 0:
                return n
        except Exception:
            continue
    return None


def _oldest_seq(messages: list[dict[str, Any]]) -> int | None:
    seqs = [_message_seq(m) for m in messages]
    nums = [s for s in seqs if s is not None]
    return min(nums) if nums else None


async def _store_messages(
    store: StoreHandler,
    group_id: str,
    messages: list[dict[str, Any]],
) -> tuple[int, int]:
    inserted = 0
    skipped = 0
    for raw in messages:
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
    return inserted, skipped


async def pull_group_history(group_id: str, count: int = 200) -> dict[str, Any]:
    """
    拉取群历史并写入 SQLite。
    会按页向前翻（message_seq），最多约 _MAX_TOTAL 条，以补启用前 / 断线缺口。
    """
    from app.channels.ids import channel_of_group_id

    if channel_of_group_id(group_id) != "qq":
        return {
            "ok": False,
            "inserted": 0,
            "skipped": 0,
            "fetched": 0,
            "message": "仅 QQ / OneBot 群支持主动拉取历史；微信与 Telegram 依赖实时监听",
        }
    settings = load_app_settings()
    if not settings.channels.qq.bound:
        raise RuntimeError("QQ 通道未绑定")
    ws_url = build_ws_url(settings.onebot_ws_url, settings.onebot_access_token)
    want = max(1, min(int(count or 200), _MAX_TOTAL))
    store = StoreHandler(sqlite_path())
    gid_param: Any = int(group_id) if str(group_id).isdigit() else group_id

    fetched = 0
    inserted = 0
    skipped = 0
    pages = 0
    cursor: int | None = None
    seen_seqs: set[int] = set()

    async with websockets.connect(
        ws_url,
        open_timeout=8,
        max_size=50 * 1024 * 1024,
    ) as ws:
        while fetched < want:
            page = min(_PAGE_SIZE, want - fetched)
            params: dict[str, Any] = {"group_id": gid_param, "count": page}
            if cursor is not None:
                params["message_seq"] = cursor
            pages += 1
            resp = await _call(
                ws,
                "get_group_msg_history",
                params,
                f"hist-{group_id}-{pages}",
            )
            if resp.get("status") != "ok" and resp.get("retcode") not in (0, None):
                if pages == 1:
                    raise RuntimeError(
                        f"拉取历史失败: status={resp.get('status')} retcode={resp.get('retcode')} "
                        f"{resp.get('message') or resp.get('wording') or ''}"
                    )
                logger.warning(
                    "历史翻页中止 group=%s page=%s retcode=%s",
                    group_id,
                    pages,
                    resp.get("retcode"),
                )
                break

            messages = _extract_messages(resp)
            if not messages:
                break

            # 去重翻页（部分实现会重复返回边界消息）
            fresh: list[dict[str, Any]] = []
            for m in messages:
                seq = _message_seq(m)
                if seq is not None and seq in seen_seqs:
                    continue
                if seq is not None:
                    seen_seqs.add(seq)
                fresh.append(m)
            if not fresh:
                break

            ins, skip = await _store_messages(store, group_id, fresh)
            fetched += len(fresh)
            inserted += ins
            skipped += skip

            oldest = _oldest_seq(fresh)
            if oldest is None or (cursor is not None and oldest >= cursor):
                break
            # 下一页从更早的 seq 继续
            cursor = oldest
            if len(messages) < page:
                break
            await asyncio.sleep(0.05)

    return {
        "ok": True,
        "groupId": str(group_id),
        "fetched": fetched,
        "inserted": inserted,
        "skipped": skipped,
        "pages": pages,
    }


async def pull_enabled_groups_history(count: int = 200) -> dict[str, Any]:
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
