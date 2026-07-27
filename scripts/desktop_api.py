"""桌面端调用的 JSON API（stdout 输出一行 JSON）。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.llm.service import list_reports, run_group_summary, sqlite_path  # noqa: E402
from app.settings_store import (  # noqa: E402
    AppSettings,
    GroupConfig,
    dump_public,
    list_group_configs,
    load_app_settings,
    load_group_config,
    provider_by_id,
    save_app_settings,
    save_group_config,
)


def out(data) -> None:
    print(json.dumps(data, ensure_ascii=False))


def db_connect():
    path = sqlite_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def cmd_list_groups(sort: str, q: str) -> None:
    # 合并：已保存配置 + 消息库出现过的群 + OneBot 列表（可选，这里用库+配置）
    configs = {c.group_id: c for c in list_group_configs()}
    last_map: dict[str, dict] = {}
    path = sqlite_path()
    if path.exists():
        with db_connect() as conn:
            rows = conn.execute(
                """
                SELECT group_id,
                       MAX(event_time) AS last_time,
                       COUNT(*) AS msg_count
                FROM messages
                GROUP BY group_id
                """
            ).fetchall()
            for r in rows:
                last_map[str(r["group_id"])] = {
                    "last_time": r["last_time"],
                    "msg_count": r["msg_count"],
                }

    # 也读 onebot cache file if present from previous pull
    cache = ROOT / "data" / "groups_cache.json"
    names: dict[str, str] = {}
    members: dict[str, dict] = {}
    if cache.exists():
        try:
            cached = json.loads(cache.read_text(encoding="utf-8"))
            for g in cached.get("groups") or []:
                gid = str(g.get("group_id"))
                names[gid] = g.get("group_name") or ""
                members[gid] = {
                    "member_count": g.get("member_count"),
                    "max_member_count": g.get("max_member_count"),
                }
        except Exception:
            pass

    ids = set(configs) | set(last_map) | set(names)
    items = []
    for gid in ids:
        cfg = configs.get(gid) or GroupConfig(group_id=gid, group_name=names.get(gid, ""))
        meta = last_map.get(gid) or {}
        mem = members.get(gid) or {}
        items.append(
            {
                "groupId": gid,
                "groupName": cfg.group_name or names.get(gid) or "",
                "enabled": cfg.enabled,
                "blocked": cfg.blocked,
                "lastTime": meta.get("last_time"),
                "msgCount": meta.get("msg_count") or 0,
                "memberCount": mem.get("member_count"),
                "maxMemberCount": mem.get("max_member_count"),
                "keywordEnabled": cfg.keyword_monitor.enabled,
                "llmEnabled": cfg.llm_monitor.enabled,
            }
        )

    qq = (q or "").strip().lower()
    if qq:
        items = [
            x
            for x in items
            if qq in x["groupId"].lower() or qq in (x["groupName"] or "").lower()
        ]

    if sort == "alpha":
        items.sort(key=lambda x: ((x["groupName"] or "").lower(), x["groupId"]))
    else:  # recent
        items.sort(
            key=lambda x: (x["lastTime"] is not None, x["lastTime"] or 0, x["msgCount"]),
            reverse=True,
        )
    out({"groups": items})


def _group_name_map() -> dict[str, str]:
    names: dict[str, str] = {}
    for c in list_group_configs():
        if c.group_name:
            names[str(c.group_id)] = c.group_name
    cache = ROOT / "data" / "groups_cache.json"
    if cache.exists():
        try:
            cached = json.loads(cache.read_text(encoding="utf-8"))
            for g in cached.get("groups") or []:
                gid = str(g.get("group_id"))
                if gid and not names.get(gid):
                    names[gid] = g.get("group_name") or ""
        except Exception:
            pass
    return names


def cmd_recent_messages(group_id: str | None, limit: int) -> None:
    path = sqlite_path()
    if not path.exists():
        out([])
        return
    lim = max(1, min(limit, 200))
    names = _group_name_map()
    with db_connect() as conn:
        if group_id:
            rows = conn.execute(
                """
                SELECT id, group_id, COALESCE(user_id,'') AS user_id,
                       COALESCE(sender_name,'') AS sender_name,
                       COALESCE(content,'') AS content,
                       event_time, COALESCE(created_at,'') AS created_at
                FROM messages WHERE group_id=?
                ORDER BY id DESC LIMIT ?
                """,
                (str(group_id), lim),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT id, group_id, COALESCE(user_id,'') AS user_id,
                       COALESCE(sender_name,'') AS sender_name,
                       COALESCE(content,'') AS content,
                       event_time, COALESCE(created_at,'') AS created_at
                FROM messages
                ORDER BY id DESC LIMIT ?
                """,
                (lim,),
            ).fetchall()
    out(
        [
            {
                "id": r["id"],
                "groupId": r["group_id"],
                "groupName": names.get(str(r["group_id"]), ""),
                "userId": r["user_id"],
                "senderName": r["sender_name"],
                "content": r["content"],
                "eventTime": r["event_time"],
                "createdAt": r["created_at"],
            }
            for r in rows
        ]
    )


