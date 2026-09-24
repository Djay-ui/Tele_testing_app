"""Tests for tgdatabridge.core.spreadsheet_types -- the cell classification,
type combination, coercion and identifier-sanitization rules the Excel/CSV
source is built on.

These are pure functions over plain Python values, so nothing here needs a
real file, a driver, or openpyxl.
"""
import datetime
import decimal

from tgdatabridge.core import spreadsheet_types as st


# ------------------------------------------------------- classify_value

def test_classify_native_python_types():
    assert st.classify_value(None) == (st.NULL, None)
    assert st.classify_value(42) == (st.INT, 42)
    assert st.classify_value(4.5) == (st.FLOAT, 4.5)
    assert st.classify_value(decimal.Decimal("1.5")) == (st.DECIMAL, decimal.Decimal("1.5"))
    assert st.classify_value(b"raw") == (st.BINARY, b"raw")


def test_bool_is_classified_before_int():
    # bool is an int subclass in Python; getting this order wrong would
    # type every TRUE/FALSE column as a number.
    assert st.classify_value(True) == (st.BOOL, True)
    assert st.classify_value(False) == (st.BOOL, False)


def test_datetime_is_classified_before_date():
    # datetime is a date subclass -- same ordering trap as bool/int.
    moment = datetime.datetime(2024, 3, 7, 9, 30)
    assert st.classify_value(moment) == (st.DATETIME, moment)
    day = datetime.date(2024, 3, 7)
    assert st.classify_value(day) == (st.DATE, day)
    clock = datetime.time(9, 30)
    assert st.classify_value(clock) == (st.TIME, clock)


def test_blank_and_whitespace_only_text_is_null():
    assert st.classify_value("") == (st.NULL, None)
    assert st.classify_value("   ") == (st.NULL, None)
    assert st.classify_value("\t\n") == (st.NULL, None)


def test_numeric_looking_text_becomes_numeric():
    assert st.classify_value("42") == (st.INT, 42)
    assert st.classify_value("-7") == (st.INT, -7)
    assert st.classify_value("  19.99  ") == (st.FLOAT, 19.99)
    assert st.classify_value("1e3") == (st.FLOAT, 1000.0)
    assert st.classify_value(".5") == (st.FLOAT, 0.5)


def test_leading_zero_text_stays_text():
    # Zip codes, account numbers, product codes -- converting these to
    # ints would destroy the leading zero irreversibly.
    assert st.classify_value("01234") == (st.STRING, "01234")
    assert st.classify_value("007") == (st.STRING, "007")
    assert st.classify_value("-0012") == (st.STRING, "-0012")


def test_plain_zero_and_sub_one_decimals_are_still_numbers():
    # The leading-zero guard must not catch ordinary numbers.
    assert st.classify_value("0") == (st.INT, 0)
    assert st.classify_value("0.5") == (st.FLOAT, 0.5)
    assert st.classify_value("-0.25") == (st.FLOAT, -0.25)


def test_nan_and_inf_text_stays_text():
    # float() would happily accept these and produce values no database
    # column can store.
    assert st.classify_value("nan")[0] == st.STRING
    assert st.classify_value("inf")[0] == st.STRING
    assert st.classify_value("-Infinity")[0] == st.STRING


def test_boolean_words_become_bools():
    assert st.classify_value("TRUE") == (st.BOOL, True)
    assert st.classify_value("false") == (st.BOOL, False)
    assert st.classify_value("Yes") == (st.BOOL, True)
    assert st.classify_value("no") == (st.BOOL, False)


def test_iso_dates_are_recognized():
    assert st.classify_value("2024-03-07") == (st.DATE, datetime.date(2024, 3, 7))
    assert st.classify_value("2024-03-07 09:30:00") == (st.DATETIME, datetime.datetime(2024, 3, 7, 9, 30))
    assert st.classify_value("2024-03-07T09:30") == (st.DATETIME, datetime.datetime(2024, 3, 7, 9, 30))
    assert st.classify_value("09:30:00") == (st.TIME, datetime.time(9, 30))


def test_ambiguous_slash_dates_stay_text():
    # "03/07/2024" is March 7th or July 3rd depending on where you live;
    # guessing would silently corrupt half the world's spreadsheets.
    assert st.classify_value("03/07/2024") == (st.STRING, "03/07/2024")
    assert st.classify_value("7-Mar-2024")[0] == st.STRING


def test_kept_strings_are_returned_unaltered():
    # Classification strips for *matching* purposes, but a value that
    # stays a string must come back byte-for-byte as it was written.
    assert st.classify_value("  hello  ") == (st.STRING, "  hello  ")


