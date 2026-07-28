"""LLM 群聊总结服务。"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from app.llm.client import TokenUsage, chat_complete, describe_image, extract_json_object
from app.media_store import (
    extract_image_refs,
    materialize_content_images,
    media_abs_path,
    read_local_image_b64,
)
from app.settings_store import (
    ROOT_DIR,
    GroupConfig,
    load_app_settings,
    load_group_config,
    provider_by_id,
)

logger = logging.getLogger(__name__)

DEFAULT_SYSTEM = (
    "你是群聊监控分析助手。只基于给定记录分析，禁止编造。"
    "必须输出合法 JSON 对象，字段含：headline, topics, key_points, risks, "
    "action_items, sentiment, notable_users, appendix, context_usage。"
    "risks 项含 level/type/detail/evidence；evidence 必须是原文摘录。"
    "notable_users 项含 user_id/name/role/summary。"
    "appendix 为附录对象，含：nouns（[{term, meaning}] 名词/黑话解析）、"
    "links（[{url, summary}] 链接详解）、notes（[string] 补充说明）。"
    "若某类附录无内容，用空数组。"
    "context_usage 必填："
    "{used_earlier_context:bool, earlier_rounds:int, earlier_messages:int, summary:string}；"
    "若分析使用了配置时间窗之前补入的消息/引用原文，used_earlier_context 必须为 true，"
    "并在 summary 中简明说明引用了哪些更早内容。"
    "记录可能含「补前文/补后文/引用补全」标记，请把它们当作同一段多轮对话理解。"
    "消息中可能含「[图片描述: …]」，这是对聊天图片的视觉识别结果，必须纳入主题与风险分析。"
)

CONTEXT_CHECK_SYSTEM = (
    "你是群聊上下文完整性审查助手。"
    "判断当前片段是否完整，是否还需要更早的前文才能理解（含被引用但未出现正文的消息）。"
    "必须输出合法 JSON："
    '{"enough":bool,"reason":string,"need_earlier":bool,'
    '"need_reply_ids":string[],"suggested_earlier_count":int}。'
    "enough=true 表示可以开始正式分析；need_earlier=true 表示应再向前取聊天记录；"
    "need_reply_ids 只填记录中已出现、但仍缺正文的引用 id；"
    "suggested_earlier_count 建议再取多少条前文（10~40）。"
    "禁止编造 id 或臆测窗外具体内容。"
)

# LLM 驱动向前补前文：最多 5 轮，总时间跨度不超过配置窗口的 5 倍
MAX_LLM_CONTEXT_ROUNDS = 5
MAX_WINDOW_MULTIPLIER = 5


def sqlite_path() -> Path:
    settings = load_app_settings()
    # 兼容 .env STORAGE_SQLITE_PATH
    env = ROOT_DIR / ".env"
    path = ROOT_DIR / "data" / "messages.db"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("STORAGE_SQLITE_PATH="):
                raw = line.split("=", 1)[1].strip().strip('"')
                p = Path(raw)
                path = p if p.is_absolute() else ROOT_DIR / p
    _ = settings
    return path


def ensure_llm_tables(db: Path) -> None:
    db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS llm_jobs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              job_type TEXT NOT NULL,
              group_id TEXT NOT NULL,
              window_start INTEGER NOT NULL,
              window_end INTEGER NOT NULL,
              status TEXT NOT NULL,
              error TEXT,
              model TEXT,
              created_at TEXT DEFAULT (datetime('now','localtime')),
              finished_at TEXT
            );
            CREATE TABLE IF NOT EXISTS llm_reports (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              job_id INTEGER,
              group_id TEXT NOT NULL,
              window_start INTEGER NOT NULL,
              window_end INTEGER NOT NULL,
              headline TEXT,
              sentiment TEXT,
              report_json TEXT NOT NULL,
              report_md TEXT,
              risk_max TEXT,
              msg_count INTEGER,
              created_at TEXT DEFAULT (datetime('now','localtime'))
            );
            CREATE INDEX IF NOT EXISTS idx_llm_reports_group_time
            ON llm_reports(group_id, window_start DESC);
            """
        )
        _ensure_report_token_columns(conn)


def _ensure_report_token_columns(conn: sqlite3.Connection) -> None:
    cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(llm_reports)").fetchall()}
    for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
        if name not in cols:
            conn.execute(
                f"ALTER TABLE llm_reports ADD COLUMN {name} INTEGER DEFAULT 0"
            )
    # 旧报告：从 report_json.token_usage 回填列，便于总统计
    rows = conn.execute(
        """
        SELECT id, report_json FROM llm_reports
        WHERE COALESCE(total_tokens, 0) = 0
          AND report_json LIKE '%token_usage%'
        """
    ).fetchall()
    for rid, raw in rows:
        try:
            payload = json.loads(raw or "")
            if not isinstance(payload, dict):
                continue
            tu = payload.get("token_usage")
            if not isinstance(tu, dict):
                continue
            prompt = int(tu.get("prompt_tokens") or 0)
            completion = int(tu.get("completion_tokens") or 0)
            total = int(tu.get("total_tokens") or 0)
            if total <= 0:
                total = prompt + completion
            if total <= 0:
                continue
            conn.execute(
                """
                UPDATE llm_reports
                SET prompt_tokens=?, completion_tokens=?, total_tokens=?
                WHERE id=?
                """,
                (prompt, completion, total, rid),
            )
        except Exception:
            continue


def token_usage_from_payload(payload: dict[str, Any] | None) -> dict[str, int]:
    if not isinstance(payload, dict):
        return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    tu = payload.get("token_usage")
    if not isinstance(tu, dict):
        return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    prompt = int(tu.get("prompt_tokens") or 0)
    completion = int(tu.get("completion_tokens") or 0)
    total = int(tu.get("total_tokens") or 0)
    if total <= 0:
        total = prompt + completion
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
    }


