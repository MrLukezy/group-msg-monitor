"""微信 PC 本地库适配器：登录态取 key → 解密 → 轮询群消息。

说明：
- 仅读取本机已登录微信账号自己的本地数据，用于个人监控。
- 微信 4.x 使用 SQLCipher 4；密钥通常缓存在进程内存中。
- 也支持直接读取「已解密目录」中的明文 SQLite（高级用户手动解密后指定）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import sqlite3
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

PAGE_SIZE = 4096
IV_SIZE = 16
HMAC_SIZE = 64
RESERVE = IV_SIZE + HMAC_SIZE
SQLITE_HEADER = b"SQLite format 3\x00"


def default_wechat_files_roots() -> list[Path]:
    """发现本机 xwechat_files / WeChat Files 根目录（含自定义安装位置）。"""
    roots: list[Path] = []
    seen: set[str] = set()

    def add(p: Path | None) -> None:
        if p is None:
            return
        try:
            p = p.resolve()
        except OSError:
            p = Path(p)
        key = str(p).lower()
        if key in seen:
            return
        if p.is_dir():
            seen.add(key)
            roots.append(p)

    home = Path.home()
    userprofile = Path(os.environ.get("USERPROFILE", "") or str(home))
    appdata = Path(os.environ.get("APPDATA", "") or "")
    localappdata = Path(os.environ.get("LOCALAPPDATA", "") or "")

    # 1) 默认文档目录
    for c in (
        home / "Documents" / "xwechat_files",
        home / "Documents" / "WeChat Files",
        userprofile / "Documents" / "xwechat_files",
        userprofile / "Documents" / "WeChat Files",
        home / "xwechat_files",
        userprofile / "xwechat_files",
    ):
        add(c)

    # 2) 从 %APPDATA%\Tencent\xwechat\config\*.ini 读自定义路径（常见为明文绝对路径）
    cfg_dir = appdata / "Tencent" / "xwechat" / "config" if appdata else None
    if cfg_dir and cfg_dir.is_dir():
        for ini in cfg_dir.glob("*.ini"):
            try:
                raw = ini.read_bytes()
            except OSError:
                continue
            # 尝试 utf-16le / utf-8 / gbk
            text = ""
            for enc in ("utf-16-le", "utf-8", "gbk", "latin-1"):
                try:
                    text = raw.decode(enc).strip("\x00").strip()
                    if text:
                        break
                except Exception:
                    continue
            if not text:
                continue
            # ini 可能整文件就是路径，或含 path= / FilePath=
            path_cand = text
            for line in text.replace("\r", "\n").split("\n"):
                line = line.strip()
                if not line:
                    continue
                if "=" in line:
                    _, v = line.split("=", 1)
                    path_cand = v.strip().strip('"')
                else:
                    path_cand = line
                p = Path(path_cand)
                if p.is_dir():
                    # 若指向账号目录，则取其父级 xwechat_files
                    if (p / "db_storage").is_dir():
                        add(p.parent)
                    elif p.name.lower() in ("xwechat_files", "wechat files"):
                        add(p)
                    else:
                        # 可能是 wechatLog 之类
                        add(p)
                        add(p / "xwechat_files")

    # 3) 跟随正在运行的 Weixin.exe / WeChat.exe，找同级 wechatLog\xwechat_files
    for exe_dir in _running_wechat_exe_dirs():
        # .../wechatApp/Weixin  →  .../wechatLog/xwechat_files
        for parent in [exe_dir, *exe_dir.parents]:
            add(parent / "xwechat_files")
            add(parent / "wechatLog" / "xwechat_files")
            add(parent / "WeChat Files")
            # 常见：Program Files\wechatApp 与 wechatLog 同级
            if parent.name.lower() in ("wechatapp", "weixin", "wechat"):
                add(parent.parent / "wechatLog" / "xwechat_files")
                add(parent.parent / "xwechat_files")

    # 4) 浅层扫描常见盘符 / 用户目录下的 xwechat_files（深度有限，避免全盘慢扫）
    scan_bases: list[Path] = [userprofile, home]
    for env_key in ("SystemDrive",):
        drive = os.environ.get(env_key)
        if drive:
            scan_bases.append(Path(drive + "\\"))
    # 额外常见自定义根
    for extra in (
        Path("C:/Lukezy"),
        Path("D:/"),
        Path("E:/"),
        Path(os.environ.get("ProgramFiles", "C:/Program Files")),
        Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")),
        appdata / "Tencent" if appdata else None,
        localappdata / "Tencent" if localappdata else None,
    ):
        if extra:
            scan_bases.append(extra)

    for base in scan_bases:
        if not base or not base.exists():
            continue
        try:
            # 直接子目录
            for child in base.iterdir():
                if not child.is_dir():
                    continue
                name = child.name.lower()
                if name in ("xwechat_files", "wechat files"):
                    add(child)
                if name in ("wechatlog", "wechat", "weixin", "wechatapp", "tencent"):
                    add(child / "xwechat_files")
                    add(child / "WeChat Files")
                    add(child / "wechatLog" / "xwechat_files")
                # 再下一层（如 Program Files\xxx）
                if name in ("program files", "program files (x86)", "lukezy"):
                    try:
                        for sub in child.iterdir():
                            if not sub.is_dir():
                                continue
                            sn = sub.name.lower()
                            if sn in ("xwechat_files", "wechat files"):
                                add(sub)
                            if "wechat" in sn or "weixin" in sn or "tencent" in sn:
                                add(sub / "xwechat_files")
                                add(sub / "wechatLog" / "xwechat_files")
                                add(sub / "WeChat Files")
                    except OSError:
                        pass
        except OSError:
            continue

    return roots


def _running_wechat_exe_dirs() -> list[Path]:
    dirs: list[Path] = []
    if sys.platform != "win32":
        return dirs
    try:
        import subprocess

        out = subprocess.check_output(
            ["tasklist", "/FO", "CSV", "/NH"],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            text=True,
            encoding="utf-8",
            errors="ignore",
        )
    except Exception:
        return dirs

    names = {"weixin.exe", "wechat.exe"}
    pids: list[int] = []
    for line in out.splitlines():
        parts = [p.strip().strip('"') for p in line.split(",")]
        if len(parts) < 2:
            continue
        if parts[0].lower() in names:
            try:
                pids.append(int(parts[1]))
            except ValueError:
                pass
    # 用 wmic / PowerShell 取路径；失败则跳过
    for pid in pids[:8]:
        try:
            import subprocess

            cmd = [
                "powershell",
                "-NoProfile",
                "-Command",
                f"(Get-Process -Id {pid} -ErrorAction SilentlyContinue).Path",
            ]
            path = subprocess.check_output(
                cmd,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                text=True,
                encoding="utf-8",
                errors="ignore",
            ).strip()
            if path and Path(path).is_file():
                d = Path(path).resolve().parent
                if d not in dirs:
                    dirs.append(d)
        except Exception:
            continue
    return dirs


def detect_wechat_accounts() -> list[dict[str, Any]]:
    """扫描本机微信账号目录。"""
    accounts: list[dict[str, Any]] = []
    skip_names = {
        "all users",
        "all_users",
        "wmpf",
        "backup",
        "radium",
        "xplugin",
        "config",
        "login",
        "log",
        "net",
        "update",
        "uh",
        "ilink",
        "crashinfo",
        "confsdk",
    }
    for root in default_wechat_files_roots():
        try:
            for child in root.iterdir():
                if not child.is_dir():
                    continue
                name = child.name
                if name.lower() in skip_names:
                    continue
                db_storage = child / "db_storage"
                msg_dir = db_storage / "message"
                # 旧版路径
                if not msg_dir.is_dir():
                    msg_dir = child / "Msg"
                # 没库的目录跳过（减少噪音）
                if not db_storage.is_dir() and not msg_dir.is_dir():
                    continue
                accounts.append(
                    {
                        "account": name,
                        "data_dir": str(child),
                        "db_storage": str(db_storage if db_storage.is_dir() else child),
                        "has_message_db": msg_dir.is_dir(),
                        "root": str(root),
                    }
                )
        except OSError:
            continue

    # 去重
    uniq: dict[str, dict[str, Any]] = {}
    for a in accounts:
        uniq[a["data_dir"].lower()] = a
    return list(uniq.values())


def searched_wechat_hints() -> list[str]:
    """给 UI 的排查提示：当前扫到的根目录。"""
    return [str(p) for p in default_wechat_files_roots()]


def wechat_keys_path() -> Path:
    return DATA_DIR / "wechat_keys.json"


def wechat_decrypted_dir(account: str = "default") -> Path:
    safe = re.sub(r"[^\w\-@.]+", "_", account or "default")
    return DATA_DIR / "wechat_decrypted" / safe


def detect_wechat_file_version() -> str:
    """读取本机 Weixin.exe / WeChat.exe 的文件版本（如 4.1.11.55）。"""
    if sys.platform != "win32":
        return ""
    for exe_dir in _running_wechat_exe_dirs():
        for name in ("Weixin.exe", "WeChat.exe"):
            exe = exe_dir / name
            if not exe.is_file():
                continue
            ver = _win_file_version(exe)
            if ver:
                return ver
    # 进程未跑时，仍尝试常见安装路径
    for cand in (
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Tencent" / "Weixin" / "Weixin.exe",
        Path(r"C:\Lukezy\Program Files\wechatApp\Weixin\Weixin.exe"),
    ):
        ver = _win_file_version(cand)
        if ver:
            return ver
    return ""


def _win_file_version(path: Path) -> str:
    try:
        import ctypes
        from ctypes import wintypes

        if not path.is_file():
            return ""
        size = ctypes.windll.version.GetFileVersionInfoSizeW(str(path), None)
        if not size:
            return ""
        buf = ctypes.create_string_buffer(size)
        if not ctypes.windll.version.GetFileVersionInfoW(str(path), 0, size, buf):
            return ""
        u = ctypes.c_void_p()
        ln = wintypes.UINT()
        if not ctypes.windll.version.VerQueryValueW(
            buf, "\\", ctypes.byref(u), ctypes.byref(ln)
        ):
            return ""

        class VS_FIXEDFILEINFO(ctypes.Structure):
            _fields_ = [
                ("dwSignature", wintypes.DWORD),
                ("dwStrucVersion", wintypes.DWORD),
                ("dwFileVersionMS", wintypes.DWORD),
                ("dwFileVersionLS", wintypes.DWORD),
                ("dwProductVersionMS", wintypes.DWORD),
                ("dwProductVersionLS", wintypes.DWORD),
                ("dwFileFlagsMask", wintypes.DWORD),
                ("dwFileFlags", wintypes.DWORD),
                ("dwFileOS", wintypes.DWORD),
                ("dwFileType", wintypes.DWORD),
                ("dwFileSubtype", wintypes.DWORD),
                ("dwFileDateMS", wintypes.DWORD),
                ("dwFileDateLS", wintypes.DWORD),
            ]

        info = ctypes.cast(u, ctypes.POINTER(VS_FIXEDFILEINFO)).contents
        ms, ls = info.dwFileVersionMS, info.dwFileVersionLS
        return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
    except Exception:
        return ""


def _version_tuple(ver: str) -> tuple[int, ...]:
    parts: list[int] = []
    for p in (ver or "").split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def wechat_memory_scan_likely_broken(ver: str = "") -> bool:
    """微信 ≥4.1.10 起，被动内存扫描 x'enc+salt' 往往失效。"""
    v = ver or detect_wechat_file_version()
    if not v:
        return False
    return _version_tuple(v) >= (4, 1, 10)


def normalize_key_items(raw: Any) -> list[dict[str, str]]:
    """兼容本工具与 wechat-decrypt 常见密钥 JSON。"""
    items: list[dict[str, str]] = []

    def add(enc_key: str, salt: str) -> None:
        enc_key = (enc_key or "").strip().lower()
        salt = (salt or "").strip().lower()
        if len(enc_key) == 64 and len(salt) == 32:
            items.append({"enc_key": enc_key, "salt": salt})

    if raw is None:
        return []
    if isinstance(raw, list):
        for it in raw:
            if isinstance(it, dict):
                add(
                    str(it.get("enc_key") or it.get("encKey") or it.get("key") or ""),
                    str(it.get("salt") or it.get("salt_hex") or ""),
                )
        return items
    if isinstance(raw, dict):
        if "keys" in raw:
            return normalize_key_items(raw.get("keys"))
        # {salt: enc_key} 或 {salt: {enc_key: ...}}
        for k, v in raw.items():
            if k in ("data_dir", "ts", "version", "db_dir", "message"):
                continue
            if isinstance(v, str) and len(k) == 32 and len(v) == 64:
                add(v, k)
            elif isinstance(v, dict):
                add(
                    str(v.get("enc_key") or v.get("encKey") or v.get("key") or ""),
                    str(v.get("salt") or k),
                )
    return items


def load_keys_file(path: str | Path) -> list[dict[str, str]]:
    p = Path(path)
    payload = json.loads(p.read_text(encoding="utf-8"))
    return normalize_key_items(payload)


def save_keys_file(keys: list[dict[str, str]], data_dir: str = "", path: Path | None = None) -> Path:
    out = path or wechat_keys_path()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {"keys": keys, "data_dir": data_dir, "ts": time.time()},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return out


def _aes_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    return decryptor.update(data) + decryptor.finalize()


def decrypt_sqlcipher_file(src: Path, enc_key: bytes, dst: Path) -> None:
    """将 SQLCipher 4 库解密为普通 SQLite（页面级 AES-CBC）。"""
    raw = src.read_bytes()
    if raw.startswith(SQLITE_HEADER):
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(raw)
        return
    if len(raw) < PAGE_SIZE or len(enc_key) != 32:
        raise ValueError(f"无法解密: {src.name}")

    out = bytearray()
    out.extend(SQLITE_HEADER)
    page_count = len(raw) // PAGE_SIZE
    for page_num in range(page_count):
        start = page_num * PAGE_SIZE
        page = raw[start : start + PAGE_SIZE]
        if page_num == 0:
            body = page[16:]
        else:
            body = page
        if len(body) < RESERVE:
            break
        iv = body[-RESERVE : -RESERVE + IV_SIZE]
        cipher_bytes = body[:-RESERVE]
        try:
            plain = _aes_cbc_decrypt(enc_key, iv, cipher_bytes)
        except Exception as e:
            raise ValueError(f"解密页失败 {src.name} page={page_num}: {e}") from e
        if page_num == 0:
            # header(16) + plain 应凑满一页
            need = PAGE_SIZE - 16
            out.extend(plain[:need])
            if len(plain) < need:
                out.extend(b"\x00" * (need - len(plain)))
        else:
            out.extend(plain)
            if len(plain) < PAGE_SIZE:
                out.extend(b"\x00" * (PAGE_SIZE - len(plain)))

    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(bytes(out))


def scan_keys_from_process() -> dict[str, Any]:
    """从已登录微信进程内存扫描 SQLCipher raw key（Windows）。"""
    if sys.platform != "win32":
        return {"ok": False, "message": "当前仅实现 Windows 内存取 key", "keys": []}

    try:
        import ctypes
        from ctypes import wintypes
    except Exception as e:
        return {"ok": False, "message": f"ctypes 不可用: {e}", "keys": []}

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    PROCESS_QUERY_INFORMATION = 0x0400
    PROCESS_VM_READ = 0x0010
    MEM_COMMIT = 0x1000
    PAGE_READABLE = {0x02, 0x04, 0x06, 0x20, 0x40, 0x80}

    class MEMORY_BASIC_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BaseAddress", ctypes.c_void_p),
            ("AllocationBase", ctypes.c_void_p),
            ("AllocationProtect", wintypes.DWORD),
            ("RegionSize", ctypes.c_size_t),
            ("State", wintypes.DWORD),
            ("Protect", wintypes.DWORD),
            ("Type", wintypes.DWORD),
        ]

    def list_pids(image_names: set[str]) -> list[int]:
        pids: list[int] = []
        try:
            import subprocess

            out = subprocess.check_output(
                ["tasklist", "/FO", "CSV", "/NH"],
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                text=True,
                encoding="utf-8",
                errors="ignore",
            )
            for line in out.splitlines():
                parts = [p.strip().strip('"') for p in line.split(",")]
                if len(parts) < 2:
                    continue
                name, pid_s = parts[0], parts[1]
                if name.lower() in image_names:
                    try:
                        pids.append(int(pid_s))
                    except ValueError:
                        pass
        except Exception:
            pass
        return pids

    image_names = {"weixin.exe", "wechat.exe"}
    pids = list_pids(image_names)
    if not pids:
        return {
            "ok": False,
            "message": "未找到已登录的微信进程（Weixin.exe / WeChat.exe），请先登录 PC 微信",
            "keys": [],
        }

    # 匹配 x'<64hex key><32hex salt>'
    pattern = re.compile(rb"x'([0-9a-fA-F]{64})([0-9a-fA-F]{32})'")
    found: dict[str, dict[str, str]] = {}

    for pid in pids:
        handle = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
        if not handle:
            continue
        try:
            address = 0
            mbi = MEMORY_BASIC_INFORMATION()
            while address < 0x7FFFFFFFFFFF:
                ret = kernel32.VirtualQueryEx(
                    handle,
                    ctypes.c_void_p(address),
                    ctypes.byref(mbi),
                    ctypes.sizeof(mbi),
                )
                if not ret:
                    break
                base = mbi.BaseAddress or 0
                size = int(mbi.RegionSize or 0)
                prot = int(mbi.Protect or 0)
                state = int(mbi.State or 0)
                next_addr = base + size
                if next_addr <= address:
                    break
                address = next_addr
                if state != MEM_COMMIT or (prot & 0xFF) not in PAGE_READABLE:
                    continue
                if size <= 0 or size > 64 * 1024 * 1024:
                    continue
                buf = (ctypes.c_char * size)()
                read = ctypes.c_size_t(0)
                ok = kernel32.ReadProcessMemory(
                    handle,
                    ctypes.c_void_p(base),
                    buf,
                    size,
                    ctypes.byref(read),
                )
                if not ok or read.value <= 0:
                    continue
                data = bytes(buf[: read.value])
                for m in pattern.finditer(data):
                    enc_key = m.group(1).decode("ascii").lower()
                    salt = m.group(2).decode("ascii").lower()
                    found[salt] = {"enc_key": enc_key, "salt": salt}
        finally:
            kernel32.CloseHandle(handle)

    keys = list(found.values())
    ver = detect_wechat_file_version()
    if not keys:
        tip = "进程内未扫到数据库密钥。请确认微信已登录，并以管理员身份运行桌面端后再试"
        if wechat_memory_scan_likely_broken(ver):
            tip = (
                f"当前微信版本 {ver or '≥4.1.10'} 通常已不再把 SQLCipher raw key 缓存在可扫描内存中，"
                "因此无法自动扫密钥、也就无法解密列群。"
                "请改用「导入密钥文件」：用旧版（≤4.1.9）提取一次后导入 wechat_keys.json / all_keys.json；"
                "密钥本身升级后一般仍可用。"
            )
        elif ver:
            tip = f"{tip}（当前版本 {ver}）"
        return {"ok": False, "message": tip, "keys": [], "version": ver}
    return {
        "ok": True,
        "message": f"扫到 {len(keys)} 组密钥" + (f"（微信 {ver}）" if ver else ""),
        "keys": keys,
        "version": ver,
    }


def _match_key_for_db(db_path: Path, keys: list[dict[str, str]]) -> bytes | None:
    try:
        salt = db_path.read_bytes()[:16].hex()
    except OSError:
        return None
    for item in keys:
        if (item.get("salt") or "").lower() == salt.lower():
            try:
                return bytes.fromhex(item["enc_key"])
            except Exception:
                return None
    # 若仅一组 key，尝试直接用（部分版本 salt 匹配失败时兜底）
    if len(keys) == 1:
        try:
            return bytes.fromhex(keys[0]["enc_key"])
        except Exception:
            return None
    return None


def decrypt_account_dbs(
    data_dir: str | Path,
    keys: list[dict[str, str]],
    out_dir: str | Path | None = None,
) -> dict[str, Any]:
    data_dir = Path(data_dir)
    account = data_dir.name
    out = Path(out_dir) if out_dir else wechat_decrypted_dir(account)
    out.mkdir(parents=True, exist_ok=True)

    db_root = data_dir / "db_storage"
    if not db_root.is_dir():
        db_root = data_dir

    targets: list[Path] = []
    for sub in ("message", "contact", "session"):
        folder = db_root / sub
        if folder.is_dir():
            targets.extend(sorted(folder.glob("*.db")))
    if not targets:
        targets = sorted(db_root.rglob("*.db"))

    ok_n = 0
    errors: list[str] = []
    for src in targets:
        rel = src.relative_to(db_root) if str(src).startswith(str(db_root)) else Path(src.name)
        dst = out / rel
        if src.read_bytes()[:16] == SQLITE_HEADER[:16]:
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(src.read_bytes())
            ok_n += 1
            continue
        key = _match_key_for_db(src, keys)
        if key is None:
            errors.append(f"无匹配密钥: {rel.as_posix()}")
            continue
        try:
            decrypt_sqlcipher_file(src, key, dst)
            # 校验头
            if not dst.read_bytes().startswith(SQLITE_HEADER):
                errors.append(f"解密结果无效: {rel.as_posix()}")
                continue
            ok_n += 1
        except Exception as e:
            errors.append(f"{rel.as_posix()}: {e}")

    return {
        "ok": ok_n > 0,
        "decrypted": ok_n,
        "total": len(targets),
        "out_dir": str(out),
        "errors": errors[:20],
        "message": f"已解密 {ok_n}/{len(targets)} 个库 → {out}",
    }


def _maybe_decompress(content: Any, ct_flag: Any) -> str:
    if content is None:
        return ""
    raw: bytes
    if isinstance(content, bytes):
        raw = content
    elif isinstance(content, str):
        return content
    else:
        raw = bytes(content)
    if ct_flag == 4 or (isinstance(ct_flag, int) and ct_flag == 4):
        try:
            import zstandard as zstd

            return zstd.ZstdDecompressor().decompress(raw).decode("utf-8", errors="replace")
        except Exception:
            pass
    try:
        return raw.decode("utf-8", errors="replace")
    except Exception:
        return ""


def _format_wx_text(local_type: int, content: str) -> str:
    t = local_type & 0xFFFF
    if t == 1:
        # 群消息常为 "wxid:\n正文"
        if ":\n" in content:
            return content.split(":\n", 1)[1]
        return content
    mapping = {
        3: "[图片]",
        34: "[语音]",
        42: "[名片]",
        43: "[视频]",
        47: "[表情]",
        48: "[位置]",
        49: "[应用消息]",
        50: "[通话]",
        10000: content or "[系统消息]",
        11000: content or "[系统通知]",
    }
    return mapping.get(t, content or f"[类型:{t}]")


def list_wechat_groups(decrypted_dir: str | Path) -> list[dict[str, Any]]:
    root = Path(decrypted_dir)
    groups: dict[str, str] = {}
    contact_db = root / "contact" / "contact.db"
    session_db = root / "session" / "session.db"

    def open_ro(path: Path) -> sqlite3.Connection | None:
        if not path.exists():
            return None
        try:
            conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            return conn
        except sqlite3.Error:
            return None

    conn = open_ro(contact_db)
    if conn is not None:
        try:
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if "contact" in tables:
                cols = {r[1] for r in conn.execute("PRAGMA table_info(contact)").fetchall()}
                name_col = "remark" if "remark" in cols else ("nick_name" if "nick_name" in cols else None)
                for row in conn.execute("SELECT * FROM contact").fetchall():
                    username = str(row["username"] if "username" in row.keys() else "")
                    if not username.endswith("@chatroom"):
                        continue
                    display = ""
                    if name_col and row[name_col]:
                        display = str(row[name_col])
                    elif "nick_name" in row.keys() and row["nick_name"]:
                        display = str(row["nick_name"])
                    groups[username] = display or username
            if "chat_room" in tables:
                for row in conn.execute("SELECT * FROM chat_room").fetchall():
                    keys = row.keys()
                    username = ""
                    for k in ("username", "chat_room_id", "room_id"):
                        if k in keys and row[k]:
                            username = str(row[k])
                            break
                    if not username.endswith("@chatroom"):
                        continue
                    title = ""
                    for k in ("nick_name", "remark", "room_name"):
                        if k in keys and row[k]:
                            title = str(row[k])
                            break
                    groups[username] = title or groups.get(username) or username
        finally:
            conn.close()

    conn = open_ro(session_db)
    if conn is not None:
        try:
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            for t in tables:
                if "session" not in t.lower():
                    continue
                try:
                    rows = conn.execute(f"SELECT * FROM [{t}] LIMIT 500").fetchall()
                except sqlite3.Error:
                    continue
                for row in rows:
                    keys = row.keys()
                    username = ""
                    for k in ("username", "user_name", "talker", "chat_name"):
                        if k in keys and row[k]:
                            username = str(row[k])
                            break
                    if not username.endswith("@chatroom"):
                        continue
                    title = ""
                    for k in ("nickname", "nick_name", "strNickName", "remark"):
                        if k in keys and row[k]:
                            title = str(row[k])
                            break
                    groups[username] = title or groups.get(username) or username
        finally:
            conn.close()

    return [
        {
            "group_id": make_group_id("wechat", u),
            "group_name": n,
            "channel": "wechat",
        }
        for u, n in sorted(groups.items(), key=lambda x: x[1] or x[0])
    ]


def _name2id_map(conn: sqlite3.Connection) -> dict[int, str]:
    out: dict[int, str] = {}
    try:
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        if "Name2Id" not in tables:
            return out
        for row in conn.execute("SELECT rowid, user_name FROM Name2Id").fetchall():
            out[int(row[0])] = str(row[1] or "")
    except sqlite3.Error:
        pass
    return out


def _username_from_msg_table(table: str, name_map: dict[int, str], conn: sqlite3.Connection) -> str | None:
    # Msg_<md5(username)>
    if not table.startswith("Msg_"):
        return None
    digest = table[4:]
    # 反查 Name2Id
    for uid, username in name_map.items():
        if hashlib.md5(username.encode("utf-8")).hexdigest() == digest:
            return username
    # 再扫一遍所有 user_name
    try:
        for row in conn.execute("SELECT user_name FROM Name2Id").fetchall():
            u = str(row[0] or "")
            if hashlib.md5(u.encode("utf-8")).hexdigest() == digest:
                return u
    except sqlite3.Error:
        pass
    return None


def iter_new_group_messages(
    decrypted_dir: str | Path,
    *,
    since_state: dict[str, int],
    only_groups: set[str] | None = None,
) -> tuple[list[GroupMessageEvent], dict[str, int]]:
    """扫描解密后的 message_*.db，返回新群消息，并更新 since_state[table]=max_local_id。"""
    root = Path(decrypted_dir)
    msg_dir = root / "message"
    if not msg_dir.is_dir():
        msg_dir = root
    events: list[GroupMessageEvent] = []
    new_state = dict(since_state)

    for db_path in sorted(msg_dir.glob("message_*.db")):
        try:
            conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
        except sqlite3.Error:
            continue
        try:
            name_map = _name2id_map(conn)
            tables = [
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"
                ).fetchall()
            ]
            for table in tables:
                username = _username_from_msg_table(table, name_map, conn)
                if not username or not username.endswith("@chatroom"):
                    continue
                gid = make_group_id("wechat", username)
                raw_id = username
                if only_groups is not None and gid not in only_groups and raw_id not in only_groups:
                    continue
                state_key = f"{db_path.name}:{table}"
                last_id = int(new_state.get(state_key, 0))
                try:
                    cols = {r[1] for r in conn.execute(f"PRAGMA table_info([{table}])").fetchall()}
                    has_ct = "WCDB_CT_message_content" in cols
                    sql = (
                        f"SELECT local_id, server_id, local_type, real_sender_id, create_time, "
                        f"message_content"
                        + (", WCDB_CT_message_content" if has_ct else "")
                        + f" FROM [{table}] WHERE local_id > ? ORDER BY local_id ASC LIMIT 200"
                    )
                    rows = conn.execute(sql, (last_id,)).fetchall()
                except sqlite3.Error:
                    continue
                for row in rows:
                    local_id = int(row["local_id"])
                    new_state[state_key] = max(new_state.get(state_key, 0), local_id)
                    ct = row["WCDB_CT_message_content"] if has_ct else 0
                    content = _maybe_decompress(row["message_content"], ct)
                    local_type = int(row["local_type"] or 0)
                    text = _format_wx_text(local_type, content)
                    sender_id = row["real_sender_id"]
                    sender_name = name_map.get(int(sender_id or 0), str(sender_id or ""))
                    # 文本里若带 wxid 前缀，优先用
                    if isinstance(content, str) and ":\n" in content:
                        sender_name = content.split(":\n", 1)[0] or sender_name
                    mid = row["server_id"] or local_id
                    events.append(
                        GroupMessageEvent(
                            post_type="message",
                            message_type="group",
                            group_id=gid,
                            user_id=sender_name,
                            message_id=mid,
                            raw_message=text,
                            message=text,
                            sender={
                                "user_id": sender_name,
                                "nickname": sender_name,
                                "card": sender_name,
                            },
                            time=int(row["create_time"] or 0) or None,
                            self_id="wechat",
                        )
                    )
        finally:
            conn.close()

    events.sort(key=lambda e: (e.time or 0, str(e.message_id)))
    return events, new_state


class WechatLocalAdapter:
    def __init__(
        self,
        *,
        data_dir: str,
        decrypted_dir: str = "",
        keys_path: str = "",
        on_message: EventHandler,
        poll_seconds: float = 1.0,
        refresh_decrypt: bool = True,
    ) -> None:
        self.data_dir = data_dir
        self.decrypted_dir = decrypted_dir
        self.keys_path = keys_path or str(wechat_keys_path())
        self.on_message = on_message
        self.poll_seconds = max(0.3, float(poll_seconds or 1.0))
        self.refresh_decrypt = refresh_decrypt
        self._stop = asyncio.Event()
        self._state_path = DATA_DIR / "wechat_poll_state.json"
        self._state: dict[str, int] = {}
        self._load_state()

    def stop(self) -> None:
        self._stop.set()

    def _load_state(self) -> None:
        if self._state_path.exists():
            try:
                self._state = json.loads(self._state_path.read_text(encoding="utf-8"))
            except Exception:
                self._state = {}

    def _save_state(self) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._state_path.write_text(
            json.dumps(self._state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _resolve_decrypted(self) -> Path | None:
        if self.decrypted_dir and Path(self.decrypted_dir).is_dir():
            return Path(self.decrypted_dir)
        if self.data_dir:
            guess = wechat_decrypted_dir(Path(self.data_dir).name)
            if guess.is_dir():
                return guess
        return None

    def _maybe_refresh_decrypt(self) -> Path | None:
        out = self._resolve_decrypted()
        if not self.refresh_decrypt:
            return out
        keys_file = Path(self.keys_path)
        if not keys_file.exists() or not self.data_dir:
            return out
        try:
            payload = json.loads(keys_file.read_text(encoding="utf-8"))
            keys = payload.get("keys") if isinstance(payload, dict) else payload
            if not isinstance(keys, list) or not keys:
                return out
            target = out or wechat_decrypted_dir(Path(self.data_dir).name)
            # 节流：最多每 10 秒全量刷新一次
            marker = target / ".last_decrypt"
            if marker.exists() and time.time() - marker.stat().st_mtime < 10:
                return target
            decrypt_account_dbs(self.data_dir, keys, target)
            marker.write_text(str(time.time()), encoding="utf-8")
            return target
        except Exception:
            logger.exception("微信库解密刷新失败")
            return out

    async def run_forever(self) -> None:
        logger.info(
            "微信本地监听启动 | data_dir=%s decrypted=%s",
            self.data_dir,
            self.decrypted_dir or "(auto)",
        )
        while not self._stop.is_set():
            try:
                await self._tick_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("微信轮询异常")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_seconds)
                break
            except asyncio.TimeoutError:
                pass

    async def _tick_once(self) -> None:
        from app.settings_store import enabled_group_ids

        root = await asyncio.to_thread(self._maybe_refresh_decrypt)
        if root is None or not root.is_dir():
            return
        allowed = enabled_group_ids()
        only: set[str] = set()
        for gid in allowed:
            only.add(gid)
            if gid.startswith("wx:"):
                only.add(gid[3:])
            elif gid.endswith("@chatroom"):
                only.add(make_group_id("wechat", gid))
        if not only:
            return
        events, new_state = await asyncio.to_thread(
            iter_new_group_messages,
            root,
            since_state=self._state,
            only_groups=only,
        )
        self._state = new_state
        if events:
            await asyncio.to_thread(self._save_state)
        for ev in events:
            try:
                await self.on_message(ev)
            except Exception:
                logger.exception("微信消息回调失败")


async def bind_wechat_flow(
    data_dir: str = "",
    scan_keys: bool = True,
    keys_file: str = "",
) -> dict[str, Any]:
    """绑定辅助：检测账号、扫/导入密钥、解密、列群。"""
    accounts = detect_wechat_accounts()
    if not data_dir:
        if not accounts:
            roots = searched_wechat_hints()
            tip = "；".join(roots[:5]) if roots else "（未扫到任何 xwechat_files 根目录）"
            return {
                "ok": False,
                "message": (
                    "未找到本机微信账号目录。请先登录 PC 微信，或在「数据目录」手动填写 "
                    r"含 db_storage 的账号路径（例如 …\xwechat_files\你的账号）。"
                    f" 已扫描根：{tip}"
                ),
                "accounts": [],
                "roots": roots,
            }
        data_dir = accounts[0]["data_dir"]

    ver = detect_wechat_file_version()
    keys_result: dict[str, Any] = {"ok": False, "keys": [], "message": "跳过扫 key"}
    keys: list[dict[str, str]] = []

    if keys_file:
        keys_path = Path(keys_file)
        if not keys_path.is_file():
            return {
                "ok": False,
                "message": (
                    f"密钥文件不存在：{keys_file}。"
                    "请填写已有密钥文件的完整路径（如 all_keys.json）；"
                    f"默认路径 {wechat_keys_path()} 只有扫 key / 导入成功后才会生成。"
                ),
                "accounts": accounts,
                "data_dir": data_dir,
                "decrypted_dir": str(wechat_decrypted_dir(Path(data_dir).name)),
                "keys_path": str(wechat_keys_path()),
                "groups": [],
                "keys_ok": False,
                "decrypt_ok": False,
                "version": ver,
                "ready": False,
            }
        try:
            keys = await asyncio.to_thread(load_keys_file, keys_file)
            if keys:
                save_keys_file(keys, data_dir=data_dir)
                keys_result = {
                    "ok": True,
                    "keys": keys,
                    "message": f"已从文件导入 {len(keys)} 组密钥",
                }
            else:
                keys_result = {
                    "ok": False,
                    "keys": [],
                    "message": f"密钥文件未解析到有效条目：{keys_file}",
                }
        except Exception as e:
            keys_result = {"ok": False, "keys": [], "message": f"读取密钥文件失败：{e}"}
    elif scan_keys:
        keys_result = await asyncio.to_thread(scan_keys_from_process)
        if keys_result.get("ok"):
            keys = list(keys_result.get("keys") or [])
            save_keys_file(keys, data_dir=data_dir)

    if not keys and wechat_keys_path().exists():
        try:
            keys = load_keys_file(wechat_keys_path())
            if keys and not keys_result.get("ok"):
                keys_result = {
                    "ok": True,
                    "keys": keys,
                    "message": f"使用已保存密钥 {len(keys)} 组",
                }
        except Exception:
            keys = []

    decrypt_result: dict[str, Any] = {"ok": False, "message": "无密钥，未解密"}
    if keys:
        decrypt_result = await asyncio.to_thread(decrypt_account_dbs, data_dir, keys)

    groups: list[dict[str, Any]] = []
    out_dir = decrypt_result.get("out_dir") or str(wechat_decrypted_dir(Path(data_dir).name))
    if Path(out_dir).is_dir() and any(Path(out_dir).rglob("*.db")):
        groups = await asyncio.to_thread(list_wechat_groups, out_dir)

    decrypt_ok = bool(decrypt_result.get("ok"))
    ok = decrypt_ok and bool(groups or Path(out_dir).is_dir())
    if not keys:
        ok = False
    msg_parts = [
        keys_result.get("message") or "",
        decrypt_result.get("message") or "",
    ]
    if groups:
        msg_parts.append(f"发现群 {len(groups)} 个")
    elif decrypt_ok:
        msg_parts.append("解密完成但未解析到群聊（联系人库可能为空）")
    elif wechat_memory_scan_likely_broken(ver):
        msg_parts.append("当前无法自动列群，请先导入密钥文件后再点「拉取微信群」")
    else:
        msg_parts.append("暂未解析到群（需先有密钥并完成解密）")

    return {
        "ok": ok,
        "message": "；".join([m for m in msg_parts if m]),
        "accounts": accounts,
        "data_dir": data_dir,
        "decrypted_dir": out_dir,
        "keys_path": str(wechat_keys_path()),
        "groups": groups,
        "keys_ok": bool(keys_result.get("ok") or keys),
        "decrypt_ok": decrypt_ok,
        "version": ver,
        "ready": decrypt_ok,
    }
