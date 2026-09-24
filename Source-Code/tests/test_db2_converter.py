"""Tests for tgdatabridge.core.db2_converter -- Oracle PL/SQL -> Db2 (LUW) SQL PL."""
from tgdatabridge.core.db2_converter import (
    _LabelGen, convert_declare_block, convert_package_body,
    convert_procedure_or_function, convert_routine, convert_trigger,
)
from tgdatabridge.core.schema_model import ConversionStatus, Routine

# ------------------------------------------------------------ declare block


def test_declare_block_simple_variable_with_default():
    sql, issues, names = convert_declare_block("v_total NUMBER(10,2) := 0;")
    assert "DECLARE v_total DECIMAL(10,2) DEFAULT 0;" in sql
    assert names == {"v_total"}


def test_declare_block_constant_flagged_info():
    sql, issues, names = convert_declare_block("v_rate CONSTANT NUMBER := 0.05;")
    assert "DECLARE v_rate" in sql
    assert any(i.severity == "info" and "CONSTANT" in i.message for i in issues)


def test_declare_block_not_null_flagged_warning():
    sql, issues, names = convert_declare_block("v_x NUMBER NOT NULL := 1;")
    assert any(i.severity == "warning" and "NOT NULL" in i.message for i in issues)


def test_declare_block_cursor():
    sql, issues, names = convert_declare_block("CURSOR c1 IS SELECT id FROM t;")
    assert "DECLARE c1 CURSOR FOR" in sql
    assert "SELECT id FROM t" in sql


def test_declare_block_cursor_with_params_flags_warning():
    sql, issues, names = convert_declare_block("CURSOR c1 (p_id NUMBER) IS SELECT id FROM t WHERE id = p_id;")
    assert any(i.severity == "warning" and "parameters" in i.message for i in issues)


def test_declare_block_pragma_dropped():
    sql, issues, names = convert_declare_block("PRAGMA AUTONOMOUS_TRANSACTION;")
    assert "removed" in sql
    assert any("PRAGMA" in i.message for i in issues)


def test_declare_block_exception_name_flagged_error():
    sql, issues, names = convert_declare_block("my_exc EXCEPTION;")
    assert any(i.severity == "error" and "my_exc" in i.message for i in issues)


def test_declare_block_skip_names_drops_row_loop_var():
    sql, issues, names = convert_declare_block("rec my_type%ROWTYPE;", skip_names={"rec"})
    assert "removed" in sql
    assert "rec" not in names


# ------------------------------------------------------------------ IF/ELSEIF


def test_if_elsif_else_becomes_elseif():
    src = """PROCEDURE p1 (p_x IN NUMBER) IS
BEGIN
  IF p_x > 0 THEN
    NULL;
  ELSIF p_x = 0 THEN
    NULL;
  ELSE
    NULL;
  END IF;
END p1;"""
    routine = Routine(name="P1", schema="APP", kind="PROCEDURE", source=src)
    ddl, issues = convert_procedure_or_function(routine)
    assert "IF p_x > 0 THEN" in ddl
    assert "ELSEIF p_x = 0 THEN" in ddl
    assert "ELSE" in ddl
    assert "END IF;" in ddl
    assert "ELSIF" not in ddl  # Oracle spelling shouldn't survive


# --------------------------------------------------------------------- loops


def test_bare_loop_gets_label_and_leave():
    src = """PROCEDURE p1 IS
  v_x NUMBER := 0;
BEGIN
  LOOP
    v_x := v_x + 1;
    EXIT WHEN v_x > 5;
  END LOOP;
END p1;"""
    routine = Routine(name="P1", schema="APP", kind="PROCEDURE", source=src)
    ddl, issues = convert_procedure_or_function(routine)
    assert ": LOOP" in ddl
    assert "END LOOP LBL_LOOP_1;" in ddl
    assert "LEAVE LBL_LOOP_1;" in ddl


def test_while_loop_becomes_while_do_end_while():
    src = """PROCEDURE p1 IS
  v_x NUMBER := 0;
BEGIN
  WHILE v_x < 5 LOOP
    v_x := v_x + 1;
  END LOOP;
END p1;"""
    routine = Routine(name="P1", schema="APP", kind="PROCEDURE", source=src)
    ddl, issues = convert_procedure_or_function(routine)
    assert "WHILE v_x < 5 DO" in ddl
    assert "END WHILE LBL_WHILE_1;" in ddl


