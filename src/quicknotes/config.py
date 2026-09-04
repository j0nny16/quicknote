"""Configuration: a YAML file selects which plugin implements each stage.

Secrets never live in the YAML. Any field named ``*_env`` names an environment
variable, which is resolved at load time -- so config.yaml stays committable
and .env holds the keys.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator


class ConfigError(RuntimeError):
    """Raised for configuration problems that should stop startup immediately."""


def _resolve_env(var: str | None, *, what: str, required: bool = True) -> str | None:
    if not var:
        return None
    value = os.environ.get(var, "").strip()
    if not value:
        if required:
            raise ConfigError(
                f"{what}: environment variable {var!r} is empty or unset. "
                f"Set it in .env (see .env.example)."
            )
        return None
    return value


class SourceConfig(BaseModel):
    type: Literal["telegram"] = "telegram"
    token_env: str = "TELEGRAM_BOT_TOKEN"
    allowed_user_ids: list[int] = Field(default_factory=list)
    ack_emoji: str = "⏳"

    @field_validator("allowed_user_ids", mode="before")
    @classmethod
    def _from_env(cls, v: Any) -> Any:
        # ALLOWED_USER_IDS="123,456" overrides the YAML list entirely.
        raw = os.environ.get("ALLOWED_USER_IDS", "").strip()
        if raw:
            return [int(p) for p in raw.replace(";", ",").split(",") if p.strip()]
        return v

    def token(self) -> str:
        return _resolve_env(self.token_env, what="source.telegram")  # type: ignore[return-value]


class TranscriberConfig(BaseModel):
    # "openai_compatible" covers the local whisper container, Groq and OpenAI --
    # they all speak POST {base_url}/audio/transcriptions.
    type: Literal["openai_compatible", "passthrough"] = "openai_compatible"
    base_url: str = "http://whisper:8000/v1"
    model: str = "deepdml/faster-whisper-large-v3-turbo-ct2"
    api_key_env: str | None = None
    language: str | None = None  # None = auto-detect
    timeout_s: float = 600.0

    def api_key(self) -> str | None:
        return _resolve_env(self.api_key_env, what="transcriber", required=False)


class EnricherConfig(BaseModel):
    type: Literal["anthropic", "noop"] = "anthropic"
    model: str = "claude-haiku-4-5"
    api_key_env: str = "ANTHROPIC_API_KEY"
    max_tokens: int = 2048
    timeout_s: float = 120.0
    generate_tags: bool = False

    def api_key(self) -> str:
        return _resolve_env(self.api_key_env, what="enricher.anthropic")  # type: ignore[return-value]


class AnytypeSinkConfig(BaseModel):
    type: Literal["anytype"] = "anytype"
    base_url: str = "http://anytype-cli:31012"
    api_version: str = "2025-11-08"
    api_key_env: str = "ANYTYPE_API_KEY"
    space_id: str = ""
    type_key: str = "quicknote"
    # Note field -> Anytype property key. Empty = skip that field.
    # Fill from `quicknotes introspect`.
    property_map: dict[str, str] = Field(default_factory=dict)
    icon_text: str = "\U0001f4dd"
    icon_voice: str = "\U0001f3a4"
    timeout_s: float = 30.0

    @field_validator("space_id", mode="before")
    @classmethod
    def _space_from_env(cls, v: Any) -> Any:
        return os.environ.get("ANYTYPE_SPACE_ID", "").strip() or v

    def api_key(self) -> str:
        return _resolve_env(self.api_key_env, what="sink.anytype")  # type: ignore[return-value]


SinkConfig = AnytypeSinkConfig


class Config(BaseModel):
    data_dir: Path = Path("/data")
    threshold_words: int = 10
    max_attempts: int = 4
    retry_backoff_s: float = 20.0
    undo_history: int = 20
    source: SourceConfig = Field(default_factory=SourceConfig)
    transcriber: TranscriberConfig = Field(default_factory=TranscriberConfig)
    enricher: EnricherConfig = Field(default_factory=EnricherConfig)
    sinks: list[SinkConfig] = Field(default_factory=lambda: [AnytypeSinkConfig()])

    @field_validator("data_dir", mode="before")
    @classmethod
    def _dir_from_env(cls, v: Any) -> Any:
        return os.environ.get("QUICKNOTES_DATA_DIR", "").strip() or v

    @field_validator("sinks")
    @classmethod
    def _at_least_one(cls, v: list[SinkConfig]) -> list[SinkConfig]:
        if not v:
            raise ValueError("at least one sink must be configured")
        return v

    @property
    def db_path(self) -> Path:
        return self.data_dir / "quicknotes.db"

    @property
    def audio_dir(self) -> Path:
        return self.data_dir / "audio"


def load_config(path: str | Path | None = None) -> Config:
    path = Path(path or os.environ.get("QUICKNOTES_CONFIG", "config.yaml"))
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    try:
        return Config.model_validate(raw)
    except Exception as exc:  # pydantic ValidationError et al
        raise ConfigError(f"{path}: {exc}") from exc
