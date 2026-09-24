"""Incremental sync: which strategy a table gets, and the SQL it runs.

The live suite (repack/incremental_live.py) proves rows actually move
between real MariaDB and PostgreSQL. These pin the decisions that are
cheap to get wrong and expensive to notice -- above all the ones that
would make a sync report the whole table as changed on every run, or
quietly skip a change.
"""
from __future__ import annotations

import datetime

import pytest

from tgdatabridge.core import incremental
from tgdatabridge.core.schema_model import Column, Constraint, Table
from tgdatabridge.db import target_sql


def table(name="customers", columns=(("id", "NUMBER"), ("name", "VARCHAR2(80)")),
          key=("id",), extra_constraints=()):
    t = Table(name=name, schema="app",
              columns=[Column(name=n, data_type=d) for n, d in columns])
    if key:
        t.constraints.append(
            Constraint(name=f"{name}_pk", kind="PRIMARY KEY", columns=list(key)))
    t.constraints.extend(extra_constraints)
    return t


# ------------------------------------------------------------- planning

def test_a_timestamp_column_gives_the_cheap_path():
    plan = incremental.plan_table(table(columns=(
        ("id", "NUMBER"), ("name", "VARCHAR2(80)"), ("updated_at", "DATE"))))
    assert plan.strategy == incremental.STRATEGY_TIMESTAMP
    assert plan.change_column == "updated_at"


@pytest.mark.parametrize("name", [
    "updated_at", "updated", "UPDATED_ON", "last_updated", "modified",
    "last_modified", "modified_at", "date_modified", "changed_at", "row_version",
])
def test_the_conventional_names_are_recognised(name):
    plan = incremental.plan_table(table(columns=(
        ("id", "NUMBER"), (name, "TIMESTAMP"))))
    assert plan.change_column == name


def test_a_column_that_is_not_a_date_is_not_used_as_a_watermark():
    """`updated_by VARCHAR` matches the name patterns. Comparing against
    it succeeds, returns the wrong rows, and looks like it worked."""
    plan = incremental.plan_table(table(columns=(
        ("id", "NUMBER"), ("updated_by", "VARCHAR2(40)"))))
    assert plan.strategy == incremental.STRATEGY_COMPARE
    assert plan.change_column is None


def test_a_date_that_is_not_about_change_is_not_used_either():
    for name in ("created_at", "joined_on", "birth_date", "start_date"):
        plan = incremental.plan_table(table(columns=(("id", "NUMBER"), (name, "DATE"))))
        assert plan.change_column is None, name


def test_updated_at_wins_over_created_at():
    plan = incremental.plan_table(table(columns=(
        ("id", "NUMBER"), ("created_at", "DATE"), ("updated_at", "DATE"))))
    assert plan.change_column == "updated_at"


def test_no_timestamp_falls_back_to_comparing():
    plan = incremental.plan_table(table())
    assert plan.strategy == incremental.STRATEGY_COMPARE
    assert "compared" in plan.reason


def test_a_table_with_no_key_is_refused_with_a_reason():
    """An 'update' would have to be delete-everything-and-reinsert, which
    is a full reload wearing an incremental costume."""
    plan = incremental.plan_table(table(key=()))
    assert plan.strategy == incremental.STRATEGY_SKIP
    assert "no primary key" in plan.reason
    assert "Add a key" in plan.reason


def test_a_unique_constraint_is_good_enough_when_there_is_no_primary_key():
    t = table(key=(), extra_constraints=[
        Constraint(name="u", kind="UNIQUE", columns=["id"])])
    plan = incremental.plan_table(t)
    assert plan.strategy != incremental.STRATEGY_SKIP
    assert plan.key_columns == ["id"]


def test_a_composite_key_is_carried_whole():
    t = table(columns=(("a", "NUMBER"), ("b", "NUMBER"), ("v", "VARCHAR2(9)")),
              key=("a", "b"))
    assert incremental.plan_table(t).key_columns == ["a", "b"]


# --------------------------------------------------------- key folding

def test_a_char_key_matches_across_engines():
    """PostgreSQL blank-pads a CHAR(8) on read and MySQL strips it, so the
    same key is 'AA      ' on one side and 'AA' on the other. Compared
    raw, every source row looks new AND every target row looks deleted --
    a sync straight after a clean migration would delete and re-insert the
    whole table."""
    assert incremental._key_of(("AA      ", "x"), [0]) == incremental._key_of(("AA", "y"), [0])


