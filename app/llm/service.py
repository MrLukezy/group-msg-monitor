"""LLM 群聊总结服务。"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
import traceback
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
    DEFAULT_LLM_MONITOR_PROMPT,
    ROOT_DIR,
    GroupConfig,
    clamp_report_keep_limit,
    load_app_settings,
    load_group_config,
    provider_by_id,
    resolve_llm_timing,
)

logger = logging.getLogger(__name__)

DEFAULT_SYSTEM = (
    "你是群聊监控分析助手。只基于给定记录分析，禁止编造。"
    "必须输出合法 JSON 对象，字段含：headline, topics, key_points, risks, "
    "action_items, sentiment, notable_users, appendix, context_usage。"
    "risks 项含 level/type/detail/evidence；evidence 必须是原文摘录。"
    "notable_users 项含 user_id/name/role/summary。"
    "key_points 必须是对象数组（禁止纯字符串），每项含："
    "title（一句话要点）、summary（2～4 句简述）、"
    "deep_dive（{detail, evidence, knowledge}："
    "detail 为基于群聊讨论的观点/脉络深入分析（建议 120～400 字）；"
    "evidence 为聊天原文摘录；"
    "knowledge 为 [{topic, content, source}] 数组，写你基于已有知识补充的背景、"
    "概念解释、技术/仓库概况等（不是群友原话复述；无外网检索时 source 填 model_knowledge；"
    "不确定处写明「依据模型知识/记录不足」；无则空数组）、"
    "nouns（[{term, meaning}] 本要点名词剖析：特有名词、黑话、英文简称必须解释）、"
    "links（[{url, summary}] 本要点相关链接）、notes（[string] 本要点补充说明）。"
    "每个要点都必须有独立的 deep_dive；深入分析须同时覆盖群内观点（detail）与背景知识（knowledge），"
    "不要把深入分析单独拆到别处。"
    "当出现 GitHub 仓库、AI/大模型相关名词，或用户自定义要求需深挖时，"
    "必须在对应要点的 deep_dive.detail 与 deep_dive.knowledge 中写长文展开，不要只写一句话；"
    "相关术语必须写入该要点 nouns。"
    "appendix 为全局附录对象，含：nouns、links、notes（格式同上）；"
    "要点相关内容优先写在该要点的 nouns/links/notes 中；appendix 仅放跨要点或无法归类的补充。"
    "若某类数组无内容，用空数组；有 GitHub/AI 内容时要点内 appendix 字段也应尽量充实。"
    "context_usage 必填："
    "{used_earlier_context:bool, earlier_rounds:int, earlier_messages:int, summary:string}；"
    "若分析使用了配置时间窗之前补入的消息/引用原文，used_earlier_context 必须为 true，"
    "并在 summary 中简明说明引用了哪些更早内容。"
    "记录可能含「补前文/补后文/引用补全」标记，请把它们当作同一段多轮对话理解。"
    "消息中可能含「[图片描述: …]」，这是对聊天图片的视觉识别结果，必须纳入主题与风险分析。"
    "用户消息中的「本群自定义分析要求」与「主题深挖规则」优先级最高，必须遵守；"
    "允许并鼓励在 key_points（含 deep_dive/nouns/notes/knowledge）中明显扩充篇幅，不要为了短而省略。"
)

CONTEXT_CHECK_SYSTEM = (
    "你是群聊上下文完整性审查助手。"
    "判断「当前分析窗口内的最新讨论」是否完整，是否还缺与该讨论直接相关的更早前文"
    "（含被引用但未出现正文的消息）。"
    "必须输出合法 JSON："
    '{"enough":bool,"reason":string,"need_earlier":bool,'
    '"need_reply_ids":string[],"suggested_earlier_count":int}。'
    "enough=true 表示可以开始正式分析；need_earlier=true 表示应再向前取相关前文；"
    "need_reply_ids 只填记录中已出现、但仍缺正文的引用 id；"
    "suggested_earlier_count 建议再取多少条（优先 8~20；仅明确断裂时才到 25）。"
    "只补与窗口内最新话题直接相关的前文；不要为凑条数灌入无关闲聊或整段更早历史。"
    "若出现 GitHub / AI·大模型相关讨论，也只向前补该主题相关段落，不要整窗无关消息。"
    "多轮审查时：若本轮只给了「新补入前文」，请结合已说明的窗口讨论判断，"
    "勿要求重复已持有内容。"
    "禁止编造 id 或臆测窗外具体内容。"
)

IMAGE_NEED_CHECK_SYSTEM = (
    "你是群聊图片识别必要性审查助手。"
    "记录中的图片已用「[图片]」占位，你只能根据文字判断是否需要视觉识别图片内容。"
    "必须输出合法 JSON："
    '{"need_images":bool,"reason":string,"image_message_ids":string[]}。'
    "need_images=true 仅当文字表明分析依赖图片内容，例如："
    "讨论/询问图中信息、引用截图/报错/配置/代码/单据、风险或关键证据可能在图中且文字不足以理解。"
    "纯表情包、无讨论的刷图、文字已充分说明图意时 need_images=false。"
    "image_message_ids 填写需要识别的消息 id（仅限记录中已出现的 id=…）；"
    "need_images=true 但无法精确指出时，可返回空数组表示识别窗口内相关图片。"
    "禁止编造 id；禁止臆测图中具体内容。"
)

# 内置：GitHub / AI 相关必须多轮深挖
TOPIC_DEEP_DIVE_RULES = (
    "若历史记录中出现 GitHub 仓库（含 github.com、gist、owner/repo、clone/PR/issue 等），"
    "或 AI 相关名词（大模型、LLM、GPT、Claude、Gemini、Cursor、Agent、Prompt、RAG、微调、"
    "OpenAI、Anthropic、通义、文心、DeepSeek 等），必须："
    "1) 通过多轮向前补文尽量凑齐该主题相关讨论上下文（勿灌入无关闲聊）；"
    "2) 为每个仓库/名词建立独立 key_point，并在该要点的 deep_dive 中深入分析、明显扩充篇幅；"
    "3) deep_dive.detail 写群内讨论观点与结论，deep_dive.knowledge 写背景知识"
    "（技术定位、常见用途、与讨论的关联；非聊天复述）；"
    "4) 仓库链接写入对应要点的 links（或全局 appendix.links；说明用途、讨论结论；不确定处标明「记录不足」）；"
    "5) AI/特有名词写入对应要点的 nouns（名词剖析：含义与群内用法），不得留空；"
    "6) 禁止只给一句带过。"
)

_AI_TOPIC_RE = re.compile(
    r"(?i)\b("
    r"llm|gpt-?\d*|chatgpt|claude|gemini|openai|anthropic|deepseek|o1|o3|"
    r"cursor|copilot|rag|prompt|agent|embedding|finetune|fine[-_ ]?tune|"
    r"transformer|diffusion|midjourney|stable[\s_-]?diffusion"
    r")\b|"
    r"(大模型|人工智能|生成式|提示词|微调|智能体|通义|文心|豆包|星火|混元)"
)
_GITHUB_TOPIC_RE = re.compile(
    r"(?i)((https?://)?(www\.)?github\.com/[\w.-]+/[\w.-]+|"
    r"gist\.github|githubusercontent|"
    r"\bgit\s*clone\b|\bpull\s*request\b|"
    r"github\s*仓库|git\s*仓库|开源仓库|开源项目)"
)

# LLM 驱动向前补前文：最多 5 轮，总时间跨度不超过配置窗口的 5 倍
MAX_LLM_CONTEXT_ROUNDS = 5
MAX_WINDOW_MULTIPLIER = 5

# 手动“立即分析”以快速返回为优先，不做多轮回溯，并限制输入规模。
# 12000 中文字符约 7500 tokens，需为系统提示和模型输出预留上下文。
MANUAL_MAX_MESSAGES = 100
MANUAL_TRANSCRIPT_MAX_CHARS = 12000
MANUAL_MAX_IMAGES = 4


def detect_focus_topics(text: str) -> dict[str, Any]:
    """扫描记录中是否含 GitHub / AI 等需深挖主题。"""
    raw = text or ""
    github = bool(_GITHUB_TOPIC_RE.search(raw))
    ai = bool(_AI_TOPIC_RE.search(raw))
    labels: list[str] = []
    if github:
        labels.append("GitHub仓库/链接")
    if ai:
        labels.append("AI/大模型相关名词")
    return {
        "github": github,
        "ai": ai,
        "hit": github or ai,
        "labels": labels,
    }


def build_analysis_instructions(custom_prompt: str) -> str:
    """合并本群自定义提示词 + 内置深挖规则（用于多轮审查与正式分析）。"""
    parts: list[str] = []
    custom = (custom_prompt or "").strip()
    if custom:
        parts.append("【本群自定义分析要求——必须遵守】\n" + custom)
    parts.append("【主题深挖规则——必须遵守】\n" + TOPIC_DEEP_DIVE_RULES)
    return "\n\n".join(parts)


def limit_recent_rows(
    rows: list[dict[str, Any]],
    *,
    max_messages: int = MANUAL_MAX_MESSAGES,
    max_chars: int = MANUAL_TRANSCRIPT_MAX_CHARS,
) -> tuple[list[dict[str, Any]], int]:
    """从末尾保留最近消息，并按近似格式化长度限制输入规模。"""
    if not rows:
        return [], 0

    candidates = rows[-max(1, int(max_messages)) :]
    selected: list[dict[str, Any]] = []
    used = 0
    budget = max(1, int(max_chars))
    for row in reversed(candidates):
        content = str(row.get("content") or "[空消息]")
        sender = str(row.get("sender_name") or row.get("user_id") or "?")
        estimated_chars = len(content) + len(sender) + 80
        if selected and used + estimated_chars > budget:
            break
        selected.append(row)
        used += estimated_chars

    limited = list(reversed(selected))
    return limited, max(0, len(rows) - len(limited))


def sqlite_path() -> Path:
    from app.settings_store import DATA_DIR, ROOT_DIR

    # 兼容 .env STORAGE_SQLITE_PATH；未配置时跟随可切换的 DATA_DIR
    env = ROOT_DIR / ".env"
    path = DATA_DIR / "messages.db"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("STORAGE_SQLITE_PATH="):
                raw = line.split("=", 1)[1].strip().strip('"')
                p = Path(raw)
                path = p if p.is_absolute() else ROOT_DIR / p
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
        _ensure_job_error_columns(conn)
        _ensure_report_token_columns(conn)
        _ensure_report_favorite_columns(conn)


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


def _ensure_report_favorite_columns(conn: sqlite3.Connection) -> None:
    cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(llm_reports)").fetchall()}
    if "favorited" not in cols:
        conn.execute(
            "ALTER TABLE llm_reports ADD COLUMN favorited INTEGER DEFAULT 0"
        )
    if "favorited_at" not in cols:
        conn.execute("ALTER TABLE llm_reports ADD COLUMN favorited_at TEXT")
    if "favorite_messages_json" not in cols:
        conn.execute(
            "ALTER TABLE llm_reports ADD COLUMN favorite_messages_json TEXT"
        )
    if "github_issue_url" not in cols:
        conn.execute("ALTER TABLE llm_reports ADD COLUMN github_issue_url TEXT")


def _ensure_job_error_columns(conn: sqlite3.Connection) -> None:
    cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(llm_jobs)").fetchall()}
    definitions = {
        "error_stage": "TEXT",
        "error_type": "TEXT",
        "error_summary": "TEXT",
        "error_detail": "TEXT",
        "log_excerpt": "TEXT",
    }
    for name, sql_type in definitions.items():
        if name not in cols:
            conn.execute(f"ALTER TABLE llm_jobs ADD COLUMN {name} {sql_type}")


def redact_sensitive_text(value: Any, max_chars: int = 8192) -> str:
    """清理可上屏/上报的错误文本，禁止带出凭据、签名 URL 和聊天原文。"""
    text = str(value or "")
    text = re.sub(
        r"(?i)\b(authorization|api[_-]?key|access[_-]?token|token|cookie|secret)"
        r"\s*[:=]\s*([^\s,;]+)",
        r"\1=[已脱敏]",
        text,
    )
    text = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [已脱敏]", text)

    def _strip_query(match: re.Match[str]) -> str:
        url = match.group(0)
        base = url.split("?", 1)[0]
        return f"{base}?[查询参数已脱敏]" if "?" in url else base

    text = re.sub(r"https?://[^\s<>'\"\]]+", _strip_query, text)
    text = re.sub(r"(?is)\[CQ:image,[^\]]+\]", "[CQ:image,已脱敏]", text)
    text = text.replace("\x00", "")
    return text[: max(0, int(max_chars))]


def _failure_summary(exc: BaseException) -> str:
    raw = str(exc).strip()
    if not raw:
        raw = exc.__class__.__name__
    return redact_sensitive_text(raw, 512)


def record_llm_failure(
    group_id: str,
    *,
    job_type: str,
    exc: BaseException,
    stage: str,
    model: str = "",
    window_start: int = 0,
    window_end: int = 0,
    job_id: int | None = None,
    log_excerpt: str = "",
) -> dict[str, Any]:
    """把任意阶段异常持久化为失败 job + 可展示的失败主题。"""
    db = sqlite_path()
    ensure_llm_tables(db)
    now = int(time.time())
    end = int(window_end or now)
    start = int(window_start or end)
    error_type = exc.__class__.__name__
    summary = _failure_summary(exc)
    detail_raw = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    detail = redact_sensitive_text(detail_raw or summary, 4096)
    logs = redact_sensitive_text(log_excerpt or detail_raw or summary, 8192)
    log_lines = logs.splitlines()[-50:]
    logs = "\n".join(log_lines)
    headline = f"[分析失败] {summary[:96]}"
    payload = {
        "failed": True,
        "headline": headline,
        "topics": ["LLM 分析错误"],
        "key_points": [f"失败阶段：{stage}", f"错误类型：{error_type}"],
        "risks": [],
        "action_items": ["检查错误详情与诊断日志后重试；必要时上报 GitHub Issue。"],
        "sentiment": "error",
        "error": {
            "stage": stage,
            "type": error_type,
            "summary": summary,
            "detail": detail,
            "log_excerpt": logs,
        },
        "period": {
            "start": start,
            "end": end,
            "msg_count": 0,
            "source": "LLM 错误记录",
        },
    }
    md = (
        f"# {headline}\n\n"
        f"- 失败阶段：{stage}\n"
        f"- 错误类型：{error_type}\n"
        f"- 模型：{redact_sensitive_text(model, 200) or '记录不足'}\n"
        f"- 错误摘要：{summary}\n\n"
        "## 错误详情\n"
        f"```text\n{detail}\n```\n\n"
        "## 诊断日志（已脱敏）\n"
        f"```text\n{logs}\n```\n\n"
        "## 建议操作\n"
        "- 检查 Provider、模型、网络和上下文长度后重试。\n"
        "- 若问题持续，可使用右上角“上报 Issue”。\n"
    )

    with sqlite3.connect(db) as conn:
        if job_id is None:
            recent = conn.execute(
                """
                SELECT id FROM llm_jobs
                WHERE group_id=? AND status='failed'
                  AND COALESCE(error_summary, error, '')=?
                  AND created_at >= datetime('now','-30 seconds')
                ORDER BY id DESC LIMIT 1
                """,
                (str(group_id), summary),
            ).fetchone()
            if recent:
                job_id = int(recent[0])
            else:
                cur = conn.execute(
                    """
                    INSERT INTO llm_jobs(
                      job_type, group_id, window_start, window_end, status, error, model,
                      error_stage, error_type, error_summary, error_detail, log_excerpt,
                      finished_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,datetime('now','localtime'))
                    """,
                    (
                        job_type,
                        str(group_id),
                        start,
                        end,
                        "failed",
                        summary,
                        model,
                        stage,
                        error_type,
                        summary,
                        detail,
                        logs,
                    ),
                )
                job_id = int(cur.lastrowid)
        else:
            conn.execute(
                """
                UPDATE llm_jobs
                SET status='failed', error=?, error_stage=?, error_type=?,
                    error_summary=?, error_detail=?, log_excerpt=?,
                    finished_at=datetime('now','localtime')
                WHERE id=?
                """,
                (summary, stage, error_type, summary, detail, logs, int(job_id)),
            )

        existing = conn.execute(
            "SELECT id FROM llm_reports WHERE job_id=? ORDER BY id DESC LIMIT 1",
            (int(job_id),),
        ).fetchone()
        if existing:
            report_id = int(existing[0])
        else:
            cur = conn.execute(
                """
                INSERT INTO llm_reports(
                  job_id, group_id, window_start, window_end, headline, sentiment,
                  report_json, report_md, risk_max, msg_count
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    int(job_id),
                    str(group_id),
                    start,
                    end,
                    headline,
                    "error",
                    json.dumps(payload, ensure_ascii=False),
                    md,
                    "high",
                    0,
                ),
            )
            report_id = int(cur.lastrowid)
    prune_old_llm_reports()
    return {
        "job_id": int(job_id),
        "report_id": report_id,
        "headline": headline,
        "error_summary": summary,
    }


