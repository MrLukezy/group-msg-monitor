"""SQLite 消息落库（带 message_id 去重）。"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from app.models import GroupMessageEvent

logger = logging.getLogger(__name__)

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
"""


class StoreHandler:
    def __init__(self, sqlite_path: Path) -> None:
        self.sqlite_path = sqlite_path
        self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.sqlite_path)
        conn.execute("PRAGMA journal_mode=WAL;")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
        logger.info("SQLite 已就绪: %s", self.sqlite_path)

    async def handle(self, event: GroupMessageEvent) -> None:
        await self.handle_upsert(event)

    async def handle_upsert(self, event: GroupMessageEvent) -> bool:
        """写入消息；返回 True 表示新插入，False 表示已存在或失败。"""
        import json

        message_id = "" if event.message_id is None else str(event.message_id)
        payload = event.model_dump(mode="json")
        try:
            with self._connect() as conn:
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
                        event.message_summary,
                        json.dumps(payload, ensure_ascii=False),
                        event.time,
                    ),
                )
                return cur.rowcount > 0
        except sqlite3.Error:
            logger.exception("SQLite 写入失败 message_id=%s", message_id)
            return False
