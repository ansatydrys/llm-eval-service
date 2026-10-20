from functools import lru_cache

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/llm_eval"

    # Dataset limits.
    max_upload_bytes: int = Field(5 * 1024 * 1024, gt=0)
    max_dataset_cases: int = Field(1000, gt=0)

    # Hard ceilings for a run's model config. Requests above them are rejected.
    max_cases_per_run: int = Field(500, gt=0)
    max_attempts_ceiling: int = Field(5, ge=1)
    max_output_tokens_ceiling: int = Field(1024, ge=1)
    max_timeout_s: float = Field(60.0, gt=0)

    # Worker.
    lease_seconds: float = Field(120.0, gt=0)
    poll_interval_s: float = Field(1.0, gt=0)
    retry_backoff_base_s: float = Field(1.0, ge=0)
    retry_backoff_max_s: float = Field(30.0, ge=0)

    # Real providers are off unless explicitly enabled.
    openai_enabled: bool = False
    openai_api_key: SecretStr | None = None
    openai_base_url: str = "https://api.openai.com/v1"

    @field_validator("openai_api_key", mode="before")
    @classmethod
    def _blank_key_is_unset(cls, value: object) -> object:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return value

    @model_validator(mode="after")
    def _lease_outlives_timeout(self) -> "Settings":
        # A lease must outlive the longest provider call, otherwise a healthy but slow
        # worker would lose its job to another worker mid-request.
        if self.lease_seconds <= self.max_timeout_s:
            raise ValueError("lease_seconds must be greater than max_timeout_s")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