def fetch_messages_in_window(
    group_id: str,
    start_ts: int,
    end_ts: int,
    *,
    limit: int = 800,
) -> list[dict[str, Any]]:
    """取时间窗内消息（正序），用于收藏快照。"""
    db = sqlite_path()
    if not db.exists() or not group_id:
        return []
    start = min(int(start_ts), int(end_ts))
    end = max(int(start_ts), int(end_ts))
    lim = max(1, min(800, int(limit)))
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT id, group_id, COALESCE(user_id,'') AS user_id,
                   COALESCE(sender_name,'') AS sender_name,
                   COALESCE(content,'') AS content,
                   event_time, COALESCE(created_at,'') AS created_at,
                   COALESCE(message_id,'') AS message_id
            FROM messages
            WHERE group_id=?
              AND COALESCE(event_time, 0) >= ?
              AND COALESCE(event_time, 0) <= ?
            ORDER BY COALESCE(event_time, 0) ASC, id ASC
            LIMIT ?
            """,
            (str(group_id), start, end, lim),
        ).fetchall()
    return [dict(r) for r in rows]


def set_report_favorited(report_id: int, favorited: bool) -> dict[str, Any]:
    """收藏/取消收藏。收藏时快照当前时间窗聊天记录，之后清理也不会删。"""
    db = sqlite_path()
    ensure_llm_tables(db)
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT id, group_id, window_start, window_end, headline,
                   COALESCE(favorited, 0) AS favorited,
                   favorite_messages_json
            FROM llm_reports WHERE id=?
            """,
            (int(report_id),),
        ).fetchone()
        if not row:
            raise ValueError(f"报告不存在: {report_id}")
        if favorited:
            msgs = fetch_messages_in_window(
                str(row["group_id"]),
                int(row["window_start"] or 0),
                int(row["window_end"] or 0),
                limit=800,
            )
            # 若窗内已无消息但之前有快照，保留旧快照
            raw_prev = row["favorite_messages_json"]
            if not msgs and raw_prev:
                snap = raw_prev
            else:
                snap = json.dumps(
                    [
                        {
                            "id": m.get("id"),
                            "groupId": m.get("group_id"),
                            "userId": m.get("user_id"),
                            "senderName": m.get("sender_name"),
                            "content": m.get("content"),
                            "eventTime": m.get("event_time"),
                            "createdAt": m.get("created_at"),
                            "messageId": m.get("message_id"),
                        }
                        for m in msgs
                    ],
                    ensure_ascii=False,
                )
            conn.execute(
                """
                UPDATE llm_reports
                SET favorited=1,
                    favorited_at=datetime('now','localtime'),
                    favorite_messages_json=?
                WHERE id=?
                """,
                (snap, int(report_id)),
            )
            msg_count = len(json.loads(snap)) if snap else 0
        else:
            conn.execute(
                """
                UPDATE llm_reports
                SET favorited=0, favorited_at=NULL
                WHERE id=?
                """,
                (int(report_id),),
            )
            # 取消收藏不删快照，便于再次收藏；清理逻辑仍会按非收藏处理
            msg_count = 0
    return {
        "ok": True,
        "id": int(report_id),
        "favorited": bool(favorited),
        "messageCount": msg_count,
        "headline": row["headline"],
    }