def test_numeric_range_forward_loop_counter_hoisted_to_declare():
    src = """PROCEDURE p1 IS
  v_total NUMBER := 0;
BEGIN
  FOR i IN 1..5 LOOP
    v_total := v_total + i;
  END LOOP;
END p1;"""
    routine = Routine(name="P1", schema="APP", kind="PROCEDURE", source=src)
    ddl, issues = convert_procedure_or_function(routine)
    # counter is declared once, up front, alongside the other DECLAREs --
    # Db2 forbids a DECLARE anywhere but the top of the compound statement.
    assert "DECLARE i INT DEFAULT 0;" in ddl
    assert "SET i = 1;" in ddl
    assert "WHILE i <= 5 DO" in ddl
    assert "SET i = i + 1;" in ddl


def test_numeric_range_reverse_loop_starts_high_ends_low():
    src = """PROCEDURE p1 IS
  v_total NUMBER := 0;
BEGIN
  FOR i IN REVERSE 1..3 LOOP
    v_total := v_total + i;
  END LOOP;
END p1;"""
    routine = Routine(name="P1", schema="APP", kind="PROCEDURE", source=src)
    ddl, issues = convert_procedure_or_function(routine)
    # must start at the *upper* bound and count down to the lower bound
    assert "SET i = 3;" in ddl
    assert "WHILE i >= 1 DO" in ddl
    assert "SET i = i - 1;" in ddl


def test_row_cursor_for_loop_uses_native_db2_for_syntax():
    src = """PROCEDURE p1 IS
  v_total NUMBER := 0;
BEGIN
  FOR rec IN (SELECT amount FROM orders) LOOP
    v_total := v_total + rec.amount;
  END LOOP;
END p1;"""
    routine = Routine(name="P1", schema="APP", kind="PROCEDURE", source=src)
    ddl, issues = convert_procedure_or_function(routine)
    assert "FOR rec AS rec_cur CURSOR FOR" in ddl
    assert "SELECT amount FROM orders" in ddl
    assert "DO" in ddl
    assert "END FOR" in ddl
    # unlike the T-SQL converter, dot access needs no rewriting at all
    assert "rec.amount" in ddl


def test_named_cursor_for_loop_inlines_its_select():
    src = """PROCEDURE p1 IS
  v_total NUMBER := 0;
  CURSOR c_orders IS SELECT amount FROM orders;
BEGIN
  FOR rec IN c_orders LOOP
    v_total := v_total + rec.amount;
  END LOOP;
END p1;"""
    routine = Routine(name="P1", schema="APP", kind="PROCEDURE", source=src)
    ddl, issues = convert_procedure_or_function(routine)
    assert "FOR rec AS c_orders CURSOR FOR" in ddl
    assert "SELECT amount FROM orders" in ddl


def test_exit_and_continue_inside_row_loop_rewritten():
    src = """PROCEDURE p1 IS
  v_total NUMBER := 0;
BEGIN
  FOR rec IN (SELECT amount FROM orders) LOOP
    IF rec.amount > 1000 THEN
      CONTINUE;
    END IF;
    EXIT WHEN v_total > 5000;
    v_total := v_total + rec.amount;
  END LOOP;
END p1;"""
    routine = Routine(name="P1", schema="APP", kind="PROCEDURE", source=src)
    ddl, issues = convert_procedure_or_function(routine)
    assert "ITERATE LBL_REC_1;" in ddl
    assert "LEAVE LBL_REC_1;" in ddl


# ---------------------------------------------------------------- exceptions


def test_others_handler_becomes_sqlexception_handler():
    src = """PROCEDURE p1 IS
BEGIN
  NULL;
EXCEPTION
  WHEN OTHERS THEN
    NULL;
END p1;"""
    routine = Routine(name="P1", schema="APP", kind="PROCEDURE", source=src)
    ddl, issues = convert_procedure_or_function(routine)
    assert "DECLARE EXIT HANDLER FOR SQLEXCEPTION" in ddl


