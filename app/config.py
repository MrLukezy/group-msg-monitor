"""配置加载：环境变量 / .env 优先，其次 config.yaml。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import (
    BaseSettings,
    NoDecode,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

ROOT_DIR = Path(__file__).resolve().parent.parent
CONFIG_YAML = ROOT_DIR / "config.yaml"


def _load_yaml() -> dict[str, Any]:
    if not CONFIG_YAML.exists():
        return {}
    with CONFIG_YAML.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        return {}
    return data


def _yaml_flat() -> dict[str, Any]:
    """把嵌套 yaml 展平为 Settings 可用的扁平字段。"""
    raw = _load_yaml()
    flat: dict[str, Any] = {}

    onebot = raw.get("onebot") or {}
    if onebot.get("ws_url") is not None:
        flat["onebot_ws_url"] = onebot["ws_url"]
    if onebot.get("access_token") is not None:
        flat["onebot_access_token"] = onebot["access_token"]

    monitor = raw.get("monitor") or {}
    if monitor.get("group_ids") is not None:
        flat["monitor_group_ids"] = monitor["group_ids"]
    if monitor.get("keywords") is not None:
        flat["monitor_keywords"] = monitor["keywords"]
    if monitor.get("log_all") is not None:
        flat["monitor_log_all"] = monitor["log_all"]

    storage = raw.get("storage") or {}
    if storage.get("enabled") is not None:
        flat["storage_enabled"] = storage["enabled"]
    if storage.get("sqlite_path") is not None:
        flat["storage_sqlite_path"] = storage["sqlite_path"]

    alert = raw.get("alert") or {}
    if alert.get("enabled") is not None:
        flat["alert_enabled"] = alert["enabled"]
    if alert.get("webhook_url") is not None:
        flat["alert_webhook_url"] = alert["webhook_url"]

    logging_cfg = raw.get("logging") or {}
    if logging_cfg.get("level") is not None:
        flat["log_level"] = logging_cfg["level"]
    if logging_cfg.get("dir") is not None:
        flat["log_dir"] = logging_cfg["dir"]

    return flat


def _split_csv(value: str | list[str] | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [part.strip() for part in str(value).split(",") if part.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ROOT_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    onebot_ws_url: str = "ws://127.0.0.1:3001"
    onebot_access_token: str = ""

    # NoDecode：允许 MONITOR_GROUP_IDS=1,2,3 这种 CSV，而不是强制 JSON 数组
    monitor_group_ids: Annotated[list[str], NoDecode] = Field(default_factory=list)
    monitor_keywords: Annotated[list[str], NoDecode] = Field(default_factory=list)
    monitor_log_all: bool = True

    storage_enabled: bool = True
    storage_sqlite_path: str = "./data/messages.db"

    alert_enabled: bool = False
    alert_webhook_url: str = ""

    log_level: str = "INFO"
    log_dir: str = "./logs"

    reconnect_min_delay: float = 1.0
    reconnect_max_delay: float = 60.0

    @field_validator("monitor_group_ids", "monitor_keywords", mode="before")
    @classmethod
    def parse_csv_lists(cls, value: Any) -> list[str]:
        return _split_csv(value)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # 优先级：环境变量 > .env > 构造参数(yaml) > 默认值
        return (
            env_settings,
            dotenv_settings,
            init_settings,
            file_secret_settings,
        )

    @property
    def allowed_groups(self) -> set[str]:
        return {str(g) for g in self.monitor_group_ids}

    @property
    def sqlite_path(self) -> Path:
        path = Path(self.storage_sqlite_path)
        if not path.is_absolute():
            path = ROOT_DIR / path
        return path

    @property
    def log_path(self) -> Path:
        path = Path(self.log_dir)
        if not path.is_absolute():
            path = ROOT_DIR / path
        return path


def load_settings() -> Settings:
    # 保证从项目根加载相对路径配置
    os.chdir(ROOT_DIR)
    # .env 覆盖进程内残留环境变量（避免联调/mock 端口如 13001 污染正式配置）
    from dotenv import load_dotenv

    load_dotenv(ROOT_DIR / ".env", override=True)
    return Settings(**_yaml_flat())
