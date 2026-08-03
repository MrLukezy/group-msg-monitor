"""全局与分群配置存储（JSON 文件）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
SETTINGS_PATH = DATA_DIR / "app_settings.json"
GROUP_DIR = DATA_DIR / "group_configs"


class LlmProvider(BaseModel):
    id: str
    name: str
    type: str = "openai_compatible"  # openai_compatible | opencode | cursor
    base_url: str = ""
    api_key: str = ""
    default_model: str = ""


class LlmGlobalSettings(BaseModel):
    providers: list[LlmProvider] = Field(default_factory=list)
    active_provider_id: str = ""
    default_image_model: str = ""
    default_prompt: str = ""
    default_every_minutes: int = 60
    default_window_minutes: int = 60
    default_min_messages: int = 8
    # 全局只保留最近 N 条 LLM 分析报告（20~500）
    report_keep_limit: int = 100


LLM_REPORT_KEEP_MIN = 20
LLM_REPORT_KEEP_MAX = 500
LLM_REPORT_KEEP_DEFAULT = 100

LEGACY_LLM_MONITOR_PROMPT = (
    "请基于群聊做中文分析。若出现 GitHub 仓库或 AI/大模型相关名词，"
    "必须多轮补齐相关上下文，并在对应 key_point 的 deep_dive 中深入展开、扩充回答；"
    "仓库写入要点 links（或 appendix.links），名词写入要点 nouns（或 appendix.nouns）。"
    "同时关注主题、风险、待办；禁止编造，不确定写「记录不足」。"
)

DEFAULT_LLM_MONITOR_PROMPT = (
    "你是群聊监控分析助手。请基于给定聊天记录输出中文 JSON："
    "headline, topics, key_points, risks, action_items, sentiment。"
    "禁止编造；不确定请写「记录不足」。risks 需带原文 evidence。"
    "帮我去分析当前这分钟重点说了什么内容。"
    "如果内容中有 GitHub 仓库、网站链接等内容，则需要在回复内容后面，"
    "单独着重分析这个仓库的内容、方案，以及网站的内容和方案。"
    "如果遇到大模型相关、AI 相关的内容，也着重分析；"
    "尤其是遇到特有名词或者单词简称，也帮我查询并解释是什么，写入要点 nouns（名词剖析）；"
    "深入分析 deep_dive 须同时包含：detail（群内观点）与 knowledge（你补充的背景知识/概念说明）。"
)


def clamp_report_keep_limit(value: Any) -> int:
    try:
        n = int(value)
    except Exception:
        n = LLM_REPORT_KEEP_DEFAULT
    return max(LLM_REPORT_KEEP_MIN, min(LLM_REPORT_KEEP_MAX, n))


class CustomThemeSettings(BaseModel):
    """自定义皮肤：纯色自动配色，或图片采样自动配色。"""

    base_color: str = "#3d8b8b"
    mode: str = "dark"  # dark | light
    source: str = "color"  # color | image

    @field_validator("mode", mode="before")
    @classmethod
    def normalize_mode(cls, value: Any) -> str:
        return "light" if str(value or "").strip().lower() == "light" else "dark"

    @field_validator("source", mode="before")
    @classmethod
    def normalize_source(cls, value: Any) -> str:
        return "image" if str(value or "").strip().lower() == "image" else "color"

    @field_validator("base_color", mode="before")
    @classmethod
    def normalize_color(cls, value: Any) -> str:
        raw = str(value or "").strip()
        if not raw.startswith("#"):
            raw = f"#{raw}"
        if len(raw) == 4 and all(c in "0123456789abcdefABCDEF#" for c in raw):
            raw = f"#{raw[1]*2}{raw[2]*2}{raw[3]*2}"
        if len(raw) == 7 and all(c in "0123456789abcdefABCDEF#" for c in raw):
            return raw.lower()
        return "#3d8b8b"


class UiSettings(BaseModel):
    """桌面端外观与交互偏好。"""

    theme: str = "midnight"
    custom: CustomThemeSettings = Field(default_factory=CustomThemeSettings)
    # 壁纸氛围透明度 0~1；None 表示跟随当前皮肤默认
    wall_opacity: float | None = None
    # 面板毛玻璃透明度 0.35~1，越大越不透明
    panel_opacity: float = 0.82

    @field_validator("wall_opacity", mode="before")
    @classmethod
    def normalize_wall_opacity(cls, value: Any) -> float | None:
        if value is None or value == "":
            return None
        try:
            n = float(value)
        except Exception:
            return None
        return max(0.0, min(1.0, n))

    @field_validator("panel_opacity", mode="before")
    @classmethod
    def normalize_panel_opacity(cls, value: Any) -> float:
        try:
            n = float(value)
        except Exception:
            n = 0.82
        return max(0.35, min(1.0, n))


class QqChannelSettings(BaseModel):
    bound: bool = False
    label: str = ""
    last_error: str = ""
    # onebot = NapCat / OneBot；passive = 官方 QQ 通知+UIA 被动采集
    mode: str = "onebot"
    notification_access: str = ""  # allowed | denied | unsupported | unknown
    uia_ready: bool = False
    poll_seconds: float = 1.5
    # 群名 -> 群 ID（可填真实 QQ 群号，或保留 qqp:hash）
    group_name_map: dict[str, str] = Field(default_factory=dict)

    @field_validator("mode", mode="before")
    @classmethod
    def normalize_mode(cls, value: Any) -> str:
        mode = str(value or "onebot").strip().lower()
        if mode in ("passive", "safe", "official", "qq_passive", "qqp"):
            return "passive"
        return "onebot"


class WechatChannelSettings(BaseModel):
    bound: bool = False
    label: str = ""
    last_error: str = ""
    data_dir: str = ""
    decrypted_dir: str = ""
    keys_path: str = ""
    poll_seconds: float = 1.0


class TelegramChannelSettings(BaseModel):
    bound: bool = False
    label: str = ""
    last_error: str = ""
    api_id: int = 0
    api_hash: str = ""
    # 兼容旧配置字段（已废弃 Bot）
    bot_token: str = ""
    poll_timeout: int = 25


class ChannelsSettings(BaseModel):
    qq: QqChannelSettings = Field(default_factory=QqChannelSettings)
    wechat: WechatChannelSettings = Field(default_factory=WechatChannelSettings)
    telegram: TelegramChannelSettings = Field(default_factory=TelegramChannelSettings)


class AppSettings(BaseModel):
    onebot_ws_url: str = "ws://127.0.0.1:3001"
    onebot_access_token: str = ""
    channels: ChannelsSettings = Field(default_factory=ChannelsSettings)
    llm: LlmGlobalSettings = Field(default_factory=LlmGlobalSettings)
    ui: UiSettings = Field(default_factory=UiSettings)


class GroupBasicConfig(BaseModel):
    log_all: bool = True
    storage_enabled: bool = True


class KeywordMonitorConfig(BaseModel):
    enabled: bool = True
    keywords: list[str] = Field(default_factory=list)
    alert_enabled: bool = False
    webhook_url: str = ""


class LlmMonitorConfig(BaseModel):
    enabled: bool = False
    use_global_defaults: bool = True
    # 文本分析
    text_enabled: bool = True
    provider_id: str = ""
    model: str = ""
    prompt: str = DEFAULT_LLM_MONITOR_PROMPT
    # 图片分析（视觉描述）
    image_enabled: bool = True
    image_same_as_text: bool = True  # True 时复用文本的 provider/model
    image_provider_id: str = ""
    image_model: str = ""
    every_minutes: int = 60
    window_minutes: int = 60
    min_messages: int = 8

    @field_validator("prompt", mode="before")
    @classmethod
    def migrate_default_prompt(cls, value: Any) -> Any:
        if value == LEGACY_LLM_MONITOR_PROMPT:
            return DEFAULT_LLM_MONITOR_PROMPT
        return value


def resolve_llm_timing(
    global_settings: LlmGlobalSettings,
    group_settings: LlmMonitorConfig,
) -> tuple[int, int, int]:
    """返回 (执行间隔, 分析窗口, 最少消息数)，统一处理全局继承。"""
    if group_settings.use_global_defaults:
        values = (
            global_settings.default_every_minutes,
            global_settings.default_window_minutes,
            global_settings.default_min_messages,
        )
    else:
        values = (
            group_settings.every_minutes,
            group_settings.window_minutes,
            group_settings.min_messages,
        )
    return (
        max(1, int(values[0] or 1)),
        max(1, int(values[1] or 1)),
        max(1, int(values[2] or 1)),
    )


class GroupConfig(BaseModel):
    group_id: str
    group_name: str = ""
    channel: str = "qq"  # qq | wechat | telegram
    enabled: bool = False
    basic: GroupBasicConfig = Field(default_factory=GroupBasicConfig)
    keyword_monitor: KeywordMonitorConfig = Field(default_factory=KeywordMonitorConfig)
    llm_monitor: LlmMonitorConfig = Field(default_factory=LlmMonitorConfig)


def _ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    GROUP_DIR.mkdir(parents=True, exist_ok=True)


def default_app_settings() -> AppSettings:
    return AppSettings(
        llm=LlmGlobalSettings(
            providers=[
                LlmProvider(
                    id="openai_compatible",
                    name="OpenAI Compatible",
                    type="openai_compatible",
                    base_url="https://api.openai.com/v1",
                    default_model="gpt-4.1-mini",
                ),
                LlmProvider(
                    id="opencode",
                    name="OpenCode SDK / Server",
                    type="opencode",
                    base_url="http://127.0.0.1:4096",
                    default_model="",
                ),
                LlmProvider(
                    id="cursor",
                    name="Cursor SDK",
                    type="cursor",
                    base_url="",
                    default_model="composer-2.5",
                ),
            ],
            active_provider_id="openai_compatible",
        )
    )


def _migrate_channel_defaults(settings: AppSettings) -> AppSettings:
    """兼容旧配置，并强制关闭当前未支持的通道。"""
    from app.channels.feature_flags import (
        TELEGRAM_CHANNEL_ENABLED,
        TELEGRAM_DISABLED_MESSAGE,
        WECHAT_CHANNEL_ENABLED,
        WECHAT_DISABLED_MESSAGE,
    )

    changed = False
    if not settings.llm.default_prompt:
        settings.llm.default_prompt = DEFAULT_LLM_MONITOR_PROMPT
        changed = True
    qq = settings.channels.qq
    if qq.mode not in ("onebot", "passive"):
        qq.mode = "onebot"
        changed = True
    if not qq.bound and (settings.onebot_ws_url or settings.onebot_access_token):
        qq.bound = True
        if not qq.label:
            qq.label = "OneBot / NapCat"
        changed = True
    if not WECHAT_CHANNEL_ENABLED:
        wx = settings.channels.wechat
        if wx.bound or wx.label or wx.last_error != WECHAT_DISABLED_MESSAGE:
            wx.bound = False
            wx.label = ""
            wx.last_error = WECHAT_DISABLED_MESSAGE
            changed = True
    if not TELEGRAM_CHANNEL_ENABLED:
        tg = settings.channels.telegram
        if tg.bound or tg.label or tg.last_error != TELEGRAM_DISABLED_MESSAGE:
            tg.bound = False
            tg.label = ""
            tg.last_error = TELEGRAM_DISABLED_MESSAGE
            changed = True
    if changed:
        save_app_settings(settings)
    return settings


def load_app_settings() -> AppSettings:
    _ensure_dirs()
    if not SETTINGS_PATH.exists():
        settings = default_app_settings()
        # 尝试从 .env 迁移 OneBot
        env_path = ROOT_DIR / ".env"
        if env_path.exists():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"')
                if k == "ONEBOT_WS_URL":
                    settings.onebot_ws_url = v
                elif k == "ONEBOT_ACCESS_TOKEN":
                    settings.onebot_access_token = v
        settings = _migrate_channel_defaults(settings)
        save_app_settings(settings)
        return settings
    data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    settings = AppSettings.model_validate(data)
    return _migrate_channel_defaults(settings)


def save_app_settings(settings: AppSettings) -> None:
    from app.channels.feature_flags import (
        TELEGRAM_CHANNEL_ENABLED,
        TELEGRAM_DISABLED_MESSAGE,
        WECHAT_CHANNEL_ENABLED,
        WECHAT_DISABLED_MESSAGE,
    )

    if settings.channels.qq.mode not in ("onebot", "passive"):
        settings.channels.qq.mode = "onebot"
    if not WECHAT_CHANNEL_ENABLED:
        settings.channels.wechat.bound = False
        settings.channels.wechat.label = ""
        settings.channels.wechat.last_error = WECHAT_DISABLED_MESSAGE
    if not TELEGRAM_CHANNEL_ENABLED:
        settings.channels.telegram.bound = False
        settings.channels.telegram.label = ""
        settings.channels.telegram.last_error = TELEGRAM_DISABLED_MESSAGE
    settings.llm.report_keep_limit = clamp_report_keep_limit(settings.llm.report_keep_limit)
    _ensure_dirs()
    SETTINGS_PATH.write_text(
        settings.model_dump_json(indent=2),
        encoding="utf-8",
    )
    try:
        from app.llm.service import prune_old_llm_reports

        prune_old_llm_reports(settings.llm.report_keep_limit)
    except Exception:
        pass


def group_config_path(group_id: str) -> Path:
    return GROUP_DIR / f"{group_id}.json"


def load_group_config(group_id: str) -> GroupConfig:
    _ensure_dirs()
    path = group_config_path(group_id)
    if not path.exists():
        from app.channels.ids import channel_of_group_id

        return GroupConfig(group_id=str(group_id), channel=channel_of_group_id(group_id))
    cfg = GroupConfig.model_validate(json.loads(path.read_text(encoding="utf-8")))
    if not cfg.channel:
        from app.channels.ids import channel_of_group_id

        cfg.channel = channel_of_group_id(cfg.group_id)
    return cfg


def save_group_config(cfg: GroupConfig) -> None:
    _ensure_dirs()
    path = group_config_path(cfg.group_id)
    path.write_text(cfg.model_dump_json(indent=2), encoding="utf-8")


def list_group_configs() -> list[GroupConfig]:
    _ensure_dirs()
    out: list[GroupConfig] = []
    for p in sorted(GROUP_DIR.glob("*.json")):
        try:
            out.append(GroupConfig.model_validate(json.loads(p.read_text(encoding="utf-8"))))
        except Exception:
            continue
    return out


def enabled_group_ids() -> set[str]:
    ids = {c.group_id for c in list_group_configs() if c.enabled}
    if ids:
        return ids
    # 兼容旧 .env MONITOR_GROUP_IDS
    env_path = ROOT_DIR / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("MONITOR_GROUP_IDS="):
                raw = line.split("=", 1)[1].strip().strip('"')
                return {x.strip() for x in raw.split(",") if x.strip()}
    return set()
def provider_by_id(settings: AppSettings, provider_id: str) -> LlmProvider | None:
    for p in settings.llm.providers:
        if p.id == provider_id:
            return p
    if settings.llm.providers:
        return settings.llm.providers[0]
    return None


def dump_public(obj: BaseModel) -> dict[str, Any]:
    return obj.model_dump(mode="json")
