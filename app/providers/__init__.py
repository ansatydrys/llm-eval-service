from typing import Any

from app.config import Settings
from app.providers.base import (
    Generation,
    GenerationRequest,
    PermanentProviderError,
    Provider,
    ProviderError,
    TransientProviderError,
)
from app.providers.fake import FakeConfig, FakeProvider
from app.providers.openai import OpenAIProvider

__all__ = [
    "Generation",
    "GenerationRequest",
    "PermanentProviderError",
    "Provider",
    "ProviderError",
    "ProviderUnavailable",
    "SYNTHETIC_PROVIDERS",
    "TransientProviderError",
    "build_provider",
    "provider_available",
]

SYNTHETIC_PROVIDERS = {"fake"}


class ProviderUnavailable(Exception):
    pass


def provider_available(name: str, settings: Settings) -> None:
    if name == "fake":
        return
    if name == "openai":
        if not settings.openai_enabled:
            raise ProviderUnavailable("openai provider is disabled (set OPENAI_ENABLED=true)")
        if settings.openai_api_key is None:
            raise ProviderUnavailable("OPENAI_API_KEY is not set")
        return
    raise ProviderUnavailable(f"unknown provider {name!r}")


def build_provider(name: str, model: str, config: dict[str, Any], settings: Settings) -> Provider:
    provider_available(name, settings)
    if name == "fake":
        return FakeProvider(FakeConfig.model_validate(config.get("fake") or {}))
    assert settings.openai_api_key is not None
    return OpenAIProvider(
        api_key=settings.openai_api_key.get_secret_value(),
        model=model,
        base_url=settings.openai_base_url,
    )
