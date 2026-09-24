"""Tests for tgdatabridge.core.tsql_converter -- the Oracle PL/SQL -> SQL Server
T-SQL routine converter. Covers the structural rewrites that have no
equivalent in the Postgres converter (IF/THEN -> IF/BEGIN/END, numeric-range
and implicit-cursor FOR loops -> explicit WHILE loops, EXCEPTION -> TRY/CATCH,
SELECT...INTO -> SELECT @var = col, @-prefixing of local variables) as well
as the builtin-function rewrites and the BEFORE-trigger/OUTPUT-function
restrictions that are unique to the SQL Server target.
"""
from tgdatabridge.core.schema_model import ConversionStatus, Routine
from tgdatabridge.core import tsql_converter as t


def _proc(source, name="P1"):
    return Routine(name=name, schema="HR", kind="PROCEDURE", source=source)


def _func(source, name="F1"):
    return Routine(name=name, schema="HR", kind="FUNCTION", source=source)


# ------------------------------------------------------------- basic shape


def test_simple_procedure_produces_create_or_alter_and_output_param():
    src = """PROCEDURE P1
(
    P_ID IN NUMBER,
    P_NAME OUT VARCHAR2
)
IS
BEGIN
    P_NAME := 'x';
END P1;
"""
    ddl, issues = t.convert_procedure_or_function(_proc(src))
    assert "CREATE OR ALTER PROCEDURE [P1]" in ddl
    assert "@P_ID DECIMAL(38,10)" in ddl
    assert "@P_NAME VARCHAR(MAX) OUTPUT" in ddl
    assert "SET @P_NAME = 'x';" in ddl
    assert ddl.strip().endswith("END;")


def test_function_returns_and_defaults_to_nvarchar_max_when_untyped():
    src = """FUNCTION F1 IS
BEGIN
    RETURN 1;
END F1;
"""
    ddl, issues = t.convert_procedure_or_function(_func(src))
    assert "RETURNS NVARCHAR(MAX)" in ddl
    assert any("no parsable RETURN type" in i.message for i in issues)


def test_function_with_output_param_flags_error():
    src = """FUNCTION F1 (P_OUT OUT NUMBER) RETURN NUMBER IS
BEGIN
    RETURN 1;
END F1;
"""
    ddl, issues = t.convert_procedure_or_function(_func(src))
    assert any(i.severity == "error" and "OUTPUT parameters" in i.message for i in issues)


# --------------------------------------------------------------- control flow


def test_if_elsif_else_becomes_if_begin_end():
    src = """PROCEDURE P1 IS
    V_X NUMBER;
BEGIN
    IF V_X > 0 THEN
        V_X := 1;
    ELSIF V_X = 0 THEN
        V_X := 0;
    ELSE
        V_X := -1;
    END IF;
END P1;
"""
    ddl, issues = t.convert_procedure_or_function(_proc(src))
    assert "IF @V_X > 0" in ddl
    assert "ELSE IF @V_X = 0" in ddl
    assert "ELSE" in ddl and "BEGIN" in ddl
    assert "END IF" not in ddl
    assert not any(i.severity == "error" for i in issues)


def test_nested_if_inside_if_converts_correctly():
    src = """PROCEDURE P1 IS
    V_X NUMBER;
BEGIN
    IF V_X > 0 THEN
        IF V_X > 10 THEN
            V_X := 100;
        END IF;
    END IF;
END P1;
"""
    ddl, issues = t.convert_procedure_or_function(_proc(src))
    assert ddl.count("IF @V_X > 0") == 1
    assert "IF @V_X > 10" in ddl
    assert not any(i.severity == "error" for i in issues)


def test_numeric_range_for_loop_becomes_while_counter():
    src = """PROCEDURE P1 IS
    V_X NUMBER := 0;
BEGIN
    FOR I IN 1..5 LOOP
        V_X := V_X + I;
    END LOOP;
END P1;
"""
    ddl, issues = t.convert_procedure_or_function(_proc(src))
    assert "DECLARE @I INT = 1;" in ddl
    assert "WHILE @I <= 5" in ddl
    assert "SET @I = @I + 1;" in ddl
    assert "@V_X + @I" in ddl


