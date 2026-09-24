"""Tests for Oracle's `SAVEPOINT name;` / `ROLLBACK TO name;` mid-procedure
checkpoint idiom -- most commonly seen as:

    SAVEPOINT before_update;
    ...
    EXCEPTION WHEN OTHERS THEN
      ROLLBACK TO before_update;
      RAISE_APPLICATION_ERROR(-20001, ...);

-- across all four SQL-target converters (the "syntax error at or near
'TO'" bug).

PL/pgSQL has no equivalent executable SAVEPOINT/ROLLBACK TO statement
inside a function/procedure body at all: entering a BEGIN...EXCEPTION...END
block already establishes an implicit savepoint and rolls back to it
automatically the instant an exception is caught, before the handler runs
-- making the whole idiom redundant there. Left untouched, `ROLLBACK TO
name;` reaches Postgres as the top-level transaction-control statement,
which a plpgsql function body rejects with a syntax error at "TO" -- a
real migration's FEATURE_PL_ADMIN_PKG hit exactly this, on the statement
right after the Round 32 RAISE_APPLICATION_ERROR fix let "Apply DDL to
Target" get that far.

  - PostgreSQL: SAVEPOINT is always dropped (commented out, kept for
    reference). ROLLBACK TO inside an exception handler is also dropped
    (redundant with the automatic rollback-on-catch); ROLLBACK TO in the
    main executable body is left untouched and flagged as an error for
    manual rewrite, since dropping a deliberate mid-flow rollback there
    could silently change behavior.
  - SQL Server: a real, mechanical keyword rename -- SAVEPOINT name; ->
    SAVE TRANSACTION name;, ROLLBACK TO [SAVEPOINT] name; -> ROLLBACK
    TRANSACTION name; -- since T-SQL supports the same idiom natively,
    under different keywords, regardless of whether it appears inside a
    TRY/CATCH handler or in the main body.
  - MySQL: already supports Oracle-compatible SAVEPOINT / ROLLBACK TO
    [SAVEPOINT] syntax natively -- covered here as a regression guard,
    no rewrite needed.
  - Db2: deliberately left unaddressed this round -- Db2 SQL PL's exact
    accepted SAVEPOINT/ROLLBACK TO syntax (Db2 requires a mandatory
    ON ROLLBACK RETAIN CURSORS clause on SAVEPOINT) was uncertain enough
    that it was scoped out rather than risk shipping unverified Db2-
    specific SQL -- covered here as a regression guard proving it is
    still passed through unchanged, not silently "fixed" wrong.
"""
import re

from tgdatabridge.core import plsql_converter as pc
from tgdatabridge.core import plsql_mysql_converter as mysql_conv
from tgdatabridge.core import tsql_converter as tsql_conv
from tgdatabridge.core.db2_converter import convert_procedure_or_function as db2_convert
from tgdatabridge.core.schema_model import Routine

_HANDLER_SRC = (
    "PROCEDURE update_balance (p_id IN NUMBER) IS\n"
    "BEGIN\n"
    "  SAVEPOINT before_update;\n"
    "  UPDATE accounts SET balance = balance - 1 WHERE id = p_id;\n"
    "EXCEPTION\n"
    "  WHEN OTHERS THEN\n"
    "    ROLLBACK TO before_update;\n"
    "    RAISE_APPLICATION_ERROR(-20001, 'update failed');\n"
    "END update_balance;"
)

_MAIN_BODY_SRC = (
    "PROCEDURE risky_batch (p_id IN NUMBER) IS\n"
    "BEGIN\n"
    "  SAVEPOINT checkpoint1;\n"
    "  UPDATE accounts SET balance = balance - 1 WHERE id = p_id;\n"
    "  IF p_id < 0 THEN\n"
    "    ROLLBACK TO checkpoint1;\n"
    "  END IF;\n"
    "END risky_batch;"
)