def cmd_get_settings() -> None:
    out(dump_public(load_app_settings()))


def cmd_save_settings(raw: str) -> None:
    data = json.loads(raw)
    # 兼容 camelCase
    mapped = {
        "onebot_ws_url": data.get("onebotWsUrl") or data.get("onebot_ws_url"),
        "onebot_access_token": data.get("onebotAccessToken") or data.get("onebot_access_token"),
        "llm": data.get("llm"),
        "ui": data.get("ui") or {},
    }
    # llm providers camelCase normalize
    llm = mapped.get("llm") or {}
    if "activeProviderId" in llm:
        llm["active_provider_id"] = llm.pop("activeProviderId")
    providers = []
    for p in llm.get("providers") or []:
        providers.append(
            {
                "id": p.get("id"),
                "name": p.get("name"),
                "type": p.get("type"),
                "base_url": p.get("baseUrl") or p.get("base_url") or "",
                "api_key": p.get("apiKey") or p.get("api_key") or "",
                "default_model": p.get("defaultModel") or p.get("default_model") or "",
            }
        )
    llm["providers"] = providers
    mapped["llm"] = llm

    ui = mapped.get("ui") or {}
    if isinstance(ui, dict):
        mapped["ui"] = {
            "compact_mode_enabled": bool(
                ui.get("compactModeEnabled")
                if "compactModeEnabled" in ui
                else ui.get("compact_mode_enabled", False)
            ),
            "theme": (ui.get("theme") or "midnight").strip() or "midnight",
        }

    settings = AppSettings.model_validate(mapped)
    save_app_settings(settings)
    out({"ok": True})


def cmd_get_group(group_id: str) -> None:
    cfg = load_group_config(group_id)
    out(dump_public(cfg))


def cmd_save_group(raw: str) -> None:
    data = json.loads(raw)

    def pick(d, *keys, default=None):
        for k in keys:
            if k in d and d[k] is not None:
                return d[k]
        return default

    basic = data.get("basic") or {}
    kw = data.get("keywordMonitor") or data.get("keyword_monitor") or {}
    llm = data.get("llmMonitor") or data.get("llm_monitor") or {}
    cfg = GroupConfig.model_validate(
        {
            "group_id": pick(data, "groupId", "group_id"),
            "group_name": pick(data, "groupName", "group_name", default="") or "",
            "enabled": pick(data, "enabled", default=False),
            "blocked": pick(data, "blocked", default=False),
            "basic": {
                "log_all": pick(basic, "logAll", "log_all", default=True),
                "storage_enabled": pick(basic, "storageEnabled", "storage_enabled", default=True),
            },
            "keyword_monitor": {
                "enabled": pick(kw, "enabled", default=True),
                "keywords": pick(kw, "keywords", default=[]) or [],
                "alert_enabled": pick(kw, "alertEnabled", "alert_enabled", default=False),
                "webhook_url": pick(kw, "webhookUrl", "webhook_url", default="") or "",
            },
            "llm_monitor": {
                "enabled": pick(llm, "enabled", default=False),
                "provider_id": pick(llm, "providerId", "provider_id", default="") or "",
                "model": pick(llm, "model", default="") or "",
                "prompt": pick(llm, "prompt", default="") or "",
                "every_minutes": pick(llm, "everyMinutes", "every_minutes", default=60),
                "window_minutes": pick(llm, "windowMinutes", "window_minutes", default=60),
                "min_messages": pick(llm, "minMessages", "min_messages", default=8),
            },
        }
    )
    # 屏蔽与启用互斥
    if cfg.blocked:
        cfg.enabled = False
    elif cfg.enabled:
        cfg.blocked = False
    if isinstance(cfg.keyword_monitor.keywords, str):
        cfg.keyword_monitor.keywords = [
            x.strip() for x in cfg.keyword_monitor.keywords.split(",") if x.strip()
        ]
    save_group_config(cfg)
    out({"ok": True})


def cmd_run_llm(group_id: str) -> None:
    cfg = load_group_config(group_id)
    if cfg.blocked:
        raise SystemExit("该群已屏蔽，无法执行 LLM 分析")
    result = asyncio.run(run_group_summary(group_id, job_type="manual"))
    out(result)


