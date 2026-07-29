"""Application configuration.

Secrets (Shopify client credentials, ERPNext API key/secret) are sourced from
environment variables — or systemd `LoadCredential=` in production — via
`Settings`. Non-secret configuration (company, donation allow-list, paths,
sync knobs) is loaded from a YAML file via `YamlConfig`.

`load_config()` is the single entry point used by every other module.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Secrets and environment-specific settings, sourced from env vars.

    Supports systemd `LoadCredential=` natively: if `CREDENTIALS_DIRECTORY` is
    set (by systemd), each credential `name` is available as a file inside it.
    We auto-load `<lowercase-setting-name>` from there when present, before
    falling back to plain env vars.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Shopify (Dev Dashboard custom app). No static access token: the client
    # mints a 24-hour token from client_id + client_secret at runtime.
    shopify_shop_domain: str = ""
    shopify_client_id: str = ""
    shopify_client_secret: SecretStr = SecretStr("")
    shopify_api_version: str = "2025-07"

    # ERPNext (Frappe REST API, static key/secret).
    erpnext_base_url: str = ""
    erpnext_api_key: str = ""
    erpnext_api_secret: SecretStr = SecretStr("")

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        """Prepend a systemd-credentials source ahead of the default chain.

        Order: init kwargs > systemd-creds > env vars > .env file.
        """
        systemd_source = _SystemdCredentialsSource(settings_cls)
        return (
            init_settings,
            systemd_source,
            env_settings,
            dotenv_settings,
            file_secret_settings,
        )


class _SystemdCredentialsSource:
    """Pydantic settings source reading from systemd's CREDENTIALS_DIRECTORY.

    When systemd uses `LoadCredential=shopify_client_id:/path`, it sets
    `CREDENTIALS_DIRECTORY=/run/credentials/<service>` and creates the file
    `shopify_client_id` inside it. We mirror that into pydantic settings,
    matching by lowercase setting name.
    """

    def __init__(self, settings_cls) -> None:
        self.settings_cls = settings_cls

    def __call__(self) -> dict[str, str]:
        creds_dir = os.environ.get("CREDENTIALS_DIRECTORY")
        if not creds_dir:
            return {}
        out: dict[str, str] = {}
        try:
            for entry in os.scandir(creds_dir):
                if not entry.is_file():
                    continue
                key = entry.name.lower()
                out[key] = Path(entry.path).read_text(encoding="utf-8").strip()
        except OSError:
            pass
        return out

    def __repr__(self) -> str:
        return "_SystemdCredentialsSource()"


class PathsConfig(BaseModel):
    state: Path = Path("./state.json")
    archive: Path = Path("./archive")
    errors: Path = Path("./errors")

    @field_validator("*", mode="after")
    @classmethod
    def _expand(cls, v: Path) -> Path:
        return v.expanduser().resolve() if not v.is_absolute() else v


class SyncConfig(BaseModel):
    page_size: int = 250
    include_test_orders: bool = False
    paid_only: bool = True
    # Shopify product tags to NOT import (generic noise). Applied to campaign tags.
    tag_skiplist: list[str] = ["Donate", "Contribute", "Campaigns"]


class BackfillConfig(BaseModel):
    since: str = "2020-01-01"


class RetryConfig(BaseModel):
    max_attempts: int = 3
    initial_wait_seconds: float = 5.0
    max_wait_seconds: float = 120.0
    backoff_multiplier: float = 2.0


class LoggingConfig(BaseModel):
    level: str = "INFO"
    json_logs: bool = False


class YamlConfig(BaseModel):
    """Non-secret configuration loaded from config.yaml."""

    company: str
    default_currency: str = "CAD"
    donation_product_ids: list[str] = Field(default_factory=list)
    recurring_product_ids: list[str] = Field(default_factory=list)
    tip_mode: str = "fold"  # fold | ignore | separate
    paths: PathsConfig = Field(default_factory=PathsConfig)
    sync: SyncConfig = Field(default_factory=SyncConfig)
    backfill: BackfillConfig = Field(default_factory=BackfillConfig)
    retry: RetryConfig = Field(default_factory=RetryConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)


class AppConfig:
    """Top-level container bundling secrets + YAML config."""

    def __init__(self, settings: Settings, yaml_cfg: YamlConfig) -> None:
        self.settings = settings
        self.yaml = yaml_cfg

    # --- derived helpers ------------------------------------------------

    @property
    def donation_product_gids(self) -> set[str]:
        """Full Shopify GIDs (gid://shopify/Product/<id>) for donation products."""
        return {f"gid://shopify/Product/{pid}" for pid in self.yaml.donation_product_ids}

    @property
    def recurring_product_gids(self) -> set[str]:
        return {f"gid://shopify/Product/{pid}" for pid in self.yaml.recurring_product_ids}


DEFAULT_CONFIG_PATHS = (
    Path("config.yaml"),
    Path("/etc/glia-sync/config.yaml"),
)


def _find_config_path() -> Path:
    env_path = os.environ.get("GLIA_SYNC_CONFIG")
    if env_path:
        p = Path(env_path).expanduser()
        if not p.is_file():
            raise FileNotFoundError(f"GLIA_SYNC_CONFIG points to missing file: {p}")
        return p
    for candidate in DEFAULT_CONFIG_PATHS:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        "No config.yaml found. Set GLIA_SYNC_CONFIG or create one of: "
        + ", ".join(str(p) for p in DEFAULT_CONFIG_PATHS)
    )


def load_yaml_config(path: Path | str | None = None) -> YamlConfig:
    if path is None:
        path = _find_config_path()
    raw: dict[str, Any] = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return YamlConfig.model_validate(raw)


@lru_cache(maxsize=1)
def load_config() -> AppConfig:
    """Cached top-level config. Call `load_config.cache_clear()` to reload."""
    return AppConfig(settings=Settings(), yaml_cfg=load_yaml_config())


def setup_logging(cfg: AppConfig) -> None:
    """Configure structlog with optional JSON output."""
    import logging

    import structlog

    level = getattr(logging, cfg.yaml.logging.level.upper(), logging.INFO)

    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
    ]
    if cfg.yaml.logging.json_logs:
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer(colors=False))

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

    logging.getLogger("urllib3").setLevel(logging.WARNING)


__all__ = [
    "AppConfig",
    "Settings",
    "YamlConfig",
    "load_config",
    "load_yaml_config",
    "setup_logging",
]
