"""Builds a self-contained HTML migration assessment report."""
from __future__ import annotations

import datetime
import html
from typing import List

from tgdatabridge import version
from tgdatabridge.core.assessment import AssessmentSummary
from tgdatabridge.core.schema_model import ConversionStatus, Schema

# Read once into module-level names purely so the f-string templates below
# stay readable -- `{version.PRODUCT_TM}` inside a 60-line HTML f-string is
# much harder to scan than `{_PRODUCT}`.
_PRODUCT = version.PRODUCT_TM
_TAGLINE = version.TAGLINE
_VENDOR = version.VENDOR

_STATUS_COLOR = {
    ConversionStatus.AUTOMATIC.value: "#2e7d32",
    ConversionStatus.AUTOMATIC_WITH_WARNINGS.value: "#ef6c00",
    ConversionStatus.MANUAL.value: "#c62828",
    ConversionStatus.NOT_SUPPORTED.value: "#6a1b9a",
}

_CSS = """
body { font-family: -apple-system, Segoe UI, Arial, sans-serif; margin: 32px; color: #1a1a1a; background:#fafafa;}
h1 { margin-bottom: 2px; }
.tagline { color: #4a6fa5; font-size: 13px; letter-spacing: 0.3px; margin-bottom: 6px; }
.subtitle { color: #666; margin-bottom: 24px; }
.summary-cards { display: flex; gap: 16px; margin-bottom: 32px; flex-wrap: wrap;}
.card { background: white; border: 1px solid #e0e0e0; border-radius: 8px; padding: 16px 20px; min-width: 160px; }
.card .value { font-size: 28px; font-weight: 700; }
.card .label { color: #666; font-size: 13px; }
table { border-collapse: collapse; width: 100%; margin-bottom: 32px; background: white; }
th, td { text-align: left; padding: 8px 12px; border-bottom: 1px solid #eee; font-size: 13px; }
th { background: #f5f5f5; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 10px; color: white; font-size: 12px; }
.section-title { margin-top: 40px; }
.action-items li { margin-bottom: 6px; }
.issue-warning { color: #ef6c00; }
.issue-error { color: #c62828; }
.issue-info { color: #1565c0; }
"""


# Rendering tens of thousands of <tr>/<li> elements inside QTextBrowser
# (Qt's rich-text widget, used for the "Assessment Report" tab) is slow
# enough on a large schema -- a big Oracle banking core easily has 20k+
# objects once tables, views, sequences, and routines/triggers are all
# counted -- that the report tab can feel frozen well before it finishes
# rendering. Cap what's actually put in the HTML, prioritizing the objects
# that need attention (anything not fully automatic) over ones that don't,
# and say plainly how many were left out rather than silently truncating.
_MAX_DETAIL_ROWS = 3000
_MAX_ACTION_ITEMS_SHOWN = 200


def _badge(status: str) -> str:
    color = _STATUS_COLOR.get(status, "#555")
    return f'<span class="badge" style="background:{color}">{html.escape(status)}</span>'


def _object_row(obj, kind: str) -> str:
    issues = "<br>".join(
        f'<span class="issue-{i.severity}">[{i.severity}] {html.escape(i.message)}</span>'
        for i in obj.issues
    ) or "&mdash;"
    return (
        f"<tr><td>{html.escape(obj.name)}</td><td>{kind}</td>"
        f"<td>{_badge(obj.status.value)}</td><td>{issues}</td></tr>"
    )


def _collect_object_rows(schema: Schema) -> List[tuple]:
    """Every object's rendered row, paired with whether it's fully
    automatic -- so the caller can prioritize the flagged ones when there
    are more objects than _MAX_DETAIL_ROWS can comfortably hold."""
    rows = []
    for obj in schema.tables:
        rows.append((obj.status == ConversionStatus.AUTOMATIC, _object_row(obj, "Table")))
    for obj in schema.views:
        rows.append((obj.status == ConversionStatus.AUTOMATIC, _object_row(obj, "View")))
    for obj in schema.sequences:
        rows.append((obj.status == ConversionStatus.AUTOMATIC, _object_row(obj, "Sequence")))
    for obj in schema.routines:
        rows.append((obj.status == ConversionStatus.AUTOMATIC, _object_row(obj, "Routine/Trigger")))
    return rows


