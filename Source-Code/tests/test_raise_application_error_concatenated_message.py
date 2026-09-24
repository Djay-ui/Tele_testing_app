"""Tests for RAISE_APPLICATION_ERROR with a *computed* message -- built
with Oracle's `||` string concatenation, e.g.
`RAISE_APPLICATION_ERROR(-20001, 'No row found for test_id=' || p_test_id)`
-- across all four SQL-target converters (the "syntax error at or near
'||'" bug).

Each target's real error-raising statement has its own rule for what a
computed message needs, and this file exists because getting that rule
wrong produces DDL that parses fine in this tool's own conversion step and
then fails the moment "Apply DDL to Target" actually runs it:

  - PostgreSQL's RAISE EXCEPTION 'format', args... requires a *literal*
    format string in that position -- `'text'||variable` there is a
    syntax error. Fixed unconditionally via RAISE EXCEPTION USING
    MESSAGE = <expr>, which takes any expression, literal or computed.
  - MySQL's SIGNAL ... SET MESSAGE_TEXT and SQL Server's THROW both
    accept only a literal or a variable, never an inline expression --
    a computed message is left unconverted and flagged for manual
    conversion instead of emitting DDL guaranteed to fail.
  - Db2's SIGNAL ... SET MESSAGE_TEXT already accepts a general
    expression, and Db2's own `||` is identical to Oracle's, so nothing
    needed to change there -- covered here as a regression guard.
"""
from tgdatabridge.core import plsql_converter as pc
from tgdatabridge.core import plsql_mysql_converter as mysql_conv
from tgdatabridge.core import tsql_converter as tsql_conv
from tgdatabridge.core.db2_converter import convert_procedure_or_function as db2_convert
from tgdatabridge.core.plsql_converter import is_simple_string_literal
from tgdatabridge.core.schema_model import Routine

_CONCAT_SRC = (
    "PROCEDURE check_test (p_test_id IN NUMBER) IS\n"
    "BEGIN\n"
    "  RAISE_APPLICATION_ERROR(-20001, 'No row found for test_id=' || p_test_id);\n"
    "END check_test;"
)

_LITERAL_SRC = (
    "PROCEDURE check_test (p_test_id IN NUMBER) IS\n"
    "BEGIN\n"
    "  RAISE_APPLICATION_ERROR(-20001, 'No row found');\n"
    "END check_test;"
)


def _routine(source: str) -> Routine:
    return Routine(name="CHECK_TEST", schema="HR", kind="PROCEDURE", source=source)


# --------------------------------------------------------------- is_simple_string_literal


def test_is_simple_string_literal_true_for_a_bare_literal():
    assert is_simple_string_literal("'No row found'") is True
    assert is_simple_string_literal(" 'padded with spaces' ") is True


def test_is_simple_string_literal_true_for_a_literal_with_an_escaped_quote():
    assert is_simple_string_literal("'it''s fine'") is True


def test_is_simple_string_literal_false_for_concatenation():
    assert is_simple_string_literal("'No row found for test_id=' || p_test_id") is False


def test_is_simple_string_literal_false_for_a_bare_identifier():
    assert is_simple_string_literal("p_test_id") is False


# --------------------------------------------------------------- PostgreSQL


def test_postgres_rewrites_concatenated_message_with_using_message():
    ddl, issues = pc.convert_procedure_or_function(_routine(_CONCAT_SRC))
    assert "||" not in ddl.split("RAISE EXCEPTION")[0]  # sanity: not stripped elsewhere
    assert "RAISE EXCEPTION USING MESSAGE = 'No row found for test_id=' || p_test_id;" in ddl
    # The old, broken form must never appear.
    assert "RAISE EXCEPTION 'No row found for test_id='||p_test_id" not in ddl


def test_postgres_rewrites_plain_literal_message_with_using_message_too():
    # No special-casing needed: USING MESSAGE = <expr> works the same way
    # whether <expr> is a literal or a computed expression.
    ddl, issues = pc.convert_procedure_or_function(_routine(_LITERAL_SRC))
    assert "RAISE EXCEPTION USING MESSAGE = 'No row found';" in ddl


# --------------------------------------------------------------- MySQL


def test_mysql_converts_a_plain_literal_message_to_signal():
    routine = Routine(name="CHECK_TEST", schema="HR", kind="PROCEDURE", source=_LITERAL_SRC)
    ddl = mysql_conv.convert_routine(routine, "MySQL").converted_source
    assert "SIGNAL SQLSTATE '45000'" in ddl
    assert "MESSAGE_TEXT = 'No row found'" in ddl


def test_mysql_flags_a_concatenated_message_instead_of_emitting_invalid_signal():
    routine = Routine(name="CHECK_TEST", schema="HR", kind="PROCEDURE", source=_CONCAT_SRC)
    result = mysql_conv.convert_routine(routine, "MySQL")
    ddl = result.converted_source
    # Left as the (unconverted, though `||` is separately normalized to
    # CONCAT(...) elsewhere in this same pass) RAISE_APPLICATION_ERROR call
    # -- never translated into SIGNAL, which MySQL would reject.
    assert "RAISE_APPLICATION_ERROR(-20001," in ddl
    assert "SIGNAL" not in ddl
    assert any(
        issue.severity == "error" and "computed expression" in issue.message
        for issue in result.issues
    )


# --------------------------------------------------------------- SQL Server


def test_sqlserver_converts_a_plain_literal_message_to_throw():
    ddl, issues = tsql_conv.convert_procedure_or_function(_routine(_LITERAL_SRC))
    assert "THROW 50000, 'No row found', 1;" in ddl
    assert any("error code -20001 was dropped" in i.message for i in issues)


def test_sqlserver_flags_a_concatenated_message_instead_of_emitting_invalid_throw():
    ddl, issues = tsql_conv.convert_procedure_or_function(_routine(_CONCAT_SRC))
    # Left as the (unconverted, though the parameter reference is separately
    # renamed to @p_test_id elsewhere in this same pass) RAISE_APPLICATION_ERROR
    # call -- never translated into THROW, which T-SQL would reject.
    assert "RAISE_APPLICATION_ERROR(-20001," in ddl
    assert "THROW" not in ddl
    assert any(
        issue.severity == "error" and "computed expression" in issue.message
        for issue in issues
    )
    # The "error code dropped" warning only makes sense when a conversion
    # actually happened -- it must not also appear here.
    assert not any("error code -20001 was dropped" in i.message for i in issues)


# --------------------------------------------------------------- Db2 (regression guard)


def test_db2_leaves_a_concatenated_message_as_a_working_expression():
    # Db2's SIGNAL ... SET MESSAGE_TEXT already accepts a general
    # expression, and Db2's `||` is identical to Oracle's -- no rewrite
    # needed, unlike Postgres/MySQL/SQL Server above.
    ddl, issues = db2_convert(_routine(_CONCAT_SRC))
    assert "SIGNAL SQLSTATE '70000' SET MESSAGE_TEXT = 'No row found for test_id=' || p_test_id" in ddl


def test_db2_converts_a_plain_literal_message_unchanged():
    ddl, issues = db2_convert(_routine(_LITERAL_SRC))
    assert "SIGNAL SQLSTATE '70000' SET MESSAGE_TEXT = 'No row found'" in ddl