def test_named_exception_maps_to_sqlstate_handler():
    src = """PROCEDURE p1 IS
BEGIN
  NULL;
EXCEPTION
  WHEN DUP_VAL_ON_INDEX THEN
    NULL;
END p1;"""
    routine = Routine(name="P1", schema="APP", kind="PROCEDURE", source=src)
    ddl, issues = convert_procedure_or_function(routine)
    assert "DECLARE EXIT HANDLER FOR SQLSTATE '23505'" in ddl


def test_no_data_found_maps_to_not_found_condition():
    src = """PROCEDURE p1 IS
BEGIN
  NULL;
EXCEPTION
  WHEN NO_DATA_FOUND THEN
    NULL;
END p1;"""
    routine = Routine(name="P1", schema="APP", kind="PROCEDURE", source=src)
    ddl, issues = convert_procedure_or_function(routine)
    assert "DECLARE EXIT HANDLER FOR NOT FOUND" in ddl


def test_or_combined_exceptions_become_comma_condition_list():
    src = """PROCEDURE p1 IS
BEGIN
  NULL;
EXCEPTION
  WHEN DUP_VAL_ON_INDEX OR ZERO_DIVIDE THEN
    NULL;
END p1;"""
    routine = Routine(name="P1", schema="APP", kind="PROCEDURE", source=src)
    ddl, issues = convert_procedure_or_function(routine)
    assert "SQLSTATE '23505', SQLSTATE '22012'" in ddl


def test_unmappable_exception_flags_error():
    src = """PROCEDURE p1 IS
BEGIN
  NULL;
EXCEPTION
  WHEN VALUE_ERROR THEN
    NULL;
END p1;"""
    routine = Routine(name="P1", schema="APP", kind="PROCEDURE", source=src)
    ddl, issues = convert_procedure_or_function(routine)
    assert any(i.severity == "error" and "VALUE_ERROR" in i.message for i in issues)


def test_bare_raise_becomes_resignal():
    src = """PROCEDURE p1 IS
BEGIN
  NULL;
EXCEPTION
  WHEN OTHERS THEN
    RAISE;
END p1;"""
    routine = Routine(name="P1", schema="APP", kind="PROCEDURE", source=src)
    ddl, issues = convert_procedure_or_function(routine)
    assert "RESIGNAL;" in ddl


def test_raise_of_declared_exception_in_main_body_converted_not_left_bare():
    # Regression test: RAISE of a locally-declared exception used to only
    # get converted to SIGNAL inside a WHEN...THEN handler's own statements,
    # not in the main body -- so a routine that raises its own exception
    # conditionally (then catches it below, a completely normal Oracle
    # pattern) came out with a bare "RAISE my_exc;" left in the main body,
    # which isn't valid Db2 SQL PL syntax. Found while building the golden
    # PL/SQL regression suite (test_golden_plsql.py).
    src = """PROCEDURE p1 IS
  my_exc EXCEPTION;
BEGIN
  IF 1 = 1 THEN
    RAISE my_exc;
  END IF;
EXCEPTION
  WHEN OTHERS THEN
    NULL;
END p1;"""
    routine = Routine(name="P1", schema="APP", kind="PROCEDURE", source=src)
    ddl, issues = convert_procedure_or_function(routine)
    assert "SIGNAL SQLSTATE '70000' SET MESSAGE_TEXT = 'my_exc';" in ddl
    assert "RAISE my_exc;" not in ddl


def test_nested_begin_block_hoists_its_own_local_handler():
    src = """PROCEDURE p1 IS
  v_x NUMBER := 0;
BEGIN
  BEGIN
    v_x := 1 / 0;
  EXCEPTION
    WHEN ZERO_DIVIDE THEN
      v_x := 0;
  END;
  v_x := v_x + 1;
END p1;"""
    routine = Routine(name="P1", schema="APP", kind="PROCEDURE", source=src)
    ddl, issues = convert_procedure_or_function(routine)
    assert "DECLARE EXIT HANDLER FOR SQLSTATE '22012'" in ddl
    # the outer routine's own DECLARE section must not contain the nested
    # block's handler -- it should sit inside the nested BEGIN...END itself
    outer_declare_section = ddl.split("BEGIN", 2)[1]
    assert "SQLSTATE '22012'" not in outer_declare_section


