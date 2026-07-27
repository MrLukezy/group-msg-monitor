"""本地 mock OneBot WS，用于无 NapCat 时验证监控服务。"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parent.parent


async def handler(ws: websockets.ServerConnection) -> None:
    await ws.send(
        json.dumps(
            {
                "post_type": "meta_event",
                "meta_event_type": "lifecycle",
                "sub_type": "connect",
            }
        )
    )
    await asyncio.sleep(0.3)
    await ws.send(
        json.dumps(
            {
                "post_type": "message",
                "message_type": "group",
                "group_id": 123456789,
                "user_id": 111,
                "message_id": 10001,
                "raw_message": "测试消息：紧急情况",
                "sender": {"nickname": "测试用户", "card": "值班员"},
                "time": 1720000000,
            }
        )
    )
    await ws.send(
        json.dumps(
            {
                "post_type": "message",
                "message_type": "group",
                "group_id": 999999999,
                "user_id": 222,
                "message_id": 10002,
                "raw_message": "其他群应被忽略",
                "sender": {"nickname": "路人"},
                "time": 1720000001,
            }
        )
    )
    await asyncio.sleep(2)


async def main() -> None:
    host = "127.0.0.1"
    port = 13001
    async with websockets.serve(handler, host, port):
        print(f"mock OneBot listening on ws://{host}:{port}")
        await asyncio.Future()


if __name__ == "__main__":
    os.chdir(ROOT)
    asyncio.run(main())
