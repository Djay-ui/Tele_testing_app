"""Oracle PL/SQL -> MySQL, at the unit level.

The live suite (repack/oracle_mysql_live.py) proves the output runs on a
real MariaDB. These pin down the pieces that are easy to regress and
expensive to notice: which constructs are refused rather than guessed at,
and the handful of translations whose being subtly wrong would produce SQL
that still executes and quietly does the wrong thing.
"""
from __future__ import annotations

import pytest

from tgdatabridge.core import plsql_mysql_converter as M
from tgdatabridge.core.plsql_converter import convert_routine as dispatch
from tgdatabridge.core.schema_model import ConversionStatus, Routine


def convert(source, kind="PROCEDURE", name="P", **kw):
    routine = Routine(name=name, schema="HR", kind=kind, source=source, **kw)
    return M.convert_routine(routine, "MySQL")


def body(source, **kw):
    return convert(source, **kw).converted_source


# ------------------------------------------------------------- dispatch

def test_an_oracle_source_with_a_mysql_target_reaches_this_converter():
    """The whole point of the round: this used to land in
    sql_translator's flag-only path."""
    routine = Routine(name="P", schema="HR", kind="PROCEDURE",
                      source="PROCEDURE p IS\nBEGIN\n  NULL;\nEND;")
    result = dispatch(routine, "MySQL")
    assert "CREATE PROCEDURE" in result.converted_source
    assert result.status == ConversionStatus.AUTOMATIC


def test_mariadb_is_treated_as_mysql():
    routine = Routine(name="P", schema="HR", kind="PROCEDURE",
                      source="PROCEDURE p IS\nBEGIN\n  NULL;\nEND;")
    assert "CREATE PROCEDURE" in dispatch(routine, "MariaDB").converted_source


def test_a_postgres_target_is_untouched_by_this():
    routine = Routine(name="P", schema="HR", kind="PROCEDURE",
                      source="PROCEDURE p IS\nBEGIN\n  NULL;\nEND;")
    assert "LANGUAGE plpgsql" in dispatch(routine, "PostgreSQL").converted_source


# ------------------------------------------------------------ signatures

def test_a_procedure_keeps_its_parameter_modes():
    sql = body("PROCEDURE p (a IN NUMBER, b OUT VARCHAR2, c IN OUT DATE) IS\n"
               "BEGIN\n  NULL;\nEND;")
    assert "IN `a`" in sql and "OUT `b`" in sql and "INOUT `c`" in sql


def test_a_function_gets_a_return_type_and_a_data_access_characteristic():
    """MySQL refuses to create a routine with no characteristic when
    binary logging is on -- error 1418."""
    sql = body("FUNCTION f RETURN NUMBER IS\nBEGIN\n  RETURN 1;\nEND;", kind="FUNCTION")
    assert "CREATE FUNCTION `P`()" in sql
    assert "RETURNS" in sql
    assert any(word in sql for word in
               ("DETERMINISTIC", "READS SQL DATA", "MODIFIES SQL DATA"))


def test_a_writing_routine_declares_modifies_sql_data():
    sql = body("PROCEDURE p IS\nBEGIN\n  INSERT INTO t VALUES (1);\nEND;")
    assert "MODIFIES SQL DATA" in sql


def test_a_reading_routine_declares_reads_sql_data():
    sql = body("PROCEDURE p IS\n  v NUMBER;\nBEGIN\n  SELECT 1 INTO v FROM t;\nEND;")
    assert "READS SQL DATA" in sql


def test_a_function_with_an_out_parameter_is_flagged():
    """MySQL function parameters are all IN; silently dropping the mode
    would compile and then never return the value."""
    result = convert("FUNCTION f (a OUT NUMBER) RETURN NUMBER IS\nBEGIN\n  RETURN 1;\nEND;",
                     kind="FUNCTION")
    assert result.status == ConversionStatus.MANUAL
    assert any("all IN" in i.message for i in result.issues)


