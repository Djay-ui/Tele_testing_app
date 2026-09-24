"""Tests for the fix to a WHEN clause referencing a user-declared Oracle
EXCEPTION (the "unrecognized exception condition" bug).

`RAISE exc_name;` was already rewritten (an older fix) to
`RAISE EXCEPTION 'exc_name';`, but a `WHEN exc_name THEN` naming the same
exception was left completely untouched -- Postgres has no user-defined
exception *type*, only SQLSTATE codes, so it tried to parse "exc_name"
itself as a real condition name (like division_by_zero) and rejected the
whole CREATE FUNCTION/PROCEDURE outright with "unrecognized exception
condition", the moment "Apply DDL to Target" tried to compile it -- a real
migration's FEATURE_PL_ADMIN_PKG hit exactly this, on the statement right
after Round 32's own RAISE_APPLICATION_ERROR fix let it get that far.

The fix mints a stable, synthetic SQLSTATE code per declared exception (in
declaration order, so repeated conversions of the same routine are
deterministic) and rewrites *both* sides to agree on it:
`RAISE exc_name;` -> `RAISE EXCEPTION 'exc_name' USING ERRCODE = 'code';`
and `WHEN exc_name THEN` -> `WHEN SQLSTATE 'code' THEN`. Each code avoids
'00000' (reserved) and never ends in three zeroes (a Postgres *category*
code, trappable only by trapping the whole category) -- see
https://www.postgresql.org/docs/current/plpgsql-errors-and-messages.html.
"""
from tgdatabridge.core import plsql_converter as pc
from tgdatabridge.core.schema_model import Routine


def _routine(source: str, name: str = "P1", kind: str = "PROCEDURE") -> Routine:
    return Routine(name=name, schema="HR", kind=kind, source=source)


def test_raise_and_when_agree_on_the_same_sqlstate():
    src = (
        "PROCEDURE p1 IS\n"
        "  bad_input EXCEPTION;\n"
        "BEGIN\n"
        "  RAISE bad_input;\n"
        "EXCEPTION\n"
        "  WHEN bad_input THEN NULL;\n"
        "END p1;"
    )
    ddl, issues = pc.convert_procedure_or_function(_routine(src))
    assert "RAISE EXCEPTION 'bad_input' USING ERRCODE = 'U0001';" in ddl
    assert "WHEN SQLSTATE 'U0001' THEN NULL;" in ddl
    # The old, broken form -- a bare, untranslated condition name -- must
    # never reach the generated DDL.
    assert "WHEN bad_input THEN" not in ddl


def test_the_reported_bug_shape_compiles_cleanly():
    # The exact shape from the real migration's screenshot: a status-
    # validating function with one declared exception, raised in the main
    # body and caught by name in the routine's own EXCEPTION section.
    src = (
        "FUNCTION validate_status (p_status IN VARCHAR2) RETURN VARCHAR2 IS\n"
        "  e_invalid_status EXCEPTION;\n"
        "BEGIN\n"
        "  IF p_status IN ('NEW','ACTIVE','CLOSED','ERROR') THEN\n"
        "    RETURN 'VALID';\n"
        "  END IF;\n"
        "  RAISE e_invalid_status;\n"
        "EXCEPTION\n"
        "  WHEN e_invalid_status THEN\n"
        "    RETURN 'INVALID';\n"
        "END validate_status;"
    )
    ddl, issues = pc.convert_procedure_or_function(_routine(src, name="VALIDATE_STATUS", kind="FUNCTION"))
    assert "RAISE EXCEPTION 'e_invalid_status' USING ERRCODE = 'U0001';" in ddl
    assert "WHEN SQLSTATE 'U0001' THEN" in ddl
    # The old, broken form must never appear as a real (uncommented)
    # WHEN clause -- only inside the "(kept for reference only)" comment.
    assert "WHEN e_invalid_status THEN" not in ddl