def test_reverse_numeric_range_for_loop_counts_down():
    src = """PROCEDURE P1 IS
BEGIN
    FOR I IN REVERSE 1..5 LOOP
        NULL;
    END LOOP;
END P1;
"""
    ddl, issues = t.convert_procedure_or_function(_proc(src))
    assert "WHILE @I >= 5" in ddl
    assert "SET @I = @I - 1;" in ddl


def test_basic_loop_becomes_while_true_with_break():
    src = """PROCEDURE P1 IS
    V_X NUMBER := 0;
BEGIN
    LOOP
        V_X := V_X + 1;
        EXIT WHEN V_X > 10;
    END LOOP;
END P1;
"""
    ddl, issues = t.convert_procedure_or_function(_proc(src))
    assert "WHILE 1 = 1" in ddl
    assert "IF @V_X > 10 BREAK;" in ddl


def test_while_loop_condition_preserved():
    src = """PROCEDURE P1 IS
    V_X NUMBER := 0;
BEGIN
    WHILE V_X < 10 LOOP
        V_X := V_X + 1;
    END LOOP;
END P1;
"""
    ddl, issues = t.convert_procedure_or_function(_proc(src))
    assert "WHILE @V_X < 10" in ddl
    assert "BEGIN" in ddl and "END" in ddl


# -------------------------------------------------------- implicit-cursor loops


def test_implicit_cursor_for_loop_becomes_explicit_cursor():
    src = """PROCEDURE P1 IS
    V_TOTAL NUMBER := 0;
BEGIN
    FOR REC IN (SELECT ACCOUNT_ID, BALANCE FROM ACCOUNT) LOOP
        V_TOTAL := V_TOTAL + REC.BALANCE;
    END LOOP;
END P1;
"""
    ddl, issues = t.convert_procedure_or_function(_proc(src))
    assert "DECLARE @ACCOUNT_ID SQL_VARIANT, @BALANCE SQL_VARIANT;" in ddl
    assert "CURSOR LOCAL FAST_FORWARD FOR" in ddl
    assert "OPEN " in ddl and "FETCH NEXT FROM" in ddl and "CLOSE " in ddl and "DEALLOCATE " in ddl
    assert "@V_TOTAL + @BALANCE" in ddl
    assert any("Converted implicit-cursor FOR loop" in i.message for i in issues)


def test_select_star_loop_flags_manual_conversion():
    src = """PROCEDURE P1 IS
BEGIN
    FOR REC IN (SELECT * FROM ACCOUNT) LOOP
        NULL;
    END LOOP;
END P1;
"""
    ddl, issues = t.convert_procedure_or_function(_proc(src))
    assert any(i.severity == "error" and "MANUAL CONVERSION REQUIRED" in i.message for i in issues)
    assert "MANUAL CONVERSION REQUIRED" in ddl


def test_named_cursor_for_loop_resolves_declared_cursor():
    src = """PROCEDURE P1 IS
    CURSOR CUR_ACCTS IS SELECT ACCOUNT_ID FROM ACCOUNT;
BEGIN
    FOR REC IN CUR_ACCTS LOOP
        NULL;
    END LOOP;
END P1;
"""
    ddl, issues = t.convert_procedure_or_function(_proc(src))
    assert "DECLARE @ACCOUNT_ID SQL_VARIANT;" in ddl
    assert "FETCH NEXT FROM CUR_ACCTS INTO @ACCOUNT_ID;" in ddl


