"""OpenAI Chat Completions provider. Only constructed when OPENAI_ENABLED=true."""

import httpx

from app.providers.base import (
    Generation,
    GenerationRequest,
    PermanentProviderError,
    TransientProviderError,
)

TRANSIENT_STATUS = {408, 409, 429, 500, 502, 503, 504}


class OpenAIProvider:
    name = "openai"
    is_synthetic = False

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str = "https://api.openai.com/v1",
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.model = model
        self._client = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            transport=transport,
        )

    def generate(self, request: GenerationRequest) -> Generation:
        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": request.prompt}],
            "max_completion_tokens": request.max_output_tokens,
            "temperature": request.temperature,
        }
        try:
            response = self._client.post("/chat/completions", json=body, timeout=request.timeout_s)
        except httpx.TimeoutException as exc:
            raise TransientProviderError(f"timeout: {type(exc).__name__}") from None
        except httpx.TransportError as exc:
            raise TransientProviderError(f"transport error: {type(exc).__name__}") from None

        if response.status_code in TRANSIENT_STATUS:
            raise TransientProviderError(f"HTTP {response.status_code}")
        if response.status_code >= 400:
            # The body may echo request details; keep only the error message.
            raise PermanentProviderError(f"HTTP {response.status_code}: {_error_message(response)}")

        try:
            data = response.json()
            text = data["choices"][0]["message"]["content"] or ""
            usage = data.get("usage") or {}
        except (ValueError, KeyError, IndexError, TypeError):
            raise PermanentProviderError("unexpected response shape") from None
        return Generation(
            text=text,
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
        )


def _error_message(response: httpx.Response) -> str:
    try:
        return str(response.json()["error"]["message"])[:300]
    except (ValueError, KeyError, TypeError):
        return "no error message"
