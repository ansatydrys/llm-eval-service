from dataclasses import dataclass
from typing import Protocol


class ProviderError(Exception):
    kind = "provider_error"


class TransientProviderError(ProviderError):
    """Worth retrying: timeouts, rate limits, 5xx, connection errors."""

    kind = "transient"


class PermanentProviderError(ProviderError):
    """Retrying will not help: bad request, auth, unsupported model."""

    kind = "permanent"


@dataclass(frozen=True)
class GenerationRequest:
    prompt: str
    max_output_tokens: int
    timeout_s: float
    temperature: float
    # Reference data is only consumed by the fake provider to simulate answers.
    # Real providers must never send it anywhere.
    case_id: str
    attempt: int
    reference_answer: str


@dataclass(frozen=True)
class Generation:
    text: str
    input_tokens: int | None
    output_tokens: int | None


class Provider(Protocol):
    name: str
    is_synthetic: bool

    def generate(self, request: GenerationRequest) -> Generation: ...
