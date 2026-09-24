"""A camelCase column must not make its table unmigratable.

On a PostgreSQL target every object is created lower-cased (quoted), so
`atd_flexCustomerFlag` becomes `atd_flexcustomerflag`. The shape check
folded the *table* name that way but not the *column* names, so it probed
for a column that by design does not exist under that spelling -- and
refused the table before a single row was written, with a message
blaming a pre-existing table.

The effect on a real run: nine tables reported as "already existed with a
different set of columns" against a **brand-new empty database**, where
nothing pre-existed at all. Every one of them had camelCase columns;
every table whose columns happened to be lower-case went through.
"""
import pytest

from tgdatabridge.core.target_shape import check_before_apply_ddl, check_table_shape
from tgdatabridge.core.schema_model import Column, Table


class _Engine:
    """A tiny SQL engine that answers `SELECT cols FROM t WHERE 1=0`
    against a fixed, case-sensitive set of column names -- which is what
    a quoted identifier means on every engine here."""

    def __init__(self, columns, table="accounts"):
        self.columns = set(columns)
        self.table = table
        self.probes = []

    def execute(self, sql, params=None):
        self.probes.append(sql)
        wanted = sql.split(" FROM ")[0][len("SELECT "):].strip()
        if wanted == "*":
            return []
        for part in wanted.split(","):
            name = part.strip().strip('"').strip("`").strip("[]")
            if name not in self.columns:
                raise RuntimeError(f'column "{name}" does not exist')
        return []


class PostgresConnector(_Engine):
    pass


class MySQLConnector(_Engine):
    pass


class OracleConnector(_Engine):
    pass


SOURCE_COLUMNS = ["id", "atd_flexCustomerFlag", "totalSeats", "created_at"]


def test_postgres_columns_are_probed_lower_cased():
    """This is the bug: the target really does hold them lower-cased,
    because that is how the DDL created them."""
    target = PostgresConnector(
        {"id", "atd_flexcustomerflag", "totalseats", "created_at"})
    assert check_table_shape(target, "accounts", SOURCE_COLUMNS) is None


def test_oracle_and_db2_columns_are_probed_upper_cased():
    target = OracleConnector({"ID", "ATD_FLEXCUSTOMERFLAG", "TOTALSEATS", "CREATED_AT"})
    assert check_table_shape(target, "ACCOUNTS", SOURCE_COLUMNS) is None


def test_mysql_keeps_the_case_it_was_given():
    """MySQL does not fold column names, so nothing may be folded here
    either -- doing so would break the engine that was working."""
    target = MySQLConnector({"id", "atd_flexCustomerFlag", "totalSeats", "created_at"})
    assert check_table_shape(target, "accounts", SOURCE_COLUMNS) is None
    lowered = MySQLConnector({"id", "atd_flexcustomerflag", "totalseats", "created_at"})
    problem = check_table_shape(lowered, "accounts", SOURCE_COLUMNS)
    assert problem is not None


def test_a_genuinely_missing_column_is_still_caught():
    target = PostgresConnector({"id", "created_at"})
    problem = check_table_shape(target, "accounts", SOURCE_COLUMNS)
    assert problem is not None
    assert problem.exists
    assert problem.missing_columns == ["atd_flexCustomerFlag", "totalSeats"]


def test_the_missing_column_is_named_the_way_the_source_spells_it():
    """The user has to find it in the source schema, so the message must
    use the source's spelling, not the folded probe."""
    target = PostgresConnector({"id", "created_at"})
    problem = check_table_shape(target, "accounts", SOURCE_COLUMNS)
    assert "atd_flexCustomerFlag" in problem.message
    assert "atd_flexcustomerflag" not in problem.message


def test_a_missing_table_is_still_reported_as_missing_not_mismatched():
    class _NoTable(PostgresConnector):
        def execute(self, sql, params=None):
            raise RuntimeError('relation "accounts" does not exist')

    problem = check_table_shape(_NoTable(set()), "accounts", SOURCE_COLUMNS)
    assert problem is not None and problem.exists is False
    assert "does not exist" in problem.message


def test_the_pre_flight_stops_blocking_a_clean_database():
    """The nine tables in the report were blocked before Apply DDL ran,
    on a database where nothing pre-existed."""
    target = PostgresConnector(
        {"id", "atd_flexcustomerflag", "totalseats", "created_at"})
    tables = [Table(name="accounts", schema="public",
                    columns=[Column(name=c, data_type="VARCHAR2(50)")
                             for c in SOURCE_COLUMNS])]
    assert check_before_apply_ddl(target, tables) == []
