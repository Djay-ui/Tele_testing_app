"""MySQL reports a column's default *unquoted*.

`DEFAULT 'Active'` comes back from information_schema as the bare word
`Active`, and `DEFAULT '0000-00-00 00:00:00'` as `0000-00-00 00:00:00`.
Interpolated into the generated DDL that is not a string at all:

    syntax error at or near "00"
    LINE 24: ..."last_modified" TIMESTAMP NOT NULL DEFAULT 0000-00-00 00:00:00

which is where a 1061-statement PostgreSQL script stopped at 117.
"""
import pytest

from tgdatabridge.core.ddl_generator import generate_schema_ddl
from tgdatabridge.core.mysql_introspector import normalise_default
from tgdatabridge.core.schema_model import Column, Schema, Table
from tgdatabridge.core.tsql_dialect import translate_default


# ------------------------------------------------- reading the default

@pytest.mark.parametrize("raw,column_type,extra,expected", [
    ("Active", "varchar(20)", "", "'Active'"),
    ("pending review", "varchar(40)", "", "'pending review'"),
    ("O'Brien", "varchar(30)", "", "'O''Brien'"),
    ("0000-00-00 00:00:00", "timestamp", "", "'0000-00-00 00:00:00'"),
    ("2020-01-01", "date", "", "'2020-01-01'"),
    ("a,b", "set('a','b')", "", "'a,b'"),
    ("{}", "json", "", "'{}'"),
])
def test_a_literal_default_is_quoted(raw, column_type, extra, expected):
    assert normalise_default(raw, column_type, extra) == expected


@pytest.mark.parametrize("raw,column_type", [
    ("0", "int(11)"),
    ("-1", "bigint"),
    ("0.00", "decimal(10,2)"),
    ("1.5e3", "double"),
])
def test_a_numeric_default_is_left_alone(raw, column_type):
    assert normalise_default(raw, column_type, "") == raw


@pytest.mark.parametrize("raw,extra", [
    ("CURRENT_TIMESTAMP", ""),
    ("CURRENT_TIMESTAMP(6)", ""),
    ("current_timestamp()", ""),
    ("NULL", ""),
    ("(uuid())", "DEFAULT_GENERATED"),
    ("(now() + interval 1 day)", "DEFAULT_GENERATED"),
])
def test_sql_that_is_already_sql_is_not_quoted(raw, extra):
    assert normalise_default(raw, "timestamp", extra) == raw


def test_mariadb_style_already_quoted_values_are_not_double_quoted():
    """MariaDB reports SQL text, quotes included. Quoting again would
    produce the literal string "'Active'" complete with apostrophes."""
    assert normalise_default("'Active'", "varchar(20)", "") == "'Active'"


def test_an_empty_string_default_survives():
    assert normalise_default("", "varchar(20)", "") == "''"
    assert normalise_default(None, "varchar(20)", "") is None


# ------------------------------------------------- the zero date itself

@pytest.mark.parametrize("target", ["PostgreSQL", "MySQL", "Oracle"])
@pytest.mark.parametrize("raw", [
    "'0000-00-00 00:00:00'", "0000-00-00 00:00:00", "'0000-00-00'", "0000-00-00",
    "'0000-00-00 00:00:00.000'",
])
def test_the_zero_date_default_is_dropped_everywhere(raw, target):
    """No engine but MySQL can store it -- and not MySQL 8 in its own
    default sql_mode either. Quoted it is out of range, unquoted it is a
    syntax error; neither is fixable by spelling."""
    clause, issues = translate_default(
        raw, "MySQL", target, column_name="last_modified", target_type="TIMESTAMP")
    if target in ("PostgreSQL", "MySQL"):
        assert clause is None
        assert issues and "zero date" in issues[0].message
        assert "NOT NULL" in issues[0].message, "say what to do about the rows"


def test_a_real_date_default_is_kept():
    clause, issues = translate_default(
        "'2020-01-01 00:00:00'", "MySQL", "PostgreSQL",
        column_name="created", target_type="TIMESTAMP")
    assert clause == "'2020-01-01 00:00:00'"
    assert issues == []


# --------------------------------------------------- the whole script

def _table_with(default, column_type="varchar(20)", pivot="VARCHAR2(20)"):
    schema = Schema(name="app", source_engine="MySQL", target_engine="PostgreSQL")
    schema.tables = [Table(name="t", schema="app", columns=[
        Column(name="id", data_type="NUMBER(10)", nullable=False, identity=True),
        Column(name="c", data_type=pivot, nullable=False,
               default=normalise_default(default, column_type, "")),
    ])]
    return schema


def test_the_generated_postgres_ddl_quotes_a_string_default():
    ddl, _issues = generate_schema_ddl(_table_with("Active"), "PostgreSQL")
    assert "DEFAULT 'Active'" in ddl
    assert "DEFAULT Active" not in ddl


def test_the_generated_postgres_ddl_has_no_bare_zero_date():
    schema = _table_with("0000-00-00 00:00:00", "timestamp", "TIMESTAMP")
    ddl, issues = generate_schema_ddl(schema, "PostgreSQL")
    assert "0000-00-00" not in ddl
    assert any("zero date" in i.message for i in issues)
    # and the column is still created, just without the default
    assert '"c"' in ddl


# ------------------------------------- MariaDB's spelling of the clock

@pytest.mark.parametrize("raw,expected", [
    ("current_timestamp()", "CURRENT_TIMESTAMP"),
    ("CURRENT_TIMESTAMP()", "CURRENT_TIMESTAMP"),
    ("now()", "CURRENT_TIMESTAMP"),
    ("sysdate()", "CURRENT_TIMESTAMP"),
    ("localtimestamp()", "LOCALTIMESTAMP"),
    ("curdate()", "CURRENT_DATE"),
    ("current_date()", "CURRENT_DATE"),
    ("curtime()", "CURRENT_TIME"),
])
def test_mariadbs_parenthesised_clock_functions_translate_for_postgres(raw, expected):
    """MariaDB reports `DEFAULT CURRENT_TIMESTAMP` back as the *call*
    `current_timestamp()`, and PostgreSQL has no parenthesised form:

        syntax error at or near ")"
        LINE 12: "created_at" TIMESTAMP NOT NULL DEFAULT current_timestamp(),
    """
    clause, issues = translate_default(
        raw, "MySQL", "PostgreSQL", column_name="created_at", target_type="TIMESTAMP")
    assert clause == expected
    assert issues == [], "a default with a perfectly good equivalent must not be dropped"


def test_utc_timestamp_keeps_its_meaning_on_postgres():
    clause, _issues = translate_default(
        "utc_timestamp()", "MySQL", "PostgreSQL",
        column_name="created_at", target_type="TIMESTAMP")
    assert "utc" in clause.lower() and "now()" in clause.lower()


def test_an_explicit_precision_is_kept_on_postgres():
    clause, _issues = translate_default(
        "CURRENT_TIMESTAMP(6)", "MySQL", "PostgreSQL",
        column_name="created_at", target_type="TIMESTAMP")
    assert clause == "CURRENT_TIMESTAMP(6)"


@pytest.mark.parametrize("raw", ["curdate()", "curtime()", "utc_date()"])
def test_the_same_functions_are_kept_on_a_mysql_target(raw):
    clause, issues = translate_default(
        raw, "MySQL", "MySQL", column_name="d", target_type="DATE")
    assert clause == f"({raw})", "MySQL 8 wants an expression default parenthesised"
    assert issues == []