def generate_html_report(schema: Schema, summary: AssessmentSummary) -> str:
    generated_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    status_cards = "".join(
        f'<div class="card"><div class="value">{count}</div>'
        f'<div class="label">{html.escape(status)}</div></div>'
        for status, count in summary.counts_by_status.items()
    )

    type_rows = "".join(
        f"<tr><td>{html.escape(t)}</td><td>{c}</td></tr>"
        for t, c in summary.counts_by_type.items()
    )

    shown_action_items = summary.action_items[:_MAX_ACTION_ITEMS_SHOWN]
    action_items = (
        "".join(f"<li>{html.escape(item)}</li>" for item in shown_action_items)
        or "<li>None &mdash; full schema converted automatically.</li>"
    )
    if len(summary.action_items) > _MAX_ACTION_ITEMS_SHOWN:
        remaining = len(summary.action_items) - _MAX_ACTION_ITEMS_SHOWN
        action_items += (
            f'<li style="color:#666;">…and {remaining} more action item(s) not shown here.</li>'
        )

    # Prioritize objects that need attention over fully-automatic ones when
    # there isn't room to show everything -- see _MAX_DETAIL_ROWS above.
    all_rows = _collect_object_rows(schema)
    flagged_html = [row_html for is_automatic, row_html in all_rows if not is_automatic]
    automatic_html = [row_html for is_automatic, row_html in all_rows if is_automatic]

    shown_rows = flagged_html[:_MAX_DETAIL_ROWS]
    remaining_capacity = _MAX_DETAIL_ROWS - len(shown_rows)
    if remaining_capacity > 0:
        shown_rows += automatic_html[:remaining_capacity]

    object_rows = "".join(shown_rows)
    omitted_count = len(all_rows) - len(shown_rows)
    detail_note = ""
    if omitted_count > 0:
        detail_note = (
            f'<p style="color:#666;font-size:13px;">Showing {len(shown_rows)} of {len(all_rows)} '
            f'objects here, prioritizing the {len(flagged_html)} that need attention -- '
            f'{omitted_count} fully-automatic object(s) are omitted from this table for '
            f'readability at this schema size. The counts above cover every object.</p>'
        )

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Migration Assessment Report — {html.escape(schema.name)}</title>
<style>{_CSS}</style>
</head>
<body>
  <h1>{_PRODUCT}</h1>
  <div class="tagline">{_TAGLINE}</div>
  <div class="subtitle">Database Migration Assessment Report &mdash; generated {generated_at}</div>

  <div class="summary-cards">
    <div class="card"><div class="value">{html.escape(summary.source_engine)} &rarr; {html.escape(summary.target_engine)}</div><div class="label">Migration path</div></div>
    <div class="card"><div class="value">{summary.total_objects}</div><div class="label">Schema objects analyzed</div></div>
    <div class="card"><div class="value">{summary.automatic_pct}%</div><div class="label">Converted automatically</div></div>
    <div class="card"><div class="value">{summary.estimated_manual_hours}h</div><div class="label">Estimated manual effort</div></div>
    {status_cards}
  </div>

  <h2 class="section-title">Objects by type</h2>
  <table>
    <tr><th>Object type</th><th>Count</th></tr>
    {type_rows}
  </table>

  <h2 class="section-title">Action items</h2>
  <ul class="action-items">{action_items}</ul>

  <h2 class="section-title">Object-level conversion detail</h2>
  {detail_note}
  <table>
    <tr><th>Name</th><th>Type</th><th>Status</th><th>Issues</th></tr>
    {object_rows}
  </table>

  <p style="color:#999; font-size:12px; margin-top:40px;">
    Generated by {_PRODUCT} ({_VENDOR}). This report is an automated
    assessment; always validate converted DDL and stored routines against the target
    engine in a non-production environment before use.
  </p>
</body>
</html>"""