# -------------------------------------------------------- combine_types

def test_combine_single_type():
    assert st.combine_types([st.INT, st.INT, st.INT]) == st.INT


def test_combine_ignores_nulls():
    assert st.combine_types([st.NULL, st.INT, st.NULL]) == st.INT


def test_combine_all_null_stays_null_rather_than_becoming_string():
    # "every sampled row was blank" and "this column holds text" are
    # different findings; from_spreadsheet has a branch that warns about
    # the former, and collapsing it to STRING here would drop that.
    assert st.combine_types([st.NULL, st.NULL]) == st.NULL
    assert st.combine_types([]) == st.NULL


def test_combine_numeric_widening():
    assert st.combine_types([st.INT, st.FLOAT]) == st.FLOAT
    assert st.combine_types([st.INT, st.FLOAT, st.DECIMAL]) == st.DECIMAL
    assert st.combine_types([st.INT, st.DECIMAL]) == st.DECIMAL


def test_combine_date_widening():
    assert st.combine_types([st.DATE, st.DATETIME]) == st.DATETIME


def test_combine_genuine_conflict_falls_back_to_string():
    # Not "most common wins" (which mongo_source_introspector does) --
    # picking int here would make the migration fail on the text rows.
    assert st.combine_types([st.INT, st.INT, st.INT, st.STRING]) == st.STRING
    assert st.combine_types([st.DATE, st.INT]) == st.STRING
    assert st.combine_types([st.BOOL, st.INT]) == st.STRING


# ---------------------------------------------------------- is_conflict

def test_is_conflict_false_for_uniform_and_null_only():
    assert st.is_conflict([st.INT, st.INT]) is False
    assert st.is_conflict([st.NULL, st.NULL]) is False
    assert st.is_conflict([]) is False


def test_is_conflict_false_for_clean_widenings():
    # A money column of 100.50 / 0 / -42.75 is an int+float mix by the
    # letter of the rules and an ordinary decimal column in reality --
    # warning about it would put a scary note on nearly every numeric
    # column of nearly every real spreadsheet.
    assert st.is_conflict([st.INT, st.FLOAT]) is False
    assert st.is_conflict([st.DATE, st.DATETIME]) is False


def test_is_conflict_true_only_for_a_real_fallback_to_text():
    assert st.is_conflict([st.INT, st.STRING]) is True
    assert st.is_conflict([st.DATE, st.INT]) is True
    assert st.is_conflict([st.BOOL, st.STRING]) is True


# --------------------------------------------------------- pivot_category

def test_pivot_category_buckets():
    assert st.pivot_category("NUMBER(9)") == "numeric"
    assert st.pivot_category("BINARY_DOUBLE") == "numeric"
    assert st.pivot_category("BOOLEAN") == "bool"
    assert st.pivot_category("TIMESTAMP") == "datetime"
    assert st.pivot_category("DATE") == "datetime"
    assert st.pivot_category("BLOB") == "binary"
    assert st.pivot_category("VARCHAR2(50)") == "string"
    assert st.pivot_category("CLOB") == "string"


def test_pivot_category_unknown_falls_back_to_string():
    assert st.pivot_category("SOMETHING_ELSE") == "string"
    assert st.pivot_category("") == "string"
    assert st.pivot_category(None) == "string"


# --------------------------------------------------- coerce_for_pivot_type

def test_coerce_blank_is_always_none():
    assert st.coerce_for_pivot_type(None, "NUMBER(9)") is None
    assert st.coerce_for_pivot_type("   ", "VARCHAR2(10)") is None


def test_coerce_numeric_text_into_a_numeric_column():
    assert st.coerce_for_pivot_type("42", "NUMBER(9)") == 42
    assert st.coerce_for_pivot_type("19.99", "BINARY_DOUBLE") == 19.99


def test_coerce_number_into_a_text_column():
    # The key case: inference saw "N/A" in the sample and made the column
    # text, so every numeric row must arrive as text too or the target
    # rejects it.
    assert st.coerce_for_pivot_type(42, "VARCHAR2(10)") == "42"
    assert st.coerce_for_pivot_type(True, "VARCHAR2(10)") == "true"
    assert st.coerce_for_pivot_type(datetime.date(2024, 3, 7), "VARCHAR2(20)") == "2024-03-07"


def test_coerce_date_widens_to_datetime_for_a_timestamp_column():
    # A DATE + DATETIME column types as TIMESTAMP; handing the driver two
    # different Python types for one column invites trouble.
    result = st.coerce_for_pivot_type("2024-03-07", "TIMESTAMP")
    assert result == datetime.datetime(2024, 3, 7, 0, 0)
    assert isinstance(result, datetime.datetime)


