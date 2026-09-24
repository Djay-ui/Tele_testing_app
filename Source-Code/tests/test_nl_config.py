"""Tests for tgdatabridge.ai.nl_config -- every test injects a fake
AiClient transport, so nothing here touches the network."""
import json

import pytest

from tgdatabridge.ai.ai_client import AiClient, AiError
from tgdatabridge.ai.ai_config import AiConfig
from tgdatabridge.ai.nl_config import NlConfigResult, parse_migration_request

_TABLES = ["customers", "orders", "order_items", "employees"]


def _client(reply) -> AiClient:
    raw = json.dumps({"content": [{"type": "text", "text": json.dumps(reply)}]}).encode("utf-8")
    return AiClient(AiConfig(enabled=True, api_key="k"), transport=lambda r, t: raw)


def test_empty_request_text_raises_before_calling_the_ai():
    called = []
    client = AiClient(AiConfig(enabled=True, api_key="k"),
                       transport=lambda r, t: called.append(1) or b"{}")
    with pytest.raises(AiError, match="Type what you want"):
        parse_migration_request(client, "   ", _TABLES)
    assert called == []


def test_no_available_tables_raises_before_calling_the_ai():
    called = []
    client = AiClient(AiConfig(enabled=True, api_key="k"),
                       transport=lambda r, t: called.append(1) or b"{}")
    with pytest.raises(AiError, match="Load the source schema"):
        parse_migration_request(client, "migrate everything", [])
    assert called == []


def test_a_matching_request_selects_the_right_tables():
    client = _client({
        "tables": ["customers", "orders"],
        "filters": {},
        "notes": "Selected customer-related tables.",
    })
    result = parse_migration_request(client, "migrate customer data", _TABLES)
    assert result == NlConfigResult(
        tables=["customers", "orders"], filters={}, notes="Selected customer-related tables.")


def test_filters_are_kept_only_for_selected_known_tables():
    client = _client({
        "tables": ["orders"],
        "filters": {"orders": "skip rows where status = archived", "made_up": "x"},
        "notes": "",
    })
    result = parse_migration_request(client, "migrate active orders", _TABLES)
    assert result.filters == {"orders": "skip rows where status = archived"}


def test_a_hallucinated_table_name_is_dropped():
    client = _client({"tables": ["customers", "not_a_real_table"], "filters": {}, "notes": ""})
    result = parse_migration_request(client, "migrate stuff", _TABLES)
    assert result.tables == ["customers"]


def test_no_confident_match_returns_an_empty_selection_with_notes():
    client = _client({"tables": [], "filters": {}, "notes": "Nothing matched \"widgets\"."})
    result = parse_migration_request(client, "migrate the widgets table", _TABLES)
    assert result.tables == []
    assert "widgets" in result.notes


def test_a_non_dict_reply_raises_ai_error():
    client = _client(["not", "a", "dict"])
    with pytest.raises(AiError, match="object"):
        parse_migration_request(client, "migrate everything", _TABLES)


def test_malformed_tables_field_falls_back_to_empty():
    client = _client({"tables": "not a list", "filters": {}, "notes": ""})
    result = parse_migration_request(client, "migrate everything", _TABLES)
    assert result.tables == []
