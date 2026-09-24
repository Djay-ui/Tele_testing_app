"""Builds a self-contained HTML log of one "Migrate Data" run.

Why this exists
---------------
A migration produced three separate traces, none of them the whole
story: a QMessageBox naming the failed tables, a scrolling console panel
cleared on restart, and JSON-lines files that are correct but nobody
reads. When a run failed, the question "which tables, how many rows, and
*why*" needed all three plus a text editor.

This is the one artefact that answers it. It is written automatically at
the end of every run, saved next to the day's ordinary logs, and holds
everything needed to diagnose the run without going back to the tool:
run context, per-table results, full error text for every failure,
post-migration validation, and the complete log for the run.

Self-contained by design -- inline CSS, no scripts, no external assets --
so it can be emailed to a colleague, attached to a change ticket, or
opened years later from an archive. It follows report_generator.py's
visual language so the two artefacts a run produces look like one tool
made them.
"""
from __future__ import annotations

import datetime
import html
from typing import Dict, List, Optional, Sequence

from tgdatabridge import version

_PRODUCT = version.PRODUCT_TM
_TAGLINE = version.TAGLINE

_CSS = """
body { font-family: -apple-system, Segoe UI, Arial, sans-serif; margin: 32px; color: #1a1a1a; background:#fafafa;}
h1 { margin-bottom: 4px; }
.subtitle { color: #666; margin-bottom: 24px; }
/* Deliberately table-based, not flexbox: this file is meant to be
   emailed, printed to PDF for a change ticket and archived, and flexbox
   is not implemented by Outlook's preview pane, LibreOffice's HTML
   importer or Qt's rich-text engine. A table row renders identically in
   all of them. */
table.summary-cards { border-collapse: separate; width: auto; margin-bottom: 32px; background: none; }
table.summary-cards td { border-bottom: none; padding: 0 16px 0 0; }
.card { background: white; border: 1px solid #e0e0e0; border-radius: 8px; padding: 16px 20px; min-width: 130px; }
.card .value { font-size: 28px; font-weight: 700; }
.card .label { color: #666; font-size: 13px; }
.card.bad .value { color: #c62828; }
.card.warn .value { color: #ef6c00; }
.card.good .value { color: #2e7d32; }
table { border-collapse: collapse; width: 100%; margin-bottom: 32px; background: white; }
th, td { text-align: left; padding: 8px 12px; border-bottom: 1px solid #eee; font-size: 13px; vertical-align: top; }
th { background: #f5f5f5; }
td.num { text-align: right; font-variant-numeric: tabular-nums; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 10px; color: white; font-size: 12px; white-space: nowrap; }
.section-title { margin-top: 40px; }
.banner { padding: 14px 18px; border-radius: 8px; margin-bottom: 28px; font-size: 15px; }
.banner.ok { background: #e8f5e9; border: 1px solid #a5d6a7; color: #1b5e20; }
.banner.warn { background: #fff8e1; border: 1px solid #ffe082; color: #e65100; }
.banner.bad { background: #ffebee; border: 1px solid #ef9a9a; color: #b71c1c; }
table.context th { width: 190px; color: #666; font-weight: 400; background: #fafafa; }
table.context td { font-size: 14px; }
pre.error { background: #fff5f5; border: 1px solid #ffcdd2; border-left: 4px solid #c62828; border-radius: 4px;
            padding: 12px 14px; font-size: 12px; overflow-x: auto; white-space: pre-wrap; word-break: break-word; margin: 0 0 16px 0; }
.failure h3 { margin-bottom: 6px; font-size: 15px; }
pre.log { background: #1e1e1e; color: #e0e0e0; border-radius: 6px; padding: 14px 16px; font-size: 12px;
          overflow-x: auto; white-space: pre-wrap; word-break: break-word; line-height: 1.5; }
pre.log .lvl-error { color: #ff8a80; }
pre.log .lvl-warning { color: #ffcc80; }
.muted { color: #888; }
footer { margin-top: 48px; color: #888; font-size: 12px; border-top: 1px solid #e0e0e0; padding-top: 12px; }
"""

# A run's log can be very long -- a table-per-batch progress line across
# hundreds of tables runs to tens of thousands of entries. Rendering all
# of it produces a file too big to open comfortably, so the tail is kept
# (that is where a failure always is) and the truncation is stated rather
# than silently applied.
_MAX_LOG_LINES = 5000