def test_a_parameter_default_is_flagged_because_mysql_has_none():
    result = convert("PROCEDURE p (a NUMBER DEFAULT 1) IS\nBEGIN\n  NULL;\nEND;")
    assert any("no parameter defaults" in i.message for i in result.issues)
    assert "DEFAULT 1" not in result.converted_source


# ---------------------------------------------------------- declarations

def test_declarations_come_first_and_in_mysqls_required_order():
    """MySQL rejects the block outright unless variables precede
    conditions precede cursors precede handlers."""
    sql = body("PROCEDURE p IS\n"
               "  v NUMBER;\n"
               "  e_bad EXCEPTION;\n"
               "  CURSOR c IS SELECT id FROM t;\n"
               "BEGIN\n"
               "  FOR r IN (SELECT id FROM t) LOOP NULL; END LOOP;\n"
               "END;")
    lines = [line.strip() for line in sql.splitlines()]
    variable = next(i for i, s in enumerate(lines) if s.startswith("DECLARE `v`"))
    condition = next(i for i, s in enumerate(lines) if "CONDITION FOR" in s)
    cursor = next(i for i, s in enumerate(lines) if "CURSOR FOR" in s)
    assert variable < condition < cursor


def test_an_oracle_exception_becomes_a_mysql_condition():
    sql = body("PROCEDURE p IS\n  e_bad EXCEPTION;\nBEGIN\n  RAISE e_bad;\nEND;")
    assert "DECLARE `e_bad` CONDITION FOR SQLSTATE '45000';" in sql
    assert "SIGNAL `e_bad`" in sql


def test_a_default_uses_default_not_walrus():
    sql = body("PROCEDURE p IS\n  v NUMBER(5) := 7;\nBEGIN\n  NULL;\nEND;")
    assert "DECLARE `v` INT DEFAULT 7;" in sql
    assert ":=" not in sql


def test_a_parameterised_cursor_is_flagged():
    result = convert("PROCEDURE p IS\n  CURSOR c (x NUMBER) IS SELECT id FROM t WHERE id = x;\n"
                     "BEGIN\n  NULL;\nEND;")
    assert any("cannot" in i.message and "parameters" in i.message for i in result.issues)


def test_a_local_type_declaration_is_refused():
    result = convert("PROCEDURE p IS\n  TYPE t_list IS TABLE OF NUMBER;\nBEGIN\n  NULL;\nEND;")
    assert result.status == ConversionStatus.MANUAL


def test_rowtype_is_refused_rather_than_guessed():
    result = convert("PROCEDURE p IS\n  r t%ROWTYPE;\nBEGIN\n  NULL;\nEND;")
    assert result.status == ConversionStatus.MANUAL


# ------------------------------------------------------------ statements

def test_assignment_becomes_set():
    assert "SET `v` = 1;" in body("PROCEDURE p IS\n  v NUMBER;\nBEGIN\n  v := 1;\nEND;")


def test_new_and_old_lose_their_colon_on_the_left_of_an_assignment():
    """`:NEW.col := x` has to be recognised as an assignment. Leaving the
    colon on stopped the pattern matching at all, and the PL/SQL `:=` went
    out verbatim -- error 1064 on every trigger."""
    sql = body("BEGIN\n  :NEW.status := 'X';\nEND;", kind="TRIGGER",
               table_name="t", timing="BEFORE", events=["INSERT"], row_level=True)
    assert "SET NEW.status = 'X';" in sql
    assert ":=" not in sql


def test_null_statement_becomes_an_empty_block():
    assert "BEGIN END;" in body("PROCEDURE p IS\nBEGIN\n  NULL;\nEND;")


def test_a_bare_return_in_a_procedure_becomes_a_labelled_leave():
    sql = body("PROCEDURE p IS\nBEGIN\n  RETURN;\nEND;")
    assert "routine_body: BEGIN" in sql
    assert "LEAVE routine_body;" in sql
    assert "END routine_body;" in sql


