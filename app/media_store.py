"""群聊图片本地落盘：接收时下载，供预览与 LLM 视觉分析。"""

from __future__ import annotations

import hashlib
import html
import logging
import mimetypes
import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import aiohttp

from app.settings_store import DATA_DIR

logger = logging.getLogger(__name__)

MEDIA_DIR = DATA_DIR / "media"
CQ_IMAGE_RE = re.compile(r"\[CQ:image,([^\]]*)\]", re.IGNORECASE)
MAX_IMAGE_BYTES = 25 * 1024 * 1024

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
}


def ensure_media_dir() -> Path:
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    return MEDIA_DIR


def media_abs_path(rel: str) -> Path:
    """将 media/... 相对路径解析为绝对路径（防穿越）。"""
    rel = (rel or "").replace("\\", "/").lstrip("/")
    if not rel.startswith("media/"):
        rel = f"media/{rel}" if not rel.startswith("media") else rel
    # 规范：一律相对 DATA_DIR
    if rel.startswith("media/"):
        candidate = (DATA_DIR / rel).resolve()
    else:
        candidate = (MEDIA_DIR / rel).resolve()
    root = DATA_DIR.resolve()
    if not str(candidate).startswith(str(root)):
        raise ValueError("非法媒体路径")
    return candidate


def is_local_media_ref(value: str | None) -> bool:
    if not value:
        return False
    v = value.strip().replace("\\", "/")
    if v.startswith("gmm-media:"):
        return True
    if v.startswith("media/"):
        return True
    if v.startswith("file://"):
        return True
    return False


def to_gmm_media_url(rel_or_path: str) -> str:
    rel = rel_or_path.strip().replace("\\", "/")
    if rel.startswith("gmm-media:"):
        return rel
    if rel.startswith("file://"):
        return rel
    if not rel.startswith("media/"):
        # 绝对路径 → 尽量转相对
        try:
            p = Path(rel).resolve()
            rel = str(p.relative_to(DATA_DIR.resolve())).replace("\\", "/")
        except Exception:
            rel = Path(rel).name
            rel = f"media/_loose/{rel}"
    return f"gmm-media:{rel}"


def parse_gmm_media_url(url: str) -> str | None:
    u = (url or "").strip()
    if u.startswith("gmm-media:"):
        return u[len("gmm-media:") :].lstrip("/")
    if u.startswith("media/"):
        return u
    return None


def _ext_from_mime(mime: str | None) -> str:
    mime = (mime or "").split(";")[0].strip().lower()
    mapping = {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/bmp": ".bmp",
        "image/svg+xml": ".svg",
    }
    if mime in mapping:
        return mapping[mime]
    guess = mimetypes.guess_extension(mime or "") or ""
    if guess == ".jpe":
        return ".jpg"
    return guess if guess else ".jpg"


def _ext_from_url(url: str) -> str:
    path = unquote(urlparse(url).path or "")
    suf = Path(path).suffix.lower()
    if suf in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}:
        return ".jpg" if suf == ".jpeg" else suf
    return ""


def _parse_cq_params(param_str: str) -> dict[str, str]:
    params: dict[str, str] = {}
    for part in param_str.split(","):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        # CQ 参数会把 URL 中的 &、逗号等转义为 HTML 实体；应在分段后还原，
        # 否则腾讯图片 URL 会携带字面量 “&amp;” 而下载失败。
        params[k.strip()] = html.unescape(v.strip())
    return params


def extract_image_refs(content: str) -> list[dict[str, str]]:
    """从消息 content 提取图片引用 {url, file, local_rel}。"""
    out: list[dict[str, str]] = []
    for m in CQ_IMAGE_RE.finditer(content or ""):
        params = _parse_cq_params(m.group(1))
        url = params.get("url") or ""
        file_v = params.get("file") or params.get("local") or ""
        local_rel = ""
        if file_v.startswith("media/") or file_v.startswith("gmm-media:"):
            local_rel = parse_gmm_media_url(file_v) or file_v
        elif url.startswith("gmm-media:") or url.startswith("media/"):
            local_rel = parse_gmm_media_url(url) or ""
        out.append(
            {
                "raw": m.group(0),
                "url": url,
                "file": file_v,
                "local_rel": local_rel,
            }
        )
    return out


