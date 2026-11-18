from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", hide_input_in_errors=True
    )

    db_host: str = "127.0.0.1"
    db_port: int = Field(default=5432, ge=1, le=65535)
    db_name: str = "siniear"
    db_user: str = "siniear"
    db_password: SecretStr = Field(min_length=1)
    app_env: Literal["development", "test", "production"] = "production"
    internal_inbound_token: SecretStr = SecretStr("")
    internal_outbound_token: SecretStr = SecretStr("")
    internal_peer: str = "C"
    internal_scopes: str = "calls:execute"
    peer_base_url: str = "http://service:8000"
    call_provider: Literal["disabled", "fake", "koreaaitel"] = "disabled"
    allow_paid_calls: bool = False
    public_base_url: str = ""
    koreaaitel_account_id: str = ""
    koreaaitel_api_key: SecretStr = SecretStr("")
    koreaaitel_signing_key: SecretStr = SecretStr("")
    koreaaitel_from_number: SecretStr = SecretStr("")
    gemini_api_key: SecretStr = SecretStr("")
    gemini_live_model: str = ""
    gemini_summary_model: str = ""
    embedding_model: str = "gemini-embedding-001"
    embedding_dimensions: int = 768
    memory_min_score: float = Field(default=0.75, ge=-1, le=1)

    @model_validator(mode="after")
    def validate_providers(self):
        if self.call_provider == "fake" and self.app_env == "production":
            raise ValueError("Fake providers are development/test only")
        if self.embedding_dimensions != 768 or self.embedding_model != "gemini-embedding-001":
            raise ValueError("MVP embedding contract requires gemini-embedding-001/768")
        if self.call_provider == "koreaaitel" and (
            not self.allow_paid_calls
            or not self.public_base_url.startswith("https://")
            or not self.koreaaitel_api_key.get_secret_value()
            or not self.koreaaitel_account_id
            or not self.koreaaitel_signing_key.get_secret_value()
            or not self.koreaaitel_from_number.get_secret_value()
            or not self.gemini_live_model
            or not self.gemini_summary_model
            or not self.gemini_api_key.get_secret_value()
        ):
            raise ValueError("Explicit paid-call enablement and provider settings are required")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