def test_a_routine_with_no_bare_return_gets_no_pointless_label():
    sql = body("PROCEDURE p IS\nBEGIN\n  NULL;\nEND;")
    assert "routine_body" not in sql


def test_raise_application_error_becomes_signal():
    sql = body("PROCEDURE p IS\nBEGIN\n  RAISE_APPLICATION_ERROR(-20001, 'boom');\nEND;")
    assert "SIGNAL SQLSTATE '45000'" in sql
    assert "MESSAGE_TEXT = 'boom'" in sql


def test_execute_immediate_becomes_prepare_execute_deallocate():
    sql = body("PROCEDURE p IS\nBEGIN\n  EXECUTE IMMEDIATE 'DROP TABLE t';\nEND;")
    assert "PREPARE _tg_ps FROM @_tg_dyn;" in sql
    assert "EXECUTE _tg_ps;" in sql
    assert "DEALLOCATE PREPARE _tg_ps;" in sql


def test_execute_immediate_with_using_is_refused():
    result = convert("PROCEDURE p IS\nBEGIN\n  EXECUTE IMMEDIATE 'x' USING 1;\nEND;")
    assert result.status == ConversionStatus.MANUAL


# ----------------------------------------------------------------- flow

def test_elsif_becomes_elseif():
    sql = body("PROCEDURE p IS\n  v NUMBER;\nBEGIN\n"
               "  IF v = 1 THEN v := 2; ELSIF v = 2 THEN v := 3; ELSE v := 4; END IF;\nEND;")
    assert "ELSEIF" in sql and "ELSIF" not in sql
    assert "END IF;" in sql


def test_a_one_line_if_is_parsed_as_a_block_not_as_a_statement():
    """`IF x THEN y := 1; END IF;` on one line is ordinary Oracle, and the
    first attempt at this converter (a line-based rewriter) mangled it."""
    sql = body("PROCEDURE p IS\n  v NUMBER;\nBEGIN\n  IF v = 1 THEN v := 2; END IF;\nEND;")
    assert "IF v = 1 THEN" in sql
    assert "SET `v` = 2;" in sql
    assert "END IF;" in sql


def test_a_case_expression_inside_a_condition_does_not_end_it_early():
    sql = body("PROCEDURE p IS\n  v NUMBER;\nBEGIN\n"
               "  IF v = CASE WHEN v > 1 THEN 1 ELSE 2 END THEN v := 9; END IF;\nEND;")
    assert "IF v = CASE WHEN v > 1 THEN 1 ELSE 2 END THEN" in sql


def test_while_loop_becomes_while_do():
    sql = body("PROCEDURE p IS\n  v NUMBER;\nBEGIN\n"
               "  WHILE v < 3 LOOP v := v + 1; END LOOP;\nEND;")
    assert "WHILE v < 3 DO" in sql
    assert "END WHILE" in sql


def test_a_numeric_for_loop_becomes_a_counted_while():
    sql = body("PROCEDURE p IS\nBEGIN\n  FOR i IN 1 .. 3 LOOP NULL; END LOOP;\nEND;")
    assert "DECLARE `i` BIGINT;" in sql
    assert "SET `i` = 1;" in sql
    assert "`i` <= " in sql
    assert "SET `i` = `i` + 1;" in sql


def test_a_reverse_for_loop_counts_down():
    sql = body("PROCEDURE p IS\nBEGIN\n"
               "  FOR i IN REVERSE 1 .. 3 LOOP NULL; END LOOP;\nEND;")
    assert "SET `i` = 3;" in sql
    assert "`i` >= " in sql
    assert "SET `i` = `i` - 1;" in sql


