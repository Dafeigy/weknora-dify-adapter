"""Environment-backed application settings."""

from functools import lru_cache
from typing import Literal

from pydantic import AnyHttpUrl, Field, SecretStr, field_validator, ValidationInfo
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Validated adapter configuration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    weknora_base_url: AnyHttpUrl
    weknora_api_key: SecretStr
    weknora_chat_api_key: SecretStr | None = None
    weknora_agent_id: str | None = None
    weknora_knowledge_base_ids: str = ""
    dify_adapter_api_key: SecretStr
    weknora_timeout_seconds: float = Field(default=15, ge=1, le=120)
    dify_stream_tokens_per_second: float = Field(default=20, ge=0, le=1000)
    dify_conversation_db_path: str = "./data/conversations.sqlite3"
    log_level: Literal["CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"] = "INFO"
    port: int = Field(default=8000, ge=1, le=65535)

    @field_validator(
        "weknora_api_key", "weknora_chat_api_key", "dify_adapter_api_key", mode="before"
    )
    @classmethod
    def reject_blank_secret(cls, value: object, info: ValidationInfo) -> object:
        if value is None:
            return None
        plain_value = value.get_secret_value() if isinstance(value, SecretStr) else value
        if info.field_name == "weknora_chat_api_key" and isinstance(plain_value, str):
            if not plain_value.strip():
                return None
        if not isinstance(plain_value, str) or not plain_value.strip():
            raise ValueError("secret must not be blank")
        return value

    @property
    def weknora_api_url(self) -> str:
        """Return the base URL with exactly one `/api/v1` suffix."""

        base_url = str(self.weknora_base_url).rstrip("/")
        if base_url.endswith("/api/v1"):
            return base_url
        return f"{base_url}/api/v1"

    @property
    def weknora_chat_key(self) -> str:
        """Use a dedicated chat key when configured, otherwise the shared key."""

        return (self.weknora_chat_api_key or self.weknora_api_key).get_secret_value()

    @property
    def weknora_kb_ids(self) -> list[str]:
        """Parse the comma-separated default knowledge-base allowlist."""

        return [item.strip() for item in self.weknora_knowledge_base_ids.split(",") if item.strip()]


@lru_cache
def get_settings() -> Settings:
    """Load process settings once."""

    return Settings()
