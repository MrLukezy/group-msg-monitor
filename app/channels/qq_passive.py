"""官方 QQ 被动采集适配器：系统通知 + UI Automation 当前会话补偿。

约束：
- 不注入、不 Hook、不调用 QQ 私有协议；
- 静音群 / 关闭通知 / 未打开会话可能漏消息；
- 图片仅记录占位文本，不下载原图。
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from app.channels.ids import make_group_id
from app.channels.qq_passive_parse import (
    ParsedPassiveMessage,
    dedupe_key,
    parse_notification_payload,
    parse_uia_message,
    stable_group_id,
)
from app.models import GroupMessageEvent, Sender
from app.settings_store import ROOT_DIR, load_app_settings, save_app_settings

logger = logging.getLogger(__name__)

EventHandler = Callable[[GroupMessageEvent], Awaitable[None]]

PROBE_SCRIPT = ROOT_DIR / "scripts" / "qq_notification_poll.ps1"
UIA_SCRIPT = ROOT_DIR / "scripts" / "qq_uia_probe.ps1"


def official_qq_running() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import psutil  # type: ignore

        for p in psutil.process_iter(["name"]):
            name = (p.info.get("name") or "").lower()
            if name == "qq.exe":
                return True
    except Exception:
        pass
    try:
        completed = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq QQ.exe"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            check=False,
            creationflags=0x08000000 if sys.platform == "win32" else 0,
        )
        return "QQ.exe" in (completed.stdout or "")
    except Exception:
        return False


def napcat_modules_likely_loaded() -> bool:
    """粗检：NapCat 启动器是否仍在跑（被动模式应避免）。"""
    if sys.platform != "win32":
        return False
    try:
        completed = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq NapCatWinBootMain.exe"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            check=False,
            creationflags=0x08000000,
        )
        return "NapCatWinBootMain.exe" in (completed.stdout or "")
    except Exception:
        return False


def _run_ps1(script: Path, *args: str, timeout: float = 12.0) -> dict[str, Any]:
    if not script.exists():
        return {"ok": False, "error": f"missing script: {script.name}", "items": []}
    cmd = [
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
        *args,
    ]
    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=timeout,
            check=False,
            creationflags=0x08000000 if sys.platform == "win32" else 0,
        )
    except Exception as e:
        return {"ok": False, "error": str(e), "items": []}
    raw = (completed.stdout or "").strip()
    if not raw:
        err = (completed.stderr or "").strip()
        return {"ok": False, "error": err or "empty powershell output", "items": []}
    # 取最后一行 JSON，兼容杂讯
    line = raw.splitlines()[-1].strip()
    try:
        data = json.loads(line)
        if isinstance(data, dict):
            return data
        return {"ok": True, "items": data if isinstance(data, list) else []}
    except json.JSONDecodeError:
        return {"ok": False, "error": f"invalid json: {line[:200]}", "items": []}


def probe_notifications() -> dict[str, Any]:
    return _run_ps1(PROBE_SCRIPT)


def probe_uia() -> dict[str, Any]:
    return _run_ps1(UIA_SCRIPT)


def detect_official_qq() -> dict[str, Any]:
    running = official_qq_running()
    napcat = napcat_modules_likely_loaded()
    notif = probe_notifications() if running or True else {}
    uia = probe_uia() if running else {"ok": False, "error": "QQ not running", "items": []}
    access = str(notif.get("access") or notif.get("status") or "")
    return {
        "ok": True,
        "officialQqRunning": running,
        "napcatRunning": napcat,
        "notificationAccess": access or ("ok" if notif.get("ok") else "unknown"),
        "notificationOk": bool(notif.get("ok")),
        "notificationError": notif.get("error") or "",
        "notificationCount": len(notif.get("items") or []),
        "uiaOk": bool(uia.get("ok")),
        "uiaError": uia.get("error") or "",
        "uiaGroupName": (uia.get("groupName") or ""),
        "uiaMessageCount": len(uia.get("messages") or []),
        "limitations": [
            "静音群若不发系统通知则无法采集",
            "当前打开会话可能抑制通知，依赖 UIA 补偿",
            "无法读取未打开窗口中的历史消息与图片原件",
            "请勿同时运行 NapCat，以免与官方 QQ 抢占登录",
        ],
    }


class QqPassiveAdapter:
    def __init__(
        self,
        on_message: EventHandler,
        *,
        poll_seconds: float = 1.5,
        group_name_map: dict[str, str] | None = None,
    ) -> None:
        self.on_message = on_message
        self.poll_seconds = max(0.8, float(poll_seconds or 1.5))
        self.group_name_map = dict(group_name_map or {})
        self._stop = False
        self._seen: OrderedDict[str, float] = OrderedDict()
        self._seen_limit = 4000
        self.last_status: dict[str, Any] = {}

    def stop(self) -> None:
        self._stop = True

    def _remember(self, key: str) -> bool:
        if key in self._seen:
            return False
        self._seen[key] = time.time()
        while len(self._seen) > self._seen_limit:
            self._seen.popitem(last=False)
        return True

    def _to_event(self, parsed: ParsedPassiveMessage) -> GroupMessageEvent:
        gid = stable_group_id(parsed.group_name, self.group_name_map)
        # 若映射到真实数字群号，保持 qq 通道兼容；qqp: 也走 qq 通道语义
        channel_gid = make_group_id("qq", gid) if not str(gid).startswith("qqp:") else gid
        mid = dedupe_key(parsed, channel_gid)
        text = parsed.text
        message: Any = text
        if parsed.has_image and "[图片]" not in text:
            text = f"{text} [图片]".strip()
            message = text
        return GroupMessageEvent(
            post_type="message",
            message_type="group",
            group_id=channel_gid,
            user_id=parsed.sender_name or "unknown",
            message_id=mid,
            raw_message=text,
            message=message,
            sender=Sender(
                user_id=parsed.sender_name or "unknown",
                nickname=parsed.sender_name or "未知",
                card=parsed.sender_name or "未知",
            ),
            time=int(parsed.observed_at),
            self_id="qq-passive",
        )

    async def _emit(self, parsed: ParsedPassiveMessage) -> None:
        event = self._to_event(parsed)
        key = str(event.message_id)
        if not self._remember(key):
            return
        # 同步群名到配置映射缓存（仅内存；持久化由 detect/bind API 负责）
        if parsed.group_name and parsed.group_name not in self.group_name_map:
            self.group_name_map.setdefault(parsed.group_name, str(event.group_id))
        await self.on_message(event)

    def _collect_notifications(self) -> list[ParsedPassiveMessage]:
        payload = probe_notifications()
        self.last_status["notification"] = {
            "ok": bool(payload.get("ok")),
            "access": payload.get("access") or payload.get("status") or "",
            "error": payload.get("error") or "",
            "count": len(payload.get("items") or []),
        }
        out: list[ParsedPassiveMessage] = []
        for item in payload.get("items") or []:
            if not isinstance(item, dict):
                continue
            parsed = parse_notification_payload(
                title=str(item.get("title") or ""),
                body=str(item.get("body") or ""),
                app_id=str(item.get("appId") or item.get("app_id") or ""),
                app_name=str(item.get("appName") or item.get("app_name") or ""),
                notification_id=str(item.get("id") or item.get("notificationId") or ""),
                observed_at=float(item.get("createdAt") or item.get("observedAt") or time.time()),
            )
            if parsed is not None:
                out.append(parsed)
        return out

    def _collect_uia(self) -> list[ParsedPassiveMessage]:
        payload = probe_uia()
        self.last_status["uia"] = {
            "ok": bool(payload.get("ok")),
            "error": payload.get("error") or "",
            "groupName": payload.get("groupName") or "",
            "count": len(payload.get("messages") or []),
        }
        group_name = str(payload.get("groupName") or "").strip()
        out: list[ParsedPassiveMessage] = []
        for item in payload.get("messages") or []:
            if isinstance(item, str):
                parsed = parse_uia_message(group_name=group_name, text=item)
            elif isinstance(item, dict):
                parsed = parse_uia_message(
                    group_name=group_name or str(item.get("groupName") or ""),
                    sender_name=str(item.get("sender") or item.get("senderName") or ""),
                    text=str(item.get("text") or item.get("body") or ""),
                    observed_at=float(item.get("observedAt") or time.time()),
                )
            else:
                parsed = None
            if parsed is not None:
                out.append(parsed)
        return out

    async def run_forever(self) -> None:
        logger.info(
            "QQ 被动采集已启动 | poll=%.1fs map=%s",
            self.poll_seconds,
            len(self.group_name_map),
        )
        while not self._stop:
            try:
                if napcat_modules_likely_loaded():
                    logger.warning("检测到 NapCat 仍在运行，被动模式可能与官方 QQ 冲突")
                batch = self._collect_notifications()
                # 通知可能被当前会话抑制，始终尝试 UIA 补偿
                batch.extend(self._collect_uia())
                for parsed in batch:
                    if self._stop:
                        break
                    await self._emit(parsed)
                self._persist_discovered_groups()
            except Exception:
                logger.exception("QQ 被动采集轮询异常")
            await asyncio.sleep(self.poll_seconds)

    def _persist_discovered_groups(self) -> None:
        """把新发现的群名写回 settings，便于桌面端展示。"""
        try:
            settings = load_app_settings()
            qq = settings.channels.qq
            changed = False
            for name, gid in self.group_name_map.items():
                if name and name not in qq.group_name_map:
                    qq.group_name_map[name] = gid
                    changed = True
            if changed:
                save_app_settings(settings)
        except Exception:
            logger.debug("持久化被动群名映射失败", exc_info=True)