def test_exit_when_becomes_a_guarded_leave_on_the_right_label():
    sql = body("PROCEDURE p IS\n  v NUMBER;\nBEGIN\n"
               "  LOOP\n    v := v + 1;\n    EXIT WHEN v > 3;\n  END LOOP;\nEND;")
    label = next(line.split(":")[0].strip() for line in sql.splitlines()
                 if line.strip().endswith(": LOOP"))
    assert f"IF v > 3 THEN LEAVE {label}; END IF;" in sql
    assert f"END LOOP {label};" in sql


def test_exit_leaves_the_innermost_loop():
    sql = body("PROCEDURE p IS\n  v NUMBER;\nBEGIN\n"
               "  LOOP\n    LOOP\n      EXIT WHEN v > 1;\n    END LOOP;\n"
               "    EXIT WHEN v > 2;\n  END LOOP;\nEND;")
    labels = [line.split(":")[0].strip() for line in sql.splitlines()
              if line.strip().endswith(": LOOP")]
    assert len(labels) == 2
    outer, inner = labels
    assert f"IF v > 1 THEN LEAVE {inner}; END IF;" in sql
    assert f"IF v > 2 THEN LEAVE {outer}; END IF;" in sql


def test_continue_when_becomes_iterate():
    sql = body("PROCEDURE p IS\n  v NUMBER;\nBEGIN\n"
               "  LOOP CONTINUE WHEN v = 1; EXIT; END LOOP;\nEND;")
    assert "ITERATE" in sql


# --------------------------------------------------------------- cursors

def test_a_cursor_for_loop_builds_the_whole_apparatus():
    sql = body("PROCEDURE p IS\nBEGIN\n"
               "  FOR r IN (SELECT id, name FROM t) LOOP NULL; END LOOP;\nEND;")
    assert "CURSOR FOR SELECT id, name FROM t" in sql
    assert "CONTINUE HANDLER FOR NOT FOUND" in sql
    assert "FETCH" in sql and "INTO `r_id`, `r_name`" in sql
    assert "OPEN" in sql and "CLOSE" in sql


def test_record_references_are_rewritten_to_the_fetch_variables():
    sql = body("PROCEDURE p IS\n  v NUMBER;\nBEGIN\n"
               "  FOR r IN (SELECT id FROM t) LOOP v := r.id; END LOOP;\nEND;")
    assert "SET `v` = `r_id`;" in sql
    assert "r.id" not in sql


def test_a_cursor_loop_gets_its_own_block_so_two_of_them_can_coexist():
    """MySQL allows one handler per condition per block (error 1338), so
    two NOT FOUND handlers in one block would refuse to create."""
    sql = body("PROCEDURE p IS\nBEGIN\n"
               "  FOR a IN (SELECT id FROM t) LOOP NULL; END LOOP;\n"
               "  FOR b IN (SELECT id FROM u) LOOP NULL; END LOOP;\nEND;")
    assert sql.count("CONTINUE HANDLER FOR NOT FOUND") == 2
    # ...each inside its own nested block: there is a block boundary
    # between them, so they are not two handlers for one condition in one
    # block, which is what error 1338 refuses.
    first = sql.index("CONTINUE HANDLER FOR NOT FOUND")
    second = sql.index("CONTINUE HANDLER FOR NOT FOUND", first + 1)
    assert "END;" in sql[first:second]


def test_a_loop_over_a_named_cursor_finds_its_query():
    sql = body("PROCEDURE p IS\n"
               "  CURSOR c IS SELECT id, name FROM t;\n"
               "BEGIN\n  FOR r IN c LOOP NULL; END LOOP;\nEND;")
    assert "INTO `r_id`, `r_name`" in sql


def test_a_loop_over_select_star_is_refused():
    """FETCH ... INTO needs a column list; guessing one would bind the
    wrong columns and still compile."""
    result = convert("PROCEDURE p IS\nBEGIN\n"
                     "  FOR r IN (SELECT * FROM t) LOOP NULL; END LOOP;\nEND;")
    assert result.status == ConversionStatus.MANUAL
    assert any("selects *" in i.message for i in result.issues)


