"""Tests for tgdatabridge.ai.data_quality -- every test injects a fake
AiClient transport, so nothing here touches the network."""
import json

import pytest

from tgdatabridge.ai.ai_client import AiClient, AiError
from tgdatabridge.ai.ai_config import AiConfig
from tgdatabridge.ai.data_quality import MAX_SAMPLE_ROWS, QualityFinding, review_sample


def _client(reply) -> AiClient:
    raw = json.dumps({"content": [{"type": "text", "text": json.dumps(reply)}]}).encode("utf-8")
    return AiClient(AiConfig(enabled=True, api_key="k"), transport=lambda r, t: raw)


def test_no_sample_data_raises_before_calling_the_ai():
    called = []
    client = AiClient(AiConfig(enabled=True, api_key="k"),
                       transport=lambda r, t: called.append(1) or b"{}")
    with pytest.raises(AiError, match="no sample data"):
        review_sample(client, "orders", ["id", "total"], [], 10, 10)
    assert called == []


def test_findings_are_parsed():
    client = _client([
        {"column": "total", "severity": "warning", "description": "several rows have total=0"},
        {"column": "", "severity": "info", "description": "row counts match"},
    ])
    findings = review_sample(client, "orders", ["id", "total"], [[1, 0], [2, 50]], 2, 2)
    assert findings == [
        QualityFinding(column="total", severity="warning", description="several rows have total=0"),
        QualityFinding(column="", severity="info", description="row counts match"),
    ]


def test_an_invalid_severity_falls_back_to_info():
    client = _client([{"column": "", "severity": "apocalyptic", "description": "uh oh"}])
    findings = review_sample(client, "orders", ["id"], [[1]], 1, 1)
    assert findings[0].severity == "info"


def test_findings_referencing_an_unknown_column_are_downgraded_to_table_level():
    client = _client([{"column": "not_a_real_column", "severity": "warning", "description": "x"}])
    findings = review_sample(client, "orders", ["id"], [[1]], 1, 1)
    assert findings[0].column == ""


def test_a_finding_with_an_empty_description_is_dropped():
    client = _client([{"column": "", "severity": "info", "description": "   "}])
    assert review_sample(client, "orders", ["id"], [[1]], 1, 1) == []


def test_sample_is_capped_defensively():
    captured = {}

    def transport(request, timeout):
        body = json.loads(request.data.decode("utf-8"))
        captured["user_prompt"] = body["messages"][0]["content"]
        return json.dumps({"content": [{"type": "text", "text": "[]"}]}).encode("utf-8")

    client = AiClient(AiConfig(enabled=True, api_key="k"), transport=transport)
    huge_sample = [[i] for i in range(MAX_SAMPLE_ROWS + 50)]
    review_sample(client, "orders", ["id"], huge_sample, 1000, 1000)
    # Only MAX_SAMPLE_ROWS row-bullets should appear in the rendered prompt.
    assert captured["user_prompt"].count("- {id=") == MAX_SAMPLE_ROWS


def test_a_long_cell_value_is_truncated():
    long_value = "x" * 500
    client = _client([])
    review_sample(client, "orders", ["notes"], [[long_value]], 1, 1)
    # No assertion needed beyond "did not raise" -- the real check is in
    # the transport-capturing test below.


def test_long_cell_values_are_truncated_in_the_prompt():
    captured = {}

    def transport(request, timeout):
        body = json.loads(request.data.decode("utf-8"))
        captured["user_prompt"] = body["messages"][0]["content"]
        return json.dumps({"content": [{"type": "text", "text": "[]"}]}).encode("utf-8")

    client = AiClient(AiConfig(enabled=True, api_key="k"), transport=transport)
    review_sample(client, "orders", ["notes"], [["x" * 500]], 1, 1)
    assert "x" * 500 not in captured["user_prompt"]
    assert "…" in captured["user_prompt"]


def test_a_non_list_reply_raises_ai_error():
    client = _client({"not": "a list"})
    with pytest.raises(AiError, match="list"):
        review_sample(client, "orders", ["id"], [[1]], 1, 1)