def cmd_pull_history(group_id: str, count: int) -> None:
    from app.history_sync import pull_group_history

    cfg = load_group_config(group_id)
    if cfg.blocked:
        raise SystemExit("该群已屏蔽，无法拉取历史")
    result = asyncio.run(pull_group_history(group_id, count=count))
    out(result)


def cmd_list_reports(group_id: str | None, limit: int) -> None:
    rows = list_reports(group_id, limit)
    out(
        [
            {
                "id": r["id"],
                "groupId": r["group_id"],
                "windowStart": r["window_start"],
                "windowEnd": r["window_end"],
                "headline": r["headline"],
                "sentiment": r["sentiment"],
                "riskMax": r["risk_max"],
                "msgCount": r["msg_count"],
                "createdAt": r["created_at"],
                "reportMd": r["report_md"],
            }
            for r in rows
        ]
    )


def cmd_cache_groups(raw: str) -> None:
    path = ROOT / "data" / "groups_cache.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(raw, encoding="utf-8")
    # 同步群名到已有配置
    data = json.loads(raw)
    for g in data.get("groups") or []:
        gid = str(g.get("group_id"))
        name = g.get("group_name") or ""
        cfg = load_group_config(gid)
        if name and cfg.group_name != name:
            cfg.group_name = name
            save_group_config(cfg)
    out({"ok": True, "count": len(data.get("groups") or [])})


def cmd_fetch_models(provider_id: str) -> None:
    settings = load_app_settings()
    provider = None
    for p in settings.llm.providers:
        if p.id == provider_id:
            provider = p
            break
    if provider is None:
        provider = provider_by_id(settings, settings.llm.active_provider_id)
    if provider is None:
        out({"models": [], "error": "未找到 provider"})
        return

    ptype = (provider.type or "openai_compatible").lower()
    if ptype == "cursor":
        out(
            {
                "models": [
                    {"id": "composer-2.5"},
                    {"id": "composer-2"},
                    {"id": "gpt-5.4"},
                    {"id": "claude-4.6-sonnet"},
                ]
            }
        )
        return

    if ptype == "opencode":
        # OpenCode 模型列表因版本差异较大，给常用占位 + 尝试 HTTP
        models = [{"id": provider.default_model or "default"}]
        try:
            import urllib.request

            base = (provider.base_url or "http://127.0.0.1:4096").rstrip("/")
            req = urllib.request.Request(f"{base}/provider", method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                raw = json.loads(resp.read().decode("utf-8"))
            # 尽力解析
            found = []
            if isinstance(raw, dict):
                for key in ("providers", "data", "models"):
                    arr = raw.get(key)
                    if isinstance(arr, list):
                        for item in arr:
                            if isinstance(item, dict):
                                mid = item.get("id") or item.get("model") or item.get("name")
                                if mid:
                                    found.append({"id": str(mid)})
            if found:
                models = found
        except Exception:
            pass
        out({"models": models})
        return

    # openai compatible
    try:
        import urllib.error
        import urllib.request

        from app.llm.client import openai_models_endpoints

        last_err = ""
        models: list[dict[str, str]] = []
        for url in openai_models_endpoints(provider.base_url):
            try:
                req = urllib.request.Request(url, method="GET")
                if provider.api_key:
                    req.add_header("Authorization", f"Bearer {provider.api_key}")
                with urllib.request.urlopen(req, timeout=20) as resp:
                    body = resp.read().decode("utf-8", errors="replace").strip()
                if not body:
                    last_err = f"{url} 返回空响应"
                    continue
                if body[:1] not in "{[":
                    last_err = f"{url} 返回非 JSON（可能是网页，请确认 Base URL 含 /v1）"
                    continue
                raw = json.loads(body)
                data = raw.get("data") if isinstance(raw, dict) else raw
                parsed: list[dict[str, str]] = []
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict) and item.get("id"):
                            parsed.append({"id": str(item["id"])})
                if parsed:
                    parsed.sort(key=lambda x: x["id"].lower())
                    out({"models": parsed, "endpoint": url})
                    return
                last_err = f"{url} 未解析到模型列表"
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", errors="replace")[:200]
                last_err = f"HTTP {e.code} @ {url}: {detail or e.reason}"
            except Exception as e:
                last_err = f"{url}: {e}"
        out({"models": models, "error": last_err or "拉取模型失败"})
    except Exception as e:
        out({"models": [], "error": str(e)})


def cmd_test_provider(provider_id: str, model: str) -> None:
    settings = load_app_settings()
    provider = None
    for p in settings.llm.providers:
        if provider_id and p.id == provider_id:
            provider = p
            break
    if provider is None:
        provider = provider_by_id(settings, settings.llm.active_provider_id)
    if provider is None:
        out({"ok": False, "message": "未找到 provider，请先保存总配置"})
        return
    from app.llm.client import test_provider_connection

    result = asyncio.run(test_provider_connection(provider, model=model or provider.default_model))
    out(result)


