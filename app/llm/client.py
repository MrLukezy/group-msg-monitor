"""LLM Provider 抽象：OpenAI Compatible / OpenCode / Cursor SDK。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any

import aiohttp

from app.settings_store import LlmProvider

logger = logging.getLogger(__name__)


@dataclass
class TokenUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    estimated: bool = False

    def add(self, other: "TokenUsage | None") -> "TokenUsage":
        if not other:
            return self
        self.prompt_tokens += int(other.prompt_tokens or 0)
        self.completion_tokens += int(other.completion_tokens or 0)
        self.total_tokens += int(other.total_tokens or 0)
        if self.total_tokens <= 0:
            self.total_tokens = self.prompt_tokens + self.completion_tokens
        if other.estimated:
            self.estimated = True
        return self

    def as_dict(self) -> dict[str, Any]:
        total = int(self.total_tokens or 0)
        if total <= 0:
            total = int(self.prompt_tokens or 0) + int(self.completion_tokens or 0)
        out: dict[str, Any] = {
            "prompt_tokens": int(self.prompt_tokens or 0),
            "completion_tokens": int(self.completion_tokens or 0),
            "total_tokens": total,
        }
        if self.estimated:
            out["estimated"] = True
        return out


def estimate_token_count(text: str) -> int:
    """无官方 usage 时的粗估：中文约 1.6 字/token，英文约 4 字符/token。"""
    if not text:
        return 0
    cjk = 0
    other = 0
    for ch in text:
        o = ord(ch)
        if (
            0x4E00 <= o <= 0x9FFF
            or 0x3400 <= o <= 0x4DBF
            or 0xF900 <= o <= 0xFAFF
            or 0x3000 <= o <= 0x303F
        ):
            cjk += 1
        elif not ch.isspace():
            other += 1
    return max(1, int(cjk / 1.6 + other / 4.0 + 0.999))


def parse_usage_payload(data: Any) -> TokenUsage:
    """从各类 Provider 响应里抽出 usage（兼容多种网关字段）。"""
    if not isinstance(data, dict):
        return TokenUsage()

    candidates: list[dict[str, Any]] = []

    def _push(obj: Any) -> None:
        if isinstance(obj, dict):
            candidates.append(obj)

    for key in ("usage", "token_usage", "tokenUsage", "tokens"):
        _push(data.get(key))
    for nest_key in ("meta", "data", "result", "output"):
        nest = data.get(nest_key)
        if isinstance(nest, dict):
            for key in ("usage", "token_usage", "tokenUsage", "tokens"):
                _push(nest.get(key))
    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        c0 = choices[0]
        if isinstance(c0, dict):
            _push(c0.get("usage"))

    # 有些响应把 usage 平铺在根上
    if any(
        k in data
        for k in (
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "input_tokens",
            "output_tokens",
        )
    ):
        candidates.append(data)

    best = TokenUsage()
    for usage in candidates:
        prompt = int(
            usage.get("prompt_tokens")
            or usage.get("input_tokens")
            or usage.get("promptTokens")
            or usage.get("inputTokens")
            or 0
        )
        completion = int(
            usage.get("completion_tokens")
            or usage.get("output_tokens")
            or usage.get("completionTokens")
            or usage.get("outputTokens")
            or 0
        )
        total = int(
            usage.get("total_tokens")
            or usage.get("totalTokens")
            or usage.get("total")
            or 0
        )
        if total <= 0:
            total = prompt + completion
        if total > best.total_tokens:
            best = TokenUsage(
                prompt_tokens=prompt,
                completion_tokens=completion,
                total_tokens=total,
            )
    return best


def usage_or_estimate(
    usage: TokenUsage,
    *,
    system: str = "",
    user: str = "",
    content: str = "",
    history: list[dict[str, str]] | None = None,
) -> TokenUsage:
    """官方 usage 缺失或为 0 时，用文本长度粗估，保证统计可用。"""
    if usage.total_tokens > 0 or usage.prompt_tokens > 0 or usage.completion_tokens > 0:
        if usage.total_tokens <= 0:
            usage.total_tokens = usage.prompt_tokens + usage.completion_tokens
        return usage
    prompt_parts = [system or "", user or ""]
    for h in history or []:
        prompt_parts.append(h.get("content") or "")
    prompt_est = estimate_token_count("\n".join(prompt_parts))
    completion_est = estimate_token_count(content or "")
    return TokenUsage(
        prompt_tokens=prompt_est,
        completion_tokens=completion_est,
        total_tokens=prompt_est + completion_est,
        estimated=True,
    )


def normalize_openai_compatible_base(base_url: str) -> str:
    """归一化 OpenAI Compatible Base URL，尽量落到 .../v1。"""
    base = (base_url or "https://api.openai.com/v1").strip().rstrip("/")
    for suffix in (
        "/chat/completions",
        "/completions",
        "/models",
        "/responses",
    ):
        if base.endswith(suffix):
            base = base[: -len(suffix)].rstrip("/")
    if not base.endswith("/v1"):
        base = f"{base}/v1"
    return base


def openai_models_endpoints(base_url: str) -> list[str]:
    """生成 /models 候选地址（优先带 /v1）。"""
    raw = (base_url or "https://api.openai.com/v1").strip().rstrip("/")
    for suffix in ("/chat/completions", "/completions", "/models", "/responses"):
        if raw.endswith(suffix):
            raw = raw[: -len(suffix)].rstrip("/")
    candidates: list[str] = []
    if raw.endswith("/v1"):
        candidates.append(f"{raw}/models")
        root = raw[: -len("/v1")].rstrip("/")
        if root:
            candidates.append(f"{root}/models")
    else:
        candidates.append(f"{raw}/v1/models")
        candidates.append(f"{raw}/models")
    # 去重保序
    seen: set[str] = set()
    out: list[str] = []
    for u in candidates:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def extract_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        raise ValueError("模型输出中未找到 JSON 对象")
    data = json.loads(m.group(0))
    if not isinstance(data, dict):
        raise ValueError("JSON 根节点不是对象")
    return data


def _is_retryable_llm_error(exc: BaseException) -> bool:
    """超时、网络抖动、限流与 5xx 可重试；4xx（除 408/429）不重试。"""
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return True
    if isinstance(exc, aiohttp.ClientError):
        return True
    if isinstance(exc, RuntimeError):
        msg = str(exc)
        m = re.search(r"HTTP\s+(\d{3})", msg)
        if m:
            code = int(m.group(1))
            return code in (408, 429, 500, 502, 503, 504)
    return False


def _client_timeout(timeout_sec: float) -> aiohttp.ClientTimeout:
    """连接尽快失败，总耗时留给模型生成。"""
    total = max(1.0, float(timeout_sec))
    connect = min(30.0, total)
    return aiohttp.ClientTimeout(total=total, connect=connect, sock_connect=connect)


async def chat_complete(
    provider: LlmProvider,
    *,
    model: str,
    system: str,
    user: str,
    temperature: float = 0.2,
    timeout_sec: float = 90,
    force_json: bool = True,
    history: list[dict[str, str]] | None = None,
    max_tokens: int | None = None,
    retries: int = 2,
) -> tuple[str, TokenUsage]:
    """单轮或多轮对话。history 为既有 user/assistant 消息（不含当前 system/user）。

    返回 (文本内容, token 用量)。部分 Provider 可能无法回报 usage，此时为 0。
    retries：可重试错误的额外尝试次数（默认 2，合计最多 3 次）。
    """
    ptype = (provider.type or "openai_compatible").lower()
    use_model = model or provider.default_model
    attempts = max(1, int(retries) + 1)
    last_err: BaseException | None = None

    for attempt in range(1, attempts + 1):
        try:
            if ptype == "openai_compatible":
                return await _openai_compatible(
                    provider,
                    model=use_model,
                    system=system,
                    user=user,
                    temperature=temperature,
                    timeout_sec=timeout_sec,
                    force_json=force_json,
                    history=history,
                    max_tokens=max_tokens,
                )
            if ptype == "opencode":
                # OpenCode 路径暂拼成单轮文本
                merged_user = user
                if history:
                    chunks = []
                    for h in history:
                        role = h.get("role") or "user"
                        chunks.append(f"[{role}]\n{h.get('content') or ''}")
                    chunks.append(f"[user]\n{user}")
                    merged_user = "\n\n".join(chunks)
                text = await _opencode(
                    provider,
                    model=use_model,
                    system=system,
                    user=merged_user,
                    timeout_sec=timeout_sec,
                )
                return text, TokenUsage()
            if ptype == "cursor":
                merged_user = user
                if history:
                    chunks = []
                    for h in history:
                        role = h.get("role") or "user"
                        chunks.append(f"[{role}]\n{h.get('content') or ''}")
                    chunks.append(f"[user]\n{user}")
                    merged_user = "\n\n".join(chunks)
                text = await _cursor_sdk(
                    provider,
                    model=use_model,
                    system=system,
                    user=merged_user,
                    timeout_sec=timeout_sec,
                )
                return text, TokenUsage()
            raise ValueError(f"不支持的 LLM provider type: {provider.type}")
        except Exception as e:
            last_err = e
            if attempt >= attempts or not _is_retryable_llm_error(e):
                raise
            delay = min(8.0, 1.0 * (2 ** (attempt - 1)))
            logger.warning(
                "LLM 调用失败将重试 attempt=%s/%s delay=%.1fs provider=%s model=%s err=%s",
                attempt,
                attempts,
                delay,
                provider.name,
                use_model,
                e,
            )
            await asyncio.sleep(delay)

    assert last_err is not None
    raise last_err


async def test_provider_connection(
    provider: LlmProvider,
    *,
    model: str = "",
    timeout_sec: float = 25,
) -> dict[str, Any]:
    """连通性测试：优先探测 /models，再按需发一条最小 chat。"""
    import time

    started = time.perf_counter()
    ptype = (provider.type or "openai_compatible").lower()
    use_model = (model or provider.default_model or "").strip()

    if ptype == "cursor":
        key = provider.api_key or os.getenv("CURSOR_API_KEY", "")
        if not key:
            return {
                "ok": False,
                "latencyMs": int((time.perf_counter() - started) * 1000),
                "message": "未配置 CURSOR_API_KEY / api_key",
            }
        return {
            "ok": True,
            "latencyMs": int((time.perf_counter() - started) * 1000),
            "message": "已检测到 Cursor API Key（跳过真实调用）",
            "provider": provider.name,
            "type": ptype,
        }

    if ptype == "opencode":
        base = (provider.base_url or "http://127.0.0.1:4096").rstrip("/")
        timeout = _client_timeout(timeout_sec)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            last_err = ""
            for path in ("/global/health", "/health", "/session", "/"):
                try:
                    async with session.get(f"{base}{path}") as resp:
                        body = (await resp.text())[:120]
                        if resp.status < 500:
                            return {
                                "ok": True,
                                "latencyMs": int((time.perf_counter() - started) * 1000),
                                "message": f"OpenCode 可达 HTTP {resp.status} @ {path}",
                                "detail": body,
                                "provider": provider.name,
                                "type": ptype,
                            }
                        last_err = f"HTTP {resp.status} @ {path}: {body}"
                except Exception as e:
                    last_err = str(e)
            return {
                "ok": False,
                "latencyMs": int((time.perf_counter() - started) * 1000),
                "message": last_err or "OpenCode 不可达",
                "provider": provider.name,
                "type": ptype,
            }

    # openai compatible: models + optional chat
    models_ok = False
    models_endpoint = ""
    models_count = 0
    last_err = ""
    timeout = _client_timeout(timeout_sec)
    headers = {}
    if provider.api_key:
        headers["Authorization"] = f"Bearer {provider.api_key}"
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for url in openai_models_endpoints(provider.base_url):
            try:
                async with session.get(url, headers=headers) as resp:
                    body = await resp.text()
                    if resp.status >= 400:
                        last_err = f"HTTP {resp.status} @ {url}: {body[:200]}"
                        continue
                    text = body.strip()
                    if not text or text[:1] not in "{[":
                        last_err = f"{url} 返回非 JSON"
                        continue
                    raw = json.loads(text)
                    data = raw.get("data") if isinstance(raw, dict) else raw
                    if isinstance(data, list):
                        models_count = len(data)
                        models_ok = True
                        models_endpoint = url
                        break
                    last_err = f"{url} 无 data 列表"
            except Exception as e:
                last_err = f"{url}: {e}"

        if not models_ok:
            return {
                "ok": False,
                "latencyMs": int((time.perf_counter() - started) * 1000),
                "message": last_err or "模型列表探测失败",
                "provider": provider.name,
                "type": ptype,
            }

        chat_preview = ""
        if use_model:
            try:
                chat_preview, _usage = await chat_complete(
                    provider,
                    model=use_model,
                    system="Reply with exactly: OK",
                    user="ping",
                    temperature=0,
                    timeout_sec=timeout_sec,
                    force_json=False,
                )
                chat_preview = (chat_preview or "").strip()[:80]
            except Exception as e:
                return {
                    "ok": False,
                    "latencyMs": int((time.perf_counter() - started) * 1000),
                    "message": f"/models 成功，但 chat 失败: {e}",
                    "endpoint": models_endpoint,
                    "modelCount": models_count,
                    "model": use_model,
                    "provider": provider.name,
                    "type": ptype,
                }

    msg = f"/models 成功（{models_count} 个模型）"
    if use_model:
        msg += f"；chat({use_model}) 成功"
        if chat_preview:
            msg += f"：{chat_preview}"
    else:
        msg += "（未选模型，跳过 chat）"
    return {
        "ok": True,
        "latencyMs": int((time.perf_counter() - started) * 1000),
        "message": msg,
        "endpoint": models_endpoint,
        "modelCount": models_count,
        "model": use_model,
        "provider": provider.name,
        "type": ptype,
    }


async def _openai_compatible(
    provider: LlmProvider,
    *,
    model: str,
    system: str,
    user: str,
    temperature: float,
    timeout_sec: float,
    force_json: bool = True,
    history: list[dict[str, str]] | None = None,
    max_tokens: int | None = None,
) -> tuple[str, TokenUsage]:
    base = normalize_openai_compatible_base(provider.base_url or "https://api.openai.com/v1")
    url = f"{base}/chat/completions"
    headers = {"Content-Type": "application/json"}
    if provider.api_key:
        headers["Authorization"] = f"Bearer {provider.api_key}"
    # 部分兼容接口要求 messages 中出现 "json" 才能启用 json_object
    system_content = system
    if force_json and "json" not in f"{system}\n{user}".lower():
        system_content = f"{system}\n请以 JSON 对象格式输出（json）。"
    messages: list[dict[str, str]] = [{"role": "system", "content": system_content}]
    for h in history or []:
        role = (h.get("role") or "").strip().lower()
        content = h.get("content") or ""
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": user})
    token_limit = int(max_tokens) if max_tokens and max_tokens > 0 else (64 if not force_json else 4096)
    payload: dict[str, Any] = {
        "model": model,
        "temperature": temperature,
        "messages": messages,
        "max_tokens": token_limit,
        "stream": False,
    }
    if force_json:
        payload["response_format"] = {"type": "json_object"}
    timeout = _client_timeout(timeout_sec)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, headers=headers, json=payload) as resp:
            body = await resp.text()
            if resp.status >= 400:
                raise RuntimeError(f"OpenAI Compatible 失败 HTTP {resp.status}: {body[:400]}")
            data = json.loads(body)
    try:
        content = str(data["choices"][0]["message"]["content"] or "")
        usage = usage_or_estimate(
            parse_usage_payload(data),
            system=system,
            user=user,
            content=content,
            history=history,
        )
        if usage.estimated:
            logger.info(
                "LLM usage 缺失，已粗估 tokens=%s (provider=%s model=%s)",
                usage.total_tokens,
                provider.name,
                model,
            )
        else:
            logger.debug(
                "LLM usage tokens=%s (provider=%s model=%s)",
                usage.total_tokens,
                provider.name,
                model,
            )
        return content, usage
    except Exception as e:
        raise RuntimeError(f"解析 OpenAI 响应失败: {e}") from e


async def _opencode(
    provider: LlmProvider,
    *,
    model: str,
    system: str,
    user: str,
    timeout_sec: float,
) -> str:
    """优先 REST；若安装了 opencode-ai 包也可后续扩展。"""
    base = (provider.base_url or "http://127.0.0.1:4096").rstrip("/")
    timeout = _client_timeout(timeout_sec)
    prompt = f"{system}\n\n---\n\n{user}"
    headers = {"Content-Type": "application/json"}
    if provider.api_key:
        headers["Authorization"] = f"Bearer {provider.api_key}"

    async with aiohttp.ClientSession(timeout=timeout) as session:
        # 创建 session
        async with session.post(f"{base}/session", headers=headers, json={}) as resp:
            if resp.status >= 400:
                # 兼容部分版本路径
                async with session.post(f"{base}/api/session", headers=headers, json={}) as resp2:
                    text = await resp2.text()
                    if resp2.status >= 400:
                        raise RuntimeError(
                            f"OpenCode 创建 session 失败 HTTP {resp.status}/{resp2.status}: {text[:300]}"
                        )
                    sess = json.loads(text)
            else:
                sess = json.loads(await resp.text())

        sid = sess.get("id") or sess.get("data", {}).get("id")
        if not sid:
            raise RuntimeError(f"OpenCode session 无 id: {sess}")

        body: dict[str, Any] = {
            "parts": [{"type": "text", "text": prompt}],
        }
        if model:
            body["model"] = model
        # 常见 chat/prompt 路径尝试
        last_err = ""
        for path in (
            f"{base}/session/{sid}/prompt",
            f"{base}/session/{sid}/chat",
            f"{base}/api/session/{sid}/prompt",
            f"{base}/api/session/{sid}/chat",
        ):
            async with session.post(path, headers=headers, json=body) as resp:
                text = await resp.text()
                if resp.status < 400:
                    data = json.loads(text) if text else {}
                    content = _dig_opencode_text(data)
                    if content:
                        return content
                    last_err = f"空响应 from {path}: {text[:200]}"
                    continue
                last_err = f"{path} -> HTTP {resp.status}: {text[:200]}"
        raise RuntimeError(f"OpenCode 调用失败: {last_err}")


def _dig_opencode_text(data: Any) -> str:
    if isinstance(data, str):
        return data
    if not isinstance(data, dict):
        return ""
    for key in ("content", "text", "result", "message"):
        v = data.get(key)
        if isinstance(v, str) and v.strip():
            return v
        if isinstance(v, dict):
            got = _dig_opencode_text(v)
            if got:
                return got
    parts = data.get("parts")
    if isinstance(parts, list):
        chunks = []
        for p in parts:
            if isinstance(p, dict) and p.get("type") in (None, "text"):
                t = p.get("text") or p.get("content")
                if t:
                    chunks.append(str(t))
        if chunks:
            return "\n".join(chunks)
    return ""


async def _cursor_sdk(
    provider: LlmProvider,
    *,
    model: str,
    system: str,
    user: str,
    timeout_sec: float,
) -> str:
    """通过 cursor-sdk（若已安装）做一次 Agent.prompt。"""
    api_key = provider.api_key or os.getenv("CURSOR_API_KEY", "")
    if not api_key:
        raise RuntimeError("Cursor SDK 需要配置 api_key 或环境变量 CURSOR_API_KEY")

    prompt = f"{system}\n\n{user}"

    def _run() -> str:
        try:
            from cursor_sdk import Agent  # type: ignore
        except Exception as e:
            raise RuntimeError(
                "未安装 cursor-sdk。请执行: pip install cursor-sdk"
            ) from e

        # 同步 API；不同版本参数可能略有差异，做兼容尝试
        kwargs_list = [
            {"api_key": api_key, "model": {"id": model} if model else None, "local": {"cwd": os.getcwd()}},
            {"apiKey": api_key, "model": {"id": model} if model else None},
            {"api_key": api_key},
        ]
        last: Exception | None = None
        for kwargs in kwargs_list:
            clean = {k: v for k, v in kwargs.items() if v is not None}
            try:
                if hasattr(Agent, "prompt"):
                    result = Agent.prompt(prompt, **clean)
                else:
                    raise RuntimeError("cursor_sdk.Agent 无 prompt 方法")
                if asyncio.iscoroutine(result):
                    # 不应在同步函数里出现；交给外层
                    raise RuntimeError("cursor Agent.prompt 返回了 coroutine，请升级 cursor-sdk")
                status = getattr(result, "status", None)
                text = getattr(result, "result", None) or getattr(result, "text", None) or str(result)
                if status and str(status).lower() not in ("ok", "success", "completed", "done"):
                    logger.warning("Cursor SDK status=%s", status)
                return str(text)
            except TypeError as e:
                last = e
                continue
        raise RuntimeError(f"Cursor SDK 调用失败: {last}")

    return await asyncio.to_thread(_run)


async def describe_image(
    provider: LlmProvider,
    *,
    model: str,
    mime: str,
    b64: str,
    timeout_sec: float = 60,
) -> tuple[str, TokenUsage]:
    """用视觉能力描述单张图片；不支持时返回空串与零用量。"""
    ptype = (provider.type or "openai_compatible").lower()
    if ptype != "openai_compatible":
        return "", TokenUsage()
    use_model = model or provider.default_model
    base = normalize_openai_compatible_base(provider.base_url or "https://api.openai.com/v1")
    url = f"{base}/chat/completions"
    headers = {"Content-Type": "application/json"}
    if provider.api_key:
        headers["Authorization"] = f"Bearer {provider.api_key}"
    data_url = f"data:{mime};base64,{b64}"
    payload: dict[str, Any] = {
        "model": use_model,
        "temperature": 0.1,
        "max_tokens": 300,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "请用中文简洁描述这张聊天图片的关键内容："
                            "可见文字请原样摘录；人物/场景/图表/截图界面也要概括。"
                            "不要臆测看不见的信息。控制在 120 字以内。"
                        ),
                    },
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
    }
    timeout = _client_timeout(timeout_sec)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, headers=headers, json=payload) as resp:
                body = await resp.text()
                if resp.status >= 400:
                    logger.warning("图片描述失败 HTTP %s: %s", resp.status, body[:240])
                    return "", TokenUsage()
                data = json.loads(body)
        text = data["choices"][0]["message"]["content"]
        content = str(text or "").strip()
        usage = usage_or_estimate(
            parse_usage_payload(data),
            system="",
            user="image",
            content=content,
        )
        return content, usage
    except Exception:
        logger.exception("图片描述调用异常")
        return "", TokenUsage()