def get_report_favorite_messages(report_id: int) -> list[dict[str, Any]]:
    db = sqlite_path()
    ensure_llm_tables(db)
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT group_id, window_start, window_end,
                   COALESCE(favorited, 0) AS favorited,
                   favorite_messages_json
            FROM llm_reports WHERE id=?
            """,
            (int(report_id),),
        ).fetchone()
    if not row:
        return []
    raw = row["favorite_messages_json"]
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                return data
        except Exception:
            pass
    return [
        {
            "id": m.get("id"),
            "groupId": m.get("group_id"),
            "userId": m.get("user_id"),
            "senderName": m.get("sender_name"),
            "content": m.get("content"),
            "eventTime": m.get("event_time"),
            "createdAt": m.get("created_at"),
        }
        for m in fetch_messages_in_window(
            str(row["group_id"]),
            int(row["window_start"] or 0),
            int(row["window_end"] or 0),
            limit=800,
        )
    ]


def build_github_issue_preview(report_id: int) -> dict[str, Any]:
    """从失败报告生成安全 Issue；绝不包含聊天原文。"""
    db = sqlite_path()
    ensure_llm_tables(db)
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT r.id, r.job_id, r.headline, r.created_at, r.report_json,
                   r.github_issue_url, j.group_id, j.job_type, j.model,
                   j.error_stage, j.error_type, j.error_summary,
                   j.error_detail, j.log_excerpt
            FROM llm_reports r
            LEFT JOIN llm_jobs j ON j.id=r.job_id
            WHERE r.id=?
            """,
            (int(report_id),),
        ).fetchone()
    if not row:
        raise ValueError(f"报告不存在: {report_id}")
    try:
        payload = json.loads(row["report_json"] or "{}")
    except Exception:
        payload = {}
    if not payload.get("failed"):
        raise ValueError("仅失败主题可以上报 Issue")

    summary = redact_sensitive_text(
        row["error_summary"] or (payload.get("error") or {}).get("summary") or row["headline"],
        512,
    )
    detail = redact_sensitive_text(
        row["error_detail"] or (payload.get("error") or {}).get("detail") or summary,
        4096,
    )
    logs = redact_sensitive_text(
        row["log_excerpt"] or (payload.get("error") or {}).get("log_excerpt") or "",
        8192,
    )
    stage = redact_sensitive_text(row["error_stage"] or "unknown", 100)
    error_type = redact_sensitive_text(row["error_type"] or "Error", 100)
    model = redact_sensitive_text(row["model"] or "记录不足", 200)
    title = f"[LLM 分析失败] {summary[:90]}"
    body = (
        "## 问题摘要\n"
        f"{summary}\n\n"
        "## 运行信息\n"
        f"- Report ID: {int(row['id'])}\n"
        f"- Job ID: {int(row['job_id'] or 0)}\n"
        f"- Job 类型: {redact_sensitive_text(row['job_type'] or 'unknown', 50)}\n"
        f"- 失败阶段: {stage}\n"
        f"- 错误类型: {error_type}\n"
        f"- 模型: {model}\n"
        f"- 时间: {redact_sensitive_text(row['created_at'] or '', 100)}\n\n"
        "## 错误详情（已脱敏）\n"
        f"```text\n{detail}\n```\n\n"
        "## 诊断日志（已脱敏）\n"
        f"```text\n{logs or '记录不足'}\n```\n\n"
        "## 隐私说明\n"
        "本 Issue 由桌面端自动生成，不包含群聊原文、用户消息或访问凭据。\n"
    )
    return {
        "reportId": int(row["id"]),
        "title": title,
        "body": body,
        "issueUrl": row["github_issue_url"] or "",
    }


def set_report_github_issue_url(report_id: int, issue_url: str) -> dict[str, Any]:
    url = str(issue_url or "").strip()
    if not re.fullmatch(r"https://github\.com/[^/\s]+/[^/\s]+/issues/\d+", url):
        raise ValueError("GitHub Issue URL 格式无效")
    db = sqlite_path()
    ensure_llm_tables(db)
    with sqlite3.connect(db) as conn:
        cur = conn.execute(
            "UPDATE llm_reports SET github_issue_url=? WHERE id=?",
            (url, int(report_id)),
        )
        if cur.rowcount <= 0:
            raise ValueError(f"报告不存在: {report_id}")
    return {"ok": True, "reportId": int(report_id), "issueUrl": url}


def recover_stale_llm_jobs(max_age_minutes: int = 30) -> int:
    """把异常退出后遗留的 running 任务转换为可见失败主题。"""
    db = sqlite_path()
    ensure_llm_tables(db)
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT id, group_id, job_type, model, window_start, window_end
            FROM llm_jobs
            WHERE status='running'
              AND created_at < datetime('now', ?)
            ORDER BY id
            """,
            (f"-{max(1, int(max_age_minutes))} minutes",),
        ).fetchall()
    for row in rows:
        record_llm_failure(
            str(row["group_id"]),
            job_type=str(row["job_type"] or "unknown"),
            exc=RuntimeError("LLM worker 异常退出或执行超时"),
            stage="interrupted",
            model=str(row["model"] or ""),
            window_start=int(row["window_start"] or 0),
            window_end=int(row["window_end"] or 0),
            job_id=int(row["id"]),
            log_excerpt="任务长时间停留在 running，未检测到正常完成记录。",
        )
    return len(rows)


def backfill_failed_llm_job_reports() -> int:
    """将旧版本只写入 llm_jobs 的失败记录补成可见主题。"""
    db = sqlite_path()
    ensure_llm_tables(db)
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT j.id, j.group_id, j.job_type, j.model, j.window_start,
                   j.window_end, j.error, j.error_stage
            FROM llm_jobs j
            LEFT JOIN llm_reports r ON r.job_id=j.id
            WHERE j.status='failed' AND r.id IS NULL
            ORDER BY j.id
            """
        ).fetchall()
    for row in rows:
        message = str(row["error"] or "").strip() or "历史 LLM 任务失败（原始错误记录不足）"
        record_llm_failure(
            str(row["group_id"]),
            job_type=str(row["job_type"] or "unknown"),
            exc=RuntimeError(message),
            stage=str(row["error_stage"] or "legacy"),
            model=str(row["model"] or ""),
            window_start=int(row["window_start"] or 0),
            window_end=int(row["window_end"] or 0),
            job_id=int(row["id"]),
            log_excerpt=message,
        )
    return len(rows)


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


