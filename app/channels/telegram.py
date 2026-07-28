"""Telegram 用户号接入（Telethon MTProto）：扫码登录 + 本地 session。

不使用 Bot API。登录后以个人账号监听已加入的群消息。
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from app.channels.ids import make_group_id
from app.models import GroupMessageEvent
from app.settings_store import DATA_DIR, ROOT_DIR

logger = logging.getLogger(__name__)

EventHandler = Callable[[GroupMessageEvent], Awaitable[None]]

SESSION_DIR = DATA_DIR / "telegram_session"
SESSION_NAME = "user"
QR_STATE_PATH = DATA_DIR / "telegram_qr_state.json"
LOGIN_LOCK_PATH = DATA_DIR / "telegram_login.pid"


def session_base_path() -> Path:
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    return SESSION_DIR / SESSION_NAME


def session_files_exist() -> bool:
    base = session_base_path()
    return base.with_suffix(".session").exists() or Path(str(base) + ".session").exists()


def detect_telegram_desktop() -> list[dict[str, Any]]:
    """检测本机 Telegram Desktop 数据目录（tdata）。"""
    found: list[dict[str, Any]] = []
    candidates: list[Path] = []
    appdata = os.environ.get("APPDATA") or ""
    local = os.environ.get("LOCALAPPDATA") or ""
    home = Path.home()
    for p in (
        Path(appdata) / "Telegram Desktop" / "tdata" if appdata else None,
        Path(local) / "Telegram Desktop" / "tdata" if local else None,
        home / "AppData" / "Roaming" / "Telegram Desktop" / "tdata",
        home / "Library" / "Application Support" / "Telegram Desktop" / "tdata",
        Path.home() / ".local" / "share" / "TelegramDesktop" / "tdata",
    ):
        if p and p not in candidates:
            candidates.append(p)
    for p in candidates:
        if p.is_dir():
            found.append(
                {
                    "path": str(p),
                    "exists": True,
                    "note": "已检测到 Telegram Desktop 本地数据；请用扫码登录生成监听用 session（tdata 无法直接复用）",
                }
            )
    # 本应用已保存的 session 也算本地记录
    if session_files_exist():
        found.append(
            {
                "path": str(session_base_path()) + ".session",
                "exists": True,
                "note": "已存在本应用 Telegram 用户 session，可直接绑定",
                "kind": "app_session",
            }
        )
    return found


def _read_qr_state() -> dict[str, Any]:
    if not QR_STATE_PATH.exists():
        return {"status": "idle"}
    try:
        return json.loads(QR_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"status": "idle"}


def _write_qr_state(data: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = dict(data)
    payload["updated_at"] = time.time()
    QR_STATE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _url_to_qr_png_b64(url: str) -> str:
    try:
        import qrcode

        img = qrcode.make(url)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception as e:
        logger.warning("生成二维码失败: %s", e)
        return ""


def _resolve_api(api_id: int | str | None, api_hash: str | None) -> tuple[int, str]:
    aid = int(api_id or 0)
    ahash = (api_hash or "").strip()
    if not aid or not ahash:
        raise ValueError(
            "请填写 Telegram api_id / api_hash（在 https://my.telegram.org 申请）"
        )
    return aid, ahash


async def check_session_authorized(api_id: int | str, api_hash: str) -> dict[str, Any]:
    try:
        from telethon import TelegramClient
    except ImportError:
        return {"ok": False, "authorized": False, "message": "未安装 telethon，请 pip install telethon qrcode"}

    try:
        aid, ahash = _resolve_api(api_id, api_hash)
    except ValueError as e:
        return {"ok": False, "authorized": False, "message": str(e)}

    if not session_files_exist():
        return {"ok": True, "authorized": False, "message": "本地尚无 Telegram session，请扫码登录"}

    client = TelegramClient(str(session_base_path()), aid, ahash)
    try:
        await client.connect()
        ok = await client.is_user_authorized()
        if not ok:
            return {"ok": True, "authorized": False, "message": "session 已失效，请重新扫码"}
        me = await client.get_me()
        label = ""
        if me:
            label = (me.username and f"@{me.username}") or (
                " ".join(x for x in [me.first_name, me.last_name] if x).strip()
                or str(me.id)
            )
        return {
            "ok": True,
            "authorized": True,
            "message": f"已登录 {label}",
            "label": label,
            "user_id": getattr(me, "id", None),
        }
    except Exception as e:
        return {"ok": False, "authorized": False, "message": str(e)}
    finally:
        await client.disconnect()


async def list_telegram_groups(api_id: int | str, api_hash: str) -> list[dict[str, Any]]:
    from telethon import TelegramClient
    from telethon.tl.types import Channel, Chat

    aid, ahash = _resolve_api(api_id, api_hash)
    client = TelegramClient(str(session_base_path()), aid, ahash)
    groups: list[dict[str, Any]] = []
    try:
        await client.connect()
        if not await client.is_user_authorized():
            return []
        async for dialog in client.iter_dialogs():
            entity = dialog.entity
            is_group = False
            if isinstance(entity, Chat):
                is_group = True
            elif isinstance(entity, Channel) and bool(getattr(entity, "megagroup", False)):
                is_group = True
            elif dialog.is_group:
                is_group = True
            if not is_group:
                continue
            cid = dialog.id
            groups.append(
                {
                    "group_id": make_group_id("telegram", cid),
                    "group_name": dialog.name or str(cid),
                    "channel": "telegram",
                }
            )
    finally:
        await client.disconnect()
    return groups


def _message_to_event(event: Any, *, local_image_rel: str | None = None) -> GroupMessageEvent | None:
    try:
        chat = event.chat
        if chat is None:
            return None
        # 仅群 / 超级群
        is_group = bool(getattr(event, "is_group", False))
        if not is_group:
            # channel broadcast 不算群聊监听目标
            return None
        chat_id = event.chat_id
        if chat_id is None:
            return None
        sender = event.sender
        name = ""
        uid: Any = None
        if sender is not None:
            uid = getattr(sender, "id", None)
            name = (
                getattr(sender, "username", None)
                and f"@{sender.username}"
            ) or " ".join(
                x
                for x in [
                    getattr(sender, "first_name", None),
                    getattr(sender, "last_name", None),
                ]
                if x
            ).strip() or str(uid or "")
        text = event.raw_text or ""
        if local_image_rel:
            from app.media_store import build_local_image_cq

            img_cq = build_local_image_cq(local_rel=local_image_rel)
            text = f"{text}{img_cq}" if text else img_cq
        elif not text:
            if getattr(event, "photo", None):
                text = "[图片]"
            elif getattr(event, "document", None):
                text = "[文件]"
            elif getattr(event, "sticker", None):
                text = "[贴纸]"
            elif getattr(event, "voice", None) or getattr(event, "audio", None):
                text = "[语音]"
            elif getattr(event, "video", None):
                text = "[视频]"
            else:
                text = "[非文本消息]"
        title = getattr(chat, "title", None) or ""
        mid = event.id
        ts = int(event.date.timestamp()) if getattr(event, "date", None) else None
        return GroupMessageEvent(
            post_type="message",
            message_type="group",
            group_id=make_group_id("telegram", chat_id),
            user_id=uid,
            message_id=mid,
            raw_message=text,
            message=text,
            sender={"user_id": uid, "nickname": name or title, "card": name},
            time=ts,
            self_id="telegram",
        )
    except Exception:
        logger.exception("转换 Telegram 消息失败")
        return None


class TelegramUserAdapter:
    """已登录用户 session 的实时群消息监听。"""

    def __init__(
        self,
        api_id: int | str,
        api_hash: str,
        on_message: EventHandler,
    ) -> None:
        self.api_id, self.api_hash = _resolve_api(api_id, api_hash)
        self.on_message = on_message
        self._stop = asyncio.Event()
        self._client = None

    def stop(self) -> None:
        self._stop.set()

    async def run_forever(self) -> None:
        from telethon import TelegramClient, events

        from app.media_store import store_image_from_bytes

        client = TelegramClient(str(session_base_path()), self.api_id, self.api_hash)
        self._client = client

        @client.on(events.NewMessage(incoming=True))
        async def _handler(event: Any) -> None:
            if not getattr(event, "is_group", False):
                return
            local_rel: str | None = None
            try:
                if getattr(event, "photo", None):
                    data = await event.download_media(file=bytes)
                    if isinstance(data, (bytes, bytearray)) and data:
                        gid = make_group_id("telegram", event.chat_id)
                        local_rel = store_image_from_bytes(
                            bytes(data),
                            group_id=gid,
                            mime="image/jpeg",
                            name_hint="tg.jpg",
                        )
            except Exception:
                logger.exception("Telegram 图片落盘失败")
            msg = _message_to_event(event, local_image_rel=local_rel)
            if msg is None:
                return
            try:
                await self.on_message(msg)
            except Exception:
                logger.exception("处理 Telegram 用户消息失败")

        await client.connect()
        if not await client.is_user_authorized():
            logger.error("Telegram session 未授权，请先在总配置扫码登录")
            await client.disconnect()
            return

        me = await client.get_me()
        logger.info(
            "Telegram 用户监听已启动 | %s",
            getattr(me, "username", None) or getattr(me, "id", "?"),
        )
        # 保持连接直到 stop
        try:
            while not self._stop.is_set():
                await asyncio.sleep(0.5)
        finally:
            await client.disconnect()


# —— 扫码登录 worker（独立进程跑，避免桌面端 invoke 超时）——

async def _qr_login_main(api_id: int, api_hash: str) -> None:
    from telethon import TelegramClient
    from telethon.errors import SessionPasswordNeededError

    _write_qr_state({"status": "starting", "message": "正在连接 Telegram…"})
    client = TelegramClient(str(session_base_path()), api_id, api_hash)
    await client.connect()
    try:
        if await client.is_user_authorized():
            me = await client.get_me()
            label = (me.username and f"@{me.username}") or str(me.id)
            _write_qr_state(
                {
                    "status": "authorized",
                    "message": f"已登录 {label}",
                    "label": label,
                    "user_id": me.id,
                }
            )
            return

        qr = await client.qr_login()
        png_b64 = _url_to_qr_png_b64(qr.url)
        _write_qr_state(
            {
                "status": "waiting_scan",
                "message": "请用手机 Telegram「设置 → 设备」扫码",
                "url": qr.url,
                "qr_png_base64": png_b64,
            }
        )

        while True:
            try:
                user = await qr.wait(timeout=30)
                label = (user.username and f"@{user.username}") or (
                    " ".join(x for x in [user.first_name, user.last_name] if x).strip()
                    or str(user.id)
                )
                _write_qr_state(
                    {
                        "status": "authorized",
                        "message": f"扫码成功，已登录 {label}",
                        "label": label,
                        "user_id": user.id,
                    }
                )
                return
            except asyncio.TimeoutError:
                # token 可能过期，刷新二维码
                try:
                    await qr.recreate()
                    png_b64 = _url_to_qr_png_b64(qr.url)
                    _write_qr_state(
                        {
                            "status": "waiting_scan",
                            "message": "二维码已刷新，请重新扫码",
                            "url": qr.url,
                            "qr_png_base64": png_b64,
                        }
                    )
                except Exception as e:
                    _write_qr_state({"status": "error", "message": f"刷新二维码失败: {e}"})
                    return
            except SessionPasswordNeededError:
                _write_qr_state(
                    {
                        "status": "need_password",
                        "message": "账号开启了两步验证，请输入二次密码",
                    }
                )
                # 等待密码文件
                pwd_path = DATA_DIR / "telegram_2fa_password.txt"
                if pwd_path.exists():
                    try:
                        pwd_path.unlink()
                    except OSError:
                        pass
                for _ in range(300):  # 最多等 ~5 分钟
                    await asyncio.sleep(1)
                    st = _read_qr_state()
                    if st.get("status") == "cancel":
                        return
                    if pwd_path.exists():
                        password = pwd_path.read_text(encoding="utf-8").strip()
                        try:
                            pwd_path.unlink()
                        except OSError:
                            pass
                        try:
                            await client.sign_in(password=password)
                            me = await client.get_me()
                            label = (me.username and f"@{me.username}") or str(me.id)
                            _write_qr_state(
                                {
                                    "status": "authorized",
                                    "message": f"两步验证通过，已登录 {label}",
                                    "label": label,
                                    "user_id": me.id,
                                }
                            )
                            return
                        except Exception as e:
                            _write_qr_state(
                                {
                                    "status": "need_password",
                                    "message": f"密码错误或失败: {e}，请重试",
                                }
                            )
                _write_qr_state({"status": "error", "message": "等待两步验证密码超时"})
                return
            except Exception as e:
                if "cancel" in str(e).lower():
                    _write_qr_state({"status": "idle", "message": "已取消"})
                    return
                _write_qr_state({"status": "error", "message": str(e)})
                return
    finally:
        await client.disconnect()


def start_qr_login_process(api_id: int | str, api_hash: str) -> dict[str, Any]:
    """启动后台扫码登录进程。"""
    try:
        aid, ahash = _resolve_api(api_id, api_hash)
    except ValueError as e:
        return {"ok": False, "message": str(e)}

    # 取消旧进程
    cancel_qr_login()
    _write_qr_state({"status": "starting", "message": "正在启动扫码…", "api_id": aid})

    python = sys.executable
    worker = ROOT_DIR / "scripts" / "telegram_qr_worker.py"
    creationflags = 0
    if sys.platform == "win32":
        creationflags = getattr(__import__("subprocess"), "CREATE_NO_WINDOW", 0)
    import subprocess

    env = os.environ.copy()
    env["GMM_TG_API_ID"] = str(aid)
    env["GMM_TG_API_HASH"] = ahash
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    proc = subprocess.Popen(
        [python, str(worker)],
        cwd=str(ROOT_DIR),
        env=env,
        creationflags=creationflags,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    LOGIN_LOCK_PATH.write_text(str(proc.pid), encoding="utf-8")
    return {"ok": True, "message": "已启动扫码登录", "pid": proc.pid}


def cancel_qr_login() -> None:
    _write_qr_state({"status": "cancel", "message": "取消中"})
    if LOGIN_LOCK_PATH.exists():
        try:
            pid = int(LOGIN_LOCK_PATH.read_text(encoding="utf-8").strip() or "0")
        except Exception:
            pid = 0
        if pid:
            try:
                if sys.platform == "win32":
                    import subprocess

                    subprocess.run(
                        ["taskkill", "/PID", str(pid), "/F"],
                        capture_output=True,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                else:
                    os.kill(pid, 15)
            except Exception:
                pass
        try:
            LOGIN_LOCK_PATH.unlink()
        except OSError:
            pass
    # 稍后再置 idle（worker 可能覆盖）
    st = _read_qr_state()
    if st.get("status") not in ("authorized",):
        _write_qr_state({"status": "idle", "message": "已取消"})


def submit_2fa_password(password: str) -> dict[str, Any]:
    pwd = (password or "").strip()
    if not pwd:
        return {"ok": False, "message": "请输入两步验证密码"}
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "telegram_2fa_password.txt").write_text(pwd, encoding="utf-8")
    st = _read_qr_state()
    if st.get("status") != "need_password":
        return {"ok": True, "message": "密码已提交（当前状态可能已变化）"}
    return {"ok": True, "message": "密码已提交"}


def qr_status() -> dict[str, Any]:
    st = _read_qr_state()
    st.setdefault("ok", True)
    return st
