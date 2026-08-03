"""QQ 被动采集探针：打印通知 / UIA 可访问性摘要。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.channels.qq_passive import detect_official_qq  # noqa: E402
from app.channels.qq_passive_parse import FIXTURE_NOTIFICATIONS, parse_notification_payload  # noqa: E402


def main() -> None:
    print("== fixture parse ==")
    for item in FIXTURE_NOTIFICATIONS:
        parsed = parse_notification_payload(
            title=item["title"],
            body=item["body"],
            app_id=item.get("app_id", ""),
            app_name=item.get("app_name", ""),
            notification_id=item.get("notification_id", ""),
        )
        expect = item.get("expect")
        ok = (parsed is None and expect is None) or (
            parsed is not None
            and expect is not None
            and parsed.group_name == expect["group_name"]
            and parsed.sender_name == expect["sender_name"]
            and parsed.text == expect["text"]
            and parsed.has_image == expect["has_image"]
        )
        print(("OK" if ok else "FAIL"), item.get("notification_id"), "->", None if parsed is None else parsed)

    print("== live detect ==")
    result = detect_official_qq()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