def prune_old_llm_reports(keep: int | None = None) -> int:
    """只清理未收藏报告：全局保留最近 keep 条未收藏记录；收藏永久保留。"""
    if keep is None:
        keep = load_app_settings().llm.report_keep_limit
    limit = clamp_report_keep_limit(keep)
    db = sqlite_path()
    ensure_llm_tables(db)
    with sqlite3.connect(db) as conn:
        non_fav = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM llm_reports
                WHERE COALESCE(favorited, 0) = 0
                """
            ).fetchone()[0]
            or 0
        )
        if non_fav <= limit:
            return 0
        to_delete = non_fav - limit
        cur = conn.execute(
            """
            DELETE FROM llm_reports
            WHERE id IN (
              SELECT id FROM llm_reports
              WHERE COALESCE(favorited, 0) = 0
              ORDER BY id ASC
              LIMIT ?
            )
            """,
            (to_delete,),
        )
        deleted = int(cur.rowcount or 0)
        # 顺带清理很久以前的失败/跳过 job（保留最近 2 倍额度）
        job_keep = limit * 2
        job_total = int(conn.execute("SELECT COUNT(*) FROM llm_jobs").fetchone()[0] or 0)
        if job_total > job_keep:
            conn.execute(
                """
                DELETE FROM llm_jobs
                WHERE id IN (
                  SELECT id FROM llm_jobs
                  ORDER BY id ASC
                  LIMIT ?
                )
                """,
                (job_total - job_keep,),
            )
        if deleted:
            logger.info(
                "已清理未收藏 LLM 报告 %s 条，未收藏保留最近 %s 条（收藏不删）",
                deleted,
                limit,
            )
        return deleted


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


_TOKEN_RE = re.compile(r"[A-Za-z0-9_./:@-]{3,}|[\u4e00-\u9fff]{2,}")
_TOKEN_STOPWORDS = frozenset(
    {
        "这个",
        "那个",
        "什么",
        "怎么",
        "为什么",
        "可以",
        "不是",
        "就是",
        "还是",
        "没有",
        "我们",
        "你们",
        "他们",
        "自己",
        "现在",
        "今天",
        "明天",
        "昨天",
        "一个",
        "一下",
        "一样",
        "然后",
        "但是",
        "不过",
        "而且",
        "因为",
        "所以",
        "如果",
        "已经",
        "可能",
        "感觉",
        "知道",
        "觉得",
        "哈哈",
        "哈哈哈",
        "谢谢",
        "收到",
        "好的",
        "嗯嗯",
        "http",
        "https",
        "www",
        "com",
        "the",
        "and",
        "for",
        "with",
        "you",
        "are",
    }
)


def _significant_tokens(text: str) -> set[str]:
    out: set[str] = set()
    for m in _TOKEN_RE.findall(text or ""):
        tok = m.strip("./:@-").lower()
        if len(tok) < 2 or tok in _TOKEN_STOPWORDS:
            continue
        if tok.isdigit():
            continue
        out.add(tok)
    return out


def filter_earlier_related_to_anchor(
    earlier: list[dict[str, Any]],
    anchor: list[dict[str, Any]],
    *,
    bridge_limit: int = 8,
) -> list[dict[str, Any]]:
    """只保留紧挨窗口的接话桥 + 与窗口最新讨论相关的更早消息。"""
    if not earlier:
        return []
    if not anchor:
        return list(earlier[-max(1, bridge_limit) :])

    anchor_ids: set[str] = set()
    anchor_reply_targets: set[str] = set()
    anchor_tokens: set[str] = set()
    for r in anchor:
        mid = r.get("message_id")
        if mid is not None and str(mid).strip():
            anchor_ids.add(str(mid))
        text = _msg_text(r)
        anchor_reply_targets.update(extract_reply_ids(text))
        anchor_tokens |= _significant_tokens(text)

    bridge_n = max(0, int(bridge_limit))
    bridge = list(earlier[-bridge_n:]) if bridge_n else []
    bridge_keys = {_row_key(r) for r in bridge}
    kept: list[dict[str, Any]] = list(bridge)

    for r in earlier:
        if _row_key(r) in bridge_keys:
            continue
        text = _msg_text(r)
        mid = r.get("message_id")
        mid_s = str(mid) if mid is not None else ""
        reply_ids = extract_reply_ids(text)
        related = False
        if mid_s and mid_s in anchor_reply_targets:
            related = True
        elif any(rid in anchor_ids for rid in reply_ids):
            related = True
        else:
            tokens = _significant_tokens(text)
            overlap = tokens & anchor_tokens
            if len(overlap) >= 2:
                related = True
            elif len(overlap) == 1:
                tok = next(iter(overlap))
                if len(tok) >= 5 or detect_focus_topics(tok)["hit"]:
                    related = True
            if not related and detect_focus_topics(text)["hit"] and detect_focus_topics(
                " ".join(sorted(anchor_tokens))
            )["hit"]:
                # 两侧都含深挖主题词时保留（避免漏 GitHub/AI 相关前文）
                if _significant_tokens(text) & anchor_tokens:
                    related = True
        if related:
            kept.append(r)

    return _merge_rows(kept)


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


def fetch_recent_messages_in_window(
    group_id: str,
    start_ts: int,
    end_ts: int,
    limit: int = MANUAL_MAX_MESSAGES,
) -> list[dict[str, Any]]:
    """取时间窗内最新的若干条消息，并按时间正序返回。"""
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
            ORDER BY event_time DESC, id DESC
            LIMIT ?
            """,
            (str(group_id), start_ts, end_ts, max(1, int(limit))),
        ).fetchall()
    return list(reversed([dict(r) for r in rows]))


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
        if not content or "[cq:image" not in content.lower():
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


def rows_contain_cq_images(rows: list[dict[str, Any]]) -> bool:
    return any("[cq:image" in (r.get("content") or "").lower() for r in rows)


def replace_cq_images_with_placeholder(
    content: str, placeholder: str = "[图片]"
) -> str:
    """把 CQ 图片标签替换为短占位，避免把超长 url 塞进文本模型。"""
    text = content or ""
    if "[cq:image" not in text.lower():
        return text
    refs = extract_image_refs(text)
    if not refs:
        return text
    new_c = text
    for ref in refs:
        new_c = new_c.replace(ref["raw"], placeholder, 1)
    return new_c


def apply_image_placeholders(rows: list[dict[str, Any]], placeholder: str = "[图片]") -> int:
    """就地把行内 CQ 图片改为占位符，返回改写条数。"""
    changed = 0
    for r in rows:
        content = r.get("content") or ""
        new_c = replace_cq_images_with_placeholder(content, placeholder)
        if new_c != content:
            r["content"] = new_c
            changed += 1
    return changed


def format_transcript_with_image_placeholders(
    rows: list[dict[str, Any]], max_chars: int = 24000
) -> str:
    """生成把图片换成 [图片] 的 transcript，不修改原 rows。"""
    shadow = [{**r, "content": replace_cq_images_with_placeholder(r.get("content") or "")} for r in rows]
    return format_transcript(shadow, max_chars=max_chars)


