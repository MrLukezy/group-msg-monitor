"""全局与分群配置存储（JSON 文件）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

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


class UiSettings(BaseModel):
    """桌面端外观与交互偏好。"""

    compact_mode_enabled: bool = False  # 开启后点缩小进入缩略窗
    theme: str = "midnight"  # midnight | daylight | ocean | forest | rose | graphite


class QqChannelSettings(BaseModel):
    bound: bool = False
    label: str = ""
    last_error: str = ""


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
    provider_id: str = ""
    model: str = ""
    prompt: str = (
        "你是群聊监控分析助手。请基于给定聊天记录输出中文 JSON："
        "headline, topics, key_points, risks, action_items, sentiment。"
        "禁止编造；不确定请写「记录不足」。risks 需带原文 evidence。"
    )
    every_minutes: int = 60
    window_minutes: int = 60
    min_messages: int = 8


class GroupConfig(BaseModel):
    group_id: str
    group_name: str = ""
    channel: str = "qq"  # qq | wechat | telegram
    enabled: bool = False
    blocked: bool = False  # 屏蔽：不落库、不处理
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
    """兼容旧配置：已有 OneBot 地址则默认视为 QQ 已绑定。"""
    changed = False
    if not settings.channels.qq.bound and (settings.onebot_ws_url or settings.onebot_access_token):
        settings.channels.qq.bound = True
        if not settings.channels.qq.label:
            settings.channels.qq.label = "OneBot / NapCat"
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
    _ensure_dirs()
    SETTINGS_PATH.write_text(
        settings.model_dump_json(indent=2),
        encoding="utf-8",
    )


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
    ids = {c.group_id for c in list_group_configs() if c.enabled and not c.blocked}
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


def blocked_group_ids() -> set[str]:
    return {c.group_id for c in list_group_configs() if c.blocked}


def provider_by_id(settings: AppSettings, provider_id: str) -> LlmProvider | None:
    for p in settings.llm.providers:
        if p.id == provider_id:
            return p
    if settings.llm.providers:
        return settings.llm.providers[0]
    return None


def dump_public(obj: BaseModel) -> dict[str, Any]:
    return obj.model_dump(mode="json")
