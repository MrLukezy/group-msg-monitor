"""一次性拉取当前登录号的群列表（OneBot get_group_list）。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

import websockets
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env", override=True)


def build_url() -> tuple[str, dict[str, str]]:
    url = os.getenv("ONEBOT_WS_URL", "ws://127.0.0.1:3001")
    token = os.getenv("ONEBOT_ACCESS_TOKEN", "")
    headers: dict[str, str] = {}
    # 仅用 query token，兼容性更好
    if token:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}access_token={token}"
    return url, headers


async def call(ws: websockets.ClientConnection, action: str, echo: str) -> dict:
    await ws.send(json.dumps({"action": action, "params": {}, "echo": echo}))
    while True:
        data = json.loads(await asyncio.wait_for(ws.recv(), timeout=8))
        if isinstance(data, dict) and data.get("echo") == echo:
            return data


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true", help="只输出一行 JSON")
    args = parser.parse_args()

    url, headers = build_url()
    async with websockets.connect(
        url,
        additional_headers=headers,
        open_timeout=5,
        max_size=50 * 1024 * 1024,
    ) as ws:
        login = await call(ws, "get_login_info", "login")
        groups = await call(ws, "get_group_list", "groups")

    if args.json:
        payload = {
            "login": login.get("data") or {},
            "groups": groups.get("data") or [],
            "status": groups.get("status"),
            "retcode": groups.get("retcode"),
        }
        print(json.dumps(payload, ensure_ascii=False))
        return

    print("登录信息:", json.dumps(login.get("data"), ensure_ascii=False))
    print("接口状态:", groups.get("status"), "retcode=", groups.get("retcode"))
    arr = groups.get("data") or []
    print(f"群数量: {len(arr)}")
    for g in arr:
        print(
            f"{g.get('group_id')}\t{g.get('group_name')}\t"
            f"成员:{g.get('member_count')}/{g.get('max_member_count')}"
        )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        raise SystemExit(1) from e