async def enrich_rows_with_image_captions(
    rows: list[dict[str, Any]],
    *,
    provider: Any,
    model: str,
    max_images: int = 8,
    message_ids: set[str] | None = None,
) -> dict[str, Any]:
    """
    对本地图片做视觉描述，把 CQ 替换为 [图片描述: …] 供文本模型分析。
    message_ids 非空时只识别这些消息中的图片，其余 CQ 改为 [图片]。
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
    allow_ids = {str(x).strip() for x in (message_ids or set()) if str(x).strip()} or None

    for r in rows:
        content = (r.get("content") or "").strip()
        if not content or "[cq:image" not in content.lower():
            continue
        mid = str(r.get("message_id") or "").strip()
        if allow_ids is not None and mid not in allow_ids:
            r["content"] = replace_cq_images_with_placeholder(content)
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


def _normalize_noun_items(raw: Any) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if isinstance(item, dict):
            term = str(item.get("term") or "").strip()
            meaning = str(item.get("meaning") or "").strip()
            if term or meaning:
                out.append({"term": term, "meaning": meaning})
        elif isinstance(item, str) and item.strip():
            out.append({"term": item.strip(), "meaning": ""})
    return out


def _normalize_link_items(raw: Any) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if isinstance(item, dict):
            url = str(item.get("url") or "").strip()
            summary = str(item.get("summary") or "").strip()
            if url or summary:
                out.append({"url": url, "summary": summary})
        elif isinstance(item, str) and item.strip():
            out.append({"url": item.strip(), "summary": ""})
    return out


def _normalize_note_items(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [str(x).strip() for x in raw if str(x).strip()]


def _normalize_knowledge_items(raw: Any) -> list[dict[str, str]]:
    """规范化 deep_dive.knowledge：LLM 补充的背景知识（非聊天原文）。"""
    out: list[dict[str, str]] = []
    if isinstance(raw, str) and raw.strip():
        return [{"topic": "", "content": raw.strip(), "source": "model_knowledge"}]
    if not isinstance(raw, list):
        return out
    for item in raw:
        if isinstance(item, dict):
            topic = str(
                item.get("topic") or item.get("title") or item.get("term") or ""
            ).strip()
            content = str(
                item.get("content")
                or item.get("claim")
                or item.get("text")
                or item.get("meaning")
                or ""
            ).strip()
            source = str(item.get("source") or "").strip() or "model_knowledge"
            if content or topic:
                out.append({"topic": topic, "content": content, "source": source})
        elif isinstance(item, str) and item.strip():
            out.append(
                {"topic": "", "content": item.strip(), "source": "model_knowledge"}
            )
    return out


def _normalize_deep_dive(raw: Any, *, fallback_topic: str = "") -> dict[str, Any]:
    if isinstance(raw, dict):
        detail = str(raw.get("detail") or "").strip()
        evidence = str(raw.get("evidence") or "").strip()
        if not detail and raw.get("topic"):
            # 兼容旧 deep_dives 项被误塞进来
            detail = str(raw.get("topic") or "").strip()
        knowledge = _normalize_knowledge_items(
            raw.get("knowledge")
            if raw.get("knowledge") is not None
            else raw.get("queried_knowledge")
            if raw.get("queried_knowledge") is not None
            else raw.get("background")
        )
        return {"detail": detail, "evidence": evidence, "knowledge": knowledge}
    if isinstance(raw, str) and raw.strip():
        return {"detail": raw.strip(), "evidence": "", "knowledge": []}
    if fallback_topic:
        return {"detail": "", "evidence": "", "knowledge": []}
    return {"detail": "", "evidence": "", "knowledge": []}


def normalize_key_point(item: Any, *, index: int = 0) -> dict[str, Any]:
    """将要点规范为绑定 deep_dive 的对象结构。"""
    empty_dive: dict[str, Any] = {"detail": "", "evidence": "", "knowledge": []}
    if isinstance(item, str):
        title = item.strip()
        return {
            "title": title or f"要点 {index + 1}",
            "summary": "",
            "deep_dive": dict(empty_dive),
            "nouns": [],
            "links": [],
            "notes": [],
        }
    if not isinstance(item, dict):
        return {
            "title": f"要点 {index + 1}",
            "summary": "",
            "deep_dive": dict(empty_dive),
            "nouns": [],
            "links": [],
            "notes": [],
        }
    title = str(item.get("title") or item.get("point") or "").strip()
    summary = str(item.get("summary") or "").strip()
    if not title and summary:
        title = summary[:40] + ("…" if len(summary) > 40 else "")
    if not title:
        title = f"要点 {index + 1}"
    dive_raw = item.get("deep_dive")
    if dive_raw is None and (item.get("detail") or item.get("evidence") or item.get("knowledge")):
        dive_raw = {
            "detail": item.get("detail"),
            "evidence": item.get("evidence"),
            "knowledge": item.get("knowledge"),
        }
    return {
        "title": title,
        "summary": summary,
        "deep_dive": _normalize_deep_dive(dive_raw),
        "nouns": _normalize_noun_items(item.get("nouns")),
        "links": _normalize_link_items(item.get("links")),
        "notes": _normalize_note_items(item.get("notes")),
    }


def normalize_report_key_points(report: dict[str, Any]) -> dict[str, Any]:
    """规范化 key_points；若仅有顶层 deep_dives，尝试并入对应要点。"""
    raw_points = report.get("key_points")
    points: list[dict[str, Any]] = []
    if isinstance(raw_points, list):
        points = [normalize_key_point(raw_points[i], index=i) for i in range(len(raw_points))]

    deep_dives = report.get("deep_dives")
    if isinstance(deep_dives, list) and deep_dives:
        # 若要点缺少 deep_dive.detail，按顺序把顶层 deep_dives 并入
        for i, d in enumerate(deep_dives):
            if not isinstance(d, dict):
                continue
            dive = _normalize_deep_dive(d)
            topic = str(d.get("topic") or "").strip()
            if i < len(points):
                existing = points[i].get("deep_dive") or {}
                if not str(existing.get("detail") or "").strip():
                    points[i]["deep_dive"] = dive
                if topic and points[i]["title"].startswith("要点 "):
                    points[i]["title"] = topic
            else:
                points.append(
                    {
                        "title": topic or f"深入分析 {i + 1}",
                        "summary": "",
                        "deep_dive": dive,
                        "nouns": [],
                        "links": [],
                        "notes": [],
                    }
                )

    report["key_points"] = points
    if not isinstance(report.get("deep_dives"), list):
        report["deep_dives"] = []
    return report


def normalize_report_payload(report: dict[str, Any]) -> dict[str, Any]:
    """落库前规范化 appendix / key_points 等结构字段。"""
    if not isinstance(report.get("appendix"), dict):
        report["appendix"] = {"nouns": [], "links": [], "notes": []}
    else:
        ap = report["appendix"]
        ap["nouns"] = _normalize_noun_items(ap.get("nouns"))
        ap["links"] = _normalize_link_items(ap.get("links"))
        ap["notes"] = _normalize_note_items(ap.get("notes"))
    normalize_report_key_points(report)
    return report


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
        for i, p in enumerate(points, 1):
            if isinstance(p, dict):
                title = p.get("title") or f"要点 {i}"
                summary = str(p.get("summary") or "").strip()
                lines.append(f"\n### {i}. {title}")
                if summary:
                    lines.append(summary)
                dive = p.get("deep_dive") if isinstance(p.get("deep_dive"), dict) else {}
                detail = str((dive or {}).get("detail") or "").strip()
                evidence = str((dive or {}).get("evidence") or "").strip()
                knowledge = (dive or {}).get("knowledge") or []
                if detail or evidence or knowledge:
                    lines.append("\n**深入分析**")
                if detail:
                    lines.append(f"\n*群内观点*\n{detail}")
                if evidence:
                    lines.append(f"\n> 依据：{evidence}")
                if isinstance(knowledge, list) and knowledge:
                    lines.append("\n*背景知识*")
                    for k in knowledge:
                        if isinstance(k, dict):
                            topic = str(k.get("topic") or "").strip()
                            content = str(k.get("content") or "").strip()
                            source = str(k.get("source") or "").strip()
                            head = f"**{topic}**：" if topic else ""
                            src = f"（{source}）" if source else ""
                            if content or topic:
                                lines.append(f"- {head}{content}{src}".strip())
                        else:
                            lines.append(f"- {k}")
                nouns = p.get("nouns") or []
                if nouns:
                    lines.append("\n**名词剖析**")
                    for n in nouns:
                        if isinstance(n, dict):
                            lines.append(f"- **{n.get('term', '')}**：{n.get('meaning', '')}")
                        else:
                            lines.append(f"- {n}")
                links = p.get("links") or []
                if links:
                    lines.append("\n**相关链接**")
                    for lk in links:
                        if isinstance(lk, dict):
                            url = lk.get("url") or ""
                            summary_l = lk.get("summary") or ""
                            lines.append(f"- {url} — {summary_l}".strip(" —"))
                        else:
                            lines.append(f"- {lk}")
                notes = p.get("notes") or []
                if notes:
                    lines.append("\n**补充说明**")
                    for note in notes:
                        lines.append(f"- {note}")
            else:
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

    # 兼容旧报告：顶层 deep_dives 仍渲染（新报告通常已并入要点）
    deep_dives = report.get("deep_dives") or []
    orphan_dives = []
    if isinstance(deep_dives, list):
        # 仅当要点里没有对应深入分析内容时才单独展示，避免重复
        has_bound = any(
            isinstance(p, dict)
            and isinstance(p.get("deep_dive"), dict)
            and str((p.get("deep_dive") or {}).get("detail") or "").strip()
            for p in points
        )
        if not has_bound:
            orphan_dives = deep_dives
    if orphan_dives:
        lines.append("\n## 深入分析")
        for i, d in enumerate(orphan_dives, 1):
            if isinstance(d, dict):
                topic = d.get("topic") or f"主题 {i}"
                detail = d.get("detail") or ""
                evidence = d.get("evidence") or ""
                lines.append(f"\n### {i}. {topic}")
                if detail:
                    lines.append(detail)
                if evidence:
                    lines.append(f"\n> 依据：{evidence}")
            else:
                lines.append(f"- {d}")

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
                lines.append("\n### 名词剖析")
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
    prune_old_llm_reports()


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
    use_global_defaults = bool(getattr(llm_cfg, "use_global_defaults", True))
    text_enabled = True if use_global_defaults else bool(getattr(llm_cfg, "text_enabled", True))
    image_enabled = True if use_global_defaults else bool(getattr(llm_cfg, "image_enabled", True))
    image_same = False if use_global_defaults else bool(
        getattr(llm_cfg, "image_same_as_text", True)
    )
    provider_id = (
        settings.llm.active_provider_id
        if use_global_defaults
        else llm_cfg.provider_id or settings.llm.active_provider_id
    )
    provider = provider_by_id(settings, provider_id)
    if provider is None:
        raise RuntimeError("未配置 LLM Provider，请先在总配置中填写")
    model_name = (
        provider.default_model
        if use_global_defaults
        else llm_cfg.model or provider.default_model
    )
    analysis_prompt = (
        settings.llm.default_prompt
        if use_global_defaults
        else llm_cfg.prompt
    ) or DEFAULT_LLM_MONITOR_PROMPT
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
    if use_global_defaults:
        image_provider = provider
        image_model_name = settings.llm.default_image_model or model_name
    elif image_same:
        image_provider = provider
        image_model_name = model_name
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
    _, configured_window, configured_min_messages = resolve_llm_timing(
        settings.llm, llm_cfg
    )
    minutes = window_minutes if window_minutes is not None else configured_window
    minutes = max(1, int(minutes))
    start = end - minutes * 60

    db = sqlite_path()
    ensure_llm_tables(db)

    if job_type == "manual":
        rows = fetch_recent_messages_in_window(
            group_id,
            start,
            end,
            limit=MANUAL_MAX_MESSAGES + 1,
        )
    else:
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
        rows = fetch_recent_messages(group_id, limit=MANUAL_MAX_MESSAGES + 1)
        if rows:
            source = f"时间窗 {minutes} 分钟无消息，已回退最近 {len(rows)} 条"
            start = int(rows[0].get("event_time") or start)
            end = int(rows[-1].get("event_time") or end)
            configured_start = start

    if rows and job_type == "manual":
        rows, dropped = limit_recent_rows(rows)
        start = int(rows[0].get("event_time") or start)
        end = int(rows[-1].get("event_time") or end)
        if dropped:
            source = (
                f"{source}；为避免超过上下文，仅分析最近 {len(rows)} 条"
            )
        elif len(format_transcript(rows, max_chars=MANUAL_TRANSCRIPT_MAX_CHARS)) >= (
            MANUAL_TRANSCRIPT_MAX_CHARS
        ):
            source = f"{source}；已按上下文预算裁剪"

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

    if rows and job_type == "manual":
        rows, dropped = limit_recent_rows(rows)
        start = int(rows[0].get("event_time") or start)
        end = int(rows[-1].get("event_time") or end)
        if dropped:
            source = f"{source}；引用补全后再次按预算保留最近 {len(rows)} 条"

    # 启发式向前接话补全（小幅），正式大规模向前补文由下方 LLM 多轮驱动
    if rows and job_type != "manual":
        rows, start, end, look_meta = extend_messages_with_context(
            group_id,
            rows,
            window_start=start,
            window_end=end,
            configured_start=configured_start,
            max_rounds=2,
            batch_size=20,
            max_extra_messages=40,
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

    min_need = max(1, int(configured_min_messages))
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
                (job_type, group_id, start, end, "skipped", reason, model_name),
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
                (job_type, group_id, start, end, "skipped", reason, model_name),
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

    analysis_instructions = build_analysis_instructions(analysis_prompt)
    configured_end = int(context_meta.get("configured_end") or end)
    # 总时间跨度上限 = 配置窗口 × 5
    min_allowed_ts = max(0, configured_end - minutes * 60 * MAX_WINDOW_MULTIPLIER)
    history: list[dict[str, str]] = []
    llm_context_rounds = 0
    earlier_added_total = 0
    earlier_reasons: list[str] = []
    focus_forced_rounds = 0  # 检测到 GitHub/AI 时至少再向前补几轮
    context_round_limit = 0 if job_type == "manual" else MAX_LLM_CONTEXT_ROUNDS
    # 多轮补文锚点：只围绕进入审查时的「最新窗口讨论」相关内容向前补
    anchor_rows = [dict(r) for r in rows]
    last_round_new_rows: list[dict[str, Any]] = []

    def _build_meta_block() -> str:
        block = (
            f"群号: {group_id}\n"
            f"群名: {cfg.group_name or '-'}\n"
            f"数据来源: {source}\n"
            f"配置时间窗: {datetime.fromtimestamp(configured_start)} ~ {datetime.fromtimestamp(configured_end)}\n"
            f"当前实际范围: {datetime.fromtimestamp(start)} ~ {datetime.fromtimestamp(end)}\n"
            f"消息数: {len(rows)}\n"
            f"已向前补轮次: {llm_context_rounds}/{context_round_limit}\n"
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

    # —— LLM 多轮：先判断是否完整，不完整则再向前取「与最新讨论相关」的记录 ——
    for round_i in range(1, context_round_limit + 1):
        focus_src = (
            format_transcript(anchor_rows, max_chars=8000)
            if round_i > 1
            else format_transcript(rows)
        )
        focus = detect_focus_topics(focus_src)
        if focus["hit"] and focus_forced_rounds == 0:
            # 命中深挖主题：最多再强制向前审查/补文 1 轮（在上限内）
            focus_forced_rounds = min(1, MAX_LLM_CONTEXT_ROUNDS - round_i + 1)
            source = f"{source}；检测到{'、'.join(focus['labels'])}，将多轮深挖"
        focus_hint = ""
        if focus["hit"]:
            focus_hint = (
                f"当前片段已检测到：{'、'.join(focus['labels'])}。"
                "若相关讨论可能跨越多条消息，可 need_earlier=true，"
                "但只补该主题相关前文，suggested_earlier_count 建议 12~20。\n"
            )
        elif focus_forced_rounds > 0:
            focus_hint = (
                "仍在深挖主题补文阶段：仅在相关前文仍缺口时 need_earlier=true，"
                "勿补无关闲聊。\n"
            )

        if round_i == 1 or not last_round_new_rows:
            transcript = format_transcript(rows)
            check_user = (
                "请审查下列群聊是否完整、是否还需要与「窗口内最新讨论」直接相关的更早前文。"
                "只补相关缺口；无关内容不要继续向前取。"
                "若需要，请设置 need_earlier=true；若已可分析，enough=true。"
                "只输出审查 JSON。\n\n"
                f"{analysis_instructions}\n\n"
                f"{focus_hint}"
                f"{_build_meta_block()}\n聊天记录:\n{transcript}"
            )
        else:
            # 第 2 轮起：只审查本轮新补入内容，避免把全部历史反复塞进会话
            anchor_digest = format_transcript(anchor_rows, max_chars=6000)
            new_transcript = format_transcript(last_round_new_rows, max_chars=8000)
            check_user = (
                "以下是上一轮新补入的前文。请判断：相对「窗口内最新讨论」，"
                "相关上下文是否已够；只要求继续补与该讨论直接相关的缺口。"
                "不要重复索要已持有内容；无关闲聊不必再取。"
                "若已可分析，enough=true。只输出审查 JSON。\n\n"
                f"{analysis_instructions}\n\n"
                f"{focus_hint}"
                f"{_build_meta_block()}\n"
                f"【窗口内最新讨论（已持有，勿重复补）】\n{anchor_digest}\n\n"
                f"【本轮新补入前文】\n{new_transcript}"
            )
        try:
            check_raw, check_usage = await chat_complete(
                provider,
                model=model_name,
                system=CONTEXT_CHECK_SYSTEM,
                user=check_user,
                temperature=0.1,
                timeout_sec=90,
                force_json=True,
                history=history or None,
                retries=1,
            )
            tokens.add(check_usage)
            check = extract_json_object(check_raw)
        except Exception:
            logger.exception(
                "上下文审查第 %s 轮失败 group=%s，停止继续向前补", round_i, group_id
            )
            break

        # history 只保留精简结论，避免多轮把完整聊天记录反复累加
        history.extend(
            [
                {
                    "role": "user",
                    "content": (
                        f"第{round_i}轮上下文审查：当前持有 {len(rows)} 条，"
                        f"本轮审查新增前文 {len(last_round_new_rows)} 条。"
                        "请判断是否还需与最新讨论相关的前文。"
                    ),
                },
                {"role": "assistant", "content": check_raw},
            ]
        )
        if len(history) > 6:
            history = history[-6:]

        enough = bool(check.get("enough"))
        need_earlier = bool(check.get("need_earlier")) and not enough
        # 深挖主题：在强制轮次内，即使模型说 enough，也继续向前取一轮
        if focus_forced_rounds > 0 and not need_earlier:
            need_earlier = True
            enough = False
            earlier_reasons.append("深挖主题强制向前补文")
        need_ids = []
        if isinstance(check.get("need_reply_ids"), list):
            need_ids = [str(x) for x in check["need_reply_ids"] if str(x).strip()]

        if enough and not need_ids and focus_forced_rounds <= 0:
            break

        # 达到最早时间边界则停止
        first_ts = _msg_ts(rows[0]) if rows else start
        if first_ts <= min_allowed_ts and not need_ids:
            earlier_reasons.append("已达配置窗口×5 的最早时间边界")
            break

        if not need_earlier and not need_ids:
            break

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

        if need_earlier or (not enough and not need_ids):
            suggest = check.get("suggested_earlier_count")
            try:
                take = int(suggest) if suggest is not None else 15
            except Exception:
                take = 15
            if focus["hit"] or focus_forced_rounds > 0:
                take = max(take, 15)
            take = max(8, min(25, take))
            # 多取一点再按相关性过滤，避免盲取整段无关闲聊
            fetch_n = min(40, take * 2)
            earlier = fetch_messages_before(group_id, first_ts, limit=fetch_n)
            kept = []
            for r in earlier:
                ts = _msg_ts(r)
                if ts and ts < min_allowed_ts:
                    continue
                kept.append(_mark_context_role(r, "before"))
            kept = filter_earlier_related_to_anchor(
                kept,
                anchor_rows,
                bridge_limit=min(8, take),
            )
            # 相关性过滤后仍可能偏多，按建议条数从近到远截断
            if len(kept) > take:
                kept = kept[-take:]
            if kept:
                batches.append(kept)

        new_rows = _merge_rows(*batches)
        if len(new_rows) <= len(rows):
            earlier_reasons.append(check.get("reason") or "未取到更多前文，结束补上下文")
            break

        # 仅记录本轮真正新增的消息，供下一轮增量审查
        prev_keys = {_row_key(r) for r in rows}
        last_round_new_rows = [r for r in new_rows if _row_key(r) not in prev_keys]
        rows = new_rows
        start = min(start, _msg_ts(rows[0]) or start)
        end = max(end, _msg_ts(rows[-1]) or end)
        llm_context_rounds = round_i
        earlier_added_total += len(last_round_new_rows)
        reason = (check.get("reason") or "模型判断需要更早前文").strip()
        earlier_reasons.append(
            f"第{round_i}轮：{reason}（+{len(last_round_new_rows)}）"
        )
        context_meta["window_extended"] = True
        context_meta["lookback_messages"] = int(
            context_meta.get("lookback_messages") or 0
        ) + len(last_round_new_rows)
        context_meta.setdefault("lookback_reasons", []).append(reason)
        source = f"{source}；第{round_i}轮向前补了 {len(last_round_new_rows)} 条"
        if focus_forced_rounds > 0:
            focus_forced_rounds -= 1

        if enough and focus_forced_rounds <= 0:
            # 只为补引用，本轮后结束
            break

    # 正式分析前：补本地化（含多轮新增消息）+ 按需视觉描述
    try:
        await ensure_rows_images_local(rows, group_id)
    except Exception:
        logger.exception("正式分析前图片本地化失败 group=%s", group_id)
    image_meta: dict[str, Any] = {
        "captioned": 0,
        "skipped": 0,
        "captions": [],
        "need_images": None,
        "gate_reason": "",
    }
    if image_enabled:
        if rows_contain_cq_images(rows):
            gate_transcript = format_transcript_with_image_placeholders(
                rows,
                max_chars=(
                    MANUAL_TRANSCRIPT_MAX_CHARS if job_type == "manual" else 24000
                ),
            )
            gate_user = (
                "请根据下列群聊文字判断：是否需要视觉识别其中「[图片]」的内容，"
                "才能完成主题/风险/要点分析。只输出审查 JSON。\n\n"
                f"{analysis_instructions}\n\n"
                f"{_build_meta_block()}\n聊天记录:\n{gate_transcript}"
            )
            need_images = False
            gate_ids: list[str] = []
            gate_reason = ""
            try:
                gate_raw, gate_usage = await chat_complete(
                    provider,
                    model=model_name,
                    system=IMAGE_NEED_CHECK_SYSTEM,
                    user=gate_user,
                    temperature=0.1,
                    timeout_sec=90,
                    force_json=True,
                    retries=1,
                )
                tokens.add(gate_usage)
                gate = extract_json_object(gate_raw)
                need_images = bool(gate.get("need_images"))
                gate_reason = str(gate.get("reason") or "").strip()
                if isinstance(gate.get("image_message_ids"), list):
                    gate_ids = [
                        str(x).strip()
                        for x in gate["image_message_ids"]
                        if str(x).strip()
                    ]
            except Exception:
                # 门控失败时回退为识别（避免静默丢掉关键图证）
                logger.exception(
                    "图片必要性审查失败 group=%s，回退为识别窗口内图片", group_id
                )
                need_images = True
                gate_reason = "图片必要性审查失败，回退识别"

            image_meta["need_images"] = need_images
            image_meta["gate_reason"] = gate_reason

            if need_images:
                try:
                    id_filter = set(gate_ids) if gate_ids else None
                    image_meta = await enrich_rows_with_image_captions(
                        rows,
                        provider=image_provider,
                        model=image_model_name,
                        max_images=MANUAL_MAX_IMAGES if job_type == "manual" else 8,
                        message_ids=id_filter,
                    )
                    image_meta["need_images"] = True
                    image_meta["gate_reason"] = gate_reason
                    img_tu = image_meta.get("token_usage")
                    if isinstance(img_tu, dict):
                        tokens.add(
                            TokenUsage(
                                prompt_tokens=int(img_tu.get("prompt_tokens") or 0),
                                completion_tokens=int(
                                    img_tu.get("completion_tokens") or 0
                                ),
                                total_tokens=int(img_tu.get("total_tokens") or 0),
                            )
                        )
                    if image_meta.get("captioned"):
                        source = (
                            f"{source}；LLM判定需识图，"
                            f"已视觉识别 {image_meta['captioned']} 张"
                        )
                    else:
                        source = f"{source}；LLM判定需识图，但未能成功识别图片"
                    if gate_reason:
                        source = f"{source}（{gate_reason}）"
                except Exception:
                    logger.exception("图片视觉描述失败 group=%s", group_id)
                    apply_image_placeholders(rows)
                    source = f"{source}；图片识别失败，已用占位替换"
            else:
                apply_image_placeholders(rows)
                reason_s = f"（{gate_reason}）" if gate_reason else ""
                source = f"{source}；LLM判定无需识图，已跳过视觉识别{reason_s}"
        else:
            image_meta["need_images"] = False
            image_meta["gate_reason"] = "窗口内无图片"
    else:
        # 未启用图片分析：把 CQ 图片改成占位，避免把超长 url 塞进文本模型
        apply_image_placeholders(rows)
        source = f"{source}；图片分析已关闭"

    transcript = format_transcript(
        rows,
        max_chars=(
            MANUAL_TRANSCRIPT_MAX_CHARS if job_type == "manual" else 24000
        ),
    )
    base_meta = _build_meta_block()
    if llm_context_rounds or earlier_added_total:
        base_meta += (
            "说明: 已按多轮审查补充与窗口内最新讨论相关的更早聊天记录（最多 "
            f"{MAX_LLM_CONTEXT_ROUNDS} 轮，时间跨度不超过配置窗口×{MAX_WINDOW_MULTIPLIER}）；"
            "正式分析时必须在 context_usage 中标明引用了更早内容。\n"
        )
    if image_meta.get("captioned") or image_meta.get("skipped"):
        base_meta += (
            f"图片识别: 成功 {image_meta.get('captioned') or 0} 张，"
            f"跳过/失败 {image_meta.get('skipped') or 0} 张；"
            "请将「[图片描述: …]」纳入主题与风险分析。\n"
        )
    elif image_meta.get("need_images") is False and image_meta.get("gate_reason"):
        base_meta += (
            f"图片识别: 已跳过（LLM 判定文字足以分析；"
            f"{image_meta.get('gate_reason')}）。\n"
        )
    elif image_meta.get("need_images") is False:
        base_meta += "图片识别: 已跳过（LLM 判定无需识别图片内容）。\n"

    user_prompt = (
        f"{analysis_instructions}\n\n"
        f"{base_meta}\n"
        "请输出完整分析 JSON（必须含 key_points、appendix、context_usage）。\n"
        "key_points 必须是对象数组：每项含 title/summary/deep_dive/nouns/links/notes，"
        "deep_dive 含 detail（群内观点）、evidence（原文）、knowledge（[{topic,content,source}] 背景知识）；"
        "深入分析写在对应要点的 deep_dive 内，不要单独拆开；名词剖析写入 nouns。\n"
        "若检测到 GitHub/AI 主题或自定义要求深挖：至少产出 1～3 个带长文 deep_dive 的要点，"
        "相关 links/nouns/knowledge 写在对应要点内，整体篇幅明显长于普通摘要。\n"
        f"\n聊天记录:\n{transcript}"
    )

    final_focus = detect_focus_topics(transcript)
    final_max_tokens = (
        4096
        if job_type == "manual"
        else (8192 if final_focus["hit"] or analysis_prompt.strip() else 4096)
    )
    analysis_system = (
        DEFAULT_SYSTEM
        + "\n以下为本群分析要求，正式输出时必须落实：\n"
        + analysis_instructions
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
            system=analysis_system,
            user=user_prompt,
            history=None,  # 正式分析只用合并后的完整记录，不重复带入多轮审查全文
            timeout_sec=300,
            max_tokens=final_max_tokens,
            retries=2,
        )
        tokens.add(final_usage)
        report = extract_json_object(raw)
        normalize_report_payload(report)

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
            "images_need_check": image_meta.get("need_images"),
            "images_gate_reason": str(image_meta.get("gate_reason") or ""),
            "focus_topics": final_focus.get("labels") or [],
            "custom_prompt_applied": bool(analysis_prompt.strip()),
            "used_global_defaults": use_global_defaults,
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
            est = "（估算）" if token_usage.get("estimated") else ""
            notes_head.append(
                f"> Token 消耗{est}：合计 {token_usage['total_tokens']}"
                f"（输入 {token_usage['prompt_tokens']} / 输出 {token_usage['completion_tokens']}）"
            )
        logger.info(
            "LLM 报告 tokens group=%s total=%s estimated=%s",
            group_id,
            token_usage.get("total_tokens"),
            bool(token_usage.get("estimated")),
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
        prune_old_llm_reports()
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
        record_llm_failure(
            group_id,
            job_type=job_type,
            exc=e,
            stage="final_analysis",
            model=model_name,
            window_start=start,
            window_end=end,
            job_id=job_id,
        )
        raise



def list_reports(
    group_id: str | None = None,
    limit: int = 30,
    *,
    favorites_only: bool = False,
) -> list[dict[str, Any]]:
    db = sqlite_path()
    ensure_llm_tables(db)
    recover_stale_llm_jobs()
    backfill_failed_llm_job_reports()
    lim = max(1, min(1000, int(limit)))
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        if favorites_only:
            rows = conn.execute(
                """
                SELECT id, job_id, group_id, window_start, window_end, headline, sentiment,
                       risk_max, msg_count, created_at, report_md, report_json,
                       COALESCE(github_issue_url, '') AS github_issue_url,
                       COALESCE(prompt_tokens, 0) AS prompt_tokens,
                       COALESCE(completion_tokens, 0) AS completion_tokens,
                       COALESCE(total_tokens, 0) AS total_tokens,
                       COALESCE(favorited, 0) AS favorited,
                       favorited_at,
                       CASE
                         WHEN favorite_messages_json IS NOT NULL
                          AND length(favorite_messages_json) > 2
                         THEN 1 ELSE 0
                       END AS has_favorite_messages
                FROM llm_reports
                WHERE COALESCE(favorited, 0) = 1
                ORDER BY COALESCE(favorited_at, created_at) DESC, id DESC
                LIMIT ?
                """,
                (lim,),
            ).fetchall()
        elif group_id:
            rows = conn.execute(
                """
                SELECT id, job_id, group_id, window_start, window_end, headline, sentiment,
                       risk_max, msg_count, created_at, report_md, report_json,
                       COALESCE(github_issue_url, '') AS github_issue_url,
                       COALESCE(prompt_tokens, 0) AS prompt_tokens,
                       COALESCE(completion_tokens, 0) AS completion_tokens,
                       COALESCE(total_tokens, 0) AS total_tokens,
                       COALESCE(favorited, 0) AS favorited,
                       favorited_at,
                       CASE
                         WHEN favorite_messages_json IS NOT NULL
                          AND length(favorite_messages_json) > 2
                         THEN 1 ELSE 0
                       END AS has_favorite_messages
                FROM llm_reports WHERE group_id=?
                ORDER BY id DESC LIMIT ?
                """,
                (str(group_id), lim),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT id, job_id, group_id, window_start, window_end, headline, sentiment,
                       risk_max, msg_count, created_at, report_md, report_json,
                       COALESCE(github_issue_url, '') AS github_issue_url,
                       COALESCE(prompt_tokens, 0) AS prompt_tokens,
                       COALESCE(completion_tokens, 0) AS completion_tokens,
                       COALESCE(total_tokens, 0) AS total_tokens,
                       COALESCE(favorited, 0) AS favorited,
                       favorited_at,
                       CASE
                         WHEN favorite_messages_json IS NOT NULL
                          AND length(favorite_messages_json) > 2
                         THEN 1 ELSE 0
                       END AS has_favorite_messages
                FROM llm_reports
                ORDER BY id DESC LIMIT ?
                """,
                (lim,),
            ).fetchall()
    return [dict(r) for r in rows]


