"""Tests for tgdatabridge.ai.schema_mapper -- every test injects a fake
AiClient transport, so nothing here touches the network."""
import json

import pytest

from tgdatabridge.ai.ai_client import AiClient, AiError
from tgdatabridge.ai.ai_config import AiConfig
from tgdatabridge.ai.schema_mapper import MappingSuggestion, review_table_mapping
from tgdatabridge.core.schema_model import Column, Table


def _client(reply) -> AiClient:
    raw = json.dumps({"content": [{"type": "text", "text": json.dumps(reply)}]}).encode("utf-8")
    return AiClient(AiConfig(enabled=True, api_key="k"), transport=lambda r, t: raw)


def _table():
    return Table(
        name="orders", schema="app",
        columns=[
            Column(name="id", data_type="NUMBER(10)", target_type="BIGINT", identity=True),
            Column(name="total", data_type="NUMBER(20,2)", target_type="NUMERIC(20,2)"),
            Column(name="notes", data_type="CLOB", target_type="TEXT"),
        ],
    )


def test_a_table_with_no_columns_returns_no_suggestions():
    client = _client([])
    assert review_table_mapping(client, Table(name="empty", schema="app"), "PostgreSQL") == []


def test_suggestions_are_parsed_for_known_columns():
    client = _client([
        {"column": "id", "risk": "none", "note": "", "suggested_target_type": None},
        {"column": "total", "risk": "medium", "note": "precision could be tight",
         "suggested_target_type": "NUMERIC(24,4)"},
    ])
    suggestions = review_table_mapping(client, _table(), "PostgreSQL")
    assert suggestions == [
        MappingSuggestion(column="id", risk="none", note=""),
        MappingSuggestion(column="total", risk="medium", note="precision could be tight",
                           suggested_target_type="NUMERIC(24,4)"),
    ]


def test_a_suggestion_for_an_unknown_column_is_dropped():
    client = _client([{"column": "made_up_column", "risk": "high", "note": "??"}])
    assert review_table_mapping(client, _table(), "PostgreSQL") == []


def test_an_invalid_risk_level_falls_back_to_none():
    client = _client([{"column": "id", "risk": "catastrophic", "note": "x"}])
    suggestions = review_table_mapping(client, _table(), "PostgreSQL")
    assert suggestions[0].risk == "none"


def test_a_non_list_reply_raises_ai_error():
    client = _client({"not": "a list"})
    with pytest.raises(AiError, match="list"):
        review_table_mapping(client, _table(), "PostgreSQL")


def test_non_dict_items_in_the_reply_are_skipped():
    client = _client(["just a string", {"column": "id", "risk": "none", "note": ""}])
    suggestions = review_table_mapping(client, _table(), "PostgreSQL")
    assert len(suggestions) == 1
    assert suggestions[0].column == "id"