def save_image_bytes(
    data: bytes,
    *,
    group_id: str,
    mime: str | None = None,
    preferred_ext: str = "",
) -> str:
    """写入本地，返回相对 DATA_DIR 的路径 media/{group}/{hash}{ext}。"""
    if not data:
        raise ValueError("空图片数据")
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("图片过大")
    ensure_media_dir()
    digest = hashlib.sha256(data).hexdigest()[:32]
    gid = re.sub(r"[^\w.\-]+", "_", str(group_id or "unknown"))[:80]
    ext = preferred_ext or _ext_from_mime(mime) or ".jpg"
    if not ext.startswith("."):
        ext = f".{ext}"
    rel = f"media/{gid}/{digest}{ext}"
    path = media_abs_path(rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(data)
    return rel.replace("\\", "/")


async def download_image_url(url: str, *, timeout: float = 30) -> tuple[bytes, str]:
    """下载远程图片，返回 (bytes, mime)。"""
    headers = dict(_BROWSER_HEADERS)
    # QQ / 腾讯图床常校验 Referer
    host = (urlparse(url).hostname or "").lower()
    if "qpic.cn" in host or "qq.com" in host or "gtimg.cn" in host:
        headers["Referer"] = "https://web.qphoto.qq.com/"
    timeout_cfg = aiohttp.ClientTimeout(total=timeout)
    async with aiohttp.ClientSession(timeout=timeout_cfg) as session:
        async with session.get(url, headers=headers, allow_redirects=True) as resp:
            if resp.status >= 400:
                raise RuntimeError(f"HTTP {resp.status}")
            mime = (resp.headers.get("Content-Type") or "image/jpeg").split(";")[0].strip()
            data = await resp.read()
    if not data:
        raise RuntimeError("空响应")
    if len(data) > MAX_IMAGE_BYTES:
        raise RuntimeError("图片过大")
    if mime and not mime.startswith("image/") and mime != "application/octet-stream":
        # 部分 CDN 不返回正确类型，若魔数像图片仍接受
        if not (
            data[:3] == b"\xff\xd8\xff"
            or data[:8] == b"\x89PNG\r\n\x1a\n"
            or data[:6] in (b"GIF87a", b"GIF89a")
            or data[:4] == b"RIFF"
        ):
            raise RuntimeError(f"非图片类型: {mime}")
        mime = "image/jpeg"
    return data, mime or "image/jpeg"


async def store_image_from_url(url: str, *, group_id: str) -> str | None:
    """下载并落盘，成功返回 media/... 相对路径。"""
    url = (url or "").strip()
    if not url.startswith("http://") and not url.startswith("https://"):
        return None
    try:
        data, mime = await download_image_url(url)
        ext = _ext_from_url(url) or _ext_from_mime(mime)
        return save_image_bytes(data, group_id=group_id, mime=mime, preferred_ext=ext)
    except Exception:
        logger.warning("下载图片失败 url=%s", url[:160], exc_info=True)
        return None


def store_image_from_bytes(
    data: bytes,
    *,
    group_id: str,
    mime: str | None = None,
    name_hint: str = "",
) -> str:
    ext = Path(name_hint).suffix if name_hint else ""
    return save_image_bytes(data, group_id=group_id, mime=mime, preferred_ext=ext)


def _escape_cq_value(v: str) -> str:
    return (
        v.replace("&", "&amp;")
        .replace("[", "&#91;")
        .replace("]", "&#93;")
        .replace(",", "&#44;")
    )


def build_local_image_cq(*, local_rel: str, url: str = "") -> str:
    local_rel = local_rel.replace("\\", "/")
    parts = [f"file={_escape_cq_value(local_rel)}"]
    if url and url.startswith("http"):
        parts.append(f"url={_escape_cq_value(url)}")
    return f"[CQ:image,{','.join(parts)}]"


async def materialize_content_images(content: str, *, group_id: str) -> str:
    """把 content 里远程图片下载到本地，并改写 CQ。"""
    if not content or "[cq:image" not in content.lower():
        return content

    refs = extract_image_refs(content)
    if not refs:
        return content

    new_content = content
    for ref in refs:
        local_rel = ref.get("local_rel") or ""
        if local_rel:
            try:
                if media_abs_path(local_rel).exists():
                    # 确保 CQ 使用标准 file=media/... 形式
                    fixed = build_local_image_cq(
                        local_rel=local_rel,
                        url=ref.get("url") or "",
                    )
                    if fixed != ref["raw"]:
                        new_content = new_content.replace(ref["raw"], fixed, 1)
                    continue
            except Exception:
                pass
        url = ref.get("url") or ""
        file_v = ref.get("file") or ""
        remote = url if url.startswith("http") else (file_v if file_v.startswith("http") else "")
        if not remote:
            continue
        saved = await store_image_from_url(remote, group_id=group_id)
        if not saved:
            continue
        new_cq = build_local_image_cq(local_rel=saved, url=remote)
        new_content = new_content.replace(ref["raw"], new_cq, 1)
    return new_content


async def materialize_event_images(event: Any) -> str:
    """基于事件结构化 message + summary，下载图片并返回最终 content。"""
    from app.models import GroupMessageEvent

    if not isinstance(event, GroupMessageEvent):
        return str(getattr(event, "message_summary", "") or "")

    group_id = event.group_id_str
    # 先从结构化段下载，再统一写 CQ
    parts: list[str] = []
    if isinstance(event.message, list):
        for seg in event.message:
            if not isinstance(seg, dict):
                continue
            seg_type = seg.get("type")
            data = seg.get("data") or {}
            if seg_type == "text":
                parts.append(str(data.get("text", "")))
            elif seg_type in ("image", "mface"):
                url = ""
                for key in ("url", "file"):
                    v = data.get(key)
                    if isinstance(v, str) and v.startswith("http"):
                        url = v
                        break
                local: str | None = None
                if url:
                    local = await store_image_from_url(url, group_id=group_id)
                if local:
                    parts.append(build_local_image_cq(local_rel=local, url=url))
                elif url:
                    parts.append(f"[CQ:image,url={_escape_cq_value(url)}]")
                else:
                    parts.append("[图片]")
            elif seg_type == "face":
                face_id = data.get("id", "")
                parts.append(f"[CQ:face,id={face_id}]" if face_id != "" else "[表情]")
            elif seg_type == "reply":
                rid = data.get("id") or data.get("seq") or ""
                rtext = data.get("text") or data.get("content") or ""
                if rtext:
                    parts.append(f"[CQ:reply,id={rid},text={rtext}]")
                else:
                    parts.append(f"[CQ:reply,id={rid}]")
            elif seg_type == "at":
                qq = data.get("qq", "")
                name = data.get("name") or ""
                if name:
                    parts.append(f"[CQ:at,qq={qq},name={name}]")
                else:
                    parts.append(f"[CQ:at,qq={qq}]")
            elif seg_type == "file":
                parts.append(f"[文件:{data.get('name', '')}]")
            elif seg_type == "record":
                parts.append("[语音]")
            elif seg_type == "video":
                parts.append("[视频]")
            elif seg_type in ("json", "xml", "forward"):
                parts.append("[卡片消息]" if seg_type != "forward" else "[合并转发]")
            elif seg_type == "share":
                url = data.get("url") or ""
                parts.append(url or "[分享]")
            else:
                parts.append(f"[{seg_type}]")
        content = "".join(parts).strip() or event.message_summary
    else:
        content = event.message_summary

    # 再处理纯文本里残留的远程 CQ
    return await materialize_content_images(content, group_id=group_id)


def read_local_image_b64(local_rel: str) -> tuple[str, str] | None:
    """返回 (mime, base64) 供视觉模型使用。"""
    import base64

    try:
        path = media_abs_path(local_rel)
    except Exception:
        return None
    if not path.exists() or not path.is_file():
        return None
    data = path.read_bytes()
    if not data or len(data) > MAX_IMAGE_BYTES:
        return None
    mime = mimetypes.guess_type(str(path))[0] or "image/jpeg"
    return mime, base64.b64encode(data).decode("ascii")
