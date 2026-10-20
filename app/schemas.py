from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.providers.fake import FakeConfig

PROMPT_PLACEHOLDER = "{{question}}"


class CaseIn(BaseModel):
    """One JSONL line of a dataset."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    question: str = Field(min_length=1, max_length=20_000)
    expected_answer: str = Field(max_length=20_000)
    required_facts: list[str] = Field(default_factory=list, max_length=50)
    json_schema: dict[str, Any] | None = None

    @field_validator("required_facts")
    @classmethod
    def _facts_not_blank(cls, facts: list[str]) -> list[str]:
        if any(not f.strip() for f in facts):
            raise ValueError("required facts must be non-empty strings")
        return facts

    @field_validator("json_schema")
    @classmethod
    def _schema_is_valid(cls, schema: dict[str, Any] | None) -> dict[str, Any] | None:
        if schema is not None:
            try:
                Draft202012Validator.check_schema(schema)
            except SchemaError as exc:
                raise ValueError(f"invalid JSON Schema: {exc.message}") from None
        return schema


class DatasetVersionOut(BaseModel):
    dataset: str
    dataset_version_id: int
    version: int
    case_count: int
    content_hash: str
    created: bool
    created_at: datetime


class PromptVersionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template: str = Field(min_length=1, max_length=50_000)

    @field_validator("template")
    @classmethod
    def _has_placeholder(cls, template: str) -> str:
        if PROMPT_PLACEHOLDER not in template:
            raise ValueError(f"template must contain {PROMPT_PLACEHOLDER}")
        return template


class PromptVersionOut(BaseModel):
    prompt: str
    prompt_version_id: int
    version: int
    content_hash: str
    created: bool
    created_at: datetime


class RunConfig(BaseModel):
    """Validated run configuration. Credentials are never accepted here."""

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    model: str = Field("fake-model", min_length=1, max_length=128)
    temperature: float = Field(0.0, ge=0, le=2)
    max_output_tokens: int = Field(256, ge=1)
    timeout_s: float = Field(30.0, gt=0)
    max_attempts: int = Field(3, ge=1)
    fake: FakeConfig | None = None


class RunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset_version_id: int
    prompt_version_id: int
    provider: Literal["fake", "openai"] = "fake"
    config: RunConfig = Field(default_factory=RunConfig)
    max_cases: int | None = Field(None, ge=1)


class RunOut(BaseModel):
    id: int
    status: str
    provider: str
    model: str
    synthetic: bool
    config: dict[str, Any]
    dataset_version_id: int
    prompt_version_id: int
    case_count: int
    progress: dict[str, int]
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class PricingIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["openai"]
    model: str = Field(min_length=1, max_length=128)
    input_usd_per_1m: Decimal = Field(ge=0)
    output_usd_per_1m: Decimal = Field(ge=0)
    effective_from: date
