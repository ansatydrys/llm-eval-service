import json
import logging

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from app.config import Settings
from app.providers import (
    GenerationRequest,
    PermanentProviderError,
    ProviderUnavailable,
    TransientProviderError,
    build_provider,
)
from app.providers.fake import FakeConfig, FakeProvider
from app.providers.openai import OpenAIProvider


def req(**kw):
    base = dict(prompt="Q: what?", max_output_tokens=100, timeout_s=5, temperature=0,
                case_id="c1", attempt=1, reference_answer="the right answer here")
    return GenerationRequest(**{**base, **kw})


def test_fake_is_deterministic():
    p = FakeProvider(FakeConfig(accuracy=0.5, seed=3))
    outputs = [p.generate(req(case_id=f"c{i}")).text for i in range(20)]
    again = [FakeProvider(FakeConfig(accuracy=0.5, seed=3)).generate(req(case_id=f"c{i}")).text
             for i in range(20)]
    assert outputs == again
    assert "the right answer here" in outputs
    assert any(o != "the right answer here" for o in outputs)


def test_fake_accuracy_extremes():
    assert FakeProvider(FakeConfig(accuracy=1)).generate(req()).text == "the right answer here"
    assert FakeProvider(FakeConfig(accuracy=0)).generate(req()).text != "the right answer here"


def test_fake_degrades_json_answers():
    ref = json.dumps({"name": "A", "age": 1})
    outs = {FakeProvider(FakeConfig(accuracy=0, seed=s)).generate(req(reference_answer=ref)).text
            for s in range(20)}
    assert ref not in outs


def test_fake_failures_and_output_limit():
    p = FakeProvider(FakeConfig(transient_failures={"c1": 2}, permanent_failures=["c9"]))
    with pytest.raises(TransientProviderError):
        p.generate(req(attempt=2))
    assert p.generate(req(attempt=3)).text
    with pytest.raises(PermanentProviderError):
        p.generate(req(case_id="c9"))
    out = FakeProvider(FakeConfig(accuracy=1)).generate(req(max_output_tokens=2))
    assert out.text == "the right" and out.output_tokens == 2


def test_fake_config_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        FakeConfig.model_validate({"api_key": "x"})


def _openai(handler):
    return OpenAIProvider("sk-secret-test", "gpt-test", transport=httpx.MockTransport(handler))


def test_openai_success_and_request_shape():
    seen = {}

    def handler(request: httpx.Request):
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "hi"}}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 2},
        })

    out = _openai(handler).generate(req(max_output_tokens=50))
    assert (out.text, out.input_tokens, out.output_tokens) == ("hi", 7, 2)
    assert seen["auth"] == "Bearer sk-secret-test"
    assert seen["body"]["max_completion_tokens"] == 50 and seen["body"]["model"] == "gpt-test"
    # Reference answers never leave the process.
    assert "the right answer here" not in json.dumps(seen["body"])


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
def test_openai_transient_statuses(status):
    with pytest.raises(TransientProviderError):
        _openai(lambda r: httpx.Response(status)).generate(req())


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_openai_permanent_statuses(status):
    body = {"error": {"message": "bad thing"}}
    with pytest.raises(PermanentProviderError, match="bad thing"):
        _openai(lambda r: httpx.Response(status, json=body)).generate(req())


def test_openai_timeouts_and_connection_errors_are_transient():
    def timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    def refused(request):
        raise httpx.ConnectError("refused", request=request)

    for handler in (timeout, refused):
        with pytest.raises(TransientProviderError):
            _openai(handler).generate(req())


def test_openai_bad_response_shape_is_permanent():
    with pytest.raises(PermanentProviderError):
        _openai(lambda r: httpx.Response(200, json={"choices": []})).generate(req())


def test_openai_errors_do_not_leak_key(caplog):
    caplog.set_level(logging.DEBUG)
    with pytest.raises(PermanentProviderError) as exc:
        _openai(lambda r: httpx.Response(401, json={"error": {"message": "nope"}})).generate(req())
    assert "sk-secret-test" not in str(exc.value)
    assert "sk-secret-test" not in caplog.text


def test_openai_requires_explicit_enable_and_key():
    off = Settings(openai_api_key=SecretStr("k"))
    with pytest.raises(ProviderUnavailable, match="disabled"):
        build_provider("openai", "m", {}, off)
    with pytest.raises(ProviderUnavailable, match="OPENAI_API_KEY"):
        build_provider("openai", "m", {}, Settings(openai_enabled=True, openai_api_key=None))
    with pytest.raises(ProviderUnavailable, match="OPENAI_API_KEY"):
        build_provider("openai", "m", {}, Settings(openai_enabled=True, openai_api_key="  "))
    on = Settings(openai_enabled=True, openai_api_key=SecretStr("k"))
    assert isinstance(build_provider("openai", "m", {}, on), OpenAIProvider)
    assert build_provider("fake", "m", {}, off).is_synthetic


def test_settings_hide_key_and_validate_lease():
    s = Settings(openai_api_key=SecretStr("sk-very-secret"))
    assert "sk-very-secret" not in repr(s) and "sk-very-secret" not in str(s.model_dump())
    with pytest.raises(ValidationError, match="lease_seconds"):
        Settings(lease_seconds=10, max_timeout_s=10)
