"""Tests for tgdatabridge.reports.report_generator's row-capping behavior.

Rendering tens of thousands of <tr>/<li> elements inside QTextBrowser
(Qt's rich-text widget) is what makes the "Assessment Report" tab feel
frozen on a very large schema -- a big Oracle banking core easily has 20k+
objects once tables/views/sequences/routines are all counted. These tests
confirm: small schemas are rendered in full exactly as before, and large
schemas cap what's rendered while always prioritizing the objects that
actually need attention (not fully automatic) over ones that don't.
"""
from tgdatabridge.core.assessment import build_assessment
from tgdatabridge.core.schema_model import Column, ConversionStatus, Schema, Table
from tgdatabridge.reports import report_generator as rg


def _make_table(name, status=ConversionStatus.AUTOMATIC):
    t = Table(name=name, schema="HR", columns=[Column(name="ID", data_type="NUMBER")])
    t.status = status
    return t


def test_small_schema_renders_every_object_with_no_omission_note():
    schema = Schema(name="HR", source_engine="Oracle", target_engine="PostgreSQL")
    schema.tables = [_make_table(f"T{i}") for i in range(5)]
    schema.tables.append(_make_table("BAD_TABLE", ConversionStatus.MANUAL))
    summary = build_assessment(schema)

    html_out = rg.generate_html_report(schema, summary)

    for i in range(5):
        assert f"T{i}</td>" in html_out
    assert "BAD_TABLE</td>" in html_out
    assert "omitted from this table" not in html_out


def test_large_schema_caps_rows_and_prioritizes_flagged_objects():
    schema = Schema(name="HR", source_engine="Oracle", target_engine="PostgreSQL")
    # Comfortably over _MAX_DETAIL_ROWS worth of clean, automatic tables...
    schema.tables = [_make_table(f"AUTO_{i}") for i in range(rg._MAX_DETAIL_ROWS + 500)]
    # ...plus a handful that need attention, added last (worst case for a
    # naive "first N" truncation, which would drop these entirely).
    flagged_names = [f"FLAGGED_{i}" for i in range(10)]
    schema.tables += [_make_table(n, ConversionStatus.MANUAL) for n in flagged_names]

    summary = build_assessment(schema)
    html_out = rg.generate_html_report(schema, summary)

    for name in flagged_names:
        assert f"{name}</td>" in html_out
    assert "omitted from this table" in html_out
    # Total <tr> rows in the detail table should be capped, not one per object.
    assert html_out.count("<tr><td>AUTO_") + html_out.count("<tr><td>FLAGGED_") == rg._MAX_DETAIL_ROWS


def test_action_items_are_capped_with_a_note():
    schema = Schema(name="HR", source_engine="Oracle", target_engine="PostgreSQL")
    schema.tables = [_make_table(f"BAD_{i}", ConversionStatus.MANUAL) for i in range(rg._MAX_ACTION_ITEMS_SHOWN + 50)]
    summary = build_assessment(schema)
    assert len(summary.action_items) == rg._MAX_ACTION_ITEMS_SHOWN + 50

    html_out = rg.generate_html_report(schema, summary)
    assert html_out.count("<li>Table BAD_") == rg._MAX_ACTION_ITEMS_SHOWN
    assert "50 more action item(s) not shown" in html_out


def test_summary_counts_are_unaffected_by_detail_row_capping():
    # The cards/summary counts must always reflect every object, even when
    # the detail table itself is capped -- capping is presentation-only.
    schema = Schema(name="HR", source_engine="Oracle", target_engine="PostgreSQL")
    schema.tables = [_make_table(f"T{i}") for i in range(rg._MAX_DETAIL_ROWS + 100)]
    summary = build_assessment(schema)
    html_out = rg.generate_html_report(schema, summary)
    assert str(summary.total_objects) in html_out
    assert summary.total_objects == rg._MAX_DETAIL_ROWS + 100
