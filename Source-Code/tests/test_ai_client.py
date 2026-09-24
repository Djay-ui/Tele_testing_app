"""Tests for tgdatabridge.ai.ai_client.AiClient -- every test injects a fake
`transport` so nothing here ever touches the network (see AiClient's own
docstring on why that seam exists)."""
import json
import urllib.error

import pytest

from tgdatabridge.ai.ai_client import AiClient, AiError
from tgdatabridge.ai.ai_config import AiConfig


def _claude_config(**overrides):
    fields = dict(enabled=True, provider="claude", api_key="sk-ant-test", model="claude-sonnet-4-5")
    fields.update(overrides)
    return AiConfig(**fields)


def _openai_config(**overrides):
    fields = dict(enabled=True, provider="openai", api_key="sk-test", model="gpt-4o-mini")
    fields.update(overrides)
    return AiConfig(**fields)


class _CapturingTransport:
    """Records the request it was given and returns a canned response."""

    def __init__(self, response_bytes: bytes):
        self.response_bytes = response_bytes
        self.last_request = None

    def __call__(self, request, timeout):
        self.last_request = request
        self.last_timeout = timeout
        return self.response_bytes


def _claude_response(text: str) -> bytes:
    return json.dumps({"content": [{"type": "text", "text": text}]}).encode("utf-8")


def _openai_response(text: str) -> bytes:
    return json.dumps({"choices": [{"message": {"content": text}}]}).encode("utf-8")


# --------------------------------------------------------------- requests


def test_claude_request_shape():
    transport = _CapturingTransport(_claude_response("hi"))
    client = AiClient(_claude_config(), transport=transport)
    result = client.complete("system prompt", "user prompt")
    assert result == "hi"

    req = transport.last_request
    assert req.full_url == "https://api.anthropic.com/v1/messages"
    assert req.headers["X-api-key"] == "sk-ant-test"
    assert req.headers["Anthropic-version"] == "2023-06-01"
    body = json.loads(req.data.decode("utf-8"))
    assert body["model"] == "claude-sonnet-4-5"
    assert body["system"] == "system prompt"
    assert body["messages"] == [{"role": "user", "content": "user prompt"}]


def test_openai_request_shape():
    transport = _CapturingTransport(_openai_response("hi"))
    client = AiClient(_openai_config(), transport=transport)
    result = client.complete("system prompt", "user prompt")
    assert result == "hi"

    req = transport.last_request
    assert req.full_url == "https://api.openai.com/v1/chat/completions"
    assert req.headers["Authorization"] == "Bearer sk-test"
    body = json.loads(req.data.decode("utf-8"))
    assert body["model"] == "gpt-4o-mini"
    assert body["messages"] == [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "user prompt"},
    ]


def test_azure_openai_request_shape():
    cfg = AiConfig(enabled=True, provider="azure_openai", api_key="azkey",
                    base_url="https://my-resource.openai.azure.com",
                    model="my-deployment", azure_api_version="2024-06-01")
    transport = _CapturingTransport(_openai_response("hi"))
    client = AiClient(cfg, transport=transport)
    client.complete("sys", "usr")

    req = transport.last_request
    assert req.full_url == (
        "https://my-resource.openai.azure.com/openai/deployments/"
        "my-deployment/chat/completions?api-version=2024-06-01")
    assert req.headers["Api-key"] == "azkey"
    assert "Authorization" not in req.headers
    body = json.loads(req.data.decode("utf-8"))
    assert "model" not in body  # the deployment name is in the URL, not the body


def test_compatible_provider_with_no_api_key_sends_no_auth_header():
    cfg = AiConfig(enabled=True, provider="compatible", api_key="",
                    base_url="http://localhost:11434", model="llama3")
    transport = _CapturingTransport(_openai_response("hi"))
    client = AiClient(cfg, transport=transport)
    client.complete("sys", "usr")

    req = transport.last_request
    assert req.full_url == "http://localhost:11434/v1/chat/completions"
    assert "Authorization" not in req.headers