def cmd_test_onebot() -> None:
    settings = load_app_settings()
    ws = settings.onebot_ws_url or "ws://127.0.0.1:3001"
    token = settings.onebot_access_token or ""
    # 粗解析 host/port
    host, port = "127.0.0.1", 3001
    try:
        from urllib.parse import urlparse

        u = urlparse(ws)
        host = u.hostname or host
        port = u.port or (443 if u.scheme == "wss" else 3001)
    except Exception:
        pass
    import socket

    started = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=3):
            pass
    except Exception as e:
        out(
            {
                "ok": False,
                "latencyMs": int((time.perf_counter() - started) * 1000),
                "message": f"无法连接 {host}:{port} — {e}",
            }
        )
        return

    # 进一步用 list_groups 轻量验证 OneBot 协议
    try:
        import subprocess

        env = os.environ.copy()
        env["ONEBOT_WS_URL"] = ws
        env["ONEBOT_ACCESS_TOKEN"] = token
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        proc = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "list_groups.py"), "--json"],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
            encoding="utf-8",
            errors="replace",
        )
        if proc.returncode != 0:
            out(
                {
                    "ok": False,
                    "latencyMs": int((time.perf_counter() - started) * 1000),
                    "message": f"端口通，但 OneBot 协议失败: {(proc.stderr or proc.stdout)[:240]}",
                }
            )
            return
        line = next((l for l in reversed(proc.stdout.splitlines()) if l.strip().startswith("{")), "")
        data = json.loads(line) if line else {}
        groups = data.get("groups") or []
        login = data.get("login") or {}
        nick = login.get("nickname") or login.get("user_id") or "-"
        out(
            {
                "ok": True,
                "latencyMs": int((time.perf_counter() - started) * 1000),
                "message": f"OneBot 连通正常，登录 {nick}，群 {len(groups)} 个",
            }
        )
    except Exception as e:
        out(
            {
                "ok": True,
                "latencyMs": int((time.perf_counter() - started) * 1000),
                "message": f"端口 {host}:{port} 可达（协议探测跳过: {e}）",
            }
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("list-groups")
    p1.add_argument("--sort", default="recent", choices=["recent", "alpha"])
    p1.add_argument("--q", default="")

    p2 = sub.add_parser("recent-messages")
    p2.add_argument("--group-id", default="")
    p2.add_argument("--limit", type=int, default=50)

    sub.add_parser("get-settings")
    p3 = sub.add_parser("save-settings")
    p3.add_argument("--json", required=True)

    p4 = sub.add_parser("get-group")
    p4.add_argument("--group-id", required=True)

    p5 = sub.add_parser("save-group")
    p5.add_argument("--json", required=True)

    p6 = sub.add_parser("run-llm")
    p6.add_argument("--group-id", required=True)

    p_hist = sub.add_parser("pull-history")
    p_hist.add_argument("--group-id", required=True)
    p_hist.add_argument("--count", type=int, default=100)

    p7 = sub.add_parser("list-reports")
    p7.add_argument("--group-id", default="")
    p7.add_argument("--limit", type=int, default=20)

    p8 = sub.add_parser("cache-groups")
    p8.add_argument("--json", required=True)

    p9 = sub.add_parser("fetch-models")
    p9.add_argument("--provider-id", default="")

    p10 = sub.add_parser("test-provider")
    p10.add_argument("--provider-id", default="")
    p10.add_argument("--model", default="")

    sub.add_parser("test-onebot")

    args = parser.parse_args()
    if args.cmd == "list-groups":
        cmd_list_groups(args.sort, args.q)
    elif args.cmd == "recent-messages":
        cmd_recent_messages(args.group_id or None, args.limit)
    elif args.cmd == "get-settings":
        cmd_get_settings()
    elif args.cmd == "save-settings":
        cmd_save_settings(args.json)
    elif args.cmd == "get-group":
        cmd_get_group(args.group_id)
    elif args.cmd == "save-group":
        cmd_save_group(args.json)
    elif args.cmd == "run-llm":
        cmd_run_llm(args.group_id)
    elif args.cmd == "pull-history":
        cmd_pull_history(args.group_id, args.count)
    elif args.cmd == "list-reports":
        cmd_list_reports(args.group_id or None, args.limit)
    elif args.cmd == "cache-groups":
        cmd_cache_groups(args.json)
    elif args.cmd == "fetch-models":
        cmd_fetch_models(args.provider_id)
    elif args.cmd == "test-provider":
        cmd_test_provider(args.provider_id, args.model)
    elif args.cmd == "test-onebot":
        cmd_test_onebot()


if __name__ == "__main__":
    main()
