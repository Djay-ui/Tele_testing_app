"""Tests for tgdatabridge.core.validation: the order-independent row checksum and
the validate_table() post-migration comparison used by migrator.py."""
from tgdatabridge.core.validation import ValidationResult, row_checksum, table_checksum, validate_table


# --------------------------------------------------------------- row_checksum


def test_row_checksum_is_deterministic():
    assert row_checksum((1, "a")) == row_checksum((1, "a"))


def test_row_checksum_distinguishes_int_from_string():
    # repr(), not str(), is used specifically so 1 and "1" hash
    # differently -- a silent int-to-string cast somewhere in a target
    # driver's round trip is exactly what this check exists to catch.
    assert row_checksum((1,)) != row_checksum(("1",))


def test_row_checksum_distinguishes_different_rows():
    assert row_checksum((1, "a")) != row_checksum((2, "a"))
    assert row_checksum((1, "a")) != row_checksum((1, "b"))


def test_row_checksum_accepts_lists_and_tuples_equivalently():
    assert row_checksum((1, "a")) == row_checksum([1, "a"])


# ------------------------------------------------------------- table_checksum


def test_table_checksum_is_order_independent():
    rows_a = [(1, "a"), (2, "b"), (3, "c")]
    rows_b = [(3, "c"), (1, "a"), (2, "b")]
    assert table_checksum(rows_a) == table_checksum(rows_b)


def test_table_checksum_empty_is_zero():
    assert table_checksum([]) == 0


def test_table_checksum_differs_when_a_row_changes():
    rows_a = [(1, "a"), (2, "b")]
    rows_b = [(1, "a"), (2, "X")]
    assert table_checksum(rows_a) != table_checksum(rows_b)


def test_table_checksum_differs_when_row_count_changes():
    rows_a = [(1, "a")]
    rows_b = [(1, "a"), (2, "b")]
    assert table_checksum(rows_a) != table_checksum(rows_b)


# ---------------------------------------------------------------- ValidationResult.ok


def test_validation_result_ok_true_when_everything_matches():
    result = ValidationResult(
        table_name="T", expected_rows=5, actual_rows=5,
        row_counts_match=True, checksum_checked=True, checksums_match=True,
    )
    assert result.ok is True


def test_validation_result_ok_false_on_row_count_mismatch():
    result = ValidationResult(table_name="T", expected_rows=5, actual_rows=4, row_counts_match=False)
    assert result.ok is False


def test_validation_result_ok_false_on_checksum_mismatch():
    result = ValidationResult(
        table_name="T", expected_rows=5, actual_rows=5,
        row_counts_match=True, checksum_checked=True, checksums_match=False,
    )
    assert result.ok is False


def test_validation_result_ok_false_on_error():
    result = ValidationResult(table_name="T", expected_rows=5, error="boom")
    assert result.ok is False


def test_validation_result_ok_true_when_checksum_was_never_checked():
    # A skipped checksum (table too large, or the connector has no
    # checksum_rows) is "unverified", not "failed".
    result = ValidationResult(table_name="T", expected_rows=5, actual_rows=5, row_counts_match=True)
    assert result.checksum_checked is False
    assert result.ok is True


# ------------------------------------------------------------------ validate_table


class _FakeTarget:
    def __init__(self, rows_by_table):
        self.rows_by_table = rows_by_table

    def count_rows(self, table_name, schema=None):
        return len(self.rows_by_table.get(table_name, []))

    def checksum_rows(self, table_name, columns, schema=None, sample_size=None):
        return table_checksum(self.rows_by_table.get(table_name, []))


def test_validate_table_matches_when_target_has_the_expected_rows():
    target = _FakeTarget({"ACCOUNT": [(1,), (2,)]})
    checksum = table_checksum([(1,), (2,)])
    result = validate_table(target, "ACCOUNT", ["ACCOUNT_ID"], expected_rows=2, expected_checksum=checksum)
    assert result.row_counts_match is True
    assert result.checksum_checked is True
    assert result.checksums_match is True
    assert result.ok is True


def test_validate_table_flags_row_count_mismatch():
    target = _FakeTarget({"ACCOUNT": [(1,)]})  # only 1 row landed, 2 expected
    result = validate_table(target, "ACCOUNT", ["ACCOUNT_ID"], expected_rows=2)
    assert result.actual_rows == 1
    assert result.row_counts_match is False
    assert result.ok is False


def test_validate_table_flags_checksum_mismatch_even_when_row_count_matches():
    target = _FakeTarget({"ACCOUNT": [(1,), (2,)]})
    wrong_checksum = table_checksum([(1,), (999,)])
    result = validate_table(target, "ACCOUNT", ["ACCOUNT_ID"], expected_rows=2, expected_checksum=wrong_checksum)
    assert result.row_counts_match is True
    assert result.checksums_match is False
    assert result.ok is False


def test_validate_table_skips_checksum_above_checksum_max_rows():
    target = _FakeTarget({"ACCOUNT": [(1,), (2,)]})
    result = validate_table(
        target, "ACCOUNT", ["ACCOUNT_ID"], expected_rows=2,
        expected_checksum=12345, checksum_max_rows=1,
    )
    assert result.row_counts_match is True
    assert result.checksum_checked is False
    assert result.ok is True  # row count alone is enough when checksum was skipped


def test_validate_table_target_without_count_rows_reports_unverified_not_ok():
    class _NoValidationTarget:
        pass

    result = validate_table(_NoValidationTarget(), "ACCOUNT", ["ACCOUNT_ID"], expected_rows=2)
    assert result.error is not None
    assert "count_rows" in result.error
    assert result.ok is False


def test_validate_table_tolerates_a_failing_count_rows_query():
    class _BrokenTarget:
        def count_rows(self, table_name, schema=None):
            raise RuntimeError("table does not exist")

    result = validate_table(_BrokenTarget(), "ACCOUNT", ["ACCOUNT_ID"], expected_rows=2)
    assert result.error == "table does not exist"
    assert result.ok is False