_PACKAGE_SRC = (
    "PACKAGE BODY FEATURE_PL_ADMIN_PKG IS\n"
    "\n"
    "PROCEDURE update_balance (p_id IN NUMBER) IS\n"
    "BEGIN\n"
    "  SAVEPOINT before_update;\n"
    "  UPDATE accounts SET balance = balance - 1 WHERE id = p_id;\n"
    "EXCEPTION\n"
    "  WHEN OTHERS THEN\n"
    "    ROLLBACK TO before_update;\n"
    "    RAISE_APPLICATION_ERROR(-20001, 'update failed');\n"
    "END update_balance;\n"
    "\n"
    "PROCEDURE archive_balance (p_id IN NUMBER) IS\n"
    "BEGIN\n"
    "  SAVEPOINT before_archive;\n"
    "  INSERT INTO accounts_history SELECT * FROM accounts WHERE id = p_id;\n"
    "EXCEPTION\n"
    "  WHEN OTHERS THEN\n"
    "    ROLLBACK TO before_archive;\n"
    "    RAISE_APPLICATION_ERROR(-20002, 'archive failed');\n"
    "END archive_balance;\n"
    "\n"
    "END FEATURE_PL_ADMIN_PKG;"
)


def _routine(source: str, name: str = "TEST_PROC") -> Routine:
    return Routine(name=name, schema="HR", kind="PROCEDURE", source=source)


# --------------------------------------------------------------- PostgreSQL


def test_postgres_drops_savepoint_unconditionally():
    ddl, issues = pc.convert_procedure_or_function(_routine(_HANDLER_SRC))
    assert "-- (kept for reference only) SAVEPOINT before_update;" in ddl
    assert not re.search(r"^\s*SAVEPOINT\b", ddl, re.IGNORECASE | re.MULTILINE)
    assert any(issue.severity == "info" and "SAVEPOINT before_update" in issue.message for issue in issues)


def test_postgres_drops_rollback_to_inside_exception_handler():
    ddl, issues = pc.convert_procedure_or_function(_routine(_HANDLER_SRC))
    assert "-- (kept for reference only) ROLLBACK TO before_update;" in ddl
    # The old, broken form -- a bare top-level ROLLBACK TO statement --
    # must never reach the generated DDL.
    assert not re.search(r"^\s*ROLLBACK\s+TO\b", ddl, re.IGNORECASE | re.MULTILINE)
    assert any(
        issue.severity == "info" and "exception handler" in issue.message and "before_update" in issue.message
        for issue in issues
    )


def test_postgres_flags_rollback_to_in_main_body_instead_of_dropping_it():
    ddl, issues = pc.convert_procedure_or_function(_routine(_MAIN_BODY_SRC))
    # Left untouched -- dropping a deliberate mid-flow rollback here could
    # silently change behavior, so it must survive verbatim for a human
    # to rewrite as a nested BEGIN...EXCEPTION...END block.
    assert "ROLLBACK TO checkpoint1;" in ddl
    assert "-- (kept for reference only) ROLLBACK TO checkpoint1;" not in ddl
    assert any(
        issue.severity == "error" and "checkpoint1" in issue.message and "outside an exception handler" in issue.message
        for issue in issues
    )
    # SAVEPOINT itself is still always safe to drop, even in the main body.
    assert "-- (kept for reference only) SAVEPOINT checkpoint1;" in ddl


def test_postgres_flattened_package_scopes_each_members_rollback_to_independently():
    # The exact shape of the real migration's bug: a flattened multi-
    # member package body where each member has its own independent
    # BEGIN/EXCEPTION block and its own SAVEPOINT/ROLLBACK TO pair --
    # proving the "nearest preceding BEGIN-or-EXCEPTION" heuristic is
    # scoped per-member (via convert_package_body's per-chunk conversion)
    # rather than accidentally looking across member boundaries.
    ddl, issues = pc.convert_package_body(_routine(_PACKAGE_SRC, name="FEATURE_PL_ADMIN_PKG"))
    assert "-- (kept for reference only) ROLLBACK TO before_update;" in ddl
    assert "-- (kept for reference only) ROLLBACK TO before_archive;" in ddl
    assert not re.search(r"^\s*ROLLBACK\s+TO\b", ddl, re.IGNORECASE | re.MULTILINE)
    info_messages = [i.message for i in issues if i.severity == "info"]
    assert any("before_update" in m and "exception handler" in m for m in info_messages)
    assert any("before_archive" in m and "exception handler" in m for m in info_messages)


