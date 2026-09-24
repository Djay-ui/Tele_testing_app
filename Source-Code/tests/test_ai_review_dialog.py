"""The AI Review dialog -- tgdatabridge/gui/ai_review_dialog.py. Every test
that would otherwise reach the network patches the module's `AiClient` (or
the feature function it calls) instead, and every test that touches
persisted AI settings monkeypatches APPDATA, exactly like
test_ai_settings_dialog.py."""
import pytest

pytest.importorskip("PySide6", reason="GUI tests need PySide6 installed")

from PySide6.QtWidgets import QApplication  # noqa: E402

from tgdatabridge.ai.ai_client import AiError  # noqa: E402
from tgdatabridge.ai.data_quality import QualityFinding  # noqa: E402
from tgdatabridge.ai.nl_config import NlConfigResult  # noqa: E402
from tgdatabridge.ai.schema_mapper import MappingSuggestion  # noqa: E402
from tgdatabridge.core.schema_model import Column, Schema, Table  # noqa: E402
from tgdatabridge.db.base import ConnectionParams  # noqa: E402
from tgdatabridge.gui.ai_review_dialog import AiReviewDialog  # noqa: E402
from tgdatabridge.utils import ai_settings  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def isolated_appdata(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    return tmp_path


@pytest.fixture()
def ai_on(isolated_appdata):
    settings = ai_settings.AiSettings(enabled=True)
    ai_settings.save_ai_settings(settings)
    ai_settings.save_api_key("sk-test")
    return settings


def _schema():
    return Schema(name="app", source_engine="Oracle", tables=[
        Table(name="orders", schema="app", columns=[
            Column(name="id", data_type="NUMBER(10)", target_type="BIGINT"),
            Column(name="total", data_type="NUMBER(20,2)", target_type="NUMERIC(20,2)"),
        ]),
    ])


def test_features_report_ai_off_when_not_enabled(qt_app, isolated_appdata):
    dialog = AiReviewDialog(_schema(), "PostgreSQL", None)
    dialog._review_mapping()
    assert "AI features are off" in dialog.mapping_result.toPlainText()


def test_a_disabled_feature_flag_is_reported(qt_app, isolated_appdata):
    ai_settings.save_ai_settings(ai_settings.AiSettings(enabled=True, feature_schema_mapping=False))
    ai_settings.save_api_key("sk-test")
    dialog = AiReviewDialog(_schema(), "PostgreSQL", None)
    dialog._review_mapping()
    assert "switched off" in dialog.mapping_result.toPlainText()


def test_mapping_review_shows_flagged_columns(qt_app, ai_on, monkeypatch):
    from tgdatabridge.gui import ai_review_dialog as mod

    def fake_review(client, table, target_engine):
        return [MappingSuggestion(column="total", risk="medium", note="precision risk",
                                   suggested_target_type="NUMERIC(24,4)")]

    monkeypatch.setattr("tgdatabridge.ai.schema_mapper.review_table_mapping", fake_review)

    dialog = AiReviewDialog(_schema(), "PostgreSQL", None)
    dialog.mapping_table_combo.setCurrentText("orders")
    dialog._review_mapping()
    text = dialog.mapping_result.toPlainText()
    assert "MEDIUM" in text
    assert "precision risk" in text
    assert "NUMERIC(24,4)" in text


def test_mapping_review_with_no_findings_says_so(qt_app, ai_on, monkeypatch):
    monkeypatch.setattr("tgdatabridge.ai.schema_mapper.review_table_mapping", lambda c, t, e: [])
    dialog = AiReviewDialog(_schema(), "PostgreSQL", None)
    dialog._review_mapping()
    assert "nothing looked risky" in dialog.mapping_result.toPlainText().lower()


def test_mapping_review_surfaces_an_ai_error(qt_app, ai_on, monkeypatch):
    def raiser(client, table, target_engine):
        raise AiError("provider is down")

    monkeypatch.setattr("tgdatabridge.ai.schema_mapper.review_table_mapping", raiser)
    dialog = AiReviewDialog(_schema(), "PostgreSQL", None)
    dialog._review_mapping()
    assert "provider is down" in dialog.mapping_result.toPlainText()


def test_nl_request_shows_suggested_tables_and_never_auto_selects(qt_app, ai_on, monkeypatch):
    def fake_parse(client, text, tables):
        return NlConfigResult(tables=["orders"], filters={"orders": "skip archived"}, notes="matched orders")

    monkeypatch.setattr("tgdatabridge.ai.nl_config.parse_migration_request", fake_parse)

    dialog = AiReviewDialog(_schema(), "PostgreSQL", None)
    dialog.nl_input.setPlainText("migrate orders, skip archived")
    dialog._parse_nl_request()
    text = dialog.nl_result.toPlainText()
    assert "orders" in text
    assert "skip archived" in text
    assert "Nothing has been selected automatically" in text


def test_quality_review_with_no_target_params_says_so(qt_app, ai_on):
    dialog = AiReviewDialog(_schema(), "PostgreSQL", None)
    dialog._review_quality()
    assert "Connect and migrate" in dialog.quality_result.toPlainText()


def test_quality_review_shows_findings(qt_app, ai_on, monkeypatch):
    dialog = AiReviewDialog(
        _schema(), "PostgreSQL",
        ConnectionParams(host="h", port=1, database="d", username="u", password="p"))

    monkeypatch.setattr(
        dialog, "_sample_target_table",
        lambda table: (["id", "total"], [(1, 0), (2, 50)], 2))
    monkeypatch.setattr(
        "tgdatabridge.ai.data_quality.review_sample",
        lambda *a, **k: [QualityFinding(column="total", severity="warning", description="zero totals")])

    dialog.quality_table_combo.setCurrentText("orders")
    dialog._review_quality()
    text = dialog.quality_result.toPlainText()
    assert "WARNING" in text
    assert "zero totals" in text


def test_quality_review_reports_a_sampling_failure(qt_app, ai_on):
    dialog = AiReviewDialog(
        _schema(), "PostgreSQL",
        ConnectionParams(host="h", port=1, database="d", username="u", password="p"))

    def boom(table):
        raise RuntimeError("driver exploded")

    dialog._sample_target_table = boom
    dialog._review_quality()
    assert "driver exploded" in dialog.quality_result.toPlainText()
