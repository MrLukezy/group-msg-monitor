"""从 OneBot 拉取群历史消息并落库（补齐实时推送缺口）。"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from contextlib import closing
from typing import Any

import websockets

from app.handlers.store_handler import StoreHandler
from app.llm.service import sqlite_path
from app.models import GroupMessageEvent, try_parse_group_message
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
    *,
    materialize_images: bool = True,
) -> tuple[int, int]:
    inserted = 0
    skipped = 0
    parsed: list[GroupMessageEvent] = []
    for raw in messages:
        event_raw = dict(raw)
        event_raw.setdefault("post_type", "message")
        event_raw.setdefault("message_type", "group")
        event_raw.setdefault("group_id", group_id)
        event = try_parse_group_message(event_raw)
        if event is None:
            skipped += 1
            continue
        if not materialize_images:
            parsed.append(event)
            continue
        changed = await store.handle_upsert(
            event, materialize_images=materialize_images
        )
        if changed:
            inserted += 1
        else:
            skipped += 1
    if parsed:
        inserted = await store.handle_many_upsert_raw(parsed)
        skipped += len(parsed) - inserted
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
        if not cfg.enabled:
            continue
        if (cfg.channel or channel_of_group_id(cfg.group_id)) != "qq":
            continue
        try:
            results.append(await pull_group_history(cfg.group_id, count=count))
        except Exception as e:
            logger.exception("启动补拉历史失败 group=%s", cfg.group_id)
            results.append({"ok": False, "groupId": cfg.group_id, "error": str(e)})
    return {"ok": True, "results": results}


async def pull_groups_recent_history(
    group_ids: list[str], count: int = 10
) -> dict[str, Any]:
    """为群列表同步短期消息；未启用群只记录最近消息时间。"""
    from app.settings_store import list_group_configs

    ids = list(dict.fromkeys(str(x).strip() for x in group_ids if str(x).strip()))
    if not ids:
        return {"ok": True, "groups": 0, "succeeded": 0, "failed": 0, "fetched": 0}
    settings = load_app_settings()
    if not settings.channels.qq.bound:
        raise RuntimeError("QQ 通道未绑定")
    enabled = {
        c.group_id
        for c in list_group_configs()
        if c.enabled and c.basic.storage_enabled
    }
    ws_url = build_ws_url(settings.onebot_ws_url, settings.onebot_access_token)
    per_group = max(1, min(int(count or 10), 20))
    store = StoreHandler(sqlite_path())
    activity_updates: list[tuple[str, int]] = []
    results: list[dict[str, Any]] = []
    fetched_total = 0

    async with websockets.connect(
        ws_url,
        open_timeout=8,
        max_size=50 * 1024 * 1024,
    ) as ws:
        for index, group_id in enumerate(ids, 1):
            gid_param: Any = int(group_id) if group_id.isdigit() else group_id
            try:
                resp = await _call(
                    ws,
                    "get_group_msg_history",
                    {"group_id": gid_param, "count": per_group},
                    f"recent-{index}-{group_id}",
                    timeout=15,
                )
                if resp.get("status") != "ok" and resp.get("retcode") not in (0, None):
                    raise RuntimeError(
                        f"status={resp.get('status')} retcode={resp.get('retcode')}"
                    )
                messages = _extract_messages(resp)
                fetched_total += len(messages)
                latest_ts = 0
                for raw in messages:
                    try:
                        latest_ts = max(latest_ts, int(raw.get("time") or 0))
                    except Exception:
                        pass
                if latest_ts:
                    activity_updates.append((group_id, latest_ts))
                inserted = 0
                if group_id in enabled and messages:
                    inserted, _ = await _store_messages(
                        store,
                        group_id,
                        messages,
                        materialize_images=False,
                    )
                results.append(
                    {
                        "groupId": group_id,
                        "ok": True,
                        "fetched": len(messages),
                        "inserted": inserted,
                        "lastTime": latest_ts,
                    }
                )
            except Exception as exc:
                logger.warning("同步群近期消息失败 group=%s error=%s", group_id, exc)
                results.append({"groupId": group_id, "ok": False, "error": str(exc)})
            await asyncio.sleep(0.02)

    if activity_updates:
        with closing(sqlite3.connect(sqlite_path(), timeout=5)) as conn, conn:
            conn.execute("PRAGMA busy_timeout=5000")
            conn.executemany(
                """
                INSERT INTO group_activity
                    (group_id, last_event_time, last_received_at, received_count)
                VALUES (?, ?, ?, 0)
                ON CONFLICT(group_id) DO UPDATE SET
                    last_event_time=MAX(
                        COALESCE(group_activity.last_event_time, 0),
                        COALESCE(excluded.last_event_time, 0)
                    )
                """,
                [(gid, ts, ts) for gid, ts in activity_updates],
            )

    succeeded = sum(1 for r in results if r.get("ok"))
    return {
        "ok": succeeded == len(results),
        "groups": len(ids),
        "succeeded": succeeded,
        "failed": len(results) - succeeded,
        "fetched": fetched_total,
        "results": results,
    }
