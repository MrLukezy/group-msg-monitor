"""GeWeChat 扫码登录后台进程。"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.channels.gewechat import run_qr_login_loop  # noqa: E402


def main() -> None:
    base_url = (os.environ.get("GMM_GEWE_BASE_URL") or "").strip()
    token = (os.environ.get("GMM_GEWE_TOKEN") or "").strip()
    app_id = (os.environ.get("GMM_GEWE_APP_ID") or "").strip()
    region_id = (os.environ.get("GMM_GEWE_REGION_ID") or "440000").strip()
    proxy_ip = (os.environ.get("GMM_GEWE_PROXY_IP") or "").strip()
    asyncio.run(
        run_qr_login_loop(
            base_url=base_url,
            token=token,
            app_id=app_id,
            region_id=region_id,
            proxy_ip=proxy_ip,
        )
    )


if __name__ == "__main__":
    main()
