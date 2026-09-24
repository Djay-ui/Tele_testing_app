"""Every defect that reached a user, pinned against a real server.

Each test here corresponds to a specific failure someone actually hit.
The unit suite covers all of them too, with fakes -- and every one of
them passed the unit suite while the bug was live, which is precisely why
this file exists. A fake cursor cannot reject a wire format, cannot fold
a table name to lower case, and cannot truncate a microsecond.

They are grouped by the report that produced them:

1.  "0 rows migrated"        -- binary COPY typing (PostgreSQL)
2.  "1054 Unknown column"    -- a pre-existing target table
3.  "Unvalidated"            -- the DATE round trip
4.  "Unvalidated"            -- sub-second truncation on MySQL
5.  quoting                  -- identifiers containing the dialect's own
                                delimiter, which C1 escaped everywhere
6.  resume                   -- a checkpoint's checksum has to mean the
                                same thing on the run that reads it
"""
from __future__ import annotations

import datetime
import decimal

import pytest

from tests.integration import fixture_schema, harness

pytestmark = pytest.mark.integration


# ------------------------------------------------------------------ 1
def test_integer_columns_survive_a_bulk_load(engine, namespace, target):
    """PostgreSQL's binary COPY inferred `numeric` for every Python int,
    which int4/int8/bool columns reject outright. COPY is all-or-nothing
    per batch, so a MySQL -> PostgreSQL run reported *0 rows migrated*
    with every non-empty table failed. Fifteen unit tests on insert_batch
    passed throughout: none of them encoded anything.

    Not PostgreSQL-only by design -- every engine gets asked the same
    question, because "the bulk path can't write an integer" is a bug
    worth catching wherever it appears.
    """
    from tgdatabridge.core.migrator import migrate_table

    schema = fixture_schema.sample_schema(namespace, engine.label)
    harness.apply_schema(target, schema, engine.label, namespace)
    table = schema.tables[0]
    table.schema = harness.target_schema_for(engine.label, namespace) or namespace

    rows = fixture_schema.sample_rows(40)
    result = migrate_table(harness.InMemorySource(fixture_schema.COLUMN_NAMES, rows),
                           target, table, batch_size=7)

    assert result.rows_copied == len(rows), result.error
    assert result.succeeded is True, result.error


def test_a_batch_boundary_does_not_lose_rows(engine, namespace, target):
    """The 0-rows bug failed a whole batch at a time, so a batch size that
    divides the row count evenly and one that doesn't are different
    cases. This is the uneven one: 40 rows in batches of 7."""
    from tgdatabridge.core.migrator import migrate_table

    schema = fixture_schema.sample_schema(namespace, engine.label)
    harness.apply_schema(target, schema, engine.label, namespace)
    table = schema.tables[0]
    table.schema = harness.target_schema_for(engine.label, namespace) or namespace

    rows = fixture_schema.sample_rows(40)
    migrate_table(harness.InMemorySource(fixture_schema.COLUMN_NAMES, rows),
                  target, table, batch_size=7)

    assert target.count_rows(
        fixture_schema.TABLE_NAME,
        schema=harness.target_schema_for(engine.label, namespace)) == 40


# ------------------------------------------------------------------ 2
def test_a_preexisting_table_of_the_wrong_shape_is_refused(engine, namespace, target):
    """The field failure: a table of that name already existed, `CREATE
    TABLE IF NOT EXISTS` left it alone and reported success, and the
    insert then failed mid-migration with

        1054 (42S22): Unknown column 'employee_id' in 'field list'

    naming neither the cause nor the fix. The check must fire *before*
    any rows are written, and its message has to be actionable.
    """
    if engine.label == "MongoDB":
        pytest.skip("a collection has no fixed columns to mismatch")

    from tgdatabridge.core.migrator import migrate_table
    from tgdatabridge.utils.identifiers import quote_backtick, quote_bracket, quote_double

    engine_key = engine.label.lower().replace(" ", "")
    quote = (quote_backtick if engine_key.startswith("mysql")
             else quote_bracket if engine_key.startswith("sqlserver")
             else quote_double)
    fold = (str.lower if engine_key.startswith("postgres")
            else str.upper if engine_key.startswith(("oracle", "db2")) else str)

    qualifier = harness.target_schema_for(engine.label, namespace)
    ref = quote(fold(fixture_schema.TABLE_NAME))
    if qualifier:
        ref = f"{quote(fold(qualifier))}.{ref}"

    # Squat on the name with an entirely different shape.
    target.execute_ddl(f"CREATE TABLE {ref} ({quote(fold('other_column'))} INTEGER)")

    schema = fixture_schema.sample_schema(namespace, engine.label)
    harness.apply_schema(target, schema, engine.label, namespace)   # a no-op, silently

    table = schema.tables[0]
    table.schema = qualifier or namespace
    result = migrate_table(harness.InMemorySource(fixture_schema.COLUMN_NAMES,
                                                  fixture_schema.sample_rows(5)),
                           target, table)

    assert result.succeeded is False
    assert result.rows_copied == 0, "rows were written into a table of the wrong shape"
    assert "different set of columns" in (result.error or ""), result.error