# ----------------------------------------------------------------- builtins


def test_builtin_rewrites_sysdate_nvl_from_dual_assignment():
    src = """PROCEDURE p1 IS
  v_d DATE;
  v_n NUMBER;
BEGIN
  v_d := SYSDATE;
  v_n := NVL(v_n, 0);
  SELECT 1 INTO v_n FROM DUAL;
END p1;"""
    routine = Routine(name="P1", schema="APP", kind="PROCEDURE", source=src)
    ddl, issues = convert_procedure_or_function(routine)
    assert "SET v_d = CURRENT TIMESTAMP;" in ddl
    assert "COALESCE(v_n, 0)" in ddl
    assert "FROM SYSIBM.SYSDUMMY1" in ddl
    # SELECT ... INTO ... FROM is native Db2 syntax -- must pass through unchanged
    assert "SELECT 1 INTO v_n FROM" in ddl


def test_instr_arg_swap_to_locate():
    src = """PROCEDURE p1 IS
  v_pos NUMBER;
BEGIN
  v_pos := INSTR('hello world', 'world');
END p1;"""
    routine = Routine(name="P1", schema="APP", kind="PROCEDURE", source=src)
    ddl, issues = convert_procedure_or_function(routine)
    assert "LOCATE('world', 'hello world')" in ddl


def test_sequence_nextval_and_currval():
    src = """PROCEDURE p1 IS
  v_id NUMBER;
BEGIN
  v_id := my_seq.NEXTVAL;
  v_id := my_seq.CURRVAL;
END p1;"""
    routine = Routine(name="P1", schema="APP", kind="PROCEDURE", source=src)
    ddl, issues = convert_procedure_or_function(routine)
    assert 'NEXT VALUE FOR "MY_SEQ"' in ddl
    assert 'PREVIOUS VALUE FOR "MY_SEQ"' in ddl


def test_dbms_output_flagged_manual():
    src = """PROCEDURE p1 IS
BEGIN
  DBMS_OUTPUT.PUT_LINE('hi');
END p1;"""
    routine = Routine(name="P1", schema="APP", kind="PROCEDURE", source=src)
    ddl, issues = convert_procedure_or_function(routine)
    assert any(i.severity == "error" and "DBMS_" in i.message for i in issues)


def test_case_statement_passed_through_untouched():
    src = """PROCEDURE p1 IS
  v_x NUMBER := 1;
  v_y VARCHAR2(10);
BEGIN
  CASE v_x
    WHEN 1 THEN v_y := 'one';
    ELSE v_y := 'other';
  END CASE;
END p1;"""
    routine = Routine(name="P1", schema="APP", kind="PROCEDURE", source=src)
    ddl, issues = convert_procedure_or_function(routine)
    assert "CASE v_x" in ddl
    assert "END CASE" in ddl
    # Db2 supports CASE statements natively -- no manual-conversion flag
    assert not any("CASE" in i.message and i.severity == "error" for i in issues)


# --------------------------------------------------------------- %TYPE/etc.


def test_rowtype_param_flagged_error_defaults_varchar():
    src = """PROCEDURE p1 (p_row IN employees%ROWTYPE) IS
BEGIN
  NULL;
END p1;"""
    routine = Routine(name="P1", schema="APP", kind="PROCEDURE", source=src)
    ddl, issues = convert_procedure_or_function(routine)
    assert "VARCHAR(4000)" in ddl
    assert any(i.severity == "error" and "%ROWTYPE" in i.message for i in issues)


def test_function_with_no_return_type_defaults_and_warns():
    src = """FUNCTION f1 IS
BEGIN
  RETURN 1;
END f1;"""
    routine = Routine(name="F1", schema="APP", kind="FUNCTION", source=src)
    ddl, issues = convert_procedure_or_function(routine)
    assert "RETURNS VARCHAR(4000)" in ddl
    assert any(i.severity == "warning" for i in issues)


# ------------------------------------------------------------------ triggers