def test_a_loop_over_an_unaliased_expression_is_refused():
    result = convert("PROCEDURE p IS\nBEGIN\n"
                     "  FOR r IN (SELECT a + b FROM t) LOOP NULL; END LOOP;\nEND;")
    assert result.status == ConversionStatus.MANUAL


def test_an_aliased_expression_is_accepted():
    sql = body("PROCEDURE p IS\nBEGIN\n"
               "  FOR r IN (SELECT a + b AS total FROM t) LOOP NULL; END LOOP;\nEND;")
    assert "INTO `r_total`" in sql


# ------------------------------------------------------------ exceptions

def test_the_exception_section_becomes_hoisted_exit_handlers():
    sql = body("PROCEDURE p IS\n  v NUMBER;\nBEGIN\n  v := 1;\n"
               "EXCEPTION\n  WHEN NO_DATA_FOUND THEN v := 0;\nEND;")
    assert "DECLARE EXIT HANDLER FOR NOT FOUND" in sql
    # Declared before the statements, as MySQL requires.
    assert sql.index("EXIT HANDLER") < sql.index("SET `v` = 1;")


def test_when_others_becomes_sqlexception():
    sql = body("PROCEDURE p IS\nBEGIN\n  NULL;\n"
               "EXCEPTION\n  WHEN OTHERS THEN NULL;\nEND;")
    assert "DECLARE EXIT HANDLER FOR SQLEXCEPTION" in sql


def test_dup_val_on_index_gets_a_real_sqlstate():
    sql = body("PROCEDURE p IS\nBEGIN\n  NULL;\n"
               "EXCEPTION\n  WHEN DUP_VAL_ON_INDEX THEN NULL;\nEND;")
    assert "SQLSTATE '23000'" in sql


def test_an_approximate_condition_says_so():
    result = convert("PROCEDURE p IS\nBEGIN\n  NULL;\n"
                     "EXCEPTION\n  WHEN TOO_MANY_ROWS THEN NULL;\nEND;")
    assert any("broader" in i.message for i in result.issues)


def test_an_unknown_exception_name_is_flagged_not_silently_broadened():
    result = convert("PROCEDURE p IS\nBEGIN\n  NULL;\n"
                     "EXCEPTION\n  WHEN SOME_ORACLE_THING THEN NULL;\nEND;")
    assert result.status == ConversionStatus.MANUAL
    assert any("Narrow it by hand" in i.message for i in result.issues)


def test_a_nested_blocks_exception_stays_with_that_block():
    sql = body("PROCEDURE p IS\n  v NUMBER;\nBEGIN\n"
               "  BEGIN\n    v := 1;\n  EXCEPTION\n    WHEN OTHERS THEN v := 2;\n  END;\n"
               "  v := 3;\nEND;")
    inner = sql.index("EXIT HANDLER")
    assert sql.index("SET `v` = 3;") > inner
    assert sql.count("EXIT HANDLER") == 1


# ------------------------------------------------------------- builtins

@pytest.mark.parametrize("oracle,mysql", [
    ("SYSDATE", "NOW()"),
    ("SYSTIMESTAMP", "NOW(6)"),
])
def test_date_builtins(oracle, mysql):
    sql = body(f"PROCEDURE p IS\n  v DATE;\nBEGIN\n  v := {oracle};\nEND;")
    assert mysql in sql


def test_nvl_becomes_ifnull():
    assert "IFNULL(a, b)" in body(
        "PROCEDURE p IS\n  v NUMBER;\nBEGIN\n  v := NVL(a, b);\nEND;")


def test_nvl2_becomes_case():
    sql = body("PROCEDURE p IS\n  v NUMBER;\nBEGIN\n  v := NVL2(a, b, c);\nEND;")
    assert "CASE WHEN (a) IS NOT NULL THEN b ELSE c END" in sql