def test_dry_run_notices_the_same_thing(engine, namespace, target):
    """Dry Run's whole promise is answering "would this work?" without
    writing. If it says ready and the migration then fails, the promise
    is worthless."""
    if engine.label == "MongoDB":
        pytest.skip("a collection has no fixed columns to mismatch")

    from tgdatabridge.core.migrator import plan_table
    from tgdatabridge.utils.identifiers import quote_backtick, quote_bracket, quote_double

    engine_key = engine.label.lower().replace(" ", "")
    quote = (quote_backtick if engine_key.startswith("mysql")
             else quote_bracket if engine_key.startswith("sqlserver")
             else quote_double)
    fold = (str.lower if engine_key.startswith("postgres")
            else str.upper if engine_key.startswith(("oracle", "db2")) else str)

    qualifier = harness.target_schema_for(engine.label, namespace)
    ref = quote(fold(fixture_schema.TABLE_NAME))
    if qualifier:
        ref = f"{quote(fold(qualifier))}.{ref}"
    target.execute_ddl(f"CREATE TABLE {ref} ({quote(fold('other_column'))} INTEGER)")

    schema = fixture_schema.sample_schema(namespace, engine.label)
    table = schema.tables[0]
    table.schema = qualifier or namespace
    plan = plan_table(harness.InMemorySource(fixture_schema.COLUMN_NAMES,
                                             fixture_schema.sample_rows(5)),
                      target, table)

    assert plan.ready is False
    assert any("different set of columns" in w for w in plan.warnings), plan.warnings


# ------------------------------------------------------------------ 3
def test_a_date_column_does_not_produce_a_false_unvalidated(engine, namespace, target):
    """The pivot is Oracle-flavoured, so a date-only source column becomes
    an Oracle DATE and every target maps it to a timestamp. The source
    driver then returns `datetime.date(2024, 1, 1)` and the target
    returns `datetime.datetime(2024, 1, 1, 0, 0)` for the same value, and
    the checksum -- which hashes `repr()` -- called that a mismatch.

    The result was a table reported as "Unvalidated" with nothing
    whatsoever wrong with it, which reads exactly like data loss. Both
    PostgreSQL and MySQL reproduced it on the first run of this harness.
    """
    from tgdatabridge.core.migrator import migrate_table

    schema = fixture_schema.sample_schema(namespace, engine.label)
    harness.apply_schema(target, schema, engine.label, namespace)
    table = schema.tables[0]
    table.schema = harness.target_schema_for(engine.label, namespace) or namespace

    rows = [(1, 1, decimal.Decimal("1.00"), "x",
             datetime.date(2024, 3, 17),                    # a bare date
             datetime.datetime(2024, 3, 17, 12, 0, 0),
             None, None, None)]
    result = migrate_table(harness.InMemorySource(fixture_schema.COLUMN_NAMES, rows),
                           target, table)

    assert result.validation is not None
    assert result.validation.checksums_match is not False, (
        "a DATE column widened to a timestamp is the documented mapping, "
        "not corruption -- validation must not report it as a mismatch")
    assert result.validation.ok


def test_a_truncated_time_is_still_reported(engine, namespace, target):
    """The complement, and the reason the fix above is narrow: widening a
    date to midnight is the documented mapping, but a target that threw
    away a real 09:30 is data loss and must still fail. Asserted at the
    checksum level, since fabricating a lossy target is not something a
    real engine will do on request."""
    from tgdatabridge.core.validation import row_checksum

    kept = (1, datetime.datetime(2024, 3, 17, 9, 30))
    lost = (1, datetime.datetime(2024, 3, 17, 0, 0))
    assert row_checksum(kept) != row_checksum(lost)