def test_two_distinct_exceptions_in_one_routine_get_distinct_codes():
    src = (
        "PROCEDURE p1 IS\n"
        "  e_a EXCEPTION;\n"
        "  e_b EXCEPTION;\n"
        "BEGIN\n"
        "  RAISE e_a;\n"
        "EXCEPTION\n"
        "  WHEN e_a THEN NULL;\n"
        "  WHEN e_b THEN NULL;\n"
        "END p1;"
    )
    ddl, issues = pc.convert_procedure_or_function(_routine(src))
    assert "RAISE EXCEPTION 'e_a' USING ERRCODE = 'U0001';" in ddl
    assert "WHEN SQLSTATE 'U0001' THEN NULL;" in ddl  # e_a's own handler
    assert "WHEN SQLSTATE 'U0011' THEN NULL;" in ddl  # e_b's own handler, a different code


def test_declared_exception_severity_is_now_a_warning_not_an_error():
    # Both RAISE and WHEN are fully, automatically handled now -- nothing
    # here actually needs manual rewriting, so this no longer forces
    # ConversionStatus.MANUAL the way an unresolved "error" issue would.
    src = (
        "PROCEDURE p1 IS\n"
        "  bad_input EXCEPTION;\n"
        "BEGIN\n"
        "  RAISE bad_input;\n"
        "EXCEPTION\n"
        "  WHEN bad_input THEN NULL;\n"
        "END p1;"
    )
    ddl, issues = pc.convert_procedure_or_function(_routine(src))
    assert all(i.severity != "error" for i in issues)
    assert any(i.severity == "warning" and "bad_input" in i.message for i in issues)


def test_no_declared_exceptions_means_no_synthetic_sqlstate_codes():
    # Regression guard: a routine with no user-defined exceptions at all
    # must not have any WHEN clause touched by this fix (only the
    # built-in Oracle exception name remapping, handled elsewhere).
    src = (
        "PROCEDURE p1 IS\n"
        "BEGIN\n"
        "  NULL;\n"
        "EXCEPTION\n"
        "  WHEN NO_DATA_FOUND THEN NULL;\n"
        "END p1;"
    )
    ddl, issues = pc.convert_procedure_or_function(_routine(src))
    assert "SQLSTATE 'U" not in ddl
    assert "WHEN NO_DATA_FOUND THEN NULL;" in ddl


def test_a_flattened_package_scopes_codes_per_member_independently():
    # Mirrors the real migration's exact context: FEATURE_PL_ADMIN_PKG,
    # flattened into standalone functions/procedures, where the SAME
    # exception name declared independently in two different members
    # must not be treated as if they were the same identity across
    # member boundaries -- each member gets its own DECLARE section and
    # its own code numbering, starting fresh at U0001.
    src = (
        "PACKAGE BODY FEATURE_PL_ADMIN_PKG IS\n"
        "\n"
        "PROCEDURE update_balance (p_id IN NUMBER) IS\n"
        "  e_invalid_status EXCEPTION;\n"
        "BEGIN\n"
        "  RAISE e_invalid_status;\n"
        "EXCEPTION\n"
        "  WHEN e_invalid_status THEN NULL;\n"
        "END update_balance;\n"
        "\n"
        "PROCEDURE archive_balance (p_id IN NUMBER) IS\n"
        "  e_invalid_status EXCEPTION;\n"
        "BEGIN\n"
        "  RAISE e_invalid_status;\n"
        "EXCEPTION\n"
        "  WHEN e_invalid_status THEN NULL;\n"
        "END archive_balance;\n"
        "\n"
        "END FEATURE_PL_ADMIN_PKG;"
    )
    ddl, issues = pc.convert_package_body(_routine(src, name="FEATURE_PL_ADMIN_PKG", kind="PACKAGE BODY"))
    assert ddl.count("RAISE EXCEPTION 'e_invalid_status' USING ERRCODE = 'U0001';") == 2
    assert ddl.count("WHEN SQLSTATE 'U0001' THEN NULL;") == 2


def test_cursor_and_row_loop_untouched_by_this_fix_regression_guard():
    # A routine with a cursor but no declared exceptions at all must be
    # completely unaffected -- this fix only ever touches RAISE/WHEN text
    # naming something in declared_exceptions.
    src = (
        "PROCEDURE p1 IS\n"
        "  CURSOR c1 IS SELECT id FROM orders;\n"
        "BEGIN\n"
        "  NULL;\n"
        "END p1;"
    )
    ddl, issues = pc.convert_procedure_or_function(_routine(src))
    assert "c1 CURSOR FOR SELECT id FROM orders;" in ddl
    assert "SQLSTATE" not in ddl