def test_decode_becomes_case_and_says_what_changed_about_nulls():
    result = convert("PROCEDURE p IS\n  v NUMBER;\nBEGIN\n  v := DECODE(a, 1, 'x', 'y');\nEND;")
    assert "CASE a WHEN 1 THEN 'x' ELSE 'y' END" in result.converted_source
    assert any("NULL = NULL" in i.message for i in result.issues)


def test_three_argument_instr_becomes_locate_with_the_arguments_swapped():
    sql = body("PROCEDURE p IS\n  v NUMBER;\nBEGIN\n  v := INSTR(s, 'x', 2);\nEND;")
    assert "LOCATE('x', s, 2)" in sql


def test_a_date_mask_is_translated_not_passed_through():
    """`'DD/MM/YYYY'` means something in Oracle and is literal text to
    MySQL, so leaving it alone returns the mask itself as the answer."""
    sql = body("PROCEDURE p IS\n  v VARCHAR2(20);\nBEGIN\n"
               "  v := TO_CHAR(d, 'DD/MM/YYYY');\nEND;")
    assert "DATE_FORMAT(d, '%d/%m/%Y')" in sql


def test_to_date_becomes_str_to_date_with_a_translated_mask():
    sql = body("PROCEDURE p IS\n  v DATE;\nBEGIN\n"
               "  v := TO_DATE('2026-03-04', 'YYYY-MM-DD');\nEND;")
    assert "STR_TO_DATE('2026-03-04', '%Y-%m-%d')" in sql


def test_a_sequence_reference_calls_the_generated_function():
    sql = body("PROCEDURE p IS\n  v NUMBER;\nBEGIN\n  v := ORDER_SEQ.NEXTVAL;\nEND;")
    assert "`ORDER_SEQ_NEXTVAL`()" in sql


# -------------------------------------------------------- concatenation

def test_concatenation_becomes_concat_because_pipes_mean_or_in_mysql():
    """`||` is logical OR to MySQL unless PIPES_AS_CONCAT happens to be
    set, so leaving it would turn every built string into 0 or 1."""
    sql = body("PROCEDURE p IS\n  v VARCHAR2(50);\nBEGIN\n  v := 'a' || b || 'c';\nEND;")
    assert "CONCAT('a', b, 'c')" in sql
    assert "||" not in sql


def test_concatenation_stops_at_the_end_of_its_own_expression():
    sql = body("PROCEDURE p IS\nBEGIN\n"
               "  UPDATE t SET a = 'x' || y WHERE z = 1;\nEND;")
    assert "SET a = CONCAT('x', y) WHERE z = 1" in sql


def test_concatenation_inside_a_function_call_keeps_the_call_intact():
    sql = body("PROCEDURE p IS\n  v VARCHAR2(50);\nBEGIN\n"
               "  v := UPPER('a' || b);\nEND;")
    assert "UPPER(CONCAT('a', b))" in sql


def test_a_case_expression_is_one_operand_of_a_concatenation():
    sql = body("PROCEDURE p IS\n  v VARCHAR2(50);\nBEGIN\n"
               "  v := 'a' || DECODE(b, 1, 'x', 'y');\nEND;")
    assert "CONCAT('a', CASE b WHEN 1 THEN 'x' ELSE 'y' END)" in sql


def test_pipes_inside_a_string_literal_are_left_alone():
    sql = body("PROCEDURE p IS\n  v VARCHAR2(50);\nBEGIN\n  v := 'a || b';\nEND;")
    assert "'a || b'" in sql


def test_keywords_inside_a_string_literal_are_not_rewritten():
    sql = body("PROCEDURE p IS\n  v VARCHAR2(50);\nBEGIN\n"
               "  v := 'END LOOP is not code';\nEND;")
    assert "'END LOOP is not code'" in sql