# ------------------------------------------------------------------ 4
def test_sub_second_precision_is_not_silently_truncated(engine, namespace, target,
                                                        source_reader):
    """MySQL's bare DATETIME keeps whole seconds only; fractional digits
    are opt-in as DATETIME(n). The pivot's TIMESTAMP(6) was mapped to
    plain DATETIME, so every microsecond was discarded on write -- real
    data loss, reported to the user only as a checksum mismatch.

    Found by this harness, not by the 1,400 unit tests, because the fake
    cursor stored whatever Python object it was handed.
    """
    from tgdatabridge.core.migrator import migrate_table

    schema = fixture_schema.sample_schema(namespace, engine.label)
    harness.apply_schema(target, schema, engine.label, namespace)
    table = schema.tables[0]
    table.schema = harness.target_schema_for(engine.label, namespace) or namespace

    precise = datetime.datetime(2024, 3, 17, 9, 30, 15, 123456)
    rows = [(1, 1, decimal.Decimal("1.00"), "x", datetime.date(2024, 3, 17),
             precise, None, None, None)]
    migrate_table(harness.InMemorySource(fixture_schema.COLUMN_NAMES, rows),
                  target, table)

    read = harness.read_back(source_reader, engine.label, namespace,
                             fixture_schema.TABLE_NAME, ["when_exact"])
    assert read, "no rows read back"
    (stored,) = read[0]
    assert stored == precise, (
        f"{engine.label} stored {stored!r} for {precise!r} -- "
        f"{precise.microsecond - (stored.microsecond if stored else 0)} "
        f"microseconds lost")


# ------------------------------------------------------------------ 5
_DELIMITER_NAMES = {
    # engine key -> a column name containing that engine's own identifier
    # delimiter, which is what C1's escaping exists to survive.
    "postgresql": 'has"quote',
    "mysql": "has`tick",
    "sqlserver": "has]bracket",
    "db2": 'has"quote',
    "oracle": 'has"quote',
}


def test_an_identifier_containing_the_delimiter_round_trips(engine, namespace, target):
    """Object names come from a third-party source database, and the DDL
    this tool generates is executed against the target. An unescaped
    delimiter in a name is therefore both a broken-DDL bug and an
    injection vector -- C1 routed every identifier through
    tgdatabridge.utils.identifiers for exactly this reason.

    Whether the escaping is *correct* can only be settled by a parser,
    which is to say by a real server.
    """
    if engine.label == "MongoDB":
        pytest.skip("collections have no DDL identifiers to escape")

    from tgdatabridge.core.migrator import migrate_table
    from tgdatabridge.core.schema_model import Column, Schema, Table

    engine_key = engine.label.lower().replace(" ", "")
    awkward = _DELIMITER_NAMES[engine_key]

    table = Table(
        name="it_quoting",
        schema=harness.target_schema_for(engine.label, namespace) or namespace,
        columns=[Column(name="id", data_type="NUMBER(9)", nullable=False),
                 Column(name=awkward, data_type="VARCHAR2(50)")])
    schema = Schema(name=namespace, source_engine="Oracle",
                    target_engine=engine.label, tables=[table])

    harness.apply_schema(target, schema, engine.label, namespace)

    rows = [(1, "value one"), (2, "value two")]
    result = migrate_table(harness.InMemorySource(["id", awkward], rows), target, table)

    assert result.succeeded is True, result.error
    assert result.rows_copied == 2


# ------------------------------------------------------------------ 6
def test_a_resumed_run_agrees_with_an_uninterrupted_one(engine, namespace, target):
    """Resume is only safe if the checksum a checkpoint carries means the
    same thing to the run that reads it. Two migrations of identical
    data -- one in a single pass, one in two -- must produce the same
    checksum, or every resumed run ends "Unvalidated"."""
    from tgdatabridge.core.migrator import migrate_table

    schema = fixture_schema.sample_schema(namespace, engine.label)
    harness.apply_schema(target, schema, engine.label, namespace)
    table = schema.tables[0]
    table.schema = harness.target_schema_for(engine.label, namespace) or namespace

    rows = fixture_schema.sample_rows(30)
    one_pass = migrate_table(
        harness.InMemorySource(fixture_schema.COLUMN_NAMES, rows),
        target, table, batch_size=1000, validate=False)
    many_passes = migrate_table(
        harness.InMemorySource(fixture_schema.COLUMN_NAMES, rows),
        target, table, batch_size=4, validate=False)

    assert one_pass.checksum == many_passes.checksum


def test_the_checkpoint_records_which_algorithm_it_used(engine):
    """A checkpoint written by one build and resumed by another with a
    different row_checksum would compare incomparable numbers. The
    algorithm string is what makes that detectable -- and it has now been
    bumped twice, so this is not theoretical."""
    from tgdatabridge.core.validation import CHECKSUM_ALGORITHM
    assert CHECKSUM_ALGORITHM, "an empty algorithm id cannot detect a mismatch"
