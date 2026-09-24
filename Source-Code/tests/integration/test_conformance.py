"""The full arc, once per reachable engine.

create namespace -> generate DDL -> apply it -> migrate rows -> validate
-> read the rows back -> drop the namespace.

Every step here is something the unit suite already covers with fakes,
and every step here has nonetheless failed in the field, because a fake
cursor accepts values a server rejects. The three defects this suite was
built after -- binary COPY typing, a pre-existing target table, and the
DATE round trip -- were all invisible to 1,300 passing unit tests and all
visible on the first run of an arc like this one.

Read a failure here as "this engine, this step", not "the tool is
broken": the test id names the engine, and the assertions are written so
the message says which column disagreed rather than that two long tuples
differ.
"""
from __future__ import annotations

import pytest

from tests.integration import fixture_schema, harness

pytestmark = pytest.mark.integration


@pytest.fixture
def rows():
    return fixture_schema.sample_rows()


@pytest.fixture
def loaded(engine, namespace, target, rows):
    """The arc up to and including the migration, since almost every test
    below needs a populated table and re-running the load per assertion
    would triple the suite's wall-clock for no extra coverage."""
    from tgdatabridge.core.migrator import migrate_table

    schema = fixture_schema.sample_schema(namespace, engine.label)
    statements = harness.apply_schema(target, schema, engine.label, namespace)

    source = harness.InMemorySource(fixture_schema.COLUMN_NAMES, rows)
    table = schema.tables[0]
    table.schema = harness.target_schema_for(engine.label, namespace) or namespace
    result = migrate_table(source, target, table, batch_size=25)
    return result, statements, table


# ------------------------------------------------------------- the arc

def test_generated_ddl_is_accepted_by_the_server(engine, namespace, target):
    """The first thing that can go wrong: DDL this tool believes is valid
    for an engine, that the engine won't parse. Nothing downstream is
    meaningful until this holds."""
    schema = fixture_schema.sample_schema(namespace, engine.label)
    statements = harness.apply_schema(target, schema, engine.label, namespace)
    assert statements, "generate_schema_ddl produced nothing to run"


def test_applying_the_same_ddl_twice_is_a_no_op(engine, namespace, target):
    """Every generated CREATE is idempotent by design so that re-running
    "Apply DDL to Target" after a partial failure is safe. That promise
    is only worth anything if the server agrees."""
    schema = fixture_schema.sample_schema(namespace, engine.label)
    harness.apply_schema(target, schema, engine.label, namespace)
    harness.apply_schema(target, schema, engine.label, namespace)   # must not raise


def test_every_row_arrives(loaded, rows):
    result, _statements, _table = loaded
    assert result.error is None
    assert result.succeeded is True
    assert result.rows_copied == len(rows)


def test_validation_confirms_the_load(loaded, rows):
    """A migration that silently copied 59 of 60 rows and reported success
    is the failure this step exists to catch, so an *unrun* validation is
    treated as a failure too."""
    result, _statements, _table = loaded
    validation = result.validation
    assert validation is not None, "validation did not run"
    assert validation.error is None, validation.error
    assert validation.actual_rows == len(rows)
    assert validation.ok, (
        f"row counts match={validation.row_counts_match}, "
        f"checksum checked={validation.checksum_checked}, "
        f"match={validation.checksums_match}")


def test_the_values_survive_the_round_trip(engine, namespace, source_reader,
                                           loaded, rows):
    """Row counts matching proves nothing about the values. This reads
    every row back through a real connector and compares field by field,
    naming the column and the row when they disagree."""
    read = harness.read_back(source_reader, engine.label, namespace,
                             fixture_schema.TABLE_NAME, fixture_schema.COLUMN_NAMES)
    assert len(read) == len(rows)

    by_id = {row[0]: row for row in read}
    for expected in rows:
        actual = by_id.get(expected[0])
        assert actual is not None, f"row id={expected[0]} is missing from the target"
        for name, want, got in zip(fixture_schema.COLUMN_NAMES, expected, actual):
            # No per-engine carve-outs here on purpose. There was one, for
            # MySQL truncating `when_exact` to whole seconds -- and it was
            # hiding a real defect (see test_known_regressions:
            # test_sub_second_precision_is_not_silently_truncated). An
            # exception written into an assertion is a bug with a comment
            # in front of it.
            assert harness.normalise(got) == harness.normalise(want), (
                f"column {name!r} differs on row id={expected[0]}: "
                f"target has {got!r}, source had {want!r}")


def test_an_all_null_column_stays_all_null(engine, namespace, source_reader, loaded):
    """A column with no non-NULL value anywhere is the one bulk-load paths
    get wrong -- there is nothing to infer a type from, and COPY in
    particular has turned these into empty strings before."""
    read = harness.read_back(source_reader, engine.label, namespace,
                             fixture_schema.TABLE_NAME, ["always_null"])
    assert read, "no rows read back"
    assert all(value is None for (value,) in read), (
        f"expected every always_null value to be NULL, got "
        f"{sorted({v for (v,) in read if v is not None})!r}")


def test_nulls_in_a_partly_populated_column_are_preserved(
        engine, namespace, source_reader, loaded, rows):
    """The complement of the test above: NULL must not become '' and ''
    must not become NULL in a column that has both."""
    expected_nulls = sum(1 for row in rows if row[7] is None)
    read = harness.read_back(source_reader, engine.label, namespace,
                             fixture_schema.TABLE_NAME, ["optional"])
    assert sum(1 for (value,) in read if value is None) == expected_nulls


def test_dry_run_agrees_the_target_is_ready(engine, namespace, target, loaded):
    """Dry Run answers "would this work?" without writing anything. If it
    disagrees with the migration that just succeeded, one of them is
    lying."""
    from tgdatabridge.core.migrator import plan_table

    _result, _statements, table = loaded
    source = harness.InMemorySource(fixture_schema.COLUMN_NAMES,
                                    fixture_schema.sample_rows())
    plan = plan_table(source, target, table)
    assert plan.ready is True, plan.warnings


def test_dropping_the_namespace_removes_everything(engine, namespace, loaded):
    """Teardown is part of the contract: this harness is meant to be safe
    to point at a shared development server, which is only true if it
    actually cleans up after itself."""
    engine.drop_namespace(namespace)
    engine.create_namespace(namespace)      # recreate so the fixture's drop is a no-op

    from tgdatabridge.core.connector_factory import make_target_connector
    conn = make_target_connector(engine.label, engine.params_for(namespace))
    conn.connect()
    try:
        from tgdatabridge.core.target_shape import check_table_shape
        problem = check_table_shape(conn, fixture_schema.TABLE_NAME,
                                    fixture_schema.COLUMN_NAMES)
        if problem is None:
            pytest.skip(f"{engine.label} cannot answer a shape check here")
        assert problem.exists is False, (
            f"{fixture_schema.TABLE_NAME} still exists after dropping {namespace}")
    finally:
        conn.close()