def test_row_loop_fetch_into_does_not_corrupt_a_later_select_into():
    # Regression test: the row/cursor FOR-loop converter emits its own
    # "FETCH NEXT FROM cursor INTO @var;" text containing the word INTO.
    # _rewrite_select_into's SELECT...INTO...FROM regex must not walk past
    # this loop's own statement boundary and latch onto that FETCH's INTO
    # (or a later statement's FROM), scrambling both the loop and whatever
    # follows it -- caught via a real end-to-end schema DDL smoke test.
    src = """FUNCTION GET_TOTAL (P_ID IN NUMBER) RETURN NUMBER IS
    V_TOTAL NUMBER := 0;
BEGIN
    FOR REC IN (SELECT BALANCE FROM ACCOUNT WHERE CUSTOMER_ID = P_ID) LOOP
        V_TOTAL := V_TOTAL + REC.BALANCE;
    END LOOP;
    RETURN V_TOTAL;
END GET_TOTAL;
"""
    ddl, issues = t.convert_procedure_or_function(_func(src))
    assert "DECLARE @BALANCE SQL_VARIANT;" in ddl
    assert "DECLARE [REC_cursor] CURSOR LOCAL FAST_FORWARD FOR" in ddl
    assert "SELECT BALANCE FROM ACCOUNT WHERE CUSTOMER_ID = @P_ID;" in ddl
    assert "OPEN [REC_cursor];" in ddl
    assert ddl.index("OPEN [REC_cursor];") < ddl.index("WHILE @@FETCH_STATUS = 0")
    assert "RETURN @V_TOTAL;" in ddl
    assert not any(i.severity == "error" for i in issues)


def test_select_into_in_a_separate_statement_after_a_row_loop_is_unaffected():
    src = """PROCEDURE P1 (P_ID IN NUMBER, P_NAME OUT VARCHAR2) IS
BEGIN
    FOR REC IN (SELECT BALANCE FROM ACCOUNT WHERE CUSTOMER_ID = P_ID) LOOP
        NULL;
    END LOOP;
    SELECT NAME INTO P_NAME FROM CUSTOMER WHERE CUSTOMER_ID = P_ID;
END P1;
"""
    ddl, issues = t.convert_procedure_or_function(_proc(src))
    assert "SELECT @P_NAME = NAME FROM CUSTOMER WHERE CUSTOMER_ID = @P_ID;" in ddl


# ------------------------------------------------------------ exceptions


def test_exception_section_becomes_try_catch_with_error_number():
    src = """PROCEDURE P1 IS
    V_X NUMBER;
BEGIN
    V_X := 1;
EXCEPTION
    WHEN DUP_VAL_ON_INDEX THEN
        V_X := -1;
    WHEN OTHERS THEN
        RAISE;
END P1;
"""
    ddl, issues = t.convert_procedure_or_function(_proc(src))
    assert "BEGIN TRY" in ddl and "END TRY" in ddl
    assert "BEGIN CATCH" in ddl and "END CATCH" in ddl
    assert "ERROR_NUMBER() IN (2627)" in ddl
    assert "THROW;" in ddl


def test_no_others_handler_still_rethrows_unmatched_errors():
    src = """PROCEDURE P1 IS
BEGIN
    NULL;
EXCEPTION
    WHEN ZERO_DIVIDE THEN
        NULL;
END P1;
"""
    ddl, issues = t.convert_procedure_or_function(_proc(src))
    assert "ERROR_NUMBER() IN (8134)" in ddl
    assert "ELSE" in ddl and "THROW;" in ddl


def test_raise_of_declared_exception_in_main_body_converted_not_left_bare():
    # Regression test: RAISE of a locally-declared exception used to only
    # get converted to THROW inside a WHEN...THEN handler's own statements,
    # not in the main try-body -- so a routine that raises its own
    # exception conditionally (then catches it below, a completely normal
    # Oracle pattern) came out with a bare "RAISE my_exc;" left in the main
    # body, which isn't valid T-SQL syntax. Found while building the golden
    # PL/SQL regression suite (test_golden_plsql.py).
    src = """PROCEDURE P1 IS
    MY_EXC EXCEPTION;
BEGIN
    IF 1 = 1 THEN
        RAISE MY_EXC;
    END IF;
EXCEPTION
    WHEN OTHERS THEN
        NULL;
END P1;
"""
    ddl, issues = t.convert_procedure_or_function(_proc(src))
    assert "THROW 50000, 'MY_EXC', 1;" in ddl
    assert "RAISE MY_EXC;" not in ddl