def test_coerce_widens_a_date_to_midnight_for_a_date_column():
    """A DATE column is widened too, not just TIMESTAMP.

    This pivot is Oracle-flavoured and Oracle's DATE is a date-*and-time*
    type, so every target maps it to one: DATETIME on MySQL, TIMESTAMP on
    PostgreSQL and Db2, DATETIME2 on SQL Server. Writing a bare
    datetime.date into one of those and reading it back gives a
    datetime.datetime, which made post-migration validation report a
    checksum mismatch on any spreadsheet column of ISO dates -- the data
    identical, the repr() different. This test used to assert the old
    behaviour of leaving the date alone; that behaviour was the bug.
    """
    day = datetime.date(2024, 3, 7)
    result = st.coerce_for_pivot_type(day, "DATE")
    assert result == datetime.datetime(2024, 3, 7, 0, 0)
    assert isinstance(result, datetime.datetime)


def test_coerce_leaves_a_real_datetime_alone_for_a_date_column():
    moment = datetime.datetime(2024, 3, 7, 14, 30)
    assert st.coerce_for_pivot_type(moment, "DATE") == moment


def test_coerce_leaves_a_time_only_column_alone():
    """TIME has no date part to widen, so it must pass through."""
    t = datetime.time(14, 30)
    assert st.coerce_for_pivot_type(t, "TIMESTAMP") == t


def test_coerce_bool_column():
    assert st.coerce_for_pivot_type("yes", "BOOLEAN") is True
    assert st.coerce_for_pivot_type(0, "BOOLEAN") is False


def test_coerce_bool_into_numeric_column():
    assert st.coerce_for_pivot_type(True, "NUMBER(9)") == 1
    assert st.coerce_for_pivot_type(False, "NUMBER(9)") == 0


def test_coerce_text_into_binary_column():
    assert st.coerce_for_pivot_type("hi", "BLOB") == b"hi"


def test_uncoercible_value_is_returned_unchanged_not_nulled():
    # Silently nulling data the user asked to migrate would be the worst
    # possible failure mode -- letting the target driver raise surfaces
    # the problem instead of hiding it.
    assert st.coerce_for_pivot_type("N/A", "NUMBER(9)") == "N/A"
    assert st.coerce_for_pivot_type("not a date", "TIMESTAMP") == "not a date"


# --------------------------------------------------- sanitize_identifier

def test_sanitize_replaces_illegal_characters():
    assert st.sanitize_identifier("Order Items (2024)", "x") == "Order_Items_2024"
    assert st.sanitize_identifier("Unit Price $", "x") == "Unit_Price"


def test_sanitize_preserves_case_and_already_legal_names():
    assert st.sanitize_identifier("customer_id", "x") == "customer_id"
    assert st.sanitize_identifier("CUSTOMER_ID", "x") == "CUSTOMER_ID"


def test_sanitize_prefixes_leading_digit():
    assert st.sanitize_identifier("2024 Sales", "x") == "_2024_Sales"


def test_sanitize_uses_fallback_when_nothing_survives():
    assert st.sanitize_identifier("###", "column_3") == "column_3"
    assert st.sanitize_identifier("", "column_3") == "column_3"
    assert st.sanitize_identifier("   ", "column_3") == "column_3"
    assert st.sanitize_identifier(None, "column_3") == "column_3"


def test_sanitize_truncates_to_a_portable_length():
    # Must stay valid on the tightest target this tool supports.
    result = st.sanitize_identifier("a" * 200, "x")
    assert len(result) == 63


def test_iso_dates_survive_a_round_trip_as_the_same_python_type():
    """Regression for a false post-migration checksum mismatch.

    A spreadsheet column of ISO dates infers as DATE. Oracle's DATE -- the
    pivot this tool uses -- carries a time component, so every target maps
    it to DATETIME/TIMESTAMP/DATETIME2 and returns a datetime.datetime on
    read-back. Writing a bare datetime.date therefore made the source and
    target checksums differ over identical data, and validation reported
    the table "Unvalidated". The coerced value must already be the type
    the target will hand back.
    """
    import datetime as _dt
    coerced = st.coerce_for_pivot_type("2024-01-05", "DATE")
    assert type(coerced) is _dt.datetime
    # ...which is what a DATETIME/TIMESTAMP column returns, so the two
    # sides of validation now hash identically.
    from tgdatabridge.core.validation import row_checksum
    assert row_checksum((coerced,)) == row_checksum((_dt.datetime(2024, 1, 5, 0, 0),))
