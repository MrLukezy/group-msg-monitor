"""SQLite 消息落库（带 message_id 去重）。"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from contextlib import closing
from pathlib import Path

from app.media_store import materialize_event_images
from app.models import GroupMessageEvent

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _StoreJob:
    event: GroupMessageEvent
    live: bool
    persist: bool
    queued_at: float

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT,
    group_id TEXT NOT NULL,
    user_id TEXT,
    sender_name TEXT,
    content TEXT,
    raw_json TEXT,
    event_time INTEGER,
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    UNIQUE(group_id, message_id)
);

CREATE INDEX IF NOT EXISTS idx_messages_group_time
ON messages(group_id, event_time DESC);

CREATE TABLE IF NOT EXISTS live_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT,
    group_id TEXT NOT NULL,
    user_id TEXT,
    sender_name TEXT,
    content TEXT,
    event_time INTEGER,
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    UNIQUE(group_id, message_id)
);

CREATE INDEX IF NOT EXISTS idx_live_messages_group_time
ON live_messages(group_id, event_time DESC);

CREATE TABLE IF NOT EXISTS group_activity (
    group_id TEXT PRIMARY KEY,
    last_event_time INTEGER,
    last_received_at INTEGER NOT NULL,
    received_count INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_group_activity_received
ON group_activity(last_received_at DESC);
"""