# --------------------------------------------------------------- triggers

def test_a_multi_event_trigger_becomes_one_trigger_per_event():
    result = convert("BEGIN\n  NULL;\nEND;", kind="TRIGGER", name="TRG",
                     table_name="t", timing="BEFORE",
                     events=["INSERT", "UPDATE"], row_level=True)
    sql = result.converted_source
    assert "CREATE TRIGGER `TRG_INSERT`" in sql
    assert "CREATE TRIGGER `TRG_UPDATE`" in sql
    assert any("one event each" in i.message for i in result.issues)


def test_a_single_event_trigger_keeps_its_own_name():
    sql = body("BEGIN\n  NULL;\nEND;", kind="TRIGGER", name="TRG",
               table_name="t", timing="BEFORE", events=["INSERT"], row_level=True)
    assert "CREATE TRIGGER `TRG`" in sql


def test_each_trigger_drops_itself_first_since_mysql_cannot_replace():
    sql = body("BEGIN\n  NULL;\nEND;", kind="TRIGGER", name="TRG",
               table_name="t", timing="BEFORE", events=["INSERT"], row_level=True)
    assert "DROP TRIGGER IF EXISTS `TRG`;" in sql


def test_inserting_and_updating_are_resolved_per_emitted_trigger():
    sql = body("BEGIN\n  IF INSERTING THEN NULL; END IF;\n"
               "  IF UPDATING THEN NULL; END IF;\nEND;",
               kind="TRIGGER", name="TRG", table_name="t", timing="BEFORE",
               events=["INSERT", "UPDATE"], row_level=True)
    insert_block, update_block = sql.split("CREATE TRIGGER `TRG_UPDATE`")
    assert "IF TRUE THEN" in insert_block and "IF FALSE THEN" in insert_block
    assert "IF TRUE THEN" in update_block and "IF FALSE THEN" in update_block
    assert "INSERTING" not in sql and "UPDATING" not in sql


def test_a_statement_level_trigger_is_flagged_because_mysql_has_none():
    result = convert("BEGIN\n  NULL;\nEND;", kind="TRIGGER", name="TRG",
                     table_name="t", timing="BEFORE", events=["INSERT"],
                     row_level=False)
    assert result.status == ConversionStatus.MANUAL
    assert any("FOR EACH ROW" in i.message for i in result.issues)


def test_issues_are_not_repeated_once_per_emitted_trigger():
    result = convert("BEGIN\n  v := DECODE(a, 1, 'x', 'y');\nEND;", kind="TRIGGER",
                     name="TRG", table_name="t", timing="BEFORE",
                     events=["INSERT", "UPDATE", "DELETE"], row_level=True)
    decode_notes = [i for i in result.issues if "DECODE" in i.message]
    assert len(decode_notes) == 1


# --------------------------------------------------------------- packages

def test_a_package_body_is_flattened_into_prefixed_routines():
    result = convert("PACKAGE BODY pkg AS\n"
                     "  PROCEDURE one IS BEGIN NULL; END one;\n"
                     "  FUNCTION two RETURN NUMBER IS BEGIN RETURN 1; END two;\n"
                     "END pkg;", kind="PACKAGE BODY", name="pkg")
    sql = result.converted_source
    assert "CREATE PROCEDURE `pkg_one`" in sql
    assert "CREATE FUNCTION `pkg_two`" in sql


def test_each_flattened_member_drops_itself_first():
    sql = body("PACKAGE BODY pkg AS\n  PROCEDURE one IS BEGIN NULL; END one;\nEND pkg;",
               kind="PACKAGE BODY", name="pkg")
    assert "DROP PROCEDURE IF EXISTS `pkg_one`;" in sql


def test_the_packages_own_closing_end_is_not_left_dangling():
    sql = body("PACKAGE BODY pkg AS\n  PROCEDURE one IS BEGIN NULL; END one;\nEND pkg;",
               kind="PACKAGE BODY", name="pkg")
    assert "END pkg;" not in sql