def sum_report_tokens(group_id: str | None = None) -> dict[str, int]:
    """汇总已落库报告的 token 消耗。"""
    db = sqlite_path()
    ensure_llm_tables(db)
    with sqlite3.connect(db) as conn:
        if group_id:
            row = conn.execute(
                """
                SELECT
                  COALESCE(SUM(prompt_tokens), 0),
                  COALESCE(SUM(completion_tokens), 0),
                  COALESCE(SUM(total_tokens), 0),
                  COUNT(*)
                FROM llm_reports
                WHERE group_id=?
                """,
                (str(group_id),),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT
                  COALESCE(SUM(prompt_tokens), 0),
                  COALESCE(SUM(completion_tokens), 0),
                  COALESCE(SUM(total_tokens), 0),
                  COUNT(*)
                FROM llm_reports
                """
            ).fetchone()
    return {
        "prompt_tokens": int(row[0] or 0),
        "completion_tokens": int(row[1] or 0),
        "total_tokens": int(row[2] or 0),
        "report_count": int(row[3] or 0),
    }


def fetch_messages_before(
    group_id: str,
    before_ts: int,
    *,
    limit: int = 40,
) -> list[dict[str, Any]]:
    """取某个时间点之前的消息（按时间正序返回）。"""
    db = sqlite_path()
    if not db.exists() or before_ts <= 0 or limit <= 0:
        return []
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT group_id, user_id, sender_name, content, event_time, message_id
            FROM messages
            WHERE group_id = ?
              AND COALESCE(event_time, 0) > 0
              AND COALESCE(event_time, 0) < ?
            ORDER BY COALESCE(event_time, 0) DESC, id DESC
            LIMIT ?
            """,
            (str(group_id), int(before_ts), int(limit)),
        ).fetchall()
    return list(reversed([dict(r) for r in rows]))


def fetch_messages_after(
    group_id: str,
    after_ts: int,
    *,
    limit: int = 40,
) -> list[dict[str, Any]]:
    """取某个时间点之后的消息（按时间正序）。"""
    db = sqlite_path()
    if not db.exists() or after_ts < 0 or limit <= 0:
        return []
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT group_id, user_id, sender_name, content, event_time, message_id
            FROM messages
            WHERE group_id = ?
              AND COALESCE(event_time, 0) > ?
            ORDER BY COALESCE(event_time, 0) ASC, id ASC
            LIMIT ?
            """,
            (str(group_id), int(after_ts), int(limit)),
        ).fetchall()
    return [dict(r) for r in rows]


def fetch_messages_by_ids(group_id: str, message_ids: list[str]) -> list[dict[str, Any]]:
    ids = [str(x).strip() for x in message_ids if str(x).strip()]
    if not ids:
        return []
    db = sqlite_path()
    if not db.exists():
        return []
    uniq = list(dict.fromkeys(ids))
    placeholders = ",".join("?" for _ in uniq)
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            SELECT group_id, user_id, sender_name, content, event_time, message_id
            FROM messages
            WHERE group_id = ?
              AND CAST(message_id AS TEXT) IN ({placeholders})
            ORDER BY COALESCE(event_time, 0) ASC
            """,
            (str(group_id), *uniq),
        ).fetchall()
    return [dict(r) for r in rows]


import re as _re

_REPLY_ID_RE = re.compile(
    r"\[CQ:reply[^\]]*[, ]id=(\d+)[^\]]*\]|CQ:reply[^\]]*id=(\d+)",
    re.IGNORECASE,
)


def extract_reply_ids(text: str) -> list[str]:
    if not text:
        return []
    out: list[str] = []
    for m in _REPLY_ID_RE.finditer(text):
        rid = m.group(1) or m.group(2)
        if rid:
            out.append(str(rid))
    return out


def _row_key(row: dict[str, Any]) -> str:
    mid = row.get("message_id")
    if mid is not None and str(mid).strip():
        return f"id:{mid}"
    return f"t:{_msg_ts(row)}:{row.get('sender_name')}:{row.get('content')}"