def test_no_exception_section_has_no_try_catch():
    src = """PROCEDURE P1 IS
BEGIN
    NULL;
END P1;
"""
    ddl, issues = t.convert_procedure_or_function(_proc(src))
    assert "BEGIN TRY" not in ddl
    assert "BEGIN CATCH" not in ddl


# --------------------------------------------------------------- builtins


def test_select_into_rewritten_to_assignment_form():
    src = """PROCEDURE P1 IS
    V_NAME VARCHAR2(50);
BEGIN
    SELECT NAME INTO V_NAME FROM CUSTOMER WHERE CUSTOMER_ID = 1;
END P1;
"""
    ddl, issues = t.convert_procedure_or_function(_proc(src))
    assert "SELECT @V_NAME = NAME FROM CUSTOMER" in ddl


def test_decode_becomes_case():
    src = """PROCEDURE P1 IS
    V_X NUMBER;
BEGIN
    V_X := DECODE(V_X, 1, 'one', 2, 'two', 'other');
END P1;
"""
    ddl, issues = t.convert_procedure_or_function(_proc(src))
    assert "CASE @V_X" in ddl
    assert "WHEN 1 THEN 'one'" in ddl


def test_instr_becomes_charindex_with_swapped_args():
    src = """PROCEDURE P1 IS
    V_X NUMBER;
BEGIN
    V_X := INSTR('hello world', 'world');
END P1;
"""
    ddl, issues = t.convert_procedure_or_function(_proc(src))
    assert "CHARINDEX('world', 'hello world')" in ddl


def test_dbms_output_becomes_print():
    src = """PROCEDURE P1 IS
BEGIN
    DBMS_OUTPUT.PUT_LINE('hi');
END P1;
"""
    ddl, issues = t.convert_procedure_or_function(_proc(src))
    assert "PRINT ('hi')" in ddl


def test_sequence_nextval_becomes_next_value_for():
    src = """PROCEDURE P1 IS
    V_ID NUMBER;
BEGIN
    V_ID := SEQ_CUSTOMER.NEXTVAL;
END P1;
"""
    ddl, issues = t.convert_procedure_or_function(_proc(src))
    assert "NEXT VALUE FOR [SEQ_CUSTOMER]" in ddl


def test_currval_flags_error_no_equivalent():
    src = """PROCEDURE P1 IS
    V_ID NUMBER;
BEGIN
    V_ID := SEQ_CUSTOMER.CURRVAL;
END P1;
"""
    ddl, issues = t.convert_procedure_or_function(_proc(src))
    assert any(i.severity == "error" and "CURRVAL" in i.message for i in issues)


def test_raise_application_error_becomes_throw():
    src = """PROCEDURE P1 IS
BEGIN
    RAISE_APPLICATION_ERROR(-20001, 'bad input');
END P1;
"""
    ddl, issues = t.convert_procedure_or_function(_proc(src))
    assert "THROW 50000, 'bad input', 1;" in ddl


def test_dbms_random_value_no_arg_form_converted_to_rand():
    src = """PROCEDURE P1 IS
    V_X NUMBER;
BEGIN
    V_X := DBMS_RANDOM.VALUE;
END P1;
"""
    ddl, issues = t.convert_procedure_or_function(_proc(src))
    assert "@V_X = RAND()" in ddl
    assert "DBMS_RANDOM" not in ddl
    assert not any(i.severity == "error" and "DBMS_" in i.message for i in issues)


