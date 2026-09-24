"""The detailed HTML log written at the end of every Migrate Data run.

A migration used to leave three partial traces -- a dialog naming the
failed tables, a console panel cleared on restart, and JSONL files nobody
reads -- so answering "which tables, how many rows, and why" needed all
three. This is the single artefact that answers it, and these tests pin
down the parts that make it worth having: the failure text is actually in
there, the validation outcome is distinguishable from success, and it is
self-contained enough to email.
"""
import html as html_mod

from tgdatabridge.core.migrator import MigrationReport, MigrationResult
from tgdatabridge.core.validation import ValidationResult
from tgdatabridge.reports.migration_log import build_migration_log_html


def _report(*results):
    report = MigrationReport()
    report.results = list(results)
    return report


def _ok(name, rows=100, validation=None):
    return MigrationResult(table_name=name, rows_copied=rows, succeeded=True, validation=validation)


def _failed(name, error):
    return MigrationResult(table_name=name, rows_copied=0, succeeded=False, error=error)


def _valid(name, expected=100):
    return ValidationResult(
        table_name=name, expected_rows=expected, actual_rows=expected,
        row_counts_match=True, checksum_checked=True, checksums_match=True)


def _invalid(name, expected=100, actual=97):
    return ValidationResult(
        table_name=name, expected_rows=expected, actual_rows=actual, row_counts_match=False,
        checksum_checked=False)


# ------------------------------------------------------------- structure

def test_produces_a_standalone_html_document():
    out = build_migration_log_html(_report(_ok("companies")))
    assert out.startswith("<!DOCTYPE html>")
    assert "</html>" in out
    # Self-contained: no external stylesheet, script or image to lose when
    # the file is emailed or archived.
    assert "<link" not in out
    assert "<script" not in out
    assert "src=" not in out


def test_summary_counts_every_outcome():
    out = build_migration_log_html(_report(
        _ok("a", rows=10, validation=_valid("a", 10)),
        _ok("b", rows=5, validation=_invalid("b", 5, 4)),
        _failed("c", "boom"),
        MigrationResult(table_name="d", rows_copied=0, succeeded=True, skipped=True),
    ))
    assert "15" in out                    # rows migrated (10 + 5)
    assert "rows migrated" in out
    assert "failed" in out and "unvalidated" in out
    assert "skipped (already done)" in out


# ---------------------------------------------------- the failure detail

def test_full_error_text_for_every_failed_table_is_included():
    """The whole point: a failure must be diagnosable from this file
    alone, without going back to the JSONL or re-running under a
    debugger."""
    error = ("ProtocolViolation: insufficient data left in message\n"
             "  while writing batch 3 of \"companies\"")
    out = build_migration_log_html(_report(_failed("companies", error)))
    assert "Failures" in out
    assert "insufficient data left in message" in out
    assert "while writing batch 3" in out


def test_a_failure_with_no_captured_error_says_so():
    out = build_migration_log_html(_report(_failed("companies", None)))
    assert "no error text was captured" in out


def test_no_failures_section_when_nothing_failed():
    out = build_migration_log_html(_report(_ok("companies")))
    assert "Failures" not in out


# -------------------------------------------------------- the validation

def test_validation_results_are_shown_per_table():
    out = build_migration_log_html(_report(
        _ok("good", 100, _valid("good", 100)),
        _ok("bad", 100, _invalid("bad", 100, 97)),
    ))
    assert "row count ✓" in out
    assert "checksum ✓" in out
    assert "row count ✗" in out
    assert "expected 100" in out and "97" in out


def test_an_unvalidated_table_is_not_reported_as_success():
    out = build_migration_log_html(_report(_ok("bad", 100, _invalid("bad"))))
    assert "Unvalidated" in out
    assert "completed successfully" not in out


def test_a_table_with_no_validation_says_not_run():
    out = build_migration_log_html(_report(_ok("companies", 10, None)))
    assert "not run" in out


# ------------------------------------------------------------- the banner

def test_banner_reflects_a_clean_run():
    out = build_migration_log_html(_report(_ok("a", 10, _valid("a", 10))))
    assert "completed successfully" in out
    assert "banner ok" in out


def test_banner_reflects_failures():
    out = build_migration_log_html(_report(_ok("a"), _failed("b", "boom")))
    assert "finished with errors" in out
    assert "banner bad" in out
    assert "checkpointed" in out          # tells the user re-running resumes


def test_banner_reflects_validation_warnings():
    out = build_migration_log_html(_report(_ok("a", 10, _invalid("a"))))
    assert "validation warnings" in out
    assert "banner warn" in out


# ------------------------------------------------------------- context

def test_run_context_is_rendered_in_order():
    out = build_migration_log_html(
        _report(_ok("a")),
        context={"Schema": "forteai_employees", "Source": "MySQL — localhost/forteai_employees",
                 "Target": "PostgreSQL — localhost/postgres", "Run by": "anil.chavan"})
    assert "forteai_employees" in out
    assert "anil.chavan" in out
    assert out.index("Schema") < out.index("Run by")


def test_the_full_log_is_included():
    records = [
        {"timestamp": "2026-08-11T12:00:00", "level": "info", "actor": "anil", "message": "starting"},
        {"timestamp": "2026-08-11T12:00:05", "level": "error", "actor": "anil", "message": "it broke"},
    ]
    out = build_migration_log_html(_report(_failed("a", "boom")), log_records=records)
    assert "starting" in out
    assert "it broke" in out
    assert "lvl-error" in out           # errors are visually distinguishable


def test_a_very_long_log_is_truncated_and_says_so():
    """A run with per-batch progress across hundreds of tables produces
    tens of thousands of lines; the file has to stay openable."""
    records = [{"timestamp": "t", "level": "info", "actor": "a", "message": f"line {i}"}
               for i in range(12000)]
    out = build_migration_log_html(_report(_ok("a")), log_records=records)
    assert "omitted to keep this file openable" in out
    assert "line 11999" in out          # the tail is kept -- failures live there
    assert "line 0" not in out


# -------------------------------------------------------------- safety

def test_html_in_a_table_name_or_error_is_escaped():
    """Table names come from a source database and error text from a
    driver. Neither is trusted input for an HTML document."""
    out = build_migration_log_html(_report(
        _failed("<script>alert(1)</script>", "<img src=x onerror=alert(2)>")))
    assert "<script>alert(1)</script>" not in out
    assert "&lt;script&gt;" in out
    assert "onerror=alert(2)" not in out or "&lt;img" in out


def test_an_empty_report_still_renders():
    """A run that died before migrating anything is exactly when the log
    is most needed."""
    out = build_migration_log_html(_report())
    assert out.startswith("<!DOCTYPE html>")
    assert "No tables were migrated" in out


def test_sharded_tables_note_their_shard_count():
    result = MigrationResult(table_name="big", rows_copied=1000, succeeded=True, shard_count=8)
    out = build_migration_log_html(_report(result))
    assert "8 shards" in out


def test_durations_and_throughput_are_shown_when_known():
    out = build_migration_log_html(
        _report(_ok("a", rows=10000)), durations={"a": 2.0})
    assert "2.0 s" in out
    assert "5,000/s" in out