def _merge_rows(*batches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    merged: list[dict[str, Any]] = []
    for batch in batches:
        for r in batch:
            key = _row_key(r)
            if key in seen:
                continue
            seen.add(key)
            merged.append(dict(r))
    merged.sort(key=lambda r: (_msg_ts(r), str(r.get("message_id") or "")))
    return merged


def _mark_context_role(row: dict[str, Any], role: str) -> dict[str, Any]:
    out = dict(row)
    prev = out.get("_ctx_role")
    if prev and prev != role and role == "window":
        return out
    out["_ctx_role"] = role
    return out


def _is_sparse_or_reply_heavy(rows: list[dict[str, Any]], *, sparse_threshold: int = 5) -> bool:
    if not rows:
        return True
    if len(rows) < sparse_threshold:
        return True
    reply_n = sum(1 for r in rows if extract_reply_ids(_msg_text(r)))
    if reply_n >= 1 and len(rows) <= 8:
        return True
    if reply_n >= max(2, len(rows) // 2):
        return True
    return False


_CONTINUATION_HINTS = (
    "对",
    "是的",
    "是啊",
    "嗯",
    "哦",
    "啊",
    "然后",
    "所以",
    "但是",
    "不过",
    "而且",
    "另外",
    "还有",
    "这个",
    "那个",
    "上面",
    "刚才",
    "之前",
    "继续",
    "同上",
    "同意",
    "收到",
    "好的",
    "确实",
    "就是",
    "啥意思",
    "什么意思",
    "为啥",
    "为什么",
    "怎么说",
    "你说的",
    "他说的",
    "前面说",
    "刚说",
    "+1",
    "＋1",
)


def _msg_ts(row: dict[str, Any]) -> int:
    try:
        return int(row.get("event_time") or 0)
    except Exception:
        return 0


def _msg_text(row: dict[str, Any]) -> str:
    return (row.get("content") or "").strip()


def _looks_like_continuation(row: dict[str, Any]) -> bool:
    text = _msg_text(row)
    if not text:
        return False
    if "[CQ:reply" in text or "CQ:reply" in text:
        return True
    # 很短的接话
    if len(text) <= 12:
        return True
    low = text.lstrip("，,。.!！?？~～… ")
    for hint in _CONTINUATION_HINTS:
        if low.startswith(hint):
            return True
    # 以问句/接话语气开头且较短
    if len(text) <= 40 and (text.endswith("？") or text.endswith("?") or text.startswith("那")):
        return True
    return False


def _has_internal_topic_break(rows: list[dict[str, Any]], *, gap_sec: int = 25 * 60) -> bool:
    """窗内相邻消息间隔过大，后半段可能缺前文。"""
    if len(rows) < 4:
        return False
    # 只检查前半段是否“突然接话”
    check_n = min(8, len(rows))
    for i in range(1, check_n):
        prev_t = _msg_ts(rows[i - 1])
        cur_t = _msg_ts(rows[i])
        if prev_t and cur_t and cur_t - prev_t >= gap_sec and _looks_like_continuation(rows[i]):
            return True
    return False


def _should_look_back(
    group_id: str,
    rows: list[dict[str, Any]],
    window_start: int,
    *,
    continuity_gap_sec: int = 20 * 60,
) -> tuple[bool, str]:
    if not rows:
        return False, ""

    first = rows[0]
    first_ts = _msg_ts(first) or window_start

    # 窗起点附近是否还有更早的连续对话
    prev_batch = fetch_messages_before(group_id, first_ts, limit=1)
    if prev_batch:
        prev_ts = _msg_ts(prev_batch[-1])
        gap = first_ts - prev_ts if prev_ts else 10**9
        if gap <= continuity_gap_sec:
            if _looks_like_continuation(first):
                return True, "开头像接话，且与前一条间隔较短"
            # 前几条都偏短/接话，说明话题在窗外已开始
            head = rows[: min(3, len(rows))]
            cont_n = sum(1 for r in head if _looks_like_continuation(r))
            if cont_n >= 2 or (cont_n >= 1 and gap <= 5 * 60):
                return True, "窗口切开了正在进行的对话"

    if _looks_like_continuation(first) and prev_batch:
        return True, "首条消息依赖前文"

    if _has_internal_topic_break(rows):
        return True, "窗内存在长时间间隔后的接话，需补前文"

    return False, ""


def extend_messages_with_context(
    group_id: str,
    rows: list[dict[str, Any]],
    *,
    window_start: int,
    window_end: int,
    configured_start: int,
    max_rounds: int = 3,
    batch_size: int = 40,
    max_extra_messages: int = 120,
    max_extra_minutes: int | None = None,
) -> tuple[list[dict[str, Any]], int, int, dict[str, Any]]:
    """
    若当前窗口内容像「切开的对话 / 前言不搭后语」，向前回溯补消息。
    返回 (rows, start, end, meta)。
    """
    meta: dict[str, Any] = {
        "window_extended": False,
        "configured_start": int(configured_start),
        "configured_end": int(window_end),
        "lookback_messages": 0,
        "lookback_rounds": 0,
        "lookback_reasons": [],
    }
    if not rows:
        return rows, window_start, window_end, meta

    start = int(window_start)
    end = int(window_end)
    extra_total = 0
    min_start = start
    if max_extra_minutes is not None and max_extra_minutes > 0:
        min_start = max(0, int(configured_start) - int(max_extra_minutes) * 60)

    seen_ids: set[str] = set()
    for r in rows:
        mid = r.get("message_id")
        if mid is not None:
            seen_ids.add(str(mid))

    for _ in range(max(1, max_rounds)):
        need, reason = _should_look_back(group_id, rows, start)
        if not need:
            break
        if extra_total >= max_extra_messages:
            break
        remain = max_extra_messages - extra_total
        take = min(batch_size, remain)
        first_ts = _msg_ts(rows[0]) or start
        batch = fetch_messages_before(group_id, first_ts, limit=take)
        if not batch:
            break
        # 过滤边界
        filtered: list[dict[str, Any]] = []
        for r in batch:
            ts = _msg_ts(r)
            if ts and ts < min_start:
                continue
            mid = r.get("message_id")
            key = str(mid) if mid is not None else f"{ts}:{r.get('sender_name')}:{r.get('content')}"
            if key in seen_ids:
                continue
            seen_ids.add(key)
            filtered.append(r)
        if not filtered:
            break
        rows = filtered + rows
        start = _msg_ts(rows[0]) or start
        extra_total += len(filtered)
        meta["lookback_rounds"] += 1
        meta["lookback_reasons"].append(reason)
        meta["window_extended"] = True

    meta["lookback_messages"] = extra_total
    meta["actual_start"] = start
    meta["actual_end"] = end
    return rows, start, end, meta


def enrich_with_surrounding_and_replies(
    group_id: str,
    rows: list[dict[str, Any]],
    *,
    window_start: int,
    window_end: int,
    configured_start: int,
    configured_end: int,
    before_limit: int = 25,
    after_limit: int = 15,
    max_reply_hops: int = 2,
) -> tuple[list[dict[str, Any]], int, int, dict[str, Any]]:
    """
    记录不足 / 含引用时：补前后文，并尽量把被引用消息（即使在窗外）并入。
    """
    meta: dict[str, Any] = {
        "sparse_enriched": False,
        "surround_before": 0,
        "surround_after": 0,
        "reply_filled": 0,
        "reasons": [],
    }
    if not rows:
        return rows, window_start, window_end, meta

    core = [_mark_context_role(r, "window") for r in rows]
    start = int(window_start)
    end = int(window_end)
    first_ts = _msg_ts(core[0]) or start
    last_ts = _msg_ts(core[-1]) or end

    need = _is_sparse_or_reply_heavy(core) or any(
        _looks_like_continuation(r) for r in core[: min(3, len(core))]
    )
    if not need:
        # 仍尝试解析引用
        reply_ids = []
        for r in core:
            reply_ids.extend(extract_reply_ids(_msg_text(r)))
        if not reply_ids:
            return core, start, end, meta
        meta["reasons"].append("含引用回复，补被引原文")
    else:
        meta["reasons"].append("记录偏少或依赖接话/引用，扩展前后文")

    before: list[dict[str, Any]] = []
    after: list[dict[str, Any]] = []
    if need:
        before = [
            _mark_context_role(r, "before")
            for r in fetch_messages_before(group_id, first_ts, limit=before_limit)
        ]
        after = [
            _mark_context_role(r, "after")
            for r in fetch_messages_after(group_id, last_ts, limit=after_limit)
        ]
        meta["surround_before"] = len(before)
        meta["surround_after"] = len(after)

    merged = _merge_rows(before, core, after)

    # 递归补引用目标
    known_ids = {
        str(r.get("message_id"))
        for r in merged
        if r.get("message_id") is not None and str(r.get("message_id")).strip()
    }
    pending: list[str] = []
    for r in merged:
        pending.extend(extract_reply_ids(_msg_text(r)))
    pending = [x for x in dict.fromkeys(pending) if x not in known_ids]
    filled = 0
    for _ in range(max(1, max_reply_hops)):
        if not pending:
            break
        found = fetch_messages_by_ids(group_id, pending)
        if not found:
            break
        tagged = [_mark_context_role(r, "reply") for r in found]
        filled += len(tagged)
        merged = _merge_rows(merged, tagged)
        known_ids = {
            str(r.get("message_id"))
            for r in merged
            if r.get("message_id") is not None and str(r.get("message_id")).strip()
        }
        nxt: list[str] = []
        for r in tagged:
            nxt.extend(extract_reply_ids(_msg_text(r)))
        pending = [x for x in dict.fromkeys(nxt) if x not in known_ids]

    meta["reply_filled"] = filled
    if filled:
        meta["reasons"].append(f"补齐被引用消息 {filled} 条")

    if len(merged) > len(core) or filled:
        meta["sparse_enriched"] = True
        start = _msg_ts(merged[0]) or start
        end = _msg_ts(merged[-1]) or end
        # 保证覆盖配置窗
        start = min(start, int(configured_start) or start)
        end = max(end, int(configured_end) or end)

    meta["actual_start"] = start
    meta["actual_end"] = end
    return merged, start, end, meta


def fetch_messages(group_id: str, start_ts: int, end_ts: int, limit: int = 800) -> list[dict[str, Any]]:
    db = sqlite_path()
    if not db.exists():
        return []
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT group_id, user_id, sender_name, content, event_time, message_id
            FROM messages
            WHERE group_id = ?
              AND COALESCE(event_time, 0) >= ?
              AND COALESCE(event_time, 0) <= ?
            ORDER BY event_time ASC
            LIMIT ?
            """,
            (str(group_id), start_ts, end_ts, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def fetch_recent_messages(group_id: str, limit: int = 80) -> list[dict[str, Any]]:
    db = sqlite_path()
    if not db.exists():
        return []
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT group_id, user_id, sender_name, content, event_time, message_id
            FROM messages
            WHERE group_id = ?
            ORDER BY COALESCE(event_time, 0) DESC, id DESC
            LIMIT ?
            """,
            (str(group_id), limit),
        ).fetchall()
    # 按时间正序给模型
    ordered = list(reversed([dict(r) for r in rows]))
    return ordered


def format_transcript(rows: list[dict[str, Any]], max_chars: int = 24000) -> str:
    lines: list[str] = []
    for r in rows:
        ts = r.get("event_time")
        if ts:
            try:
                clock = datetime.fromtimestamp(int(ts)).strftime("%H:%M")
            except Exception:
                clock = "--:--"
        else:
            clock = "--:--"
        name = r.get("sender_name") or r.get("user_id") or "?"
        content = (r.get("content") or "").strip() or "[空消息]"
        role = r.get("_ctx_role") or "window"
        tag = {
            "before": "补前文",
            "after": "补后文",
            "reply": "引用补全",
            "window": "窗内",
        }.get(str(role), str(role))
        mid = r.get("message_id")
        mid_s = f" id={mid}" if mid is not None and str(mid).strip() else ""
        reply_ids = extract_reply_ids(content)
        reply_s = f" 引用→{','.join(reply_ids)}" if reply_ids else ""
        lines.append(f"[{clock}][{tag}{mid_s}{reply_s}] {name}: {content}")
    text = "\n".join(lines)
    if len(text) <= max_chars:
        return text
    head = text[: max_chars // 2]
    tail = text[-(max_chars // 2) :]
    return head + "\n\n...[中间已截断]...\n\n" + tail


def _update_message_content(group_id: str, message_id: str, content: str) -> None:
    if not message_id:
        return
    db = sqlite_path()
    if not db.exists():
        return
    try:
        with sqlite3.connect(db) as conn:
            conn.execute(
                """
                UPDATE messages SET content=?
                WHERE group_id=? AND message_id=?
                """,
                (content, str(group_id), str(message_id)),
            )
    except sqlite3.Error:
        logger.exception("回写本地化图片 content 失败 group=%s mid=%s", group_id, message_id)


async def ensure_rows_images_local(rows: list[dict[str, Any]], group_id: str) -> int:
    """分析前尽量把远程图补下到本地，并回写 content。返回成功改写条数。"""
    changed = 0
    for r in rows:
        content = (r.get("content") or "").strip()
        if not content or "[CQ:image" not in content.lower():
            continue
        try:
            new_content = await materialize_content_images(content, group_id=group_id)
        except Exception:
            logger.exception("补本地化图片失败")
            continue
        if new_content != content:
            r["content"] = new_content
            mid = r.get("message_id")
            if mid is not None:
                _update_message_content(group_id, str(mid), new_content)
            changed += 1
    return changed


async def enrich_rows_with_image_captions(
    rows: list[dict[str, Any]],
    *,
    provider: Any,
    model: str,
    max_images: int = 8,
) -> dict[str, Any]:
    """
    对本地图片做视觉描述，把 CQ 替换为 [图片描述: …] 供文本模型分析。
    返回 meta：{captioned, skipped, captions:[{path, text}], token_usage}
    """
    meta: dict[str, Any] = {
        "captioned": 0,
        "skipped": 0,
        "captions": [],
        "token_usage": TokenUsage().as_dict(),
    }
    tokens = TokenUsage()
    cache: dict[str, str] = {}
    budget = max(0, int(max_images))

    for r in rows:
        content = (r.get("content") or "").strip()
        if not content or "[CQ:image" not in content.lower():
            continue
        refs = extract_image_refs(content)
        if not refs:
            continue
        new_content = content
        for ref in refs:
            local_rel = ref.get("local_rel") or ""
            if not local_rel:
                placeholder = "[图片描述: 未本地存储，无法识别内容]"
                new_content = new_content.replace(ref["raw"], placeholder, 1)
                meta["skipped"] += 1
                continue
            try:
                if not media_abs_path(local_rel).exists():
                    placeholder = "[图片描述: 本地文件缺失]"
                    new_content = new_content.replace(ref["raw"], placeholder, 1)
                    meta["skipped"] += 1
                    continue
            except Exception:
                meta["skipped"] += 1
                continue

            if local_rel in cache:
                caption = cache[local_rel]
            elif budget <= 0:
                caption = "（本窗口图片过多，已跳过详细识别）"
                meta["skipped"] += 1
                cache[local_rel] = caption
            else:
                packed = read_local_image_b64(local_rel)
                if not packed:
                    caption = "无法读取本地图片"
                    meta["skipped"] += 1
                else:
                    mime, b64 = packed
                    caption, img_usage = await describe_image(
                        provider, model=model, mime=mime, b64=b64
                    )
                    tokens.add(img_usage)
                    budget -= 1
                    if not caption:
                        caption = "当前模型未能识别该图片（可能不支持视觉）"
                        meta["skipped"] += 1
                    else:
                        meta["captioned"] += 1
                        meta["captions"].append({"path": local_rel, "text": caption})
                cache[local_rel] = caption

            new_content = new_content.replace(
                ref["raw"], f"[图片描述: {cache[local_rel]}]", 1
            )
        r["content"] = new_content
    meta["token_usage"] = tokens.as_dict()
    return meta


def risk_max_from_report(report: dict[str, Any]) -> str:
    risks = report.get("risks") or []
    order = {"high": 3, "mid": 2, "medium": 2, "low": 1}
    best = 0
    label = "none"
    for r in risks:
        if not isinstance(r, dict):
            continue
        lv = str(r.get("level", "")).lower()
        score = order.get(lv, 0)
        if score > best:
            best = score
            label = "high" if score == 3 else "mid" if score == 2 else "low"
    return label


def report_to_md(report: dict[str, Any]) -> str:
    lines = [f"# {report.get('headline') or '群聊摘要'}", ""]
    if report.get("sentiment"):
        lines.append(f"- 情绪：{report['sentiment']}")
    topics = report.get("topics") or []
    if topics:
        lines.append("\n## 主题")
        for t in topics:
            if isinstance(t, dict):
                lines.append(f"- **{t.get('title', '')}**：{t.get('summary', '')}")
            else:
                lines.append(f"- {t}")
    points = report.get("key_points") or []
    if points:
        lines.append("\n## 要点")
        for p in points:
            lines.append(f"- {p}")
    risks = report.get("risks") or []
    if risks:
        lines.append("\n## 风险")
        for r in risks:
            if isinstance(r, dict):
                lines.append(f"- [{r.get('level')}] {r.get('detail')}")
            else:
                lines.append(f"- {r}")
    actions = report.get("action_items") or []
    if actions:
        lines.append("\n## 待办")
        for a in actions:
            if isinstance(a, dict):
                lines.append(f"- {a.get('task')}（{a.get('owner_hint') or '待确认'}）")
            else:
                lines.append(f"- {a}")
    notable = report.get("notable_users") or []
    if notable:
        lines.append("\n## 关键人物")
        for u in notable:
            if isinstance(u, dict):
                name = u.get("name") or u.get("user_id") or "?"
                summary = u.get("summary") or u.get("role") or ""
                lines.append(f"- **{name}**：{summary}")
            else:
                lines.append(f"- {u}")

    usage = report.get("context_usage") or {}
    if isinstance(usage, dict) and (
        usage.get("used_earlier_context")
        or usage.get("earlier_rounds")
        or usage.get("summary")
    ):
        lines.append("\n## 上下文引用说明")
        if usage.get("used_earlier_context"):
            lines.append("- 本分析引用了配置时间窗之前的聊天记录")
        rounds = usage.get("earlier_rounds")
        if rounds:
            lines.append(f"- 向前补上下文轮数：{rounds}")
        earlier_n = usage.get("earlier_messages")
        if earlier_n:
            lines.append(f"- 补充更早消息数：{earlier_n}")
        if usage.get("summary"):
            lines.append(f"- 说明：{usage.get('summary')}")

    appendix = report.get("appendix") or {}
    if isinstance(appendix, dict):
        nouns = appendix.get("nouns") or []
        links = appendix.get("links") or []
        notes = appendix.get("notes") or []
        if nouns or links or notes:
            lines.append("\n## 附录")
            if nouns:
                lines.append("\n### 名词解析")
                for n in nouns:
                    if isinstance(n, dict):
                        lines.append(f"- **{n.get('term', '')}**：{n.get('meaning', '')}")
                    else:
                        lines.append(f"- {n}")
            if links:
                lines.append("\n### 链接详解")
                for lk in links:
                    if isinstance(lk, dict):
                        url = lk.get("url") or ""
                        summary = lk.get("summary") or ""
                        lines.append(f"- {url} — {summary}".strip(" —"))
                    else:
                        lines.append(f"- {lk}")
            if notes:
                lines.append("\n### 补充说明")
                for note in notes:
                    lines.append(f"- {note}")
    return "\n".join(lines)


def _insert_skip_report(
    db: Path,
    *,
    job_id: int | None,
    group_id: str,
    start: int,
    end: int,
    reason: str,
    msg_count: int,
) -> None:
    headline = f"[定时跳过] {reason}"
    md = f"# {headline}\n\n- 消息数：{msg_count}\n- 说明：未调用 LLM，请调低「最少消息数」或加大「分析窗口」。"
    payload = {
        "headline": headline,
        "sentiment": "neutral",
        "topics": [],
        "key_points": [],
        "risks": [],
        "action_items": [],
        "skipped": True,
        "reason": reason,
        "period": {"start": start, "end": end, "msg_count": msg_count},
    }
    with sqlite3.connect(db) as conn:
        conn.execute(
            """
            INSERT INTO llm_reports(
              job_id, group_id, window_start, window_end, headline, sentiment,
              report_json, report_md, risk_max, msg_count
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                job_id,
                group_id,
                start,
                end,
                headline,
                "neutral",
                json.dumps(payload, ensure_ascii=False),
                md,
                "none",
                msg_count,
            ),
        )


async def run_group_summary(
    group_id: str,
    *,
    job_type: str = "manual",
    window_minutes: int | None = None,
    end_ts: int | None = None,
) -> dict[str, Any]:
    cfg = load_group_config(group_id)
    settings = load_app_settings()
    llm_cfg = cfg.llm_monitor
    text_enabled = bool(getattr(llm_cfg, "text_enabled", True))
    image_enabled = bool(getattr(llm_cfg, "image_enabled", True))
    image_same = bool(getattr(llm_cfg, "image_same_as_text", True))

    provider = provider_by_id(settings, llm_cfg.provider_id or settings.llm.active_provider_id)
    if provider is None:
        raise RuntimeError("未配置 LLM Provider，请先在总配置中填写")
    if not text_enabled and not image_enabled:
        return {
            "status": "skipped",
            "reason": "文本分析与图片分析均未启用",
            "msg_count": 0,
            "source": "配置关闭",
        }
    if not text_enabled:
        return {
            "status": "skipped",
            "reason": "文本分析未启用（仅图片分析无法生成完整报告）",
            "msg_count": 0,
            "source": "配置关闭",
        }

    # 图片分析 Provider / 模型（可与文本一致，也可独立）
    if image_same:
        image_provider = provider
        image_model_name = llm_cfg.model or provider.default_model
    else:
        image_provider = provider_by_id(
            settings,
            (getattr(llm_cfg, "image_provider_id", None) or "")
            or llm_cfg.provider_id
            or settings.llm.active_provider_id,
        ) or provider
        image_model_name = (
            getattr(llm_cfg, "image_model", None)
            or image_provider.default_model
            or llm_cfg.model
            or provider.default_model
        )

    end = end_ts or int(time.time())
    minutes = window_minutes if window_minutes is not None else (llm_cfg.window_minutes or 60)
    minutes = max(1, int(minutes))
    start = end - minutes * 60

    db = sqlite_path()
    ensure_llm_tables(db)

    rows = fetch_messages(group_id, start, end)
    configured_start = start
    source = f"时间窗 {minutes} 分钟"
    context_meta: dict[str, Any] = {
        "window_extended": False,
        "configured_start": configured_start,
        "configured_end": end,
    }
    if not rows and job_type == "manual":
        # 手动执行：窗口为空时回退到最近消息，避免空记录仍调模型
        rows = fetch_recent_messages(group_id, limit=80)
        if rows:
            source = f"时间窗 {minutes} 分钟无消息，已回退最近 {len(rows)} 条"
            start = int(rows[0].get("event_time") or start)
            end = int(rows[-1].get("event_time") or end)
            configured_start = start

    # 轻量确定性补齐：当前窗内引用 id（不扩展时间窗）
    if rows:
        reply_ids: list[str] = []
        for r in rows:
            reply_ids.extend(extract_reply_ids(_msg_text(r)))
        if reply_ids:
            found = fetch_messages_by_ids(group_id, reply_ids)
            if found:
                before_n = len(rows)
                rows = _merge_rows(
                    [_mark_context_role(r, "reply") for r in found],
                    [_mark_context_role(r, "window") for r in rows],
                )
                added = len(rows) - before_n
                if added > 0:
                    start = min(start, _msg_ts(rows[0]) or start)
                    end = max(end, _msg_ts(rows[-1]) or end)
                    source = f"{source}；已补齐窗内引用原文 {added} 条"
                    context_meta["window_extended"] = True
                    context_meta["lookback_messages"] = int(
                        context_meta.get("lookback_messages") or 0
                    ) + added
                    context_meta.setdefault("lookback_reasons", []).append("补齐被引用消息")

    # 启发式向前接话补全（小幅），正式大规模向前补文由下方 LLM 多轮驱动
    if rows:
        rows, start, end, look_meta = extend_messages_with_context(
            group_id,
            rows,
            window_start=start,
            window_end=end,
            configured_start=configured_start,
            max_rounds=2,
            batch_size=30,
            max_extra_messages=60,
            max_extra_minutes=minutes,  # 启发式最多再扩 1 个配置窗
        )
        if look_meta.get("window_extended"):
            prev_msgs = int(context_meta.get("lookback_messages") or 0)
            prev_reasons = list(context_meta.get("lookback_reasons") or [])
            context_meta["window_extended"] = True
            context_meta["lookback_messages"] = prev_msgs + int(
                look_meta.get("lookback_messages") or 0
            )
            context_meta["lookback_reasons"] = prev_reasons + list(
                look_meta.get("lookback_reasons") or []
            )
            reasons = "；".join(look_meta.get("lookback_reasons") or []) or "上下文不完整"
            n = int(look_meta.get("lookback_messages") or 0)
            source = f"{source}；启发式向前补了 {n} 条（{reasons}）"

    min_need = max(1, int(llm_cfg.min_messages or 1))
    # 短窗口 + 过高门槛会导致定时永远跳过；按窗口做温和上限
    if job_type == "schedule" and minutes <= 5:
        min_need = min(min_need, max(1, minutes * 2))

    if job_type == "schedule" and len(rows) < min_need:
        reason = f"消息过少({len(rows)}<{min_need})"
        with sqlite3.connect(db) as conn:
            cur = conn.execute(
                """
                INSERT INTO llm_jobs(job_type, group_id, window_start, window_end, status, error, model)
                VALUES(?,?,?,?,?,?,?)
                """,
                (job_type, group_id, start, end, "skipped", reason, llm_cfg.model),
            )
            job_id = int(cur.lastrowid)
        _insert_skip_report(
            db,
            job_id=job_id,
            group_id=group_id,
            start=start,
            end=end,
            reason=reason,
            msg_count=len(rows),
        )
        return {"status": "skipped", "reason": reason, "msg_count": len(rows), "source": source}

    if not rows:
        reason = "无消息可分析"
        with sqlite3.connect(db) as conn:
            cur = conn.execute(
                """
                INSERT INTO llm_jobs(job_type, group_id, window_start, window_end, status, error, model)
                VALUES(?,?,?,?,?,?,?)
                """,
                (job_type, group_id, start, end, "skipped", reason, llm_cfg.model),
            )
            job_id = int(cur.lastrowid)
        if job_type == "schedule":
            _insert_skip_report(
                db,
                job_id=job_id,
                group_id=group_id,
                start=start,
                end=end,
                reason=reason,
                msg_count=0,
            )
        return {
            "status": "skipped",
            "reason": f"无消息可分析（{source}）。请确认监控已落库，或加大「分析窗口」。",
            "msg_count": 0,
            "source": source,
        }

    user_prompt_body = llm_cfg.prompt.strip() or (
        "请总结并分析以下群聊，关注主题、风险、待办，并填写 appendix 附录。"
    )
    configured_end = int(context_meta.get("configured_end") or end)
    # 总时间跨度上限 = 配置窗口 × 5
    min_allowed_ts = max(0, configured_end - minutes * 60 * MAX_WINDOW_MULTIPLIER)
    history: list[dict[str, str]] = []
    llm_context_rounds = 0
    earlier_added_total = 0
    earlier_reasons: list[str] = []
    model_name = llm_cfg.model or provider.default_model

    def _build_meta_block() -> str:
        block = (
            f"群号: {group_id}\n"
            f"群名: {cfg.group_name or '-'}\n"
            f"数据来源: {source}\n"
            f"配置时间窗: {datetime.fromtimestamp(configured_start)} ~ {datetime.fromtimestamp(configured_end)}\n"
            f"当前实际范围: {datetime.fromtimestamp(start)} ~ {datetime.fromtimestamp(end)}\n"
            f"消息数: {len(rows)}\n"
            f"已向前补轮次: {llm_context_rounds}/{MAX_LLM_CONTEXT_ROUNDS}\n"
            f"时间跨度上限: 配置窗口×{MAX_WINDOW_MULTIPLIER}（最早可到 {datetime.fromtimestamp(min_allowed_ts)}）\n"
        )
        if earlier_added_total:
            block += f"已补充更早消息: {earlier_added_total} 条\n"
        return block

    # 分析前尽量把仍指向远程的图片补落到本地（旧消息兼容）
    try:
        localized = await ensure_rows_images_local(rows, group_id)
        if localized:
            source = f"{source}；分析前补存本地图片涉及 {localized} 条消息"
    except Exception:
        logger.exception("分析前图片本地化失败 group=%s", group_id)

    tokens = TokenUsage()

    # —— LLM 多轮：先判断是否完整，不完整则再向前取记录（最多 5 轮）——
    for round_i in range(1, MAX_LLM_CONTEXT_ROUNDS + 1):
        transcript = format_transcript(rows)
        check_user = (
            "请审查下列群聊是否完整、是否还需要更早前文才能理解。"
            "若需要，请设置 need_earlier=true；若已可分析，enough=true。"
            "只输出审查 JSON。\n\n"
            f"{_build_meta_block()}\n聊天记录:\n{transcript}"
        )
        try:
            check_raw, check_usage = await chat_complete(
                provider,
                model=model_name,
                system=CONTEXT_CHECK_SYSTEM,
                user=check_user,
                temperature=0.1,
                timeout_sec=60,
                force_json=True,
                history=history or None,
            )
            tokens.add(check_usage)
            check = extract_json_object(check_raw)
        except Exception:
            logger.exception(
                "上下文审查第 %s 轮失败 group=%s，停止继续向前补", round_i, group_id
            )
            break

        history.extend(
            [
                {"role": "user", "content": check_user},
                {"role": "assistant", "content": check_raw},
            ]
        )
        # 控制 history 长度，避免 prompt 无限膨胀
        if len(history) > 8:
            history = history[-8:]

        enough = bool(check.get("enough"))
        need_earlier = bool(check.get("need_earlier")) and not enough
        need_ids = []
        if isinstance(check.get("need_reply_ids"), list):
            need_ids = [str(x) for x in check["need_reply_ids"] if str(x).strip()]

        if enough and not need_ids:
            break

        # 达到最早时间边界则停止
        first_ts = _msg_ts(rows[0]) if rows else start
        if first_ts <= min_allowed_ts and not need_ids:
            earlier_reasons.append("已达配置窗口×5 的最早时间边界")
            break

        added_this_round = 0
        batches: list[list[dict[str, Any]]] = [rows]

        if need_ids:
            found = fetch_messages_by_ids(group_id, need_ids)
            tagged = []
            for r in found:
                ts = _msg_ts(r)
                if ts and ts < min_allowed_ts:
                    continue
                tagged.append(_mark_context_role(r, "reply"))
            if tagged:
                batches.append(tagged)
                added_this_round += len(tagged)

        if need_earlier or (not enough and not need_ids):
            suggest = check.get("suggested_earlier_count")
            try:
                take = int(suggest) if suggest is not None else 25
            except Exception:
                take = 25
            take = max(10, min(40, take))
            earlier = fetch_messages_before(group_id, first_ts, limit=take)
            kept = []
            for r in earlier:
                ts = _msg_ts(r)
                if ts and ts < min_allowed_ts:
                    continue
                kept.append(_mark_context_role(r, "before"))
            if kept:
                batches.append(kept)
                added_this_round += len(kept)

        new_rows = _merge_rows(*batches)
        if len(new_rows) <= len(rows):
            earlier_reasons.append(check.get("reason") or "未取到更多前文，结束补上下文")
            break

        rows = new_rows
        start = min(start, _msg_ts(rows[0]) or start)
        end = max(end, _msg_ts(rows[-1]) or end)
        llm_context_rounds = round_i
        earlier_added_total += added_this_round
        reason = (check.get("reason") or "模型判断需要更早前文").strip()
        earlier_reasons.append(f"第{round_i}轮：{reason}（+{added_this_round}）")
        context_meta["window_extended"] = True
        context_meta["lookback_messages"] = int(context_meta.get("lookback_messages") or 0) + added_this_round
        context_meta.setdefault("lookback_reasons", []).append(reason)
        source = f"{source}；第{round_i}轮向前补了 {added_this_round} 条"

        if enough:
            # 只为补引用，本轮后结束
            break

    # 正式分析前：补本地化（含多轮新增消息）+ 可选视觉描述
    try:
        await ensure_rows_images_local(rows, group_id)
    except Exception:
        logger.exception("正式分析前图片本地化失败 group=%s", group_id)
    image_meta: dict[str, Any] = {"captioned": 0, "skipped": 0, "captions": []}
    if image_enabled:
        try:
            image_meta = await enrich_rows_with_image_captions(
                rows,
                provider=image_provider,
                model=image_model_name,
                max_images=8,
            )
            img_tu = image_meta.get("token_usage")
            if isinstance(img_tu, dict):
                tokens.add(
                    TokenUsage(
                        prompt_tokens=int(img_tu.get("prompt_tokens") or 0),
                        completion_tokens=int(img_tu.get("completion_tokens") or 0),
                        total_tokens=int(img_tu.get("total_tokens") or 0),
                    )
                )
            if image_meta.get("captioned"):
                source = f"{source}；已视觉识别图片 {image_meta['captioned']} 张"
        except Exception:
            logger.exception("图片视觉描述失败 group=%s", group_id)
    else:
        # 未启用图片分析：把 CQ 图片改成占位，避免把超长 url 塞进文本模型
        for r in rows:
            content = (r.get("content") or "")
            if "[CQ:image" in content.lower():
                refs = extract_image_refs(content)
                new_c = content
                for ref in refs:
                    new_c = new_c.replace(ref["raw"], "[图片]", 1)
                r["content"] = new_c
        source = f"{source}；图片分析已关闭"

    transcript = format_transcript(rows)
    base_meta = _build_meta_block()
    if llm_context_rounds or earlier_added_total:
        base_meta += (
            "说明: 已按多轮审查补充更早聊天记录（最多 "
            f"{MAX_LLM_CONTEXT_ROUNDS} 轮，时间跨度不超过配置窗口×{MAX_WINDOW_MULTIPLIER}）；"
            "正式分析时必须在 context_usage 中标明引用了更早内容。\n"
        )
    if image_meta.get("captioned") or image_meta.get("skipped"):
        base_meta += (
            f"图片识别: 成功 {image_meta.get('captioned') or 0} 张，"
            f"跳过/失败 {image_meta.get('skipped') or 0} 张；"
            "请将「[图片描述: …]」纳入主题与风险分析。\n"
        )

    user_prompt = (
        f"{user_prompt_body}\n\n"
        f"{base_meta}\n"
        "请输出完整分析 JSON（含 appendix 与 context_usage）。\n"
        f"\n聊天记录:\n{transcript}"
    )

    with sqlite3.connect(db) as conn:
        cur = conn.execute(
            """
            INSERT INTO llm_jobs(job_type, group_id, window_start, window_end, status, model)
            VALUES(?,?,?,?,?,?)
            """,
            (job_type, group_id, start, end, "running", model_name),
        )
        job_id = int(cur.lastrowid)

    try:
        raw, final_usage = await chat_complete(
            provider,
            model=model_name,
            system=DEFAULT_SYSTEM,
            user=user_prompt,
            history=history or None,
            timeout_sec=120,
        )
        tokens.add(final_usage)
        report = extract_json_object(raw)
        if not isinstance(report.get("appendix"), dict):
            report["appendix"] = {"nouns": [], "links": [], "notes": []}
        else:
            ap = report["appendix"]
            ap.setdefault("nouns", [])
            ap.setdefault("links", [])
            ap.setdefault("notes", [])

        usage = report.get("context_usage")
        if not isinstance(usage, dict):
            usage = {}
        if earlier_added_total or llm_context_rounds:
            usage["used_earlier_context"] = True
            usage["earlier_rounds"] = int(usage.get("earlier_rounds") or llm_context_rounds)
            usage["earlier_messages"] = int(
                usage.get("earlier_messages") or earlier_added_total
            )
            if not usage.get("summary"):
                usage["summary"] = "；".join(earlier_reasons) or "引用了配置时间窗之前的相关聊天"
        else:
            usage.setdefault("used_earlier_context", False)
            usage.setdefault("earlier_rounds", 0)
            usage.setdefault("earlier_messages", 0)
            usage.setdefault("summary", "")
        report["context_usage"] = usage

        period = {
            "start": start,
            "end": end,
            "msg_count": len(rows),
            "configured_start": configured_start,
            "configured_end": configured_end,
            "window_extended": bool(context_meta.get("window_extended") or earlier_added_total),
            "lookback_messages": int(context_meta.get("lookback_messages") or earlier_added_total),
            "lookback_reasons": list(context_meta.get("lookback_reasons") or earlier_reasons),
            "analysis_turns": 1 + llm_context_rounds,
            "llm_context_rounds": llm_context_rounds,
            "max_context_rounds": MAX_LLM_CONTEXT_ROUNDS,
            "max_window_multiplier": MAX_WINDOW_MULTIPLIER,
            "earlier_messages": earlier_added_total,
            "images_captioned": int(image_meta.get("captioned") or 0),
            "images_skipped": int(image_meta.get("skipped") or 0),
            "source": source,
        }
        report["period"] = period
        token_usage = tokens.as_dict()
        report["token_usage"] = token_usage
        risk = risk_max_from_report(report)
        md = report_to_md(report)
        notes_head: list[str] = []
        if usage.get("used_earlier_context"):
            notes_head.append(
                f"> 已引用更早聊天记录：向前审查 {llm_context_rounds} 轮，"
                f"补充 {earlier_added_total} 条"
                f"（上限 {MAX_LLM_CONTEXT_ROUNDS} 轮 / 配置窗口×{MAX_WINDOW_MULTIPLIER}）"
            )
            if usage.get("summary"):
                notes_head.append(f"> 引用说明：{usage.get('summary')}")
        elif period["window_extended"]:
            notes_head.append(
                f"> 已扩展相关上下文 {period['lookback_messages']} 条"
                f"（{ '；'.join(period['lookback_reasons']) or '上下文补全' }）"
            )
        if llm_context_rounds > 0:
            notes_head.append("> 本报告经多轮对话：先审查完整性，再取前文，最后输出分析与附录")
        if token_usage.get("total_tokens"):
            notes_head.append(
                f"> Token 消耗：合计 {token_usage['total_tokens']}"
                f"（输入 {token_usage['prompt_tokens']} / 输出 {token_usage['completion_tokens']}）"
            )
        if notes_head:
            md = "\n".join(notes_head) + "\n\n" + md
        with sqlite3.connect(db) as conn:
            conn.execute(
                """
                UPDATE llm_jobs SET status=?, finished_at=datetime('now','localtime') WHERE id=?
                """,
                ("ok", job_id),
            )
            conn.execute(
                """
                INSERT INTO llm_reports(
                  job_id, group_id, window_start, window_end, headline, sentiment,
                  report_json, report_md, risk_max, msg_count,
                  prompt_tokens, completion_tokens, total_tokens
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    job_id,
                    group_id,
                    start,
                    end,
                    report.get("headline"),
                    report.get("sentiment"),
                    json.dumps(report, ensure_ascii=False),
                    md,
                    risk,
                    len(rows),
                    token_usage["prompt_tokens"],
                    token_usage["completion_tokens"],
                    token_usage["total_tokens"],
                ),
            )
        return {
            "status": "ok",
            "job_id": job_id,
            "msg_count": len(rows),
            "risk_max": risk,
            "report": report,
            "report_md": md,
            "source": source,
            "window_extended": period["window_extended"],
            "window_start": start,
            "window_end": end,
            "analysis_turns": period["analysis_turns"],
            "llm_context_rounds": llm_context_rounds,
            "earlier_messages": earlier_added_total,
            "token_usage": token_usage,
            "total_tokens": token_usage["total_tokens"],
            "prompt_tokens": token_usage["prompt_tokens"],
            "completion_tokens": token_usage["completion_tokens"],
        }
    except Exception as e:
        logger.exception("LLM 总结失败 group=%s", group_id)
        with sqlite3.connect(db) as conn:
            conn.execute(
                """
                UPDATE llm_jobs SET status=?, error=?, finished_at=datetime('now','localtime')
                WHERE id=?
                """,
                ("failed", str(e)[:500], job_id),
            )
        raise



def list_reports(group_id: str | None = None, limit: int = 30) -> list[dict[str, Any]]:
    db = sqlite_path()
    ensure_llm_tables(db)
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        if group_id:
            rows = conn.execute(
                """
                SELECT id, group_id, window_start, window_end, headline, sentiment,
                       risk_max, msg_count, created_at, report_md, report_json,
                       COALESCE(prompt_tokens, 0) AS prompt_tokens,
                       COALESCE(completion_tokens, 0) AS completion_tokens,
                       COALESCE(total_tokens, 0) AS total_tokens
                FROM llm_reports WHERE group_id=?
                ORDER BY id DESC LIMIT ?
                """,
                (str(group_id), limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT id, group_id, window_start, window_end, headline, sentiment,
                       risk_max, msg_count, created_at, report_md, report_json,
                       COALESCE(prompt_tokens, 0) AS prompt_tokens,
                       COALESCE(completion_tokens, 0) AS completion_tokens,
                       COALESCE(total_tokens, 0) AS total_tokens
                FROM llm_reports
                ORDER BY id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
    return [dict(r) for r in rows]