def test_dbms_random_value_ranged_form_still_flags_manual_marker():
    # The 2-arg ranged form is out of scope for the no-arg -> RAND() mapping
    # and has no direct T-SQL equivalent -- it must still fall through to
    # the generic DBMS_* manual marker rather than being silently dropped.
    src = """PROCEDURE P1 IS
    V_X NUMBER;
BEGIN
    V_X := DBMS_RANDOM.VALUE(1, 10);
END P1;
"""
    ddl, issues = t.convert_procedure_or_function(_proc(src))
    assert "DBMS_RANDOM.VALUE(1, 10)" in ddl
    assert any(i.severity == "error" and "DBMS_" in i.message for i in issues)


# ---------------------------------------------------------------- triggers


def test_after_row_trigger_rewrites_new_old_as_single_row_lookup():
    src = """
BEGIN
    IF :NEW.BALANCE < 0 THEN
        NULL;
    END IF;
END;
"""
    trig = Routine(name="TRG1", schema="HR", kind="TRIGGER", source=src,
                    table_name="ACCOUNT", timing="AFTER", events=["UPDATE"], row_level=True)
    ddl, issues = t.convert_trigger(trig)
    assert "CREATE OR ALTER TRIGGER [TRG1]" in ddl
    assert "AFTER UPDATE" in ddl
    assert "(SELECT BALANCE FROM inserted)" in ddl
    assert any("statement-level" in i.message for i in issues)


def test_before_trigger_flagged_manual_not_silently_converted():
    src = "BEGIN NULL; END;"
    trig = Routine(name="TRG1", schema="HR", kind="TRIGGER", source=src,
                    table_name="ACCOUNT", timing="BEFORE", events=["INSERT"], row_level=True)
    ddl, issues = t.convert_trigger(trig)
    assert "MANUAL CONVERSION REQUIRED" in ddl
    assert "CREATE OR ALTER TRIGGER" not in ddl
    assert any(i.severity == "error" and "BEFORE" in i.message for i in issues)


def test_instead_of_trigger_preserved():
    src = "BEGIN NULL; END;"
    trig = Routine(name="TRG1", schema="HR", kind="TRIGGER", source=src,
                    table_name="V_ACCOUNT", timing="INSTEAD OF", events=["INSERT"], row_level=False)
    ddl, issues = t.convert_trigger(trig)
    assert "INSTEAD OF INSERT" in ddl


# ------------------------------------------------------------- package body


def test_package_body_flattened_into_named_procedures():
    src = """PACKAGE BODY PKG_BANK AS
    PROCEDURE GET_CUSTOMER (P_ID IN NUMBER, P_NAME OUT VARCHAR2) IS
    BEGIN
        SELECT NAME INTO P_NAME FROM CUSTOMER WHERE CUSTOMER_ID = P_ID;
    END GET_CUSTOMER;

    FUNCTION GET_TOTAL_CUSTOMERS RETURN NUMBER IS
        V_COUNT NUMBER;
    BEGIN
        SELECT COUNT(*) INTO V_COUNT FROM CUSTOMER;
        RETURN V_COUNT;
    END GET_TOTAL_CUSTOMERS;
END PKG_BANK;
"""
    routine = Routine(name="PKG_BANK", schema="HR", kind="PACKAGE BODY", source=src)
    ddl, issues = t.convert_package_body(routine)
    assert "CREATE OR ALTER PROCEDURE [PKG_BANK_GET_CUSTOMER]" in ddl
    assert "CREATE OR ALTER FUNCTION [PKG_BANK_GET_TOTAL_CUSTOMERS]" in ddl
    assert "END;\n\nEND;" not in ddl  # no duplicate/mismatched trailing END


# -------------------------------------------------------------- top-level


def test_convert_routine_sets_status_manual_on_error():
    src = "BEGIN NULL; END;"
    trig = Routine(name="TRG1", schema="HR", kind="TRIGGER", source=src,
                    table_name="ACCOUNT", timing="BEFORE", events=["INSERT"], row_level=True)
    result = t.convert_routine(trig, "SQL Server")
    assert result.status == ConversionStatus.MANUAL
    assert result.converted_source is not None


