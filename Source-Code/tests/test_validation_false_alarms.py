"""Validation must not report a difference the type mapping created.

A real MySQL -> PostgreSQL run reported a 692-row table as a validation
issue with the message "expected 692 row(s), target has 692" -- two
identical numbers, which reads like a bug in the tool. Two separate
faults: a checksum that mismatched for a reason that was not the data,
and a message that never said which check had actually failed.
"""
import datetime
from decimal import Decimal

import pytest

from tgdatabridge.core.validation import (CHECKSUM_ALGORITHM, ValidationResult,
                                   row_checksum, table_checksum)


# ---------------------------------------- differences the mapping creates

def test_a_char_column_padded_by_the_target_still_matches():
    """PostgreSQL, Oracle and Db2 blank-pad CHAR(n) on read; MySQL strips
    it. The same stored value comes back differently from each side."""
    source_row = ("id-0", "abc")
    target_row = ("id-0" + " " * 32, "abc" + " " * 7)
    assert row_checksum(source_row) == row_checksum(target_row)


def test_a_tinyint_that_became_a_boolean_still_matches():
    assert row_checksum((1,)) == row_checksum((True,))
    assert row_checksum((0,)) == row_checksum((False,))


def test_a_date_widened_to_a_timestamp_still_matches():
    """The case this already handled -- kept as a guard so folding the
    two new ones cannot break it."""
    assert row_checksum((datetime.date(2024, 1, 1),)) == \
        row_checksum((datetime.datetime(2024, 1, 1, 0, 0),))


# ------------------------------------------ differences that are real

@pytest.mark.parametrize("a,b", [
    ("x", " x"),                       # leading space is not padding
    ("a b", "ab"),                     # interior whitespace
    ("x", "x\t"),                      # a tab is not a space
    (1, "1"),                          # a silent cast
    (None, ""),                        # NULL is not the empty string
    (Decimal("1.50"), 1.5),            # exact vs floating
    (datetime.datetime(2024, 1, 1, 9, 30), datetime.datetime(2024, 1, 1, 0, 0)),
])
def test_real_corruption_is_still_caught(a, b):
    assert row_checksum((a,)) != row_checksum((b,))


def test_the_table_checksum_is_still_order_independent():
    rows = [("a",), ("b",), ("c",)]
    assert table_checksum(rows) == table_checksum(list(reversed(rows)))


def test_the_algorithm_id_changed_with_the_algorithm():
    """A checkpoint written by the previous version must be recognised as
    having come from a different algorithm rather than compared against
    this one's numbers."""
    assert CHECKSUM_ALGORITHM == "blake2b64-v4"


# ------------------------------------------------------ the message

def test_a_checksum_mismatch_does_not_print_two_identical_numbers():
    result = ValidationResult(
        table_name="team_members", expected_rows=692, actual_rows=692,
        row_counts_match=True, checksum_checked=True, checksums_match=False)
    summary = result.summary
    assert "checksum" in summary
    assert "row count matches (692)" in summary
    assert "target has 692" not in summary


def test_a_row_count_mismatch_says_both_numbers():
    result = ValidationResult(
        table_name="t", expected_rows=692, actual_rows=100, row_counts_match=False)
    assert "692" in result.summary and "100" in result.summary
    assert "row count mismatch" in result.summary


def test_a_check_that_could_not_run_says_so():
    result = ValidationResult(table_name="t", expected_rows=1,
                              error="FakeTarget has no count_rows()")
    assert "could not run" in result.summary
    assert "no count_rows()" in result.summary


def test_a_clean_result_says_validated():
    result = ValidationResult(
        table_name="t", expected_rows=5, actual_rows=5, row_counts_match=True,
        checksum_checked=True, checksums_match=True)
    assert result.ok and result.summary == "validated"


def test_the_gui_does_not_call_an_unverified_table_a_failed_one():
    """Every row was copied and the migration succeeded; what could not
    be confirmed is that what landed is identical. Calling that "failed"
    sends someone hunting for data that is not missing."""
    import inspect

    from tgdatabridge.gui import main_window

    source = inspect.getsource(main_window.MainWindow._migrate_data)
    assert "could not\n" in source or "could not " in source
    assert "failed post-migration validation" not in source