def test_a_date_widened_to_midnight_still_matches():
    """The tool's own type mapping turns a source DATE into a target
    timestamp; that difference must not read as a change on every run."""
    assert incremental._key_of((datetime.date(2026, 1, 1),), [0]) \
        == incremental._key_of((datetime.datetime(2026, 1, 1, 0, 0),), [0])


def test_a_real_difference_still_differs():
    assert incremental._key_of(("AA",), [0]) != incremental._key_of(("AB",), [0])
    assert incremental._key_of((datetime.datetime(2026, 1, 1, 9, 30),), [0]) \
        != incremental._key_of((datetime.datetime(2026, 1, 1, 0, 0),), [0])


# --------------------------------------------------------- target SQL

class PostgresConnector:
    class params:
        schema = "public"


class MySQLConnector:
    class params:
        schema = None


class SQLServerConnector:
    class params:
        schema = "dbo"


class OracleConnector:
    class params:
        schema = "HR"


class Db2Connector:
    class params:
        schema = "HR"


class MongoConnector:
    class params:
        schema = None


COLUMNS = ["id", "name", "updated_at"]


def test_postgres_upserts_with_on_conflict():
    sql = target_sql.upsert_sql(PostgresConnector(), "Customers", COLUMNS, ["id"])
    assert 'INSERT INTO "customers"' in sql          # lowercased, as the DDL created it
    assert 'ON CONFLICT ("id") DO UPDATE SET' in sql
    assert '"name" = EXCLUDED."name"' in sql
    assert '"id" = EXCLUDED."id"' not in sql          # never overwrite the key with itself


def test_mysql_upserts_with_on_duplicate_key():
    sql = target_sql.upsert_sql(MySQLConnector(), "Customers", COLUMNS, ["id"])
    assert "INSERT INTO `Customers`" in sql          # case preserved, as its DDL created it
    assert "ON DUPLICATE KEY UPDATE" in sql
    assert "`name` = VALUES(`name`)" in sql


@pytest.mark.parametrize("connector", [SQLServerConnector, OracleConnector, Db2Connector])
def test_the_merge_engines_use_merge(connector):
    sql = target_sql.upsert_sql(connector(), "customers", COLUMNS, ["id"])
    assert sql.startswith("MERGE INTO")
    assert "WHEN MATCHED THEN UPDATE SET" in sql
    assert "WHEN NOT MATCHED THEN INSERT" in sql


def test_a_key_only_table_has_nothing_to_overwrite():
    assert "DO NOTHING" in target_sql.upsert_sql(
        PostgresConnector(), "t", ["id"], ["id"])
    mysql = target_sql.upsert_sql(MySQLConnector(), "t", ["id"], ["id"])
    assert "`id` = `id`" in mysql                    # MySQL has no DO NOTHING


def test_an_engine_with_no_merge_form_says_so_rather_than_guessing():
    assert target_sql.upsert_sql(MongoConnector(), "t", COLUMNS, ["id"]) is None


def test_each_driver_gets_its_own_bind_marker():
    assert target_sql.placeholder(PostgresConnector(), 1) == "%s"
    assert target_sql.placeholder(MySQLConnector(), 1) == "%s"
    assert target_sql.placeholder(SQLServerConnector(), 1) == "?"
    assert target_sql.placeholder(Db2Connector(), 1) == "?"
    assert target_sql.placeholder(OracleConnector(), 3) == ":3"


def test_objects_are_named_the_way_the_ddl_created_them():
    assert target_sql.identifier(PostgresConnector(), "Employees") == '"employees"'
    assert target_sql.identifier(MySQLConnector(), "Employees") == "`Employees`"
    assert target_sql.identifier(SQLServerConnector(), "Employees") == "[Employees]"
    assert target_sql.identifier(OracleConnector(), "Employees") == '"EMPLOYEES"'
    assert target_sql.identifier(Db2Connector(), "Employees") == '"EMPLOYEES"'


def test_only_the_engines_whose_inserts_qualify_get_a_schema_prefix():
    assert target_sql.qualified(PostgresConnector(), "t") == '"t"'
    assert target_sql.qualified(MySQLConnector(), "t") == "`t`"
    assert target_sql.qualified(SQLServerConnector(), "t") == "[dbo].[t]"
    assert target_sql.qualified(OracleConnector(), "t") == '"HR"."T"'


