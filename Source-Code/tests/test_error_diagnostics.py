"""Tests for tgdatabridge.ai.error_diagnostics -- every test injects a fake
AiClient transport, so nothing here touches the network."""
import json

import pytest

from tgdatabridge.ai.ai_client import AiClient, AiError
from tgdatabridge.ai.ai_config import AiConfig
from tgdatabridge.ai.error_diagnostics import Diagnosis, explain_error


def _client(reply) -> AiClient:
    raw = json.dumps({"content": [{"type": "text", "text": json.dumps(reply)}]}).encode("utf-8")
    return AiClient(AiConfig(enabled=True, api_key="k"), transport=lambda r, t: raw)


def test_empty_error_text_raises_before_calling_the_ai():
    called = []
    client = AiClient(AiConfig(enabled=True, api_key="k"),
                       transport=lambda r, t: called.append(1) or b"{}")
    with pytest.raises(AiError, match="no error text"):
        explain_error(client, "   ")
    assert called == []


def test_a_diagnosis_is_parsed():
    client = _client({
        "explanation": "The target rejected the connection because TLS is required.",
        "suggested_fixes": ["Turn on TLS in the connection dialog.", "Check the port number."],
    })
    result = explain_error(client, "SSL required error from server")
    assert result == Diagnosis(
        explanation="The target rejected the connection because TLS is required.",
        suggested_fixes=["Turn on TLS in the connection dialog.", "Check the port number."],
    )


def test_context_is_included_in_the_prompt_when_given():
    captured = {}

    def transport(request, timeout):
        import json as _json
        body = _json.loads(request.data.decode("utf-8"))
        captured["user_prompt"] = body["messages"][0]["content"]
        return json.dumps({"content": [{"type": "text",
                                         "text": json.dumps({"explanation": "x", "suggested_fixes": []})}]}
                           ).encode("utf-8")

    client = AiClient(AiConfig(enabled=True, api_key="k"), transport=transport)
    explain_error(client, "connection refused", context="Connect Target, MySQL")
    assert "Connect Target, MySQL" in captured["user_prompt"]
    assert "connection refused" in captured["user_prompt"]


def test_non_string_items_in_suggested_fixes_are_dropped():
    client = _client({"explanation": "x", "suggested_fixes": ["real fix", 42, None, ""]})
    result = explain_error(client, "some error")
    assert result.suggested_fixes == ["real fix"]


def test_a_non_dict_reply_raises_ai_error():
    client = _client(["not", "a", "dict"])
    with pytest.raises(AiError, match="object"):
        explain_error(client, "some error")