def test_postgres_rollback_to_savepoint_keyword_form_is_also_recognized():
    # Oracle also accepts the optional `SAVEPOINT` keyword after `ROLLBACK
    # TO` -- `ROLLBACK TO SAVEPOINT name;` -- which must match too.
    src = (
        "PROCEDURE p1 IS\n"
        "BEGIN\n"
        "  SAVEPOINT s1;\n"
        "  NULL;\n"
        "EXCEPTION\n"
        "  WHEN OTHERS THEN\n"
        "    ROLLBACK TO SAVEPOINT s1;\n"
        "END p1;"
    )
    ddl, issues = pc.convert_procedure_or_function(_routine(src))
    assert "-- (kept for reference only) ROLLBACK TO s1;" in ddl
    assert not re.search(r"^\s*ROLLBACK\s+TO\b", ddl, re.IGNORECASE | re.MULTILINE)


def test_postgres_does_not_mistake_a_string_literal_mentioning_rollback_for_the_real_statement():
    src = (
        "PROCEDURE p1 IS\n"
        "BEGIN\n"
        "  DBMS_OUTPUT.PUT_LINE('rollback to the last save point if needed');\n"
        "END p1;"
    )
    ddl, issues = pc.convert_procedure_or_function(_routine(src))
    assert "rollback to the last save point if needed" in ddl
    assert not any("ROLLBACK TO" in i.message for i in issues)


# --------------------------------------------------------------- SQL Server


def test_sqlserver_renames_savepoint_and_rollback_to_to_save_transaction():
    ddl, issues = tsql_conv.convert_procedure_or_function(_routine(_HANDLER_SRC))
    assert "SAVE TRANSACTION before_update;" in ddl
    assert "ROLLBACK TRANSACTION before_update;" in ddl
    # Neither Oracle keyword should survive -- this is a real syntax
    # rewrite, not a flag-and-leave.
    assert "SAVEPOINT" not in ddl
    assert "ROLLBACK TO" not in ddl


def test_sqlserver_renames_rollback_to_savepoint_keyword_form_too():
    src = (
        "PROCEDURE p1 IS\n"
        "BEGIN\n"
        "  SAVEPOINT s1;\n"
        "  NULL;\n"
        "EXCEPTION\n"
        "  WHEN OTHERS THEN\n"
        "    ROLLBACK TO SAVEPOINT s1;\n"
        "END p1;"
    )
    ddl, issues = tsql_conv.convert_procedure_or_function(_routine(src))
    assert "SAVE TRANSACTION s1;" in ddl
    assert "ROLLBACK TRANSACTION s1;" in ddl


def test_sqlserver_rename_works_in_the_main_body_too_not_only_in_a_handler():
    # Unlike Postgres, T-SQL's SAVE TRANSACTION/ROLLBACK TRANSACTION work
    # identically whether they appear inside a TRY/CATCH handler or in
    # the main body, so no handler-position heuristic applies here.
    ddl, issues = tsql_conv.convert_procedure_or_function(_routine(_MAIN_BODY_SRC))
    assert "SAVE TRANSACTION checkpoint1;" in ddl
    assert "ROLLBACK TRANSACTION checkpoint1;" in ddl
    assert "ROLLBACK TO" not in ddl


# --------------------------------------------------------------- MySQL (regression guard)


def test_mysql_leaves_savepoint_and_rollback_to_unchanged():
    # MySQL/InnoDB already supports Oracle-compatible SAVEPOINT and
    # ROLLBACK TO [SAVEPOINT] syntax natively -- no rewrite needed.
    routine = Routine(name="UPDATE_BALANCE", schema="HR", kind="PROCEDURE", source=_HANDLER_SRC)
    ddl = mysql_conv.convert_routine(routine, "MySQL").converted_source
    assert "SAVEPOINT before_update;" in ddl
    assert "ROLLBACK TO before_update;" in ddl


# --------------------------------------------------------------- Db2 (regression guard)


def test_db2_leaves_savepoint_and_rollback_to_unchanged_this_round():
    # Deliberately out of scope this round (see module docstring) --
    # this guards against silently "fixing" it wrong later without a
    # matching update to this test and to WHATS-FIXED.md.
    ddl, issues = db2_convert(_routine(_HANDLER_SRC))
    assert "SAVEPOINT before_update;" in ddl
    assert "ROLLBACK TO before_update;" in ddl