def test_a_lookup_asks_about_a_whole_batch_of_keys_at_once():
    """One question per batch, not per row -- per-key round trips are what
    make a sync slower than the migration it replaces."""
    sql = target_sql.select_by_keys_sql(
        PostgresConnector(), "t", COLUMNS, ["id"], 3)
    assert sql.count("%s") == 3
    assert " IN (" in sql


def test_a_composite_key_lookup_uses_row_values():
    sql = target_sql.select_by_keys_sql(
        PostgresConnector(), "t", COLUMNS, ["a", "b"], 2)
    assert '("a", "b") IN ((%s, %s), (%s, %s))' in sql


def test_flattening_keys_matches_the_bind_order():
    assert target_sql.flatten_keys([(1, "a"), (2, "b")]) == [1, "a", 2, "b"]


# ------------------------------------------------------------ reporting

def result(**kw):
    base = dict(table_name="t", strategy="timestamp")
    base.update(kw)
    return incremental.TableSyncResult(**base)


def test_an_unchanged_run_says_so_rather_than_showing_zeroes():
    report = incremental.SyncReport(results=[result(), result(table_name="u")])
    assert "Already in sync" in report.headline()


def test_a_changed_run_totals_the_three_kinds():
    report = incremental.SyncReport(results=[
        result(inserted=2, updated=1),
        result(table_name="u", deleted=3),
    ])
    assert report.inserted == 2 and report.updated == 1 and report.deleted == 3
    assert "6 row(s) synced" in report.headline()


def test_a_skipped_table_is_not_counted_as_failed():
    report = incremental.SyncReport(results=[
        result(skipped=True, reason="no primary key")])
    assert report.skipped == ["t"]
    assert report.failed == []
    assert "skipped" in report.results[0].summary()


def test_a_failed_table_is_named():
    report = incremental.SyncReport(results=[result(error="connection lost")])
    assert report.failed == ["t"]
    assert "FAILED" in report.results[0].summary()


def test_a_failed_table_does_not_advance_its_watermark():
    """Advancing past rows that were read but never written would make
    those changes invisible to every future sync."""
    report = incremental.SyncReport(results=[
        result(table_name="ok", watermark="2026-06-01 12:00:00"),
        result(table_name="bad", watermark="2026-06-01 12:00:00", error="boom"),
    ])
    marks = incremental.watermarks_from(report, {"bad": "2026-01-01 00:00:00"})
    assert marks["ok"] == "2026-06-01 12:00:00"
    assert marks["bad"] == "2026-01-01 00:00:00"


def test_a_skipped_table_does_not_advance_either():
    report = incremental.SyncReport(results=[
        result(table_name="t", watermark="2026-06-01", skipped=True)])
    assert incremental.watermarks_from(report, {}) == {}


# --------------------------------------------------------- the watermark

def test_the_watermark_filter_is_inclusive():
    """A row written in the same second as the last run's mark would be
    missed by a strict `>`. Re-reading a few rows is free -- the upsert
    makes them no-ops -- while missing one is silent data loss."""
    class MySQLConnector:
        pass

    plan = incremental.plan_table(table(columns=(
        ("id", "NUMBER"), ("updated_at", "DATE"))))
    sql = incremental._select_source(
        MySQLConnector(), plan, ["id", "updated_at"], "2026-06-01 12:00:00")
    assert ">= '2026-06-01 12:00:00'" in sql


def test_no_watermark_yet_reads_the_whole_table():
    class MySQLConnector:
        pass

    plan = incremental.plan_table(table(columns=(
        ("id", "NUMBER"), ("updated_at", "DATE"))))
    sql = incremental._select_source(MySQLConnector(), plan, ["id", "updated_at"], None)
    assert "WHERE" not in sql


def test_a_datetime_watermark_is_stored_as_text_that_sorts_correctly():
    assert incremental._as_watermark(datetime.datetime(2026, 6, 1, 12, 30)) \
        == "2026-06-01 12:30:00"
    assert incremental._as_watermark(datetime.datetime(2026, 6, 1, 9, 0)) \
        < incremental._as_watermark(datetime.datetime(2026, 6, 1, 12, 30))
    assert incremental._as_watermark(None) is None