def test_compatible_provider_with_an_api_key_sends_bearer_auth():
    cfg = AiConfig(enabled=True, provider="compatible", api_key="local-key",
                    base_url="http://localhost:8000", model="local-model")
    transport = _CapturingTransport(_openai_response("hi"))
    client = AiClient(cfg, transport=transport)
    client.complete("sys", "usr")
    assert transport.last_request.headers["Authorization"] == "Bearer local-key"


def test_an_invalid_config_raises_before_the_transport_is_called():
    called = []

    def transport(request, timeout):
        called.append(request)
        return _claude_response("hi")

    client = AiClient(_claude_config(api_key=""), transport=transport)
    with pytest.raises(AiError, match="API key"):
        client.complete("sys", "usr")
    assert called == []


# -------------------------------------------------------------- responses


def test_claude_response_joins_multiple_text_blocks():
    raw = json.dumps({"content": [
        {"type": "text", "text": "hello "},
        {"type": "tool_use", "id": "x"},  # non-text blocks are ignored
        {"type": "text", "text": "world"},
    ]}).encode("utf-8")
    client = AiClient(_claude_config(), transport=lambda r, t: raw)
    assert client.complete("sys", "usr") == "hello world"


def test_an_unrecognisable_response_shape_raises_ai_error():
    client = AiClient(_claude_config(), transport=lambda r, t: b'{"unexpected": true}')
    with pytest.raises(AiError, match="could not understand"):
        client.complete("sys", "usr")


def test_non_json_response_raises_ai_error():
    client = AiClient(_claude_config(), transport=lambda r, t: b"not json at all")
    with pytest.raises(AiError, match="could not understand"):
        client.complete("sys", "usr")


# ----------------------------------------------------------------- errors


def test_http_error_includes_the_status_and_provider_message():
    def transport(request, timeout):
        body = json.dumps({"error": {"message": "invalid x-api-key"}}).encode("utf-8")
        raise urllib.error.HTTPError(
            request.full_url, 401, "Unauthorized", {}, __import__("io").BytesIO(body))

    client = AiClient(_claude_config(), transport=transport)
    with pytest.raises(AiError, match="401"):
        client.complete("sys", "usr")
    with pytest.raises(AiError, match="invalid x-api-key"):
        client.complete("sys", "usr")


def test_url_error_is_reported_as_unreachable():
    def transport(request, timeout):
        raise urllib.error.URLError("Name or service not known")

    client = AiClient(_claude_config(), transport=transport)
    with pytest.raises(AiError, match="Could not reach"):
        client.complete("sys", "usr")


def test_timeout_is_reported_plainly():
    def transport(request, timeout):
        raise TimeoutError()

    client = AiClient(_claude_config(timeout_seconds=5), transport=transport)
    with pytest.raises(AiError, match="5"):
        client.complete("sys", "usr")


def test_generic_os_error_is_reported():
    def transport(request, timeout):
        raise OSError("network is unreachable")

    client = AiClient(_claude_config(), transport=transport)
    with pytest.raises(AiError, match="Could not reach"):
        client.complete("sys", "usr")


# -------------------------------------------------------------- JSON mode


def test_complete_json_parses_a_plain_json_reply():
    client = AiClient(_claude_config(), transport=lambda r, t: _claude_response('{"a": 1}'))
    assert client.complete_json("sys", "usr") == {"a": 1}


def test_complete_json_strips_a_markdown_fence():
    text = '```json\n{"a": 1}\n```'
    client = AiClient(_claude_config(), transport=lambda r, t: _claude_response(text))
    assert client.complete_json("sys", "usr") == {"a": 1}


def test_complete_json_strips_a_bare_fence_with_no_language_tag():
    text = '```\n[1, 2, 3]\n```'
    client = AiClient(_claude_config(), transport=lambda r, t: _claude_response(text))
    assert client.complete_json("sys", "usr") == [1, 2, 3]


def test_complete_json_raises_ai_error_on_malformed_json():
    client = AiClient(_claude_config(), transport=lambda r, t: _claude_response("not json"))
    with pytest.raises(AiError, match="valid JSON"):
        client.complete_json("sys", "usr")