def structured_report_for_api(payload: dict[str, Any]) -> dict[str, Any]:
    """前端展示用的精简结构化字段（已规范化 key_points）。"""
    if not isinstance(payload, dict):
        return {}
    data = json.loads(json.dumps(payload, ensure_ascii=False))
    if not isinstance(data, dict):
        return {}
    normalize_report_payload(data)
    return {
        "headline": data.get("headline") or "",
        "sentiment": data.get("sentiment") or "",
        "topics": data.get("topics") if isinstance(data.get("topics"), list) else [],
        "keyPoints": data.get("key_points") if isinstance(data.get("key_points"), list) else [],
        "risks": data.get("risks") if isinstance(data.get("risks"), list) else [],
        "actionItems": data.get("action_items") if isinstance(data.get("action_items"), list) else [],
        "notableUsers": data.get("notable_users") if isinstance(data.get("notable_users"), list) else [],
        "appendix": data.get("appendix") if isinstance(data.get("appendix"), dict) else {},
        "contextUsage": data.get("context_usage") if isinstance(data.get("context_usage"), dict) else {},
        "failed": bool(data.get("failed")),
        "skipped": bool(data.get("skipped")),
    }


def has_structured_key_points(payload: dict[str, Any]) -> bool:
    """是否具备可卡片化展示的要点。

    - 新报告：key_points 已是对象 → True
    - 旧报告：纯字符串要点 → False（继续用 Markdown）
    - 旧报告仅有顶层 deep_dives：规范化并入要点后若有深入内容 → True
    """
    if not isinstance(payload, dict):
        return False
    raw_points = payload.get("key_points")
    raw_has_object = isinstance(raw_points, list) and any(
        isinstance(p, dict) for p in raw_points
    )
    data = json.loads(json.dumps(payload, ensure_ascii=False))
    if not isinstance(data, dict):
        return False
    normalize_report_payload(data)
    points = data.get("key_points") or []
    if not isinstance(points, list) or not points:
        return False
    if raw_has_object:
        return True
    return any(
        isinstance(p, dict)
        and (
            str(p.get("summary") or "").strip()
            or str(((p.get("deep_dive") or {}) if isinstance(p.get("deep_dive"), dict) else {}).get("detail") or "").strip()
            or p.get("nouns")
            or p.get("links")
            or p.get("notes")
        )
        for p in points
    )


