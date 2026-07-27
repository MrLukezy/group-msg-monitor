"""LLM 群聊总结服务。"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from app.llm.client import chat_complete, extract_json_object
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
    "action_items, sentiment, notable_users。"
    "risks 项含 level/type/detail/evidence；evidence 必须是原文摘录。"
)


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
        lines.append(f"[{clock}] {name}: {content}")
    text = "\n".join(lines)
    if len(text) <= max_chars:
        return text
    # 头尾保留
    head = text[: max_chars // 2]
    tail = text[-(max_chars // 2) :]
    return head + "\n\n...[中间已截断]...\n\n" + tail


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
    actions = report.get("action_items") or []
    if actions:
        lines.append("\n## 待办")
        for a in actions:
            if isinstance(a, dict):
                lines.append(f"- {a.get('task')}（{a.get('owner_hint') or '待确认'}）")
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
    provider = provider_by_id(settings, llm_cfg.provider_id or settings.llm.active_provider_id)
    if provider is None:
        raise RuntimeError("未配置 LLM Provider，请先在总配置中填写")

    end = end_ts or int(time.time())
    minutes = window_minutes if window_minutes is not None else (llm_cfg.window_minutes or 60)
    minutes = max(1, int(minutes))
    start = end - minutes * 60

    db = sqlite_path()
    ensure_llm_tables(db)

    rows = fetch_messages(group_id, start, end)
    source = f"时间窗 {minutes} 分钟"
    if not rows and job_type == "manual":
        # 手动执行：窗口为空时回退到最近消息，避免空记录仍调模型
        rows = fetch_recent_messages(group_id, limit=80)
        if rows:
            source = f"时间窗 {minutes} 分钟无消息，已回退最近 {len(rows)} 条"
            start = int(rows[0].get("event_time") or start)
            end = int(rows[-1].get("event_time") or end)

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

    transcript = format_transcript(rows)
    user_prompt_body = llm_cfg.prompt.strip() or (
        "请总结并分析以下群聊，关注主题、风险与待办。"
    )
    user_prompt = (
        f"{user_prompt_body}\n\n"
        f"群号: {group_id}\n"
        f"群名: {cfg.group_name or '-'}\n"
        f"数据来源: {source}\n"
        f"时间范围: {datetime.fromtimestamp(start)} ~ {datetime.fromtimestamp(end)}\n"
        f"消息数: {len(rows)}\n\n"
        f"聊天记录:\n{transcript}"
    )

    with sqlite3.connect(db) as conn:
        cur = conn.execute(
            """
            INSERT INTO llm_jobs(job_type, group_id, window_start, window_end, status, model)
            VALUES(?,?,?,?,?,?)
            """,
            (job_type, group_id, start, end, "running", llm_cfg.model or provider.default_model),
        )
        job_id = int(cur.lastrowid)

    try:
        raw = await chat_complete(
            provider,
            model=llm_cfg.model or provider.default_model,
            system=DEFAULT_SYSTEM,
            user=user_prompt,
        )
        report = extract_json_object(raw)
        report.setdefault("period", {"start": start, "end": end, "msg_count": len(rows)})
        risk = risk_max_from_report(report)
        md = report_to_md(report)
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
                  report_json, report_md, risk_max, msg_count
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
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
                       risk_max, msg_count, created_at, report_md, report_json
                FROM llm_reports WHERE group_id=?
                ORDER BY id DESC LIMIT ?
                """,
                (str(group_id), limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT id, group_id, window_start, window_end, headline, sentiment,
                       risk_max, msg_count, created_at, report_md, report_json
                FROM llm_reports
                ORDER BY id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
    return [dict(r) for r in rows]