def test_a_package_spec_explains_where_its_members_went():
    result = convert("PACKAGE pkg AS\n  PROCEDURE one;\nEND pkg;",
                     kind="PACKAGE", name="pkg")
    assert "pkg_<member>" in result.converted_source
    assert result.status == ConversionStatus.AUTOMATIC_WITH_WARNINGS


def test_package_level_state_is_called_out():
    result = convert("PACKAGE BODY pkg AS\n  PROCEDURE one IS BEGIN NULL; END one;\nEND pkg;",
                     kind="PACKAGE BODY", name="pkg")
    assert any("do not share state" in i.message for i in result.issues)


# ------------------------------------------------------------- refusals

@pytest.mark.parametrize("fragment", [
    "SELECT id BULK COLLECT INTO v FROM t;",
    "FORALL i IN 1..10 INSERT INTO t VALUES (i);",
    "DBMS_LOCK.SLEEP(1);",
    "UTL_FILE.FOPEN('a', 'b', 'c');",
    "SELECT id FROM t CONNECT BY PRIOR id = pid;",
    "GOTO done;",
])
def test_constructs_with_no_mysql_equivalent_stay_manual(fragment):
    result = convert(f"PROCEDURE p IS\n  v NUMBER;\nBEGIN\n  {fragment}\nEND;")
    assert result.status == ConversionStatus.MANUAL


def test_an_unparsable_body_is_reported_rather_than_half_converted():
    result = convert("PROCEDURE p IS\nBEGIN\n  IF x\nEND;")
    assert result.status == ConversionStatus.MANUAL
    assert any("could not be parsed" in i.message or "no THEN" in i.message
               for i in result.issues)


def test_conversion_never_raises_on_junk():
    for junk in ("", "   ", "PROCEDURE", "BEGIN", "END;", "))))"):
        convert(junk)


# ----------------------------------------------------------------- types

def test_unconstrained_number_becomes_double_so_it_prints_like_oracle():
    """DECIMAL(65,30) is exact but renders every value with thirty decimal
    places, so `'Order ' || id` came out as "Order 10.000000000...". Oracle's
    NUMBER has no fixed scale, and neither does DOUBLE."""
    result = convert("PROCEDURE p (a NUMBER) IS\nBEGIN\n  NULL;\nEND;")
    assert "IN `a` DOUBLE" in result.converted_source
    assert any("holds money" in i.message for i in result.issues)


def test_a_precise_number_keeps_its_precision():
    sql = body("PROCEDURE p (a NUMBER(12,2)) IS\nBEGIN\n  NULL;\nEND;")
    assert "DECIMAL(12,2)" in sql


@pytest.mark.parametrize("oracle,mysql", [
    ("PLS_INTEGER", "BIGINT"), ("BINARY_INTEGER", "BIGINT"),
    ("BINARY_DOUBLE", "DOUBLE"), ("BINARY_FLOAT", "FLOAT"),
])
def test_plsql_only_scalar_types(oracle, mysql):
    sql = body(f"PROCEDURE p IS\n  v {oracle};\nBEGIN\n  NULL;\nEND;")
    assert f"DECLARE `v` {mysql};" in sql


def test_boolean_becomes_tinyint_with_a_note():
    result = convert("PROCEDURE p IS\n  v BOOLEAN;\nBEGIN\n  NULL;\nEND;")
    assert "TINYINT(1)" in result.converted_source
    assert any("no boolean" in i.message for i in result.issues)


def test_a_type_anchor_is_flagged_rather_than_sized_by_guesswork():
    result = convert("PROCEDURE p IS\n  v emp.name%TYPE;\nBEGIN\n  NULL;\nEND;")
    assert any("%TYPE" in i.message for i in result.issues)
    assert "DECLARE `v` TEXT;" in result.converted_source