def test_convert_routine_sets_status_automatic_when_clean():
    src = """PROCEDURE P1 IS
BEGIN
    NULL;
END P1;
"""
    result = t.convert_routine(_proc(src), "SQL Server")
    assert result.status in (ConversionStatus.AUTOMATIC, ConversionStatus.AUTOMATIC_WITH_WARNINGS)


def test_package_spec_has_no_tsql_equivalent_placeholder():
    routine = Routine(name="PKG_BANK", schema="HR", kind="PACKAGE", source="PACKAGE PKG_BANK AS END;")
    result = t.convert_routine(routine, "SQL Server")
    assert "no T-SQL equivalent" in result.converted_source
    assert result.status == ConversionStatus.AUTOMATIC_WITH_WARNINGS


def test_plsql_converter_dispatches_sqlserver_to_tsql_converter():
    from tgdatabridge.core import plsql_converter
    src = """PROCEDURE P1 IS
BEGIN
    NULL;
END P1;
"""
    routine = plsql_converter.convert_routine(_proc(src), "SQL Server")
    assert "CREATE OR ALTER PROCEDURE" in routine.converted_source


def test_plsql_converter_postgres_path_unaffected_by_sqlserver_dispatch():
    from tgdatabridge.core import plsql_converter
    src = """PROCEDURE P1 IS
BEGIN
    NULL;
END P1;
"""
    routine = plsql_converter.convert_routine(_proc(src), "PostgreSQL")
    assert "CREATE OR REPLACE PROCEDURE" in routine.converted_source
    assert '"p1"' in routine.converted_source


# ---------------------------------------------- CONNECT BY -> recursive CTE

def test_connect_by_row_for_loop_rewritten_and_columns_fetched_by_name():
    source = (
        "PROCEDURE print_org_chart (p_dummy IN NUMBER) IS\n"
        "  CURSOR c1 IS\n"
        "    SELECT employee_id, manager_id, last_name\n"
        "    FROM employees\n"
        "    START WITH manager_id IS NULL\n"
        "    CONNECT BY PRIOR employee_id = manager_id;\n"
        "  v_id NUMBER;\n"
        "BEGIN\n"
        "  FOR rec IN c1 LOOP\n"
        "    v_id := rec.employee_id;\n"
        "  END LOOP;\n"
        "END print_org_chart;"
    )
    routine = t.convert_routine(_proc(source, name="PRINT_ORG_CHART"), "SQL Server")
    ddl = routine.converted_source
    assert "WITH cb_cte_1" in ddl
    assert "WITH RECURSIVE" not in ddl  # T-SQL infers recursion, no RECURSIVE keyword
    assert "CONNECT BY" not in ddl.upper()
    # the CTE's own declared column list let the row-FOR-loop converter still
    # do its normal FETCH-by-name rewrite instead of bailing to manual
    assert "FETCH NEXT FROM c1 INTO @employee_id, @manager_id, @last_name" in ddl
    assert routine.status != ConversionStatus.MANUAL
    assert any(i.severity == "info" and "recursive CTE" in i.message for i in routine.issues)


def test_connect_by_unsupported_shape_still_flagged_manual_for_sqlserver():
    source = (
        "PROCEDURE print_org_chart IS\n"
        "  CURSOR c1 IS\n"
        "    SELECT * FROM employees\n"
        "    START WITH manager_id IS NULL\n"
        "    CONNECT BY PRIOR employee_id = manager_id;\n"
        "BEGIN\n"
        "  NULL;\n"
        "END print_org_chart;"
    )
    routine = t.convert_routine(_proc(source), "SQL Server")
    assert "CONNECT BY" in routine.converted_source.upper()
    assert routine.status == ConversionStatus.MANUAL


# --------------------------------------------------- nested subprogram / TYPE decl


