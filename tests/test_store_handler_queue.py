from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path

from app.handlers.store_handler import StoreHandler
from app.models import GroupMessageEvent


def _event(group_id: str, message_id: int, event_time: int) -> GroupMessageEvent:
    return GroupMessageEvent(
        post_type="message",
        message_type="group",
        group_id=group_id,
        user_id=10001,
        message_id=message_id,
        message=f"message-{message_id}",
        time=event_time,
    )


class StoreHandlerQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_permanent_write_failure_uses_recovery_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "messages.db"
            store = StoreHandler(db)

            def broken_write(_batch: list[object]) -> None:
                raise ValueError("permanent test failure")

            store._write_batch = broken_write  # type: ignore[method-assign]
            await store.start()
            await store.enqueue(
                _event("998", 1, 1_700_000_000),
                live=True,
                persist=True,
            )
            await store.stop()

            recovery = db.parent / "failed_messages.jsonl"
            self.assertTrue(recovery.exists())
            self.assertIn('"group_id": "998"', recovery.read_text(encoding="utf-8"))

    async def test_transient_write_failure_retries_without_loss(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "messages.db"
            store = StoreHandler(db)
            original = store._write_batch
            attempts = 0

            def flaky_write(batch: list[object]) -> None:
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise sqlite3.OperationalError("database is locked")
                original(batch)  # type: ignore[arg-type]

            store._write_batch = flaky_write  # type: ignore[method-assign]
            await store.start()
            await store.enqueue(
                _event("999", 1, 1_700_000_000),
                live=True,
                persist=True,
            )
            await store.stop()

            with closing(sqlite3.connect(db)) as conn:
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
                    1,
                )
            self.assertGreaterEqual(attempts, 2)

    async def test_batch_queue_persists_without_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "messages.db"
            store = StoreHandler(db)
            await store.start()
            for index in range(30):
                await store.enqueue(
                    _event("123", index + 1, 1_700_000_000 + index),
                    live=True,
                    persist=True,
                )
            # 重复 message_id 不应产生重复消息。
            await store.enqueue(
                _event("123", 1, 1_700_000_000),
                live=True,
                persist=True,
            )
            # 未启用群只更新活跃元数据。
            await store.enqueue(
                _event("456", 99, 1_700_000_100),
                live=False,
                persist=False,
            )
            await store.stop()

            with closing(sqlite3.connect(db)) as conn:
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM live_messages WHERE group_id='123'"
                    ).fetchone()[0],
                    30,
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM messages WHERE group_id='123'"
                    ).fetchone()[0],
                    30,
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM messages WHERE group_id='456'"
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT received_count FROM group_activity WHERE group_id='123'"
                    ).fetchone()[0],
                    31,
                )

    async def test_medium_load_batches_thirty_groups(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "messages.db"
            store = StoreHandler(db)
            await store.start()
            started = time.perf_counter()
            for group in range(30):
                for index in range(20):
                    await store.enqueue(
                        _event(
                            str(10_000 + group),
                            group * 1000 + index + 1,
                            1_700_000_000 + index,
                        ),
                        live=True,
                        persist=True,
                    )
            await store.stop()
            elapsed = time.perf_counter() - started

            with closing(sqlite3.connect(db)) as conn:
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM live_messages").fetchone()[0],
                    600,
                )
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
                    600,
                )
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM group_activity").fetchone()[0],
                    30,
                )
            self.assertLess(elapsed, 5.0)


if __name__ == "__main__":
    unittest.main()