def test_before_row_trigger_supported_natively_with_referencing():
    src = """BEGIN
  IF :NEW.salary > :OLD.salary THEN
    :NEW.raise_flag := 'Y';
  END IF;
END;"""
    routine = Routine(
        name="TRG1", schema="APP", kind="TRIGGER", source=src,
        table_name="EMPLOYEES", timing="BEFORE", events=["UPDATE"], row_level=True,
    )
    ddl, issues = convert_trigger(routine)
    assert "NO CASCADE BEFORE UPDATE" in ddl
    assert "REFERENCING NEW AS NEW_ROW OLD AS OLD_ROW" in ddl
    assert "NEW_ROW.salary > OLD_ROW.salary" in ddl
    assert "SET NEW_ROW.raise_flag = 'Y';" in ddl
    # unlike SQL Server, Db2 supports BEFORE triggers -- must not be flagged manual
    assert not any("no BEFORE trigger" in i.message for i in issues)


def test_multi_event_trigger_split_into_one_per_event():
    src = "BEGIN\n  NULL;\nEND;"
    routine = Routine(
        name="TRG1", schema="APP", kind="TRIGGER", source=src,
        table_name="EMPLOYEES", timing="AFTER", events=["INSERT", "UPDATE"], row_level=False,
    )
    ddl, issues = convert_trigger(routine)
    assert 'CREATE OR REPLACE TRIGGER "TRG1_INSERT"' in ddl
    assert 'CREATE OR REPLACE TRIGGER "TRG1_UPDATE"' in ddl
    assert any(i.severity == "info" and "split" in i.message for i in issues)


# ------------------------------------------------------------- package body


def test_package_body_flattened_into_prefixed_members():
    src = """PACKAGE BODY pkg_bank IS
  PROCEDURE get_balance (p_id IN NUMBER) IS
  BEGIN
    NULL;
  END get_balance;

  FUNCTION get_rate RETURN NUMBER IS
  BEGIN
    RETURN 1;
  END get_rate;
END pkg_bank;"""
    routine = Routine(name="PKG_BANK", schema="APP", kind="PACKAGE BODY", source=src)
    ddl, issues = convert_package_body(routine)
    assert 'CREATE OR REPLACE PROCEDURE "PKG_BANK_GET_BALANCE"' in ddl
    assert 'CREATE OR REPLACE FUNCTION "PKG_BANK_GET_RATE"' in ddl


# --------------------------------------------------------------- label gen


def test_label_gen_is_deterministic_per_instance():
    gen = _LabelGen()
    assert gen.next("rec") == "LBL_REC_1"
    assert gen.next("rec") == "LBL_REC_2"
    gen2 = _LabelGen()
    assert gen2.next("rec") == "LBL_REC_1"


# ---------------------------------------------- CONNECT BY -> recursive CTE

def test_connect_by_cursor_declaration_rewritten_for_db2():
    source = (
        "PROCEDURE PRINT_ORG_CHART (P_DUMMY IN NUMBER) IS\n"
        "  CURSOR C1 IS\n"
        "    SELECT EMPLOYEE_ID, MANAGER_ID, LAST_NAME\n"
        "    FROM EMPLOYEES\n"
        "    START WITH MANAGER_ID IS NULL\n"
        "    CONNECT BY PRIOR EMPLOYEE_ID = MANAGER_ID;\n"
        "  V_ID NUMBER;\n"
        "BEGIN\n"
        "  FOR REC IN C1 LOOP\n"
        "    V_ID := REC.EMPLOYEE_ID;\n"
        "  END LOOP;\n"
        "END PRINT_ORG_CHART;"
    )
    routine = Routine(name="PRINT_ORG_CHART", schema="HR", kind="PROCEDURE", source=source)
    result = convert_routine(routine, "DB2")
    ddl = result.converted_source
    assert "WITH cb_cte_1" in ddl
    assert "WITH RECURSIVE" not in ddl  # Db2 infers recursion, no RECURSIVE keyword
    assert "CONNECT BY" not in ddl.upper()
    assert result.status != ConversionStatus.MANUAL
    assert any(i.severity == "info" and "recursive CTE" in i.message for i in result.issues)