def _esc(value) -> str:
    return html.escape("" if value is None else str(value))


def _badge(text: str, color: str) -> str:
    return f'<span class="badge" style="background:{color}">{_esc(text)}</span>'


def _status_badge(result) -> str:
    if getattr(result, "skipped", False):
        return _badge("Skipped", "#607d8b")
    if not result.succeeded:
        return _badge("Failed", "#c62828")
    validation = getattr(result, "validation", None)
    if validation is not None and not validation.ok:
        return _badge("Unvalidated", "#ef6c00")
    return _badge("Migrated", "#2e7d32")


def _validation_cell(result) -> str:
    validation = getattr(result, "validation", None)
    if validation is None:
        return '<span class="muted">not run</span>'
    bits: List[str] = []
    if validation.row_counts_match is True:
        bits.append("row count ✓")
    elif validation.row_counts_match is False:
        expected = validation.expected_rows
        actual = validation.actual_rows if validation.actual_rows is not None else "unknown"
        bits.append(f'<span class="issue-error">row count ✗ (expected {_esc(expected)}, target has {_esc(actual)})</span>')
    else:
        bits.append('<span class="muted">row count not checked</span>')

    if validation.checksum_checked:
        bits.append("checksum ✓" if validation.checksums_match else '<span class="issue-error">checksum ✗</span>')
    else:
        bits.append('<span class="muted">checksum not checked</span>')

    if validation.error:
        bits.append(f'<span class="issue-error">{_esc(validation.error)}</span>')
    return "<br>".join(bits)


def _duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return '<span class="muted">—</span>'
    if seconds < 1:
        return f"{seconds * 1000:.0f} ms"
    if seconds < 60:
        return f"{seconds:.1f} s"
    minutes, secs = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m {secs}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def _rate(rows: int, seconds: Optional[float]) -> str:
    if not seconds or seconds <= 0 or not rows:
        return '<span class="muted">—</span>'
    return f"{rows / seconds:,.0f}/s"


def _log_line(record: Dict[str, str]) -> str:
    level = str(record.get("level", "info"))
    timestamp = str(record.get("timestamp", ""))[-8:]
    actor = record.get("actor", "")
    message = _esc(record.get("message", ""))
    cls = f' class="lvl-{_esc(level)}"' if level in ("warning", "error") else ""
    actor_part = f" [{_esc(actor)}]" if actor else ""
    return f"<span{cls}>{_esc(timestamp)}{actor_part} {level.upper():<7} {message}</span>"