def test_nested_subprogram_flagged_not_silently_corrupted():
    # Regression test for the bug fixed in nested_subprogram.py -- see the
    # matching test in test_plsql_converter.py for the full failure mode.
    source = (
        "PROCEDURE outer_proc (p_id IN NUMBER) IS\n"
        "  v_count NUMBER := 0;\n"
        "  PROCEDURE inner_helper (p_x IN NUMBER) IS\n"
        "  BEGIN\n"
        "    v_count := v_count + p_x;\n"
        "  END inner_helper;\n"
        "  v_total NUMBER;\n"
        "BEGIN\n"
        "  inner_helper(p_id);\n"
        "  v_total := v_count;\n"
        "END outer_proc;"
    )
    routine = t.convert_procedure_or_function(_proc(source, name="OUTER_PROC"))
    ddl, issues = routine
    assert "MANUAL CONVERSION REQUIRED: nested PROCEDURE inner_helper" in ddl
    assert "END inner_helper;" in ddl  # original source embedded verbatim, not shredded
    assert any(i.severity == "error" and "inner_helper" in i.message for i in issues)
    # The outer routine's own declarations/body must still convert cleanly.
    assert "@v_count DECIMAL(38,10) = 0;" in ddl
    assert "@v_total DECIMAL(38,10);" in ddl

    result = t.convert_routine(_proc(source, name="OUTER_PROC"), "SQL Server")
    assert result.status == ConversionStatus.MANUAL


def test_local_type_declaration_flagged_not_silently_corrupted():
    source = (
        "PROCEDURE sum_ids IS\n"
        "  TYPE t_id_list IS TABLE OF NUMBER INDEX BY BINARY_INTEGER;\n"
        "  v_ids t_id_list;\n"
        "BEGIN\n"
        "  NULL;\n"
        "END sum_ids;"
    )
    ddl, issues = t.convert_procedure_or_function(_proc(source, name="SUM_IDS"))
    assert "MANUAL CONVERSION REQUIRED: TYPE t_id_list IS TABLE OF" in ddl
    assert any(i.severity == "error" and "t_id_list" in i.message for i in issues)
    result = t.convert_routine(_proc(source, name="SUM_IDS"), "SQL Server")
    assert result.status == ConversionStatus.MANUAL


def test_nested_subprogram_in_trigger_declare_section_flagged():
    source = (
        "DECLARE\n"
        "  v_count NUMBER := 0;\n"
        "  PROCEDURE bump IS\n"
        "  BEGIN\n"
        "    v_count := v_count + 1;\n"
        "  END bump;\n"
        "BEGIN\n"
        "  bump;\n"
        "  IF :NEW.BALANCE < 0 THEN\n"
        "    NULL;\n"
        "  END IF;\n"
        "END;"
    )
    trig = Routine(name="TRG1", schema="HR", kind="TRIGGER", source=source,
                    table_name="ACCOUNT", timing="AFTER", events=["UPDATE"], row_level=True)
    ddl, issues = t.convert_trigger(trig)
    assert "MANUAL CONVERSION REQUIRED" in ddl
    assert any(i.severity == "error" and "bump" in i.message for i in issues)


# --------------------------------------------------------- DBMS_LOB / DBMS_RANDOM


def test_dbms_lob_getlength_and_substr_mapped():
    source = (
        "PROCEDURE p1 IS\n"
        "  v_len NUMBER;\n"
        "  v_part VARCHAR2(100);\n"
        "BEGIN\n"
        "  v_len := DBMS_LOB.GETLENGTH(v_clob);\n"
        "  v_part := DBMS_LOB.SUBSTR(v_clob, 10, 5);\n"
        "END p1;"
    )
    ddl, issues = t.convert_procedure_or_function(_proc(source, name="P1"))
    assert "LEN(v_clob)" in ddl
    # DBMS_LOB.SUBSTR(lob, amount, offset) -> SUBSTRING(lob, offset, amount): args 2 and 3 swap.
    assert "SUBSTRING(v_clob, 5, 10)" in ddl
    assert "DBMS_LOB" not in ddl
    assert any("DATALENGTH" in i.message for i in issues)  # trailing-space caveat noted
    assert not any(i.severity == "error" and "DBMS_" in i.message for i in issues)