ASK_REPORT_SYSTEM = (
    "你是群聊分析追问助手。综合两路信息作答："
    "（1）用户提供的报告/群聊摘录；（2）你自身的通用知识与概念解释。"
    "禁止伪造「群里说过但摘录中没有」的聊天原文或发言人；"
    "摘录不足时不要只回「记录不足」，应改用模型知识回答概念/背景问题，并标明来源。"
    "回答必须用中文，结构清晰，并按来源分段标注，建议格式：\n"
    "【来自群聊/报告】……（仅写摘录能支撑的内容；没有就写「摘录未直接说明」）\n"
    "【来自模型知识】……（概念定义、背景、常见说法；不确定处标明「依据模型知识，可能不完整」）\n"
    "若两路可互相印证，可在末尾用一两句做综合判断。"
    "不要复述整份报告；不要输出 JSON。"
)


async def ask_about_report(
    report_id: int,
    question: str,
    *,
    selection: str = "",
    point_index: int | None = None,
) -> dict[str, Any]:
    """基于已有报告 + 模型知识做单轮追问，返回带来源标注的纯文本答案。"""
    q = (question or "").strip()
    if not q:
        raise ValueError("问题不能为空")
    db = sqlite_path()
    ensure_llm_tables(db)
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT id, group_id, headline, report_json
            FROM llm_reports WHERE id=?
            """,
            (int(report_id),),
        ).fetchone()
    if not row:
        raise ValueError("报告不存在")

    payload: dict[str, Any] = {}
    raw_json = row["report_json"] or ""
    if raw_json:
        try:
            parsed = json.loads(raw_json)
            if isinstance(parsed, dict):
                payload = parsed
        except Exception:
            payload = {}
    normalize_report_payload(payload)

    group_id = str(row["group_id"])
    cfg = load_group_config(group_id)
    settings = load_app_settings()
    llm_cfg = cfg.llm_monitor
    use_global_defaults = bool(getattr(llm_cfg, "use_global_defaults", True))
    provider_id = (
        settings.llm.active_provider_id
        if use_global_defaults
        else llm_cfg.provider_id or settings.llm.active_provider_id
    )
    provider = provider_by_id(settings, provider_id)
    if provider is None:
        raise RuntimeError("未配置 LLM Provider，请先在总配置中填写")
    model_name = (
        provider.default_model
        if use_global_defaults
        else llm_cfg.model or provider.default_model
    )

    context_parts: list[str] = [
        f"报告标题：{payload.get('headline') or row['headline'] or ''}",
    ]
    topics = payload.get("topics") or []
    if topics:
        context_parts.append(
            "主题：\n" + json.dumps(topics[:8], ensure_ascii=False)
        )
    points = payload.get("key_points") or []
    sel = (selection or "").strip()
    focused_point: dict[str, Any] | None = None
    if point_index is not None and isinstance(points, list) and 0 <= int(point_index) < len(points):
        raw_point = points[int(point_index)]
        if isinstance(raw_point, dict):
            focused_point = raw_point
            context_parts.append(
                "聚焦要点：\n" + json.dumps(raw_point, ensure_ascii=False)
            )
    if sel:
        context_parts.append(f"用户选中片段：\n{sel[:2000]}")
        # 选中短词时，尽量附上匹配的要点/名词，便于结合群聊语境
        if focused_point is None and isinstance(points, list) and len(sel) <= 80:
            related: list[dict[str, Any]] = []
            needle = sel.lower()
            for i, p in enumerate(points[:20]):
                if not isinstance(p, dict):
                    continue
                blob = json.dumps(p, ensure_ascii=False).lower()
                if needle and needle in blob:
                    dive = p.get("deep_dive") if isinstance(p.get("deep_dive"), dict) else {}
                    related.append(
                        {
                            "index": i,
                            "title": p.get("title"),
                            "summary": p.get("summary"),
                            "deep_dive_excerpt": str((dive or {}).get("detail") or "")[:400],
                            "knowledge": (dive or {}).get("knowledge") or [],
                            "nouns": p.get("nouns") or [],
                            "evidence": str((dive or {}).get("evidence") or "")[:300],
                        }
                    )
                if len(related) >= 3:
                    break
            if related:
                context_parts.append(
                    "与选中片段相关的要点：\n" + json.dumps(related, ensure_ascii=False)
                )
    if focused_point is None and not sel:
        brief = []
        for i, p in enumerate(points[:12] if isinstance(points, list) else []):
            if not isinstance(p, dict):
                brief.append({"index": i, "title": str(p)})
                continue
            dive = p.get("deep_dive") if isinstance(p.get("deep_dive"), dict) else {}
            brief.append(
                {
                    "index": i,
                    "title": p.get("title"),
                    "summary": p.get("summary"),
                    "deep_dive_excerpt": str((dive or {}).get("detail") or "")[:400],
                    "knowledge": ((dive or {}).get("knowledge") or [])[:3],
                    "nouns": (p.get("nouns") or [])[:5],
                    "evidence": str((dive or {}).get("evidence") or "")[:300],
                }
            )
        context_parts.append("要点摘要：\n" + json.dumps(brief, ensure_ascii=False))

    appendix = payload.get("appendix") if isinstance(payload.get("appendix"), dict) else {}
    if appendix.get("nouns") or appendix.get("notes"):
        context_parts.append(
            "全局附录：\n"
            + json.dumps(
                {
                    "nouns": (appendix.get("nouns") or [])[:12],
                    "notes": (appendix.get("notes") or [])[:8],
                },
                ensure_ascii=False,
            )
        )

    user_prompt = (
        "下面是可供参考的群聊/报告摘录（可能不完整）：\n\n"
        + "\n\n".join(context_parts)
        + "\n\n用户问题：\n"
        + q
        + "\n\n请综合摘录与模型知识回答；必须分段标注【来自群聊/报告】与【来自模型知识】。"
        "不要只回答「记录不足」。"
    )
    answer, usage = await chat_complete(
        provider,
        model=model_name,
        system=ASK_REPORT_SYSTEM,
        user=user_prompt,
        timeout_sec=90,
        max_tokens=1600,
        force_json=False,
    )
    text = (answer or "").strip()
    return {
        "ok": True,
        "answer": text,
        "model": model_name,
        "tokenUsage": usage.as_dict() if usage else {},
    }