def build_migration_log_html(
    report,
    context: Optional[Dict[str, object]] = None,
    log_records: Optional[Sequence[Dict[str, str]]] = None,
    durations: Optional[Dict[str, float]] = None,
) -> str:
    """Render one migration run as a standalone HTML document.

    `report` is a migrator.MigrationReport. `context` is free-form
    run metadata (engines, hosts, schemas, who ran it, timings) rendered
    verbatim in declaration order, so adding a field here needs no change
    to this function. `log_records` are logger.history()-shaped dicts, and
    `durations` maps a table name to its elapsed seconds when known.

    Nothing here raises on a partially-populated report: a run that died
    early still produces a readable document, which is precisely when one
    is most needed.
    """
    context = dict(context or {})
    log_records = list(log_records or [])
    durations = dict(durations or {})
    results = list(getattr(report, "results", []) or [])

    failed = [r for r in results if not r.succeeded]
    skipped = [r for r in results if getattr(r, "skipped", False)]
    unvalidated = [
        r for r in results
        if r.succeeded and not getattr(r, "skipped", False)
        and getattr(r, "validation", None) is not None and not r.validation.ok
    ]
    total_rows = sum(r.rows_copied for r in results)
    generated = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if failed:
        banner = (
            '<div class="banner bad"><strong>Migration finished with errors.</strong> '
            f'{len(failed)} of {len(results)} table(s) failed. Every failure\'s full error text is in '
            '"Failures" below. Progress was checkpointed, so re-running Migrate Data resumes '
            'rather than starting over.</div>'
        )
    elif unvalidated:
        banner = (
            '<div class="banner warn"><strong>Migration completed, with validation warnings.</strong> '
            f'{len(unvalidated)} table(s) copied without error but could not be confirmed against the '
            'target. See "Per-table results".</div>'
        )
    else:
        banner = (
            '<div class="banner ok"><strong>Migration completed successfully.</strong> '
            f'{total_rows:,} row(s) across {len(results)} table(s), validated against the target.</div>'
        )

    cards = [
        ("good" if not failed else "", f"{total_rows:,}", "rows migrated"),
        ("", str(len(results)), "tables"),
        ("bad" if failed else "", str(len(failed)), "failed"),
        ("warn" if unvalidated else "", str(len(unvalidated)), "unvalidated"),
        ("", str(len(skipped)), "skipped (already done)"),
    ]
    cards_html = (
        '<table class="summary-cards"><tr>'
        + "".join(
            f'<td><div class="card {cls}"><div class="value">{_esc(value)}</div>'
            f'<div class="label">{_esc(label)}</div></div></td>'
            for cls, value, label in cards
        )
        + "</tr></table>"
    )

    context_rows = "".join(
        f"<tr><th>{_esc(k)}</th><td>{_esc(v)}</td></tr>" for k, v in context.items()
    ) or '<tr><td class="muted">No run context recorded.</td></tr>'
    context_html = f'<table class="context">{context_rows}</table>'

    rows_html = []
    for r in sorted(results, key=lambda x: (x.succeeded, x.table_name)):
        seconds = durations.get(r.table_name)
        shard_note = ""
        if getattr(r, "shard_count", 1) and r.shard_count > 1:
            shard_note = f'<br><span class="muted">{_esc(r.shard_count)} shards</span>'
        rows_html.append(
            "<tr>"
            f"<td><strong>{_esc(r.table_name)}</strong>{shard_note}</td>"
            f"<td>{_status_badge(r)}</td>"
            f'<td class="num">{r.rows_copied:,}</td>'
            f'<td class="num">{_duration(seconds)}</td>'
            f'<td class="num">{_rate(r.rows_copied, seconds)}</td>'
            f"<td>{_validation_cell(r)}</td>"
            "</tr>"
        )
    table_html = (
        "<table><thead><tr><th>Table</th><th>Status</th><th>Rows</th><th>Duration</th>"
        "<th>Throughput</th><th>Validation</th></tr></thead><tbody>"
        + ("".join(rows_html) or '<tr><td colspan="6" class="muted">No tables were migrated.</td></tr>')
        + "</tbody></table>"
    )

    if failed:
        failures_html = "".join(
            f'<div class="failure"><h3>{_esc(r.table_name)}</h3>'
            f'<pre class="error">{_esc(r.error) or "(no error text was captured)"}</pre></div>'
            for r in failed
        )
        failures_section = f'<h2 class="section-title">Failures</h2>{failures_html}'
    else:
        failures_section = ""

    truncated_note = ""
    if len(log_records) > _MAX_LOG_LINES:
        dropped = len(log_records) - _MAX_LOG_LINES
        log_records = log_records[-_MAX_LOG_LINES:]
        truncated_note = (
            f'<p class="muted">The first {dropped:,} of {dropped + _MAX_LOG_LINES:,} log lines were '
            "omitted to keep this file openable; the most recent are kept. The complete record is in "
            "the day's .jsonl log file alongside this one.</p>"
        )
    log_html = "\n".join(_log_line(rec) for rec in log_records) or "(no log lines were recorded)"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Migration log — {_esc(context.get('Schema', ''))} — {_esc(generated)}</title>
<style>{_CSS}</style>
</head>
<body>
<h1>Data migration log</h1>
<div class="subtitle">{_esc(_PRODUCT)} · {_esc(_TAGLINE)} · generated {_esc(generated)}</div>

{banner}

{cards_html}

<h2 class="section-title">Run context</h2>
{context_html}

<h2 class="section-title">Per-table results</h2>
{table_html}

{failures_section}

<h2 class="section-title">Full log</h2>
{truncated_note}
<pre class="log">{log_html}</pre>

<footer>
Written automatically at the end of the run by {_esc(_PRODUCT)}.
This file is self-contained — it can be emailed, attached to a change ticket, or archived as-is.
</footer>
</body>
</html>
"""
