"""GeWeChat（iPad 协议）只读监听：扫码登录 + HTTP 回调收群消息。

说明：
- 依赖外部已部署的 GeWeChat 服务（默认 http://127.0.0.1:2531/v2/api）。
- 仅接收消息，不调用任何发送 / 群管理写入接口。
- 群 ID 使用 gw:{chatroom}，与本地库微信通道 wx: 区分。
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import re
import socket
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from aiohttp import ClientSession, ClientTimeout, web

from app.channels.ids import make_group_id
from app.models import GroupMessageEvent
from app.settings_store import DATA_DIR, ROOT_DIR

logger = logging.getLogger(__name__)

EventHandler = Callable[[GroupMessageEvent], Awaitable[None]]

QR_STATE_PATH = DATA_DIR / "gewechat_qr_state.json"
LOGIN_LOCK_PATH = DATA_DIR / "gewechat_login.pid"
DEFAULT_BASE_URL = "http://127.0.0.1:2531/v2/api"
DEFAULT_REGION_ID = "440000"
DEFAULT_CALLBACK_PORT = 9919
CALLBACK_PATH = "/gewechat/callback"

# MsgType → 展示文案（非文本）
_MSG_TYPE_LABEL = {
    3: "[图片]",
    34: "[语音]",
    43: "[视频]",
    47: "[表情]",
    48: "[位置]",
    49: "[卡片消息]",
    10000: "[系统消息]",
    10002: "[系统消息]",
}


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


def guess_lan_ip() -> str:
    """猜测本机局域网 IP（回调地址不能用 127.0.0.1，GeWe 服务需能访问）。"""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("8.8.8.8", 80))
            ip = sock.getsockname()[0]
            if ip and not ip.startswith("127."):
                return ip
        finally:
            sock.close()
    except OSError:
        pass
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith("127."):
                return ip
    except OSError:
        pass
    return "127.0.0.1"


def build_callback_url(host: str, port: int, path: str = CALLBACK_PATH) -> str:
    h = (host or "").strip() or guess_lan_ip()
    p = int(port or DEFAULT_CALLBACK_PORT)
    path = path if path.startswith("/") else f"/{path}"
    return f"http://{h}:{p}{path}"


def _url_to_qr_png_b64(url_or_data: str) -> str:
    raw = (url_or_data or "").strip()
    if not raw:
        return ""
    if raw.startswith("data:image"):
        # data:image/png;base64,xxxx
        if "," in raw:
            return raw.split(",", 1)[1].strip()
        return ""
    # 已是纯 base64 图片
    if len(raw) > 200 and not raw.startswith("http") and "://" not in raw[:12]:
        try:
            base64.b64decode(raw[:64] + "==", validate=False)
            return raw
        except Exception:
            pass
    try:
        import qrcode

        img = qrcode.make(raw)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception as e:
        logger.warning("生成 GeWeChat 二维码失败: %s", e)
        return ""


def _unwrap_string(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        return str(value.get("string") or value.get("String") or "")
    return str(value)


class GeweApiClient:
    """GeWeChat REST 客户端（只读接口）。"""

    def __init__(self, base_url: str, token: str = "", *, timeout: float = 30.0) -> None:
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.token = (token or "").strip()
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["X-GEWE-TOKEN"] = self.token
        return headers

    async def post(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self.base_url}{path if path.startswith('/') else '/' + path}"
        timeout = ClientTimeout(total=self.timeout)
        async with ClientSession(timeout=timeout) as session:
            async with session.post(url, json=body or {}, headers=self._headers()) as resp:
                text = await resp.text()
                try:
                    data = json.loads(text) if text else {}
                except json.JSONDecodeError:
                    return {
                        "ret": resp.status,
                        "msg": f"非 JSON 响应 HTTP {resp.status}: {text[:200]}",
                        "data": None,
                    }
                if not isinstance(data, dict):
                    return {"ret": resp.status, "msg": "响应格式异常", "data": data}
                return data

    def post_sync(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        return asyncio.run(self.post(path, body))

    async def get_token(self) -> dict[str, Any]:
        return await self.post("/tools/getTokenId", {})

    async def ensure_token(self) -> str:
        if self.token:
            return self.token
        resp = await self.get_token()
        if resp.get("ret") != 200:
            raise RuntimeError(resp.get("msg") or f"获取 token 失败: {resp}")
        token = resp.get("data")
        if isinstance(token, dict):
            token = token.get("token") or token.get("tokenId") or ""
        token = str(token or "").strip()
        if not token:
            raise RuntimeError("getTokenId 未返回 token")
        self.token = token
        return token

    async def set_callback(self, callback_url: str) -> dict[str, Any]:
        await self.ensure_token()
        return await self.post(
            "/tools/setCallback",
            {"token": self.token, "callbackUrl": callback_url},
        )

    async def get_login_qr(
        self,
        app_id: str = "",
        region_id: str = DEFAULT_REGION_ID,
        *,
        proxy_ip: str = "",
        login_type: str = "ipad",
    ) -> dict[str, Any]:
        await self.ensure_token()
        body: dict[str, Any] = {
            "appId": app_id or "",
            "type": login_type or "ipad",
        }
        if region_id:
            body["regionId"] = region_id
        if proxy_ip:
            body["proxyIp"] = proxy_ip
        # 新版路径；失败时兼容旧路径
        resp = await self.post("/login/getLoginQrCode", body)
        if resp.get("ret") == 200:
            return resp
        alt = await self.post("/login/getQrCode", body)
        if alt.get("ret") == 200:
            return alt
        return resp

    async def check_login(
        self,
        app_id: str,
        uuid: str,
        *,
        captch_code: str = "",
        proxy_ip: str = "",
        auto_sliding: bool = False,
    ) -> dict[str, Any]:
        await self.ensure_token()
        body: dict[str, Any] = {
            "appId": app_id,
            "uuid": uuid,
            "autoSliding": auto_sliding,
        }
        if captch_code:
            body["captchCode"] = captch_code
        if proxy_ip:
            body["proxyIp"] = proxy_ip
        return await self.post("/login/checkLogin", body)

    async def check_online(self, app_id: str) -> dict[str, Any]:
        await self.ensure_token()
        return await self.post("/login/checkOnline", {"appId": app_id})

    async def fetch_contacts_list(self, app_id: str) -> dict[str, Any]:
        await self.ensure_token()
        return await self.post("/contacts/fetchContactsList", {"appId": app_id})

    async def get_brief_info(self, app_id: str, wxids: list[str]) -> dict[str, Any]:
        await self.ensure_token()
        return await self.post(
            "/contacts/getBriefInfo",
            {"appId": app_id, "wxids": wxids},
        )


def _extract_appmsg_title(content: str) -> str:
    text = content or ""
    # 群消息前缀 wxid:\n
    if ":\n" in text:
        text = text.split(":\n", 1)[-1]
    try:
        # 可能夹带非 XML 前缀
        start = text.find("<msg")
        if start < 0:
            start = text.find("<appmsg")
        if start >= 0:
            text = text[start:]
        root = ET.fromstring(text)
        title = root.findtext(".//title") or root.findtext(".//des") or ""
        if title:
            return f"[卡片:{title.strip()}]"
    except ET.ParseError:
        pass
    return "[卡片消息]"


def _parse_group_sender_and_text(content: str) -> tuple[str, str]:
    raw = content or ""
    # 常见格式：wxid_xxx:\n正文
    m = re.match(r"^([^\n:]+):\n([\s\S]*)$", raw)
    if m:
        return m.group(1).strip(), m.group(2)
    # 少数实现用冒号空格
    m2 = re.match(r"^([a-zA-Z0-9_-]+)[:：]\s*([\s\S]*)$", raw)
    if m2 and "@chatroom" not in m2.group(1):
        return m2.group(1).strip(), m2.group(2)
    return "", raw


def callback_payload_to_event(
    payload: dict[str, Any],
    *,
    self_wxid: str = "",
) -> GroupMessageEvent | None:
    """将 GeWe 回调 JSON 转为群消息事件；非群 / 非 AddMsg 返回 None。"""
    if not isinstance(payload, dict):
        return None
    if "testMsg" in payload:
        return None

    type_name = str(payload.get("TypeName") or payload.get("typeName") or "").strip()
    if type_name and type_name not in ("AddMsg", "add_msg", "AddMsgNotification"):
        return None

    data = payload.get("Data") or payload.get("data") or {}
    if not isinstance(data, dict):
        return None

    from_user = _unwrap_string(data.get("FromUserName") or data.get("fromUserName"))
    if "@chatroom" not in from_user:
        return None

    msg_type = int(data.get("MsgType") or data.get("msgType") or 0)
    if msg_type in (10000, 10002):
        return None

    content_raw = _unwrap_string(data.get("Content") or data.get("content"))
    sender_id, body = _parse_group_sender_and_text(content_raw)
    self_id = (
        str(payload.get("Wxid") or payload.get("wxid") or self_wxid or "").strip()
    )
    # 忽略自己发出的消息（只监听）
    if self_id and sender_id and sender_id == self_id:
        return None
    if self_id and from_user == self_id:
        return None

    if msg_type == 1:
        text = body
    elif msg_type == 49:
        text = _extract_appmsg_title(content_raw)
    else:
        text = _MSG_TYPE_LABEL.get(msg_type, f"[消息类型:{msg_type}]")
        if body and msg_type not in (3, 34, 43):
            # 保留可解析的短文本补充
            stripped = body.strip()
            if stripped and len(stripped) < 80 and not stripped.startswith("<"):
                text = f"{text} {stripped}"

    mid = data.get("NewMsgId") or data.get("newMsgId") or data.get("MsgId") or data.get("msgId")
    create_time = int(data.get("CreateTime") or data.get("createTime") or 0) or None
    nick = ""
    push = str(data.get("PushContent") or data.get("pushContent") or "")
    # PushContent 常见：「张三：你好」或「张三在群聊中@了你」
    if "：" in push:
        nick = push.split("：", 1)[0].strip()
    elif ":" in push and "http" not in push[:8]:
        nick = push.split(":", 1)[0].strip()
    if nick.endswith("在群聊中@了你"):
        nick = nick.replace("在群聊中@了你", "").strip()

    gid = make_group_id("gewechat", from_user)
    user_id = sender_id or "unknown"
    display = nick or user_id
    return GroupMessageEvent(
        post_type="message",
        message_type="group",
        group_id=gid,
        user_id=user_id,
        message_id=mid,
        raw_message=text,
        message=text,
        sender={
            "user_id": user_id,
            "nickname": display,
            "card": display,
        },
        time=create_time,
        self_id="gewechat",
    )


async def check_online_status(
    base_url: str,
    token: str,
    app_id: str,
) -> dict[str, Any]:
    if not app_id:
        return {"ok": False, "online": False, "message": "缺少 appId，请先扫码登录"}
    client = GeweApiClient(base_url, token)
    try:
        await client.ensure_token()
        resp = await client.check_online(app_id)
    except Exception as e:
        return {"ok": False, "online": False, "message": str(e), "token": client.token}
    online = bool(resp.get("data")) if resp.get("ret") == 200 else False
    return {
        "ok": resp.get("ret") == 200,
        "online": online,
        "message": "在线" if online else (resp.get("msg") or "未在线"),
        "token": client.token,
        "raw": resp,
    }


async def list_gewechat_groups(
    base_url: str,
    token: str,
    app_id: str,
) -> list[dict[str, Any]]:
    client = GeweApiClient(base_url, token)
    await client.ensure_token()
    resp = await client.fetch_contacts_list(app_id)
    if resp.get("ret") != 200:
        raise RuntimeError(resp.get("msg") or f"拉取通讯录失败: {resp}")
    data = resp.get("data") or {}
    chatrooms = data.get("chatrooms") or data.get("chatRooms") or []
    if not isinstance(chatrooms, list):
        chatrooms = []
    room_ids = [str(x).strip() for x in chatrooms if str(x).strip()]
    name_map: dict[str, str] = {}
    # 分批取群名
    for i in range(0, len(room_ids), 20):
        batch = room_ids[i : i + 20]
        try:
            brief = await client.get_brief_info(app_id, batch)
            items = brief.get("data") or []
            if isinstance(items, dict):
                items = items.get("list") or items.get("contacts") or []
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                wxid = str(
                    item.get("userName")
                    or item.get("UserName")
                    or item.get("wxid")
                    or item.get("wxId")
                    or ""
                ).strip()
                nick = str(
                    item.get("nickName")
                    or item.get("NickName")
                    or item.get("remark")
                    or item.get("Remark")
                    or ""
                ).strip()
                if wxid:
                    name_map[wxid] = nick or wxid
        except Exception:
            logger.exception("getBriefInfo 失败 batch=%s", batch[:3])

    groups: list[dict[str, Any]] = []
    for rid in room_ids:
        groups.append(
            {
                "group_id": make_group_id("gewechat", rid),
                "group_name": name_map.get(rid) or rid,
                "channel": "gewechat",
            }
        )
    groups.sort(key=lambda g: g["group_name"] or g["group_id"])
    return groups


async def run_qr_login_loop(
    *,
    base_url: str,
    token: str = "",
    app_id: str = "",
    region_id: str = DEFAULT_REGION_ID,
    proxy_ip: str = "",
) -> None:
    """扫码登录主循环（供后台 worker 调用）。"""
    client = GeweApiClient(base_url, token)
    _write_qr_state({"status": "starting", "message": "正在连接 GeWeChat…"})
    try:
        await client.ensure_token()
    except Exception as e:
        _write_qr_state({"status": "error", "message": f"获取 token 失败: {e}"})
        return

    current_app_id = (app_id or "").strip()
    if current_app_id:
        try:
            online = await client.check_online(current_app_id)
            if online.get("ret") == 200 and online.get("data"):
                _write_qr_state(
                    {
                        "status": "authorized",
                        "message": "该 appId 已在线，无需重新扫码",
                        "app_id": current_app_id,
                        "token": client.token,
                        "already_online": True,
                    }
                )
                return
        except Exception:
            logger.exception("checkOnline 失败，继续取码")

    try:
        qr_resp = await client.get_login_qr(
            current_app_id,
            region_id or DEFAULT_REGION_ID,
            proxy_ip=proxy_ip,
        )
    except Exception as e:
        _write_qr_state({"status": "error", "message": f"取码失败: {e}"})
        return

    if qr_resp.get("ret") != 200:
        detail = qr_resp.get("msg") or f"取码失败: {qr_resp}"
        data = qr_resp.get("data")
        if isinstance(data, dict) and data.get("msg"):
            detail = f"{detail}（{data.get('msg')}）"
        if "创建设备" in str(detail):
            detail += (
                "。常见原因：GeWe 底层无法连接设备库/微信短链（容器日志常见"
                "「无法与设备库进行通信」）；官方仓库已停维，本机自建镜像可能已失效。"
                "可尝试：关闭系统代理后重启容器并多次重试；或改用 WeChatFerry 等本机方案。"
            )
        _write_qr_state(
            {
                "status": "error",
                "message": detail,
                "token": client.token,
            }
        )
        return

    qr_data = qr_resp.get("data") or {}
    current_app_id = str(qr_data.get("appId") or current_app_id or "").strip()
    uuid = str(qr_data.get("uuid") or "").strip()
    qr_payload = (
        qr_data.get("qrImgBase64")
        or qr_data.get("qr_img_base64")
        or qr_data.get("qrData")
        or qr_data.get("qr_data")
        or ""
    )
    png_b64 = _url_to_qr_png_b64(str(qr_payload))
    if not current_app_id or not uuid:
        _write_qr_state(
            {
                "status": "error",
                "message": "取码响应缺少 appId/uuid",
                "token": client.token,
            }
        )
        return

    _write_qr_state(
        {
            "status": "waiting_scan",
            "message": "请用手机微信扫码，并在手机上确认登录",
            "app_id": current_app_id,
            "uuid": uuid,
            "token": client.token,
            "qr_png_base64": png_b64,
            "qr_data": str(qr_data.get("qrData") or ""),
        }
    )

    for _ in range(120):
        st = _read_qr_state()
        if st.get("status") == "cancel":
            _write_qr_state({"status": "idle", "message": "已取消"})
            return
        await asyncio.sleep(5)
        try:
            chk = await client.check_login(current_app_id, uuid, proxy_ip=proxy_ip)
        except Exception as e:
            _write_qr_state(
                {
                    "status": "error",
                    "message": f"检查登录失败: {e}",
                    "app_id": current_app_id,
                    "token": client.token,
                }
            )
            return
        if chk.get("ret") != 200:
            # 偶发网络错误：继续等
            logger.warning("checkLogin 异常: %s", chk)
            continue
        data = chk.get("data") or {}
        if not isinstance(data, dict):
            continue

        verify_url = data.get("url") or data.get("verifyUrl") or ""
        if verify_url:
            _write_qr_state(
                {
                    "status": "need_verify",
                    "message": "需要安全验证，请打开下方链接完成人脸/滑块后再确认登录",
                    "app_id": current_app_id,
                    "uuid": uuid,
                    "token": client.token,
                    "verify_url": verify_url,
                    "qr_png_base64": png_b64,
                }
            )
            continue

        expired = int(data.get("expiredTime") or data.get("expired_time") or 999)
        if expired <= 5:
            # 重新取码
            qr_resp = await client.get_login_qr(
                current_app_id,
                region_id or DEFAULT_REGION_ID,
                proxy_ip=proxy_ip,
            )
            if qr_resp.get("ret") != 200:
                _write_qr_state(
                    {
                        "status": "error",
                        "message": qr_resp.get("msg") or "二维码过期且重新取码失败",
                        "app_id": current_app_id,
                        "token": client.token,
                    }
                )
                return
            qr_data = qr_resp.get("data") or {}
            uuid = str(qr_data.get("uuid") or uuid).strip()
            qr_payload = (
                qr_data.get("qrImgBase64")
                or qr_data.get("qrData")
                or ""
            )
            png_b64 = _url_to_qr_png_b64(str(qr_payload)) or png_b64
            _write_qr_state(
                {
                    "status": "waiting_scan",
                    "message": "二维码已刷新，请重新扫码",
                    "app_id": current_app_id,
                    "uuid": uuid,
                    "token": client.token,
                    "qr_png_base64": png_b64,
                }
            )
            continue

        status = int(data.get("status") or 0)
        if status == 2:
            login_info = data.get("loginInfo") or data.get("login_info") or {}
            if not isinstance(login_info, dict):
                login_info = {}
            nick = str(
                data.get("nickName")
                or login_info.get("nickName")
                or login_info.get("nickname")
                or ""
            ).strip()
            wxid = str(
                login_info.get("wxid")
                or login_info.get("wxId")
                or login_info.get("userName")
                or ""
            ).strip()
            _write_qr_state(
                {
                    "status": "authorized",
                    "message": f"扫码成功{(' · ' + nick) if nick else ''}",
                    "app_id": current_app_id,
                    "token": client.token,
                    "wxid": wxid,
                    "label": nick or wxid or current_app_id,
                    "login_info": login_info,
                }
            )
            return
        nick_hint = str(data.get("nickName") or "").strip()
        _write_qr_state(
            {
                "status": "waiting_scan",
                "message": (
                    f"已扫码，请在手机确认{('（' + nick_hint + '）') if nick_hint else ''}…"
                    if status == 1
                    else "等待扫码…"
                ),
                "app_id": current_app_id,
                "uuid": uuid,
                "token": client.token,
                "qr_png_base64": png_b64,
                "scan_status": status,
            }
        )

    _write_qr_state(
        {
            "status": "error",
            "message": "登录超时，请重新扫码",
            "app_id": current_app_id,
            "token": client.token,
        }
    )


def start_qr_login_process(
    *,
    base_url: str,
    token: str = "",
    app_id: str = "",
    region_id: str = DEFAULT_REGION_ID,
    proxy_ip: str = "",
) -> dict[str, Any]:
    if not (base_url or "").strip():
        return {"ok": False, "message": "请填写 GeWeChat base_url"}
    cancel_qr_login()
    _write_qr_state(
        {
            "status": "starting",
            "message": "正在启动扫码…",
            "base_url": base_url,
        }
    )
    python = sys.executable
    worker = ROOT_DIR / "scripts" / "gewechat_qr_worker.py"
    creationflags = 0
    if sys.platform == "win32":
        creationflags = getattr(__import__("subprocess"), "CREATE_NO_WINDOW", 0)
    import subprocess

    env = os.environ.copy()
    env["GMM_GEWE_BASE_URL"] = (base_url or "").strip()
    env["GMM_GEWE_TOKEN"] = (token or "").strip()
    env["GMM_GEWE_APP_ID"] = (app_id or "").strip()
    env["GMM_GEWE_REGION_ID"] = (region_id or DEFAULT_REGION_ID).strip()
    env["GMM_GEWE_PROXY_IP"] = (proxy_ip or "").strip()
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
    return {"ok": True, "message": "已启动 GeWeChat 扫码登录", "pid": proc.pid}


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
    st = _read_qr_state()
    if st.get("status") not in ("authorized",):
        _write_qr_state({"status": "idle", "message": "已取消"})


def qr_status() -> dict[str, Any]:
    st = _read_qr_state()
    st.setdefault("ok", True)
    return st


class GeWeChatAdapter:
    """HTTP 回调接收群消息；不提供发送能力。"""

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        app_id: str,
        on_message: EventHandler,
        callback_host: str = "",
        callback_port: int = DEFAULT_CALLBACK_PORT,
        wxid: str = "",
        callback_path: str = CALLBACK_PATH,
    ) -> None:
        self.base_url = base_url
        self.token = token
        self.app_id = app_id
        self.on_message = on_message
        self.callback_host = (callback_host or "").strip() or guess_lan_ip()
        self.callback_port = int(callback_port or DEFAULT_CALLBACK_PORT)
        self.wxid = (wxid or "").strip()
        self.callback_path = callback_path if callback_path.startswith("/") else f"/{callback_path}"
        self._stop = asyncio.Event()
        self._queue: asyncio.Queue[GroupMessageEvent] = asyncio.Queue()
        self._runner: web.AppRunner | None = None

    def stop(self) -> None:
        self._stop.set()

    async def _on_callback_get(self, request: web.Request) -> web.Response:
        return web.Response(text="gewechat callback ok")

    async def _on_callback(self, request: web.Request) -> web.Response:
        try:
            payload = await request.json()
        except Exception:
            return web.Response(text="success")
        if isinstance(payload, dict) and "testMsg" in payload:
            logger.debug("GeWeChat 回调连通测试")
            return web.Response(text="success")
        try:
            ev = callback_payload_to_event(payload if isinstance(payload, dict) else {}, self_wxid=self.wxid)
            if ev is not None:
                await self._queue.put(ev)
        except Exception:
            logger.exception("解析 GeWeChat 回调失败")
        return web.Response(text="success")

    async def _start_server(self) -> str:
        app = web.Application()
        app.router.add_post(self.callback_path, self._on_callback)
        app.router.add_get(self.callback_path, self._on_callback_get)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "0.0.0.0", self.callback_port)
        await site.start()
        url = build_callback_url(self.callback_host, self.callback_port, self.callback_path)
        logger.info("GeWeChat 回调服务已启动 | %s (listen 0.0.0.0:%s)", url, self.callback_port)
        return url

    async def run_forever(self) -> None:
        client = GeweApiClient(self.base_url, self.token)
        try:
            await client.ensure_token()
            self.token = client.token
        except Exception:
            logger.exception("GeWeChat ensure_token 失败")
            return

        if self.app_id:
            try:
                online = await client.check_online(self.app_id)
                if not (online.get("ret") == 200 and online.get("data")):
                    logger.warning(
                        "GeWeChat appId=%s 当前不在线，仍启动回调；请重新扫码登录",
                        self.app_id,
                    )
            except Exception:
                logger.exception("GeWeChat checkOnline 失败")

        try:
            callback_url = await self._start_server()
        except OSError:
            logger.exception("GeWeChat 回调端口 %s 启动失败", self.callback_port)
            return

        try:
            cb_resp = await client.set_callback(callback_url)
            if cb_resp.get("ret") != 200:
                logger.error("设置 GeWeChat 回调失败: %s", cb_resp)
            else:
                logger.info("已向 GeWeChat 注册回调 | %s", callback_url)
        except Exception:
            logger.exception("setCallback 失败")

        logger.info(
            "通道 GeWeChat 监听中（只收不发）| app_id=%s wxid=%s",
            self.app_id,
            self.wxid or "(unknown)",
        )
        try:
            while not self._stop.is_set():
                try:
                    ev = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                try:
                    await self.on_message(ev)
                except Exception:
                    logger.exception("GeWeChat 消息回调失败")
        finally:
            if self._runner is not None:
                await self._runner.cleanup()
                self._runner = None
