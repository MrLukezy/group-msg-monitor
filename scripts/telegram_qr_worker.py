"""Telegram 扫码登录后台进程。"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.channels.telegram import _qr_login_main, _write_qr_state  # noqa: E402


def main() -> None:
    api_id = int(os.environ.get("GMM_TG_API_ID") or "0")
    api_hash = (os.environ.get("GMM_TG_API_HASH") or "").strip()
    if not api_id or not api_hash:
        _write_qr_state({"status": "error", "message": "缺少 api_id / api_hash"})
        return
    try:
        asyncio.run(_qr_login_main(api_id, api_hash))
    except Exception as e:
        _write_qr_state({"status": "error", "message": str(e)})


if __name__ == "__main__":
    main()