def test_connect_by_unsupported_shape_still_flagged_manual_for_db2():
    source = (
        "PROCEDURE PRINT_ORG_CHART IS\n"
        "  CURSOR C1 IS\n"
        "    SELECT EMPLOYEE_ID, MANAGER_ID FROM EMPLOYEES\n"
        "    START WITH MANAGER_ID IS NULL\n"
        "    CONNECT BY PRIOR EMPLOYEE_ID = MANAGER_ID AND STATUS = 'ACTIVE';\n"
        "BEGIN\n"
        "  NULL;\n"
        "END PRINT_ORG_CHART;"
    )
    routine = Routine(name="PRINT_ORG_CHART", schema="HR", kind="PROCEDURE", source=source)
    result = convert_routine(routine, "DB2")
    assert "CONNECT BY" in result.converted_source.upper()
    assert result.status == ConversionStatus.MANUAL


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
    routine = Routine(name="OUTER_PROC", schema="HR", kind="PROCEDURE", source=source)
    ddl, issues = convert_procedure_or_function(routine)
    assert "MANUAL CONVERSION REQUIRED: nested PROCEDURE inner_helper" in ddl
    assert "END inner_helper;" in ddl  # original source embedded verbatim, not shredded
    assert any(i.severity == "error" and "inner_helper" in i.message for i in issues)
    # The outer routine's own declarations/body must still convert cleanly.
    assert "DECLARE v_count DECIMAL(31,9) DEFAULT 0;" in ddl
    assert "DECLARE v_total DECIMAL(31,9);" in ddl

    result = convert_routine(routine, "DB2")
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
    routine = Routine(name="SUM_IDS", schema="HR", kind="PROCEDURE", source=source)
    ddl, issues = convert_procedure_or_function(routine)
    assert "MANUAL CONVERSION REQUIRED: TYPE t_id_list IS TABLE OF" in ddl
    assert any(i.severity == "error" and "t_id_list" in i.message for i in issues)
    result = convert_routine(routine, "DB2")
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
    ddl, issues = convert_trigger(trig)
    assert "MANUAL CONVERSION REQUIRED" in ddl
    assert any(i.severity == "error" and "bump" in i.message for i in issues)


# --------------------------------------------------------- DBMS_LOB / DBMS_RANDOM


def test_dbms_lob_getlength_and_substr_and_random_value_mapped():
    source = (
        "PROCEDURE p1 IS\n"
        "  v_len NUMBER;\n"
        "  v_part VARCHAR2(100);\n"
        "  v_r NUMBER;\n"
        "BEGIN\n"
        "  v_len := DBMS_LOB.GETLENGTH(v_clob);\n"
        "  v_part := DBMS_LOB.SUBSTR(v_clob, 10, 5);\n"
        "  v_r := DBMS_RANDOM.VALUE;\n"
        "END p1;"
    )
    routine = Routine(name="P1", schema="HR", kind="PROCEDURE", source=source)
    ddl, issues = convert_procedure_or_function(routine)
    assert "SET v_len = LENGTH(v_clob);" in ddl
    # DBMS_LOB.SUBSTR(lob, amount, offset) -> SUBSTR(lob, offset, amount): args 2 and 3 swap.
    assert "SET v_part = SUBSTR(v_clob, 5, 10);" in ddl
    assert "SET v_r = RAND();" in ddl
    assert "DBMS_LOB" not in ddl and "DBMS_RANDOM" not in ddl
    assert not any(i.severity == "error" and "DBMS_" in i.message for i in issues)

    result = convert_routine(routine, "DB2")
    assert result.status != ConversionStatus.MANUAL


def test_dbms_output_still_flagged_manual_for_db2():
    # Task scope explicitly leaves Db2's DBMS_OUTPUT unmapped (no console-
    # output statement to substitute it with) even though DBMS_LOB/
    # DBMS_RANDOM are now mapped -- this must not regress.
    source = (
        "PROCEDURE p1 IS\n"
        "BEGIN\n"
        "  DBMS_OUTPUT.PUT_LINE('hi');\n"
        "END p1;"
    )
    routine = Routine(name="P1", schema="HR", kind="PROCEDURE", source=source)
    ddl, issues = convert_procedure_or_function(routine)
    assert "DBMS_OUTPUT.PUT_LINE" in ddl
    assert any(i.severity == "error" and "DBMS_" in i.message for i in issues)