class StoreHandler:
    def __init__(self, sqlite_path: Path) -> None:
        self.sqlite_path = sqlite_path
        self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_queue: asyncio.Queue[_StoreJob | None] = asyncio.Queue(maxsize=5000)
        self._media_queue: asyncio.Queue[GroupMessageEvent | None] = asyncio.Queue(maxsize=1000)
        self._writer_task: asyncio.Task[None] | None = None
        self._media_task: asyncio.Task[None] | None = None
        self._writer_conn: sqlite3.Connection | None = None
        self._written = 0
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.sqlite_path, timeout=5, check_same_thread=False)
        conn.execute("PRAGMA busy_timeout=5000;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        return conn

    def _init_db(self) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.executescript(_SCHEMA)
        logger.info("SQLite 已就绪: %s", self.sqlite_path)

    async def start(self) -> None:
        if self._writer_task is not None:
            return
        self._writer_conn = self._connect()
        self._writer_task = asyncio.create_task(
            self._writer_loop(), name="message-store-writer"
        )
        self._media_task = asyncio.create_task(
            self._media_loop(), name="message-media-worker"
        )

    async def stop(self) -> None:
        if self._writer_task is None:
            return
        await self._write_queue.put(None)
        await self._writer_task
        self._writer_task = None
        await self._media_queue.put(None)
        if self._media_task is not None:
            try:
                await asyncio.wait_for(self._media_task, timeout=3)
            except asyncio.TimeoutError:
                self._media_task.cancel()
                await asyncio.gather(self._media_task, return_exceptions=True)
                pending = self._media_queue.qsize()
                logger.warning(
                    "退出时取消未完成的媒体本地化任务 pending=%s；消息原文已落库",
                    pending,
                )
            self._media_task = None
        if self._writer_conn is not None:
            self._writer_conn.close()
            self._writer_conn = None

    async def enqueue(
        self,
        event: GroupMessageEvent,
        *,
        live: bool,
        persist: bool,
    ) -> None:
        """把消息放入热路径队列；队列满时施加背压而不是丢消息。"""
        await self._write_queue.put(
            _StoreJob(
                event=event,
                live=bool(live),
                persist=bool(persist),
                queued_at=time.perf_counter(),
            )
        )

    async def _writer_loop(self) -> None:
        while True:
            first = await self._write_queue.get()
            if first is None:
                self._write_queue.task_done()
                break
            batch = [first]
            stop_after_batch = False
            deadline = asyncio.get_running_loop().time() + 0.075
            while len(batch) < 100:
                remain = deadline - asyncio.get_running_loop().time()
                if remain <= 0:
                    break
                try:
                    item = await asyncio.wait_for(self._write_queue.get(), timeout=remain)
                except asyncio.TimeoutError:
                    break
                if item is None:
                    self._write_queue.task_done()
                    stop_after_batch = True
                    break
                batch.append(item)

            retry = 0
            while True:
                try:
                    await asyncio.to_thread(self._write_batch, batch)
                    break
                except Exception as exc:
                    retry += 1
                    retryable = isinstance(exc, sqlite3.OperationalError) and any(
                        word in str(exc).lower() for word in ("locked", "busy")
                    )
                    max_retries = 3 if retryable else 2
                    if retry >= max_retries:
                        self._write_dead_letter(batch, exc)
                        logger.critical(
                            "SQLite 批量写入最终失败，已写入恢复文件 batch=%s error=%s",
                            len(batch),
                            exc,
                        )
                        break
                    delay = min(2.0, 0.05 * (2 ** min(retry, 5)))
                    logger.exception(
                        "SQLite 批量写入失败，保留批次并重试 batch=%s retry=%s delay=%.2fs",
                        len(batch),
                        retry,
                        delay,
                    )
                    await asyncio.sleep(delay)
            for _ in batch:
                self._write_queue.task_done()

            for job in batch:
                if job.persist and "[cq:image" in job.event.message_summary.lower():
                    try:
                        self._media_queue.put_nowait(job.event)
                    except asyncio.QueueFull:
                        logger.warning(
                            "媒体队列已满，暂不本地化图片 message_id=%s",
                            job.event.message_id,
                        )
            if stop_after_batch:
                return

    def _write_batch(self, batch: list[_StoreJob]) -> None:
        conn = self._writer_conn
        if conn is None:
            return
        started = time.perf_counter()
        now = int(time.time())
        with conn:
            for job in batch:
                event = job.event
                message_id = "" if event.message_id is None else str(event.message_id)
                conn.execute(
                    """
                    INSERT INTO group_activity
                        (group_id, last_event_time, last_received_at, received_count)
                    VALUES (?, ?, ?, 1)
                    ON CONFLICT(group_id) DO UPDATE SET
                        last_event_time=MAX(
                            COALESCE(group_activity.last_event_time, 0),
                            COALESCE(excluded.last_event_time, 0)
                        ),
                        last_received_at=excluded.last_received_at,
                        received_count=group_activity.received_count + 1
                    """,
                    (event.group_id_str, event.time, now),
                )
                if job.live:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO live_messages
                            (message_id, group_id, user_id, sender_name, content, event_time)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            message_id,
                            event.group_id_str,
                            "" if event.user_id is None else str(event.user_id),
                            event.display_name,
                            event.message_summary,
                            event.time,
                        ),
                    )
                if job.persist:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO messages
                            (message_id, group_id, user_id, sender_name, content, raw_json, event_time)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            message_id,
                            event.group_id_str,
                            "" if event.user_id is None else str(event.user_id),
                            event.display_name,
                            event.message_summary,
                            json.dumps(event.model_dump(mode="json"), ensure_ascii=False),
                            event.time,
                        ),
                    )
            self._written += len(batch)
            if self._written % 100 < len(batch):
                conn.execute(
                    """
                    DELETE FROM live_messages
                    WHERE id NOT IN (
                        SELECT id FROM live_messages ORDER BY id DESC LIMIT 5000
                    )
                    """
                )
        elapsed_ms = (time.perf_counter() - started) * 1000
        queue_ms = max(
            (time.perf_counter() - job.queued_at) * 1000 for job in batch
        )
        if elapsed_ms >= 50 or queue_ms >= 500 or self._written % 500 < len(batch):
            logger.info(
                "消息批量落库 batch=%s write_ms=%.1f max_queue_ms=%.1f total=%s",
                len(batch),
                elapsed_ms,
                queue_ms,
                self._written,
            )

    def _write_dead_letter(self, batch: list[_StoreJob], exc: Exception) -> None:
        path = self.sqlite_path.parent / "failed_messages.jsonl"
        try:
            with path.open("a", encoding="utf-8") as fh:
                for job in batch:
                    fh.write(
                        json.dumps(
                            {
                                "error": str(exc),
                                "live": job.live,
                                "persist": job.persist,
                                "event": job.event.model_dump(mode="json"),
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
        except Exception:
            logger.exception(
                "消息恢复文件写入失败 batch=%s path=%s",
                len(batch),
                path,
            )

    async def _media_loop(self) -> None:
        while True:
            event = await self._media_queue.get()
            if event is None:
                self._media_queue.task_done()
                return
            message_id = "" if event.message_id is None else str(event.message_id)
            try:
                content = await materialize_event_images(event)
                await asyncio.to_thread(
                    self._update_materialized_content,
                    event.group_id_str,
                    message_id,
                    content,
                )
            except Exception:
                logger.exception("后台图片本地化失败 message_id=%s", message_id)
            finally:
                self._media_queue.task_done()

    def _update_materialized_content(
        self, group_id: str, message_id: str, content: str
    ) -> None:
        if not message_id:
            return
        try:
            with closing(self._connect()) as conn, conn:
                conn.execute(
                    "UPDATE messages SET content=? WHERE group_id=? AND message_id=?",
                    (content, group_id, message_id),
                )
        except sqlite3.Error:
            logger.exception("图片本地化结果回写失败 message_id=%s", message_id)

    async def handle(self, event: GroupMessageEvent) -> None:
        await self.handle_upsert(event)

    async def handle_many_upsert_raw(
        self, events: list[GroupMessageEvent]
    ) -> int:
        """在一个事务中写入一批无需图片本地化的历史消息。"""
        if not events:
            return 0
        return await asyncio.to_thread(self._insert_many_raw, events)

    def _insert_many_raw(self, events: list[GroupMessageEvent]) -> int:
        rows = [
            (
                "" if event.message_id is None else str(event.message_id),
                event.group_id_str,
                "" if event.user_id is None else str(event.user_id),
                event.display_name,
                event.message_summary,
                json.dumps(event.model_dump(mode="json"), ensure_ascii=False),
                event.time,
            )
            for event in events
        ]
        with closing(self._connect()) as conn, conn:
            before = conn.total_changes
            conn.executemany(
                """
                INSERT OR IGNORE INTO messages
                    (message_id, group_id, user_id, sender_name, content, raw_json, event_time)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            return conn.total_changes - before

    async def record_activity(self, event: GroupMessageEvent) -> None:
        """只记录群活跃时间，不保存未启用群的消息正文。"""
        received_at = int(time.time())
        try:
            with closing(self._connect()) as conn, conn:
                conn.execute(
                    """
                    INSERT INTO group_activity
                        (group_id, last_event_time, last_received_at, received_count)
                    VALUES (?, ?, ?, 1)
                    ON CONFLICT(group_id) DO UPDATE SET
                        last_event_time = MAX(
                            COALESCE(group_activity.last_event_time, 0),
                            COALESCE(excluded.last_event_time, 0)
                        ),
                        last_received_at = excluded.last_received_at,
                        received_count = group_activity.received_count + 1
                    """,
                    (event.group_id_str, event.time, received_at),
                )
        except sqlite3.Error:
            logger.exception("群活跃时间写入失败 group_id=%s", event.group_id_str)

    async def record_live_message(self, event: GroupMessageEvent) -> None:
        """保存已启用监听群的实时滚动消息，不触发媒体下载。"""
        message_id = "" if event.message_id is None else str(event.message_id)
        try:
            with closing(self._connect()) as conn, conn:
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO live_messages
                    (message_id, group_id, user_id, sender_name, content, event_time)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        message_id,
                        event.group_id_str,
                        "" if event.user_id is None else str(event.user_id),
                        event.display_name,
                        event.message_summary,
                        event.time,
                    ),
                )
                # 实时流仅保留最近 5000 条，避免“监听所有群”无限增长数据库。
                if cur.rowcount > 0 and cur.lastrowid and int(cur.lastrowid) % 50 == 0:
                    conn.execute(
                        """
                        DELETE FROM live_messages
                        WHERE id NOT IN (
                            SELECT id FROM live_messages ORDER BY id DESC LIMIT 5000
                        )
                        """
                    )
        except sqlite3.Error:
            logger.exception("实时消息写入失败 message_id=%s", message_id)

    async def handle_upsert(
        self, event: GroupMessageEvent, *, materialize_images: bool = True
    ) -> bool:
        """写入消息；返回 True 表示新插入，False 表示已存在或失败。"""
        message_id = "" if event.message_id is None else str(event.message_id)
        payload = event.model_dump(mode="json")
        content = event.message_summary
        if materialize_images:
            try:
                content = await materialize_event_images(event)
            except Exception:
                logger.exception("图片本地化失败 message_id=%s，回退原文", message_id)
        try:
            with closing(self._connect()) as conn, conn:
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO messages
                    (message_id, group_id, user_id, sender_name, content, raw_json, event_time)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        message_id,
                        event.group_id_str,
                        "" if event.user_id is None else str(event.user_id),
                        event.display_name,
                        content,
                        json.dumps(payload, ensure_ascii=False),
                        event.time,
                    ),
                )
                return cur.rowcount > 0
        except sqlite3.Error:
            logger.exception("SQLite 写入失败 message_id=%s", message_id)
            return False
