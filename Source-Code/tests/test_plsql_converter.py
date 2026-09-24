from tgdatabridge.core import plsql_converter as pc
from tgdatabridge.core.schema_model import ConversionStatus, Routine


def test_split_top_level_respects_parens_and_strings():
    assert pc.split_top_level("NUMBER(10,2), 'a,b', x") == ["NUMBER(10,2)", "'a,b'", "x"]


def test_transform_function_calls_basic():
    text = "x := FOO(1, 2) + FOO(3, 4);"
    out, count = pc.transform_function_calls(text, "FOO", lambda args: f"BAR({'+'.join(args)})")
    assert count == 2
    assert "BAR(1+2)" in out
    assert "BAR(3+4)" in out


def test_simple_procedure_conversion():
    source = (
        "PROCEDURE greet (p_name IN VARCHAR2) IS\n"
        "  v_msg VARCHAR2(100);\n"
        "BEGIN\n"
        "  v_msg := 'Hello ' || p_name;\n"
        "  DBMS_OUTPUT.PUT_LINE(v_msg);\n"
        "END greet;"
    )
    routine = Routine(name="GREET", schema="HR", kind="PROCEDURE", source=source)
    ddl, issues = pc.convert_procedure_or_function(routine)
    # Routine name is quoted lowercase (same reasoning as ddl_generator._quote_pg);
    # parameter names themselves aren't quoted at all, so keep their source case.
    assert 'CREATE OR REPLACE PROCEDURE "greet"(p_name VARCHAR)' in ddl
    assert "DECLARE" in ddl
    assert "v_msg VARCHAR(100);" in ddl
    assert "RAISE NOTICE '%', v_msg" in ddl
    assert ddl.strip().endswith("$$ LANGUAGE plpgsql;")
    assert "END;" in ddl and "END greet;" not in ddl


def test_function_with_return_type_and_decode():
    source = (
        "FUNCTION grade (p_score IN NUMBER) RETURN VARCHAR2 IS\n"
        "BEGIN\n"
        "  RETURN DECODE(p_score, 1, 'A', 2, 'B', 'F');\n"
        "END grade;"
    )
    routine = Routine(name="GRADE", schema="HR", kind="FUNCTION", source=source)
    ddl, issues = pc.convert_procedure_or_function(routine)
    assert 'CREATE OR REPLACE FUNCTION "grade"(p_score NUMERIC)' in ddl
    assert "RETURNS VARCHAR" in ddl
    assert "CASE p_score" in ddl
    assert "WHEN 1 THEN 'A'" in ddl
    assert "ELSE 'F'" in ddl
    assert any("NULL" in i.message for i in issues)  # DECODE NULL-semantics warning


def test_nvl_and_nvl2_and_instr():
    source = (
        "FUNCTION f (p IN VARCHAR2, q IN VARCHAR2) RETURN VARCHAR2 IS\n"
        "BEGIN\n"
        "  RETURN NVL(p, q) || NVL2(p, 'has', 'empty') || INSTR(p, 'x');\n"
        "END f;"
    )
    routine = Routine(name="F", schema="HR", kind="FUNCTION", source=source)
    ddl, issues = pc.convert_procedure_or_function(routine)
    assert "COALESCE(p, q)" in ddl
    assert "CASE WHEN (p) IS NOT NULL THEN 'has' ELSE 'empty' END" in ddl
    assert "POSITION('x' IN p)" in ddl


def test_sys_context_userenv_session_user_is_mapped_to_the_postgres_keyword():
    # The exact real-world failure: an audit trigger stamping the calling
    # user onto a row via SYS_CONTEXT('USERENV', 'SESSION_USER') reached
    # Postgres unconverted and blew up on every insert with
    # `sys_context(unknown, unknown) does not exist`.
    source = (
        "FUNCTION who IS\n"
        "BEGIN\n"
        "  RETURN SYS_CONTEXT('USERENV', 'SESSION_USER');\n"
        "END who;"
    )
    routine = Routine(name="WHO", schema="HR", kind="FUNCTION", source=source)
    ddl, issues = pc.convert_procedure_or_function(routine)
    assert "session_user" in ddl
    assert "SYS_CONTEXT" not in ddl.upper() or "sys_context" not in ddl.lower()


def test_sys_context_unmappable_userenv_parameter_is_left_and_flagged():
    # IP_ADDRESS has no faithful Postgres session equivalent -- guessing
    # one would compile fine and silently return the wrong value forever,
    # worse than the loud failure it replaces. Left unconverted and flagged
    # instead, matching this file's DECODE/INSTR "don't guess" pattern.
    source = (
        "FUNCTION client_ip RETURN VARCHAR2 IS\n"
        "BEGIN\n"
        "  RETURN SYS_CONTEXT('USERENV', 'IP_ADDRESS');\n"
        "END client_ip;"
    )
    routine = Routine(name="CLIENT_IP", schema="HR", kind="FUNCTION", source=source)
    ddl, issues = pc.convert_procedure_or_function(routine)
    assert "SYS_CONTEXT('USERENV', 'IP_ADDRESS')" in ddl
    assert any("IP_ADDRESS" in i.message and i.severity == "warning" for i in issues)


def test_sequence_and_execute_immediate_and_dual():
    source = (
        "PROCEDURE next_id IS\n"
        "  v_id NUMBER;\n"
        "BEGIN\n"
        "  SELECT EMP_SEQ.NEXTVAL INTO v_id FROM DUAL;\n"
        "  EXECUTE IMMEDIATE 'ALTER SESSION SET x = 1';\n"
        "END next_id;"
    )
    routine = Routine(name="NEXT_ID", schema="HR", kind="PROCEDURE", source=source)
    ddl, issues = pc.convert_procedure_or_function(routine)
    assert "nextval('\"emp_seq\"')" in ddl
    assert "EXECUTE 'ALTER SESSION SET x = 1'" in ddl
    assert "FROM DUAL" not in ddl.upper() or "FROM  DUAL" not in ddl  # removed


def test_manual_markers_flag_dbms_lock():
    source = (
        "PROCEDURE wait_a_bit IS\n"
        "BEGIN\n"
        "  DBMS_LOCK.SLEEP(5);\n"
        "END wait_a_bit;"
    )
    routine = Routine(name="WAIT_A_BIT", schema="HR", kind="PROCEDURE", source=source)
    ddl, issues = pc.convert_procedure_or_function(routine)
    assert any(i.severity == "error" and "DBMS_" in i.message for i in issues)


def test_declared_exception_and_pragma_and_constant():
    # RAISE bad_input; and WHEN bad_input THEN both get the SAME synthetic
    # SQLSTATE ('U0001', the first/only declared exception) -- see
    # convert_body's own comment on why both sides need to agree. Before
    # that fix, WHEN bad_input THEN was left completely untouched and
    # Postgres rejected the whole CREATE PROCEDURE with "unrecognized
    # exception condition" the moment it tried to parse "bad_input" as a
    # real condition name (the exact bug a real migration's
    # FEATURE_PL_ADMIN_PKG hit on WHEN OTHERS -> ROLLBACK TO's neighboring
    # user-defined exception).
    source = (
        "PROCEDURE risky IS\n"
        "  c_limit CONSTANT NUMBER := 100;\n"
        "  bad_input EXCEPTION;\n"
        "  PRAGMA EXCEPTION_INIT(bad_input, -20001);\n"
        "BEGIN\n"
        "  IF c_limit > 0 THEN\n"
        "    RAISE bad_input;\n"
        "  END IF;\n"
        "EXCEPTION\n"
        "  WHEN bad_input THEN NULL;\n"
        "END risky;"
    )
    routine = Routine(name="RISKY", schema="HR", kind="PROCEDURE", source=source)
    ddl, issues = pc.convert_procedure_or_function(routine)
    assert "c_limit CONSTANT NUMERIC := 100;" in ddl
    assert "RAISE EXCEPTION 'bad_input' USING ERRCODE = 'U0001';" in ddl
    assert "WHEN SQLSTATE 'U0001' THEN NULL;" in ddl
    assert any("PRAGMA" in i.message for i in issues)
    # No longer an "error" -- both RAISE and WHEN are now fully, automatically
    # converted, so this is a "review only if it matters" warning instead.
    assert any("bad_input" in i.message and i.severity == "warning" for i in issues)


def test_cursor_declaration_conversion():
    source = (
        "PROCEDURE list_emps IS\n"
        "  CURSOR emp_cur IS SELECT emp_id FROM employees;\n"
        "  v_id NUMBER;\n"
        "BEGIN\n"
        "  OPEN emp_cur;\n"
        "  CLOSE emp_cur;\n"
        "END list_emps;"
    )
    routine = Routine(name="LIST_EMPS", schema="HR", kind="PROCEDURE", source=source)
    ddl, issues = pc.convert_procedure_or_function(routine)
    assert "emp_cur CURSOR FOR SELECT emp_id FROM employees;" in ddl


def test_parameterized_cursor_declared_types_are_mapped_to_postgres():
    """Real bug, reported from the field: a parameterized cursor's own
    parameter list (`CURSOR cur_x(cp_category VARCHAR2) IS SELECT ...`) was
    passed through completely unmapped -- unlike a routine's own parameter
    list, which convert_procedure_or_function already maps through
    _convert_declared_type. The unmapped cursor parameter list reached
    "Apply DDL to Target" with literal Oracle "VARCHAR2" still in it, and
    Postgres rejected the whole CREATE FUNCTION/PROCEDURE with
    'type "varchar2" does not exist'. Cursor parameters never carry an
    IN/OUT mode keyword in Oracle (they're always effectively IN), so the
    mapped Postgres parameter list should carry only name and mapped type,
    exactly mirroring a plain routine parameter with no mode/default."""
    source = (
        "PROCEDURE list_by_category IS\n"
        "  CURSOR cur_by_category(cp_category VARCHAR2, cp_limit NUMBER) IS\n"
        "    SELECT product_id FROM products WHERE category = cp_category;\n"
        "  v_id NUMBER;\n"
        "BEGIN\n"
        "  OPEN cur_by_category('BOOKS', 10);\n"
        "  CLOSE cur_by_category;\n"
        "END list_by_category;"
    )
    routine = Routine(name="LIST_BY_CATEGORY", schema="HR", kind="PROCEDURE", source=source)
    ddl, issues = pc.convert_procedure_or_function(routine)
    assert "varchar2" not in ddl.lower()
    assert "cur_by_category CURSOR(cp_category VARCHAR, cp_limit NUMERIC) FOR" in ddl
    result = pc.convert_routine(routine, "PostgreSQL")
    assert result.status != ConversionStatus.MANUAL


def test_sys_refcursor_out_parameter_is_mapped_and_open_for_still_works():
    """See test_sys_refcursor_maps_to_postgres_refcursor in
    test_type_mapping.py for the underlying mapping fix; this test checks
    it end-to-end through the converter the way the real, reported routine
    actually used it: a SYS_REFCURSOR OUT parameter, opened with a plain
    query."""
    source = (
        "PROCEDURE get_products(p_category IN VARCHAR2, p_cursor OUT SYS_REFCURSOR) IS\n"
        "BEGIN\n"
        "  OPEN p_cursor FOR SELECT product_id FROM products WHERE category = p_category;\n"
        "END get_products;"
    )
    routine = Routine(name="GET_PRODUCTS", schema="HR", kind="PROCEDURE", source=source)
    ddl, issues = pc.convert_procedure_or_function(routine)
    assert "OUT p_cursor REFCURSOR" in ddl
    assert "sys_refcursor" not in ddl.lower()
    result = pc.convert_routine(routine, "PostgreSQL")
    assert result.status != ConversionStatus.MANUAL


def test_parameterized_cursor_with_default_value_is_preserved():
    source = (
        "PROCEDURE list_top IS\n"
        "  CURSOR cur_top(cp_n NUMBER := 10) IS\n"
        "    SELECT product_id FROM products;\n"
        "BEGIN\n"
        "  OPEN cur_top;\n"
        "  CLOSE cur_top;\n"
        "END list_top;"
    )
    routine = Routine(name="LIST_TOP", schema="HR", kind="PROCEDURE", source=source)
    ddl, issues = pc.convert_procedure_or_function(routine)
    assert "cp_n" in ddl and "DEFAULT 10" in ddl


def test_exception_name_remapping():
    source = (
        "PROCEDURE p IS\n"
        "BEGIN\n"
        "  NULL;\n"
        "EXCEPTION\n"
        "  WHEN DUP_VAL_ON_INDEX THEN NULL;\n"
        "  WHEN ZERO_DIVIDE THEN NULL;\n"
        "END p;"
    )
    routine = Routine(name="P", schema="HR", kind="PROCEDURE", source=source)
    ddl, issues = pc.convert_procedure_or_function(routine)
    assert "WHEN unique_violation THEN" in ddl
    assert "WHEN division_by_zero THEN" in ddl


def test_trigger_conversion_produces_function_and_trigger():
    body = (
        "BEGIN\n"
        "  :NEW.updated_at := SYSDATE;\n"
        "END;"
    )
    routine = Routine(
        name="TRG_EMP_AUDIT", schema="HR", kind="TRIGGER", source=body,
        table_name="EMPLOYEES", timing="BEFORE", events=["INSERT", "UPDATE"], row_level=True,
    )
    ddl, issues = pc.convert_trigger(routine)
    assert 'CREATE OR REPLACE FUNCTION "trg_emp_audit_fn"()' in ddl
    assert "RETURNS TRIGGER" in ddl
    assert "NEW.updated_at := CURRENT_TIMESTAMP;" in ddl
    assert "RETURN NEW;" in ddl
    assert 'CREATE TRIGGER "trg_emp_audit"' in ddl
    assert "BEFORE INSERT OR UPDATE ON \"employees\"" in ddl
    assert "FOR EACH ROW" in ddl
    assert 'EXECUTE FUNCTION "trg_emp_audit_fn"();' in ddl


def test_trigger_ddl_drops_existing_trigger_before_recreating():
    # CREATE TRIGGER has no IF NOT EXISTS (and CREATE OR REPLACE TRIGGER is
    # PG14+ only); precede it with DROP TRIGGER IF EXISTS so re-applying the
    # same DDL against a target that already has this trigger doesn't fail
    # with "trigger ... already exists".
    body = "BEGIN\n  :NEW.updated_at := SYSDATE;\nEND;"
    routine = Routine(
        name="TRG_EMP_AUDIT", schema="HR", kind="TRIGGER", source=body,
        table_name="EMPLOYEES", timing="BEFORE", events=["INSERT"], row_level=True,
    )
    ddl, issues = pc.convert_trigger(routine)
    drop_pos = ddl.index('DROP TRIGGER IF EXISTS "trg_emp_audit" ON "employees";')
    create_pos = ddl.index('CREATE TRIGGER "trg_emp_audit"')
    assert drop_pos < create_pos


def test_trigger_conversion_resolves_inserting_updating_deleting_to_tg_op():
    # The exact bug a real migration hit on FEATURE_TEST_DATA: left as
    # bare keywords, IF INSERTING/UPDATING/DELETING reach Postgres as
    # undeclared identifiers and fail at trigger-fire time with
    # `column "inserting" does not exist`. Postgres has no such
    # pseudo-columns -- TG_OP is how a trigger function tells the
    # firing event apart.
    body = (
        "BEGIN\n"
        "  IF INSERTING THEN\n"
        "    NEW.created_at := SYSDATE;\n"
        "  ELSIF UPDATING THEN\n"
        "    NEW.updated_at := SYSDATE;\n"
        "  ELSIF DELETING THEN\n"
        "    NULL;\n"
        "  END IF;\n"
        "END;"
    )
    routine = Routine(
        name="TRG_FEATURE_TEST_AUDIT", schema="HR", kind="TRIGGER", source=body,
        table_name="FEATURE_TEST_DATA", timing="BEFORE",
        events=["INSERT", "UPDATE", "DELETE"], row_level=True,
    )
    ddl, issues = pc.convert_trigger(routine)
    assert "INSERTING" not in ddl.upper()
    assert "UPDATING" not in ddl.upper()
    assert "DELETING" not in ddl.upper()
    assert "IF (TG_OP = 'INSERT') THEN" in ddl
    assert "ELSIF (TG_OP = 'UPDATE') THEN" in ddl
    assert "ELSIF (TG_OP = 'DELETE') THEN" in ddl


def test_trigger_conversion_leaves_inserting_inside_a_string_literal_alone():
    # A message like "inserting a new row" is data, not a pseudo-column
    # reference, and must not be rewritten -- only a bare keyword use is.
    body = (
        "BEGIN\n"
        "  IF INSERTING THEN\n"
        "    RAISE NOTICE 'inserting a new row: %', :NEW.id;\n"
        "  END IF;\n"
        "END;"
    )
    routine = Routine(
        name="TRG_NOTICE", schema="HR", kind="TRIGGER", source=body,
        table_name="WIDGETS", timing="BEFORE", events=["INSERT"], row_level=True,
    )
    ddl, issues = pc.convert_trigger(routine)
    assert "IF (TG_OP = 'INSERT') THEN" in ddl
    assert "'inserting a new row: %'" in ddl


def test_package_body_flattening():
    source = (
        "PACKAGE BODY math_utils IS\n"
        "  FUNCTION add_one (p IN NUMBER) RETURN NUMBER IS\n"
        "  BEGIN\n"
        "    RETURN p + 1;\n"
        "  END add_one;\n"
        "\n"
        "  PROCEDURE log_it (p IN VARCHAR2) IS\n"
        "  BEGIN\n"
        "    DBMS_OUTPUT.PUT_LINE(p);\n"
        "  END log_it;\n"
        "END math_utils;"
    )
    routine = Routine(name="MATH_UTILS", schema="HR", kind="PACKAGE BODY", source=source)
    ddl, issues = pc.convert_package_body(routine)
    assert 'CREATE OR REPLACE FUNCTION "math_utils_add_one"' in ddl
    assert 'CREATE OR REPLACE PROCEDURE "math_utils_log_it"' in ddl


def test_package_body_last_member_does_not_get_duplicate_end():
    # The last member's source chunk runs through the end of the package
    # body source, which includes the package's own closing "END
    # math_utils;" after the member's own "END log_it;". Left unstripped,
    # that trailing END normalizes on top of the wrong statement (the
    # *package's* END, not the member's) and leaves the member's real END
    # with its name suffix intact -- e.g. "END log_it;\n\nEND;" -- which
    # Postgres rejects. Regression test for exactly this structural bug,
    # found while diagnosing the real PKG_BANK package from a live target.
    source = (
        "PACKAGE BODY math_utils IS\n"
        "  PROCEDURE log_it (p IN VARCHAR2) IS\n"
        "  BEGIN\n"
        "    DBMS_OUTPUT.PUT_LINE(p);\n"
        "  END log_it;\n"
        "END math_utils;"
    )
    routine = Routine(name="MATH_UTILS", schema="HR", kind="PACKAGE BODY", source=source)
    ddl, issues = pc.convert_package_body(routine)
    assert "END log_it" not in ddl  # member's own END was normalized away
    assert "END math_utils" not in ddl  # package's closing END never leaked in
    assert ddl.count("END;") == 1  # exactly one, correctly matched END


def test_pkg_bank_end_to_end_matches_real_target_error_report():
    # Direct reproduction of the PKG_BANK package pasted from the user's
    # live Oracle source (ALL_SOURCE), which hit "loop variable of loop
    # over rows must be a record variable or list of scalar variables" and
    # a duplicate END on the target. Covers both fixes together.
    source = (
        "PACKAGE BODY PKG_BANK AS\n"
        "    PROCEDURE GET_CUSTOMER\n"
        "    (\n"
        "        P_CUSTOMER_ID IN NUMBER\n"
        "    )\n"
        "    AS\n"
        "    BEGIN\n"
        "        FOR REC IN\n"
        "        (\n"
        "            SELECT *\n"
        "            FROM CUSTOMER\n"
        "            WHERE CUSTOMER_ID = P_CUSTOMER_ID\n"
        "        )\n"
        "        LOOP\n"
        "            DBMS_OUTPUT.PUT_LINE('Customer ID   : ' || REC.CUSTOMER_ID);\n"
        "        END LOOP;\n"
        "    END GET_CUSTOMER;\n"
        "\n"
        "    FUNCTION GET_TOTAL_CUSTOMERS\n"
        "    RETURN NUMBER\n"
        "    AS\n"
        "        V_COUNT NUMBER;\n"
        "    BEGIN\n"
        "        SELECT COUNT(*)\n"
        "        INTO V_COUNT\n"
        "        FROM CUSTOMER;\n"
        "        RETURN V_COUNT;\n"
        "    END GET_TOTAL_CUSTOMERS;\n"
        "END PKG_BANK;"
    )
    routine = Routine(name="PKG_BANK", schema="HR", kind="PACKAGE BODY", source=source)
    ddl, issues = pc.convert_package_body(routine)

    # GET_CUSTOMER: the implicit-cursor FOR loop variable is explicitly
    # declared as RECORD.
    get_customer = ddl.split('CREATE OR REPLACE FUNCTION "pkg_bank_get_total_customers"')[0]
    assert "REC RECORD;" in get_customer

    # GET_TOTAL_CUSTOMERS: no duplicate/mismatched END statement.
    assert "END GET_TOTAL_CUSTOMERS" not in ddl
    assert "END PKG_BANK" not in ddl
    get_total = ddl.split('CREATE OR REPLACE FUNCTION "pkg_bank_get_total_customers"')[1]
    assert get_total.count("END;") == 1


def test_convert_routine_sets_status_and_score():
    source = (
        "PROCEDURE ok_proc IS\n"
        "BEGIN\n"
        "  NULL;\n"
        "END ok_proc;"
    )
    routine = Routine(name="OK_PROC", schema="HR", kind="PROCEDURE", source=source)
    result = pc.convert_routine(routine, "PostgreSQL")
    assert result.converted_source is not None
    assert result.status in (ConversionStatus.AUTOMATIC, ConversionStatus.AUTOMATIC_WITH_WARNINGS)
    assert result.complexity_score == 0


def test_implicit_cursor_for_loop_variable_gets_explicit_record_declaration():
    # Oracle's implicit-cursor FOR loop never declares `rec` — Oracle infers
    # it automatically. PostgreSQL is documented to do the same for an
    # undeclared target, but real-world testing against a live target hit
    # "loop variable of loop over rows must be a record variable or list of
    # scalar variables" with it left undeclared, so this converter emits an
    # explicit `rec RECORD;` declaration (matching Postgres's own doc
    # examples) for reliability.
    source = (
        "PROCEDURE list_customers IS\n"
        "BEGIN\n"
        "  FOR rec IN (SELECT customer_id, customer_name FROM customer) LOOP\n"
        "    DBMS_OUTPUT.PUT_LINE(rec.customer_name);\n"
        "  END LOOP;\n"
        "END list_customers;"
    )
    routine = Routine(name="LIST_CUSTOMERS", schema="HR", kind="PROCEDURE", source=source)
    ddl, issues = pc.convert_procedure_or_function(routine)
    assert "DECLARE" in ddl
    assert "rec RECORD;" in ddl
    assert "FOR rec IN (SELECT customer_id, customer_name FROM customer) LOOP" in ddl
    assert any("Explicitly declared rec as RECORD" in i.message for i in issues)


def test_implicit_cursor_for_loop_wrong_predeclaration_is_replaced_with_record():
    # Simulates a routine where `rec` also has an (incorrect/redundant)
    # explicit scalar declaration alongside the implicit-cursor FOR loop use
    # — the wrong declaration must be dropped and replaced with `rec
    # RECORD;`, not translated as NUMERIC, or Postgres raises "loop variable
    # of loop over rows must be a record variable...".
    source = (
        "PROCEDURE list_customers IS\n"
        "  rec NUMBER;\n"
        "BEGIN\n"
        "  FOR rec IN (SELECT customer_id FROM customer) LOOP\n"
        "    NULL;\n"
        "  END LOOP;\n"
        "END list_customers;"
    )
    routine = Routine(name="LIST_CUSTOMERS", schema="HR", kind="PROCEDURE", source=source)
    ddl, issues = pc.convert_procedure_or_function(routine)
    assert "rec NUMERIC;" not in ddl
    assert "rec RECORD;" in ddl
    assert any("Dropped declaration of 'rec'" in i.message for i in issues)
    assert any("Explicitly declared rec as RECORD" in i.message for i in issues)


def test_implicit_cursor_for_loop_with_bare_cursor_name():
    source = (
        "PROCEDURE list_emps IS\n"
        "  CURSOR emp_cur IS SELECT emp_id FROM employees;\n"
        "BEGIN\n"
        "  FOR rec IN emp_cur LOOP\n"
        "    NULL;\n"
        "  END LOOP;\n"
        "END list_emps;"
    )
    routine = Routine(name="LIST_EMPS", schema="HR", kind="PROCEDURE", source=source)
    ddl, issues = pc.convert_procedure_or_function(routine)
    assert "emp_cur CURSOR FOR SELECT emp_id FROM employees;" in ddl
    assert "FOR rec IN emp_cur LOOP" in ddl
    assert "rec RECORD;" in ddl


def test_row_loop_variable_with_existing_rowtype_declaration_is_not_duplicated():
    # If the loop target already has a valid row-compatible declaration
    # (e.g. a cursor %ROWTYPE), no second `RECORD` declaration should be
    # added alongside it.
    source = (
        "PROCEDURE list_emps IS\n"
        "  CURSOR emp_cur IS SELECT emp_id FROM employees;\n"
        "  rec emp_cur%ROWTYPE;\n"
        "BEGIN\n"
        "  FOR rec IN emp_cur LOOP\n"
        "    NULL;\n"
        "  END LOOP;\n"
        "END list_emps;"
    )
    routine = Routine(name="LIST_EMPS", schema="HR", kind="PROCEDURE", source=source)
    ddl, issues = pc.convert_procedure_or_function(routine)
    assert "rec RECORD;" not in ddl
    assert "rec emp_cur%ROWTYPE;" in ddl
    assert not any("Explicitly declared rec as RECORD" in i.message for i in issues)


def test_numeric_range_for_loop_variable_is_left_alone():
    # Numeric range loops (FOR i IN 1..10 LOOP) are safe to leave
    # pre-declared: Postgres just shadows the outer declaration inside the
    # loop rather than erroring, and the variable may be reused elsewhere.
    source = (
        "PROCEDURE count_up IS\n"
        "  i NUMBER;\n"
        "BEGIN\n"
        "  FOR i IN 1..10 LOOP\n"
        "    NULL;\n"
        "  END LOOP;\n"
        "  i := 99;\n"
        "END count_up;"
    )
    routine = Routine(name="COUNT_UP", schema="HR", kind="PROCEDURE", source=source)
    ddl, issues = pc.convert_procedure_or_function(routine)
    assert "i NUMERIC;" in ddl
    assert "FOR i IN 1..10 LOOP" in ddl


def test_convert_routine_mysql_target_now_really_converts():
    """This used to assert the opposite -- that a MySQL target produced a
    flagged draft. That draft was PL/SQL text MySQL could not run, so the
    contract changed in Round 19: a routine this simple converts cleanly,
    and the result is real MySQL. See tgdatabridge.core.plsql_mysql_converter."""
    source = "PROCEDURE ok_proc IS\nBEGIN\n  NULL;\nEND ok_proc;"
    routine = Routine(name="OK_PROC", schema="HR", kind="PROCEDURE", source=source)
    result = pc.convert_routine(routine, "MySQL")
    assert result.status == ConversionStatus.AUTOMATIC
    assert "CREATE PROCEDURE `OK_PROC`()" in result.converted_source
    assert ":=" not in result.converted_source
    assert "END;" in result.converted_source


# ---------------------------------------------- CONNECT BY -> recursive CTE

def test_connect_by_in_cursor_declaration_rewritten_for_postgres():
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
    routine = Routine(name="PRINT_ORG_CHART", schema="HR", kind="PROCEDURE", source=source)
    result = pc.convert_routine(routine, "PostgreSQL")
    assert result.status == ConversionStatus.AUTOMATIC
    assert "CURSOR FOR WITH RECURSIVE cb_cte_1" in result.converted_source
    assert "CONNECT BY" not in result.converted_source.upper()
    assert any(i.severity == "info" and "recursive CTE" in i.message for i in result.issues)


def test_connect_by_not_rewritten_for_mysql_legacy_flag_only_path():
    # MySQL routines intentionally stay on the flag-only legacy path (no real
    # syntax conversion at all -- see sql_translator.translate_routine), so a
    # CONNECT BY query there must be left completely untouched rather than
    # silently rewritten into SQL the rest of that path was never taught to
    # handle.
    source = (
        "PROCEDURE print_org_chart IS\n"
        "  CURSOR c1 IS\n"
        "    SELECT employee_id, manager_id FROM employees\n"
        "    START WITH manager_id IS NULL\n"
        "    CONNECT BY PRIOR employee_id = manager_id;\n"
        "BEGIN\n"
        "  NULL;\n"
        "END print_org_chart;"
    )
    routine = Routine(name="PRINT_ORG_CHART", schema="HR", kind="PROCEDURE", source=source)
    result = pc.convert_routine(routine, "MySQL")
    assert "WITH RECURSIVE" not in result.converted_source
    assert routine.source.upper().count("CONNECT BY") == 1  # source itself untouched too
    assert result.status == ConversionStatus.MANUAL


def test_connect_by_unsupported_shape_still_flagged_manual_for_postgres():
    # a WHERE clause before START WITH disqualifies it from the auto-rewrite
    source = (
        "PROCEDURE print_org_chart IS\n"
        "  CURSOR c1 IS\n"
        "    SELECT employee_id, manager_id FROM employees WHERE department_id = 10\n"
        "    START WITH manager_id IS NULL\n"
        "    CONNECT BY PRIOR employee_id = manager_id;\n"
        "BEGIN\n"
        "  NULL;\n"
        "END print_org_chart;"
    )
    routine = Routine(name="PRINT_ORG_CHART", schema="HR", kind="PROCEDURE", source=source)
    result = pc.convert_routine(routine, "PostgreSQL")
    assert "CONNECT BY" in result.converted_source.upper()
    assert any("must be rewritten as a recursive CTE" in i.message for i in result.issues)
    assert result.status == ConversionStatus.MANUAL


def test_a_mysql_source_now_converts_for_a_postgres_target():
    """This used to assert the opposite. The Oracle-specific converters
    still must not run against a MySQL body -- but as of Round 20 there is
    a MySQL front end, so the honest answer is a conversion, not a flag.
    See tgdatabridge.core.native_routine_converter."""
    source = "BEGIN\n  UPDATE t SET x = 1;\nEND"
    routine = Routine(name="BUMP_X", schema="APP", kind="PROCEDURE", source=source,
                      source_engine="MySQL")
    result = pc.convert_routine(routine, "PostgreSQL")
    assert result.status == ConversionStatus.AUTOMATIC
    assert "CREATE OR REPLACE PROCEDURE" in result.converted_source
    assert "LANGUAGE plpgsql" in result.converted_source


def test_a_source_engine_with_no_front_end_is_still_flagged_not_guessed():
    source = "BEGIN\n  UPDATE t SET x = 1;\nEND"
    routine = Routine(name="BUMP_X", schema="APP", kind="PROCEDURE", source=source,
                      source_engine="Db2")
    result = pc.convert_routine(routine, "PostgreSQL")
    assert result.status == ConversionStatus.MANUAL
    assert "MANUAL CONVERSION REQUIRED" in result.converted_source
    assert source in result.converted_source
    assert result.issues[0].severity == "error"
    assert "PostgreSQL" in result.issues[0].message


def test_a_postgres_source_converts_for_the_engines_with_a_front_end_only():
    source = "BEGIN\n  SELECT 1;\nEND"
    for target in ("PostgreSQL", "MySQL"):
        routine = Routine(name="P", schema="APP", kind="PROCEDURE", source=source,
                          source_engine="PostgreSQL")
        assert pc.convert_routine(routine, target).status != ConversionStatus.MANUAL
    for target in ("SQL Server", "Db2"):
        routine = Routine(name="P", schema="APP", kind="PROCEDURE", source=source,
                          source_engine="PostgreSQL")
        result = pc.convert_routine(routine, target)
        assert result.status == ConversionStatus.MANUAL
        assert target in result.issues[0].message


def test_convert_routine_mongodb_target_is_always_manual_even_for_oracle_source():
    # Unlike every other target, MongoDB gets no attempted translation at
    # all -- there's no server-side stored-procedure/trigger concept in
    # MongoDB for a translated body to even target -- so even a genuinely
    # Oracle-sourced routine (which every other target *would* run through
    # its real converter) is unconditionally flagged manual for a MongoDB
    # target.
    source = (
        "PROCEDURE raise_salary (p_id IN NUMBER) IS\n"
        "BEGIN\n"
        "  UPDATE employees SET salary = salary * 1.1 WHERE employee_id = p_id;\n"
        "END raise_salary;"
    )
    routine = Routine(name="RAISE_SALARY", schema="HR", kind="PROCEDURE", source=source, source_engine="Oracle")
    result = pc.convert_routine(routine, "MongoDB")
    assert result.status == ConversionStatus.MANUAL
    assert result.complexity_score == 5
    assert "MANUAL CONVERSION REQUIRED" in result.converted_source
    assert source in result.converted_source
    assert "no server-side stored-procedure/trigger equivalent" in result.converted_source
    assert len(result.issues) == 1
    assert result.issues[0].severity == "error"
    assert "no stored-procedure/trigger equivalent" in result.issues[0].message


def test_convert_routine_oracle_target_procedure_is_pure_passthrough_with_create_prefix():
    # ALL_SOURCE (what introspector.py reads PROCEDURE/FUNCTION/PACKAGE
    # source from) never includes the leading "CREATE [OR REPLACE]"
    # keyword -- an Oracle target needs it added back, nothing else.
    source = "PROCEDURE raise_salary (p_id IN NUMBER) IS\nBEGIN\n  NULL;\nEND raise_salary;"
    routine = Routine(name="RAISE_SALARY", schema="HR", kind="PROCEDURE", source=source, source_engine="Oracle")
    result = pc.convert_routine(routine, "Oracle")
    assert result.status == ConversionStatus.AUTOMATIC
    assert result.converted_source == f"CREATE OR REPLACE {source}"
    assert result.issues == []


def test_convert_routine_oracle_target_trigger_reconstructs_full_header():
    # ALL_TRIGGERS.TRIGGER_BODY omits the *entire* CREATE TRIGGER header
    # (name/timing/event/table/FOR EACH ROW), starting straight at
    # DECLARE/BEGIN -- this must be rebuilt from the Routine's own
    # table_name/timing/events/row_level metadata, not just prefixed.
    body = "BEGIN\n  :NEW.UPDATED_AT := SYSDATE;\nEND;"
    routine = Routine(
        name="TRG_TOUCH", schema="HR", kind="TRIGGER", source=body, source_engine="Oracle",
        table_name="EMPLOYEES", timing="BEFORE", events=["INSERT", "UPDATE"], row_level=True,
    )
    result = pc.convert_routine(routine, "Oracle")
    assert result.status == ConversionStatus.AUTOMATIC
    assert result.converted_source.startswith('CREATE OR REPLACE TRIGGER "TRG_TOUCH"')
    assert "BEFORE INSERT OR UPDATE" in result.converted_source
    assert 'ON "EMPLOYEES"' in result.converted_source
    assert "FOR EACH ROW" in result.converted_source
    assert body in result.converted_source
    assert result.issues == []


def test_convert_routine_oracle_target_statement_level_trigger_omits_for_each_row():
    body = "BEGIN\n  NULL;\nEND;"
    routine = Routine(
        name="TRG_STMT", schema="HR", kind="TRIGGER", source=body, source_engine="Oracle",
        table_name="EMPLOYEES", timing="AFTER", events=["DELETE"], row_level=False,
    )
    result = pc.convert_routine(routine, "Oracle")
    assert "FOR EACH ROW" not in result.converted_source
    assert "AFTER DELETE" in result.converted_source


def test_convert_routine_oracle_target_still_computes_complexity_score_for_reporting():
    source = "PROCEDURE p IS\nBEGIN\n  DBMS_OUTPUT.PUT_LINE('x');\n  CURSOR c IS SELECT 1 FROM dual;\nEND p;"
    routine = Routine(name="P", schema="APP", kind="PROCEDURE", source=source, source_engine="Oracle")
    result = pc.convert_routine(routine, "Oracle")
    assert result.status == ConversionStatus.AUTOMATIC  # still automatic -- score is for reporting only
    assert result.complexity_score > 0


def test_convert_routine_non_oracle_source_still_flagged_manual_for_oracle_target():
    # The non-Oracle-source guard applies to every target, Oracle included
    # -- this tool has no MySQL-SQL-to-Oracle-SQL translator, so a
    # MySQL-sourced routine must not be assumed safe to run on Oracle
    # unchanged just because Oracle is a permissive-looking target.
    source = "BEGIN\n  UPDATE t SET x = 1;\nEND"
    routine = Routine(name="BUMP_X", schema="APP", kind="PROCEDURE", source=source, source_engine="MySQL")
    result = pc.convert_routine(routine, "Oracle")
    assert result.status == ConversionStatus.MANUAL
    assert "Oracle" in result.issues[0].message


def test_convert_routine_non_oracle_source_guard_takes_precedence_over_mongodb_branch():
    # The non-Oracle-source guard at the top of convert_routine returns
    # early, before the target-engine dispatch even runs -- so a
    # non-Oracle-sourced routine targeting MongoDB gets the *generic*
    # "source engine is X, not Oracle" placeholder/message, not the
    # MongoDB-specific "no stored-procedure/trigger equivalent" one. Both
    # still land on MANUAL either way, but the message differs.
    source = "BEGIN\n  UPDATE t SET x = 1;\nEND"
    routine = Routine(name="BUMP_X", schema="APP", kind="PROCEDURE", source=source, source_engine="MySQL")
    result = pc.convert_routine(routine, "MongoDB")
    assert result.status == ConversionStatus.MANUAL
    assert "MANUAL CONVERSION REQUIRED" in result.converted_source
    assert "Source engine is MySQL, not Oracle" in result.converted_source
    assert "no server-side stored-procedure/trigger equivalent" not in result.converted_source
    assert "must be rewritten by hand for MongoDB" in result.issues[0].message


def test_convert_routine_oracle_source_is_unaffected_by_the_guard():
    source = (
        "PROCEDURE greet (p_name IN VARCHAR2) IS\n"
        "BEGIN\n"
        "  DBMS_OUTPUT.PUT_LINE(p_name);\n"
        "END greet;"
    )
    routine = Routine(name="GREET", schema="HR", kind="PROCEDURE", source=source)  # source_engine defaults to "Oracle"
    result = pc.convert_routine(routine, "PostgreSQL")
    assert result.status != ConversionStatus.MANUAL
    assert "MANUAL CONVERSION REQUIRED" not in result.converted_source


# --------------------------------------------------- nested subprogram / TYPE decl


def test_nested_subprogram_flagged_not_silently_corrupted():
    # Regression test for the bug fixed in nested_subprogram.py: a
    # PROCEDURE/FUNCTION declared inside another routine's own DECLARE
    # section used to get shredded by the generic variable-declaration
    # parser (producing garbage like a bogus "PROCEDURE <mapped-type>;"
    # line) instead of being cut out whole and flagged for manual review.
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
    ddl, issues = pc.convert_procedure_or_function(routine)
    assert "MANUAL CONVERSION REQUIRED: nested PROCEDURE inner_helper" in ddl
    assert "END inner_helper;" in ddl  # original source embedded verbatim, not shredded
    assert any(
        i.severity == "error" and "inner_helper" in i.message for i in issues
    )
    # The outer routine's own declarations/body must still convert cleanly --
    # this is exactly what silently broke before the find_top_level_begin fix
    # (the declare/body split landed on the nested subprogram's own BEGIN).
    assert "v_count NUMERIC := 0;" in ddl
    assert "v_total NUMERIC;" in ddl
    assert "PROCEDURE <mapped-type>" not in ddl  # sanity: no corrupted line

    result = pc.convert_routine(routine, "PostgreSQL")
    assert result.status == ConversionStatus.MANUAL


def test_local_type_declaration_with_no_variable_using_it_is_flagged_inline():
    """A locally-declared type that no variable ever uses is dead weight,
    not a ticking time bomb -- the rest of the routine still converts and
    still reaches "Apply DDL to Target", with just that one DECLARE line
    commented out and flagged."""
    source = (
        "PROCEDURE sum_ids IS\n"
        "  TYPE t_id_list IS TABLE OF NUMBER INDEX BY BINARY_INTEGER;\n"
        "  v_count NUMBER := 0;\n"
        "BEGIN\n"
        "  v_count := v_count + 1;\n"
        "END sum_ids;"
    )
    routine = Routine(name="SUM_IDS", schema="HR", kind="PROCEDURE", source=source)
    ddl, issues = pc.convert_procedure_or_function(routine)
    assert "MANUAL CONVERSION REQUIRED: TYPE t_id_list IS TABLE OF" in ddl
    assert 'CREATE OR REPLACE PROCEDURE "sum_ids"' in ddl
    assert any(
        i.severity == "error" and "t_id_list" in i.message for i in issues
    )
    from tgdatabridge.utils.sql_split import has_executable_sql
    assert has_executable_sql(ddl)
    result = pc.convert_routine(routine, "PostgreSQL")
    assert result.status == ConversionStatus.MANUAL


def test_a_variable_declared_from_a_local_unsupported_type_bails_the_whole_routine():
    """The real bug, reported from the field: a locally-declared Oracle
    associative array (TYPE ... IS TABLE OF ... INDEX BY ...) was flagged
    correctly on its own DECLARE line, but a variable declared from that
    local type (`v_values t_num_tab;`) fell through to the generic
    scalar-type mapper and silently became `v_values TEXT;` -- while the
    body's Oracle-only element-assignment syntax (`v_values(1) := 100;`)
    was left completely untouched. That combination reached "Apply DDL to
    Target" as syntactically-plausible PL/pgSQL and failed there with
    "syntax error at or near v_values", 135 statements into a real
    migration, instead of ever being flagged. The fix: bail the whole
    routine out to a manual placeholder the moment any variable is
    declared from a locally-unsupported type, exactly like every other
    "no safe mechanical translation" case in this module."""
    source = (
        "PROCEDURE feature_pl_bulk_pkg_collection_demo IS\n"
        "  TYPE t_num_tab IS TABLE OF NUMBER INDEX BY PLS_INTEGER;\n"
        "  v_values t_num_tab;\n"
        "  v_sum NUMBER := 0;\n"
        "BEGIN\n"
        "  v_values(1) := 100;\n"
        "  v_values(2) := 200;\n"
        "  v_values(3) := 300;\n"
        "  v_sum := v_values(1) + v_values(2) + v_values(3);\n"
        "  DBMS_OUTPUT.PUT_LINE('Associative array sum=' || v_sum);\n"
        "END feature_pl_bulk_pkg_collection_demo;"
    )
    routine = Routine(
        name="feature_pl_bulk_pkg_collection_demo", schema="HR",
        kind="PROCEDURE", source=source,
    )
    ddl, issues = pc.convert_procedure_or_function(routine)

    assert ddl.startswith(
        "-- MANUAL CONVERSION REQUIRED for PROCEDURE feature_pl_bulk_pkg_collection_demo")
    assert "v_values(1) := 100;" in ddl  # original source kept verbatim inside the comment, for reference
    assert "CREATE OR REPLACE PROCEDURE" not in ddl  # no live, broken DDL emitted
    assert "v_values TEXT" not in ddl  # the old silent mismap must not reappear
    assert any(
        i.severity == "error" and "v_values" in i.message for i in issues
    )

    from tgdatabridge.utils.sql_split import has_executable_sql
    assert not has_executable_sql(ddl), (
        "a bailed-out routine must be comment-only, or Apply DDL to Target sends it "
        "to the server and fails exactly as reported"
    )

    result = pc.convert_routine(routine, "PostgreSQL")
    assert result.status == ConversionStatus.MANUAL


def test_collection_element_assignment_is_flagged_even_without_a_local_type_declaration():
    """Real bug, reported from the field (FEATURE_PL_ADMIN_PKG's
    v_values(1):=100;): unlike test_a_variable_declared_from_a_local_
    unsupported_type_bails_the_whole_routine above, this routine's
    collection variable is NOT declared from a local `TYPE ... IS TABLE
    OF ...` in its own DECLARE section -- it's an Oracle-built-in
    collection type used directly (this same gap also applies to a type
    declared only in the PACKAGE SPECIFICATION, which this converter never
    parses since it only ever receives the PACKAGE BODY). Because
    convert_declare_block's unsupported_type_vars check can only see a
    *locally*-declared unsupported type, it had nothing to catch here, so
    'v_values' silently fell through to a generic scalar mapping with zero
    issues raised -- and the untouched 'v_values(1):=100;' reached "Apply
    DDL to Target" as live, broken SQL. The fix is a second, declaration-
    independent detection: the assignment-target syntax `name(args) :=`
    is invalid PL/pgSQL no matter how (or whether) the type was resolved."""
    source = (
        "PROCEDURE feature_pl_admin_pkg_validate_status IS\n"
        "  v_values DBMS_SQL.NUMBER_TABLE;\n"
        "BEGIN\n"
        "  v_values(1):=100;\n"
        "  v_values(2):=200;\n"
        "  v_values(3):=300;\n"
        "END feature_pl_admin_pkg_validate_status;"
    )
    routine = Routine(
        name="feature_pl_admin_pkg_validate_status", schema="HR",
        kind="PROCEDURE", source=source,
    )
    ddl, issues = pc.convert_procedure_or_function(routine)
    assert "v_values(1):=100;" in ddl  # not silently rewritten to something plausible-but-broken
    assert any(
        i.severity == "error" and "element-assignment" in i.message
        for i in issues
    )
    result = pc.convert_routine(routine, "PostgreSQL")
    assert result.status == ConversionStatus.MANUAL


def test_a_record_typed_variable_from_a_local_type_also_bails_the_whole_routine():
    """The same latent flaw applies to a local RECORD type, not just an
    associative array -- both share the same `^TYPE\\s+` detection and the
    same missing propagation to variable declarations."""
    source = (
        "PROCEDURE rec_proc IS\n"
        "  TYPE t_rec IS RECORD (a NUMBER, b VARCHAR2(10));\n"
        "  v_rec t_rec;\n"
        "BEGIN\n"
        "  v_rec.a := 1;\n"
        "END rec_proc;"
    )
    routine = Routine(name="REC_PROC", schema="HR", kind="PROCEDURE", source=source)
    ddl, issues = pc.convert_procedure_or_function(routine)
    from tgdatabridge.utils.sql_split import has_executable_sql
    assert not has_executable_sql(ddl)
    assert any(i.severity == "error" and "v_rec" in i.message for i in issues)


def test_a_variable_declared_from_a_local_unsupported_type_bails_a_trigger_too():
    source = (
        "DECLARE\n"
        "  TYPE t_tab IS TABLE OF NUMBER INDEX BY PLS_INTEGER;\n"
        "  v_tab t_tab;\n"
        "BEGIN\n"
        "  v_tab(1) := :NEW.id;\n"
        "END;"
    )
    routine = Routine(
        name="TRG_TEST", schema="HR", kind="TRIGGER", source=source,
        table_name="EMPLOYEES", timing="BEFORE", events=["INSERT"], row_level=True,
    )
    ddl, issues = pc.convert_trigger(routine)
    from tgdatabridge.utils.sql_split import has_executable_sql
    assert not has_executable_sql(ddl)
    assert ddl.startswith("-- MANUAL CONVERSION REQUIRED for TRIGGER TRG_TEST")
    assert any(i.severity == "error" and "v_tab" in i.message for i in issues)


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
                    table_name="ACCOUNT", timing="BEFORE", events=["INSERT"], row_level=True)
    ddl, issues = pc.convert_trigger(trig)
    assert "MANUAL CONVERSION REQUIRED" in ddl
    assert any(i.severity == "error" and "bump" in i.message for i in issues)


# --------------------------------------------------------------- BULK COLLECT


def test_bulk_collect_select_rewritten_to_array_agg():
    source = (
        "PROCEDURE get_ids IS\n"
        "  v_ids TEST_ARR;\n"
        "  v_names TEST_ARR;\n"
        "BEGIN\n"
        "  SELECT emp_id, emp_name BULK COLLECT INTO v_ids, v_names FROM employees WHERE dept_id = 10;\n"
        "END get_ids;"
    )
    routine = Routine(name="GET_IDS", schema="HR", kind="PROCEDURE", source=source)
    ddl, issues = pc.convert_procedure_or_function(routine)
    assert (
        "SELECT array_agg(emp_id), array_agg(emp_name) INTO v_ids, v_names "
        "FROM employees WHERE dept_id = 10;"
    ) in ddl
    assert "BULK COLLECT" not in ddl
    assert any(
        i.severity == "info" and "array_agg()" in i.message and "v_ids" in i.message for i in issues
    )
    assert not any(i.severity == "error" and "BULK COLLECT" in i.message for i in issues)

    result = pc.convert_routine(routine, "PostgreSQL")
    assert result.status != ConversionStatus.MANUAL


def test_bulk_collect_select_single_column_still_rewritten():
    source = (
        "PROCEDURE get_ids IS\n"
        "  v_ids TEST_ARR;\n"
        "BEGIN\n"
        "  SELECT emp_id BULK COLLECT INTO v_ids FROM employees;\n"
        "END get_ids;"
    )
    routine = Routine(name="GET_IDS", schema="HR", kind="PROCEDURE", source=source)
    ddl, issues = pc.convert_procedure_or_function(routine)
    assert "SELECT array_agg(emp_id) INTO v_ids FROM employees;" in ddl


def test_bulk_collect_column_variable_count_mismatch_flagged_not_guessed():
    source = (
        "PROCEDURE get_ids IS\n"
        "  v_ids TEST_ARR;\n"
        "BEGIN\n"
        "  SELECT emp_id, emp_name BULK COLLECT INTO v_ids FROM employees;\n"
        "END get_ids;"
    )
    routine = Routine(name="GET_IDS", schema="HR", kind="PROCEDURE", source=source)
    ddl, issues = pc.convert_procedure_or_function(routine)
    # Left unrewritten -- still literally present -- rather than guessed at.
    assert "BULK COLLECT INTO v_ids FROM employees" in ddl
    assert "array_agg" not in ddl
    matches = [i for i in issues if i.severity == "error" and "BULK COLLECT" in i.message]
    assert len(matches) == 1  # exactly one diagnostic, not a duplicate generic + specific pair
    assert "2 selected column(s)" in matches[0].message and "1 target variable(s)" in matches[0].message

    result = pc.convert_routine(routine, "PostgreSQL")
    assert result.status == ConversionStatus.MANUAL


def test_bulk_collect_select_star_flagged_not_guessed():
    source = (
        "PROCEDURE get_recs IS\n"
        "  v_recs TEST_ARR;\n"
        "BEGIN\n"
        "  SELECT * BULK COLLECT INTO v_recs FROM employees;\n"
        "END get_recs;"
    )
    routine = Routine(name="GET_RECS", schema="HR", kind="PROCEDURE", source=source)
    ddl, issues = pc.convert_procedure_or_function(routine)
    assert "array_agg" not in ddl
    matches = [i for i in issues if i.severity == "error" and "BULK COLLECT" in i.message]
    assert len(matches) == 1
    assert "SELECT *" in matches[0].message


def test_fetch_bulk_collect_flagged_with_cursor_specific_message():
    source = (
        "PROCEDURE p1 IS\n"
        "  CURSOR c1 IS SELECT emp_id FROM employees;\n"
        "  v_ids TEST_ARR;\n"
        "BEGIN\n"
        "  OPEN c1;\n"
        "  FETCH c1 BULK COLLECT INTO v_ids LIMIT 100;\n"
        "  CLOSE c1;\n"
        "END p1;"
    )
    routine = Routine(name="P1", schema="HR", kind="PROCEDURE", source=source)
    ddl, issues = pc.convert_procedure_or_function(routine)
    matches = [i for i in issues if i.severity == "error" and "FETCH" in i.message and "BULK COLLECT" in i.message]
    assert len(matches) == 1
    assert "array_append" in matches[0].message


def test_forall_insert_flagged_with_dml_specific_message():
    source = (
        "PROCEDURE bulk_insert IS\n"
        "BEGIN\n"
        "  FORALL i IN v_ids.FIRST .. v_ids.LAST\n"
        "    INSERT INTO employees (emp_id) VALUES (v_ids(i));\n"
        "END bulk_insert;"
    )
    routine = Routine(name="BULK_INSERT", schema="HR", kind="PROCEDURE", source=source)
    ddl, issues = pc.convert_procedure_or_function(routine)
    assert any(
        i.severity == "error" and "FORALL ... INSERT" in i.message for i in issues
    )
    result = pc.convert_routine(routine, "PostgreSQL")
    assert result.status == ConversionStatus.MANUAL


def test_forall_update_flagged_with_dml_specific_message():
    source = (
        "PROCEDURE bulk_update IS\n"
        "BEGIN\n"
        "  FORALL i IN v_ids.FIRST .. v_ids.LAST\n"
        "    UPDATE employees SET salary = salary * 1.1 WHERE emp_id = v_ids(i);\n"
        "END bulk_update;"
    )
    routine = Routine(name="BULK_UPDATE", schema="HR", kind="PROCEDURE", source=source)
    ddl, issues = pc.convert_procedure_or_function(routine)
    assert any(
        i.severity == "error" and "FORALL ... UPDATE" in i.message for i in issues
    )


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
    ddl, issues = pc.convert_procedure_or_function(routine)
    assert "v_len := LENGTH(v_clob);" in ddl
    # DBMS_LOB.SUBSTR(lob, amount, offset) -> SUBSTR(lob, offset, amount): args 2 and 3 swap.
    assert "v_part := SUBSTR(v_clob, 5, 10);" in ddl
    assert "v_r := random();" in ddl
    assert "DBMS_LOB" not in ddl and "DBMS_RANDOM" not in ddl
    assert not any(i.severity == "error" and "DBMS_" in i.message for i in issues)

    result = pc.convert_routine(routine, "PostgreSQL")
    assert result.status != ConversionStatus.MANUAL


def test_dbms_lob_substr_argument_count_variants():
    source = (
        "PROCEDURE p1 IS\n"
        "  v_a VARCHAR2(100);\n"
        "  v_b VARCHAR2(100);\n"
        "BEGIN\n"
        "  v_a := DBMS_LOB.SUBSTR(v_clob);\n"
        "  v_b := DBMS_LOB.SUBSTR(v_clob, 20);\n"
        "END p1;"
    )
    routine = Routine(name="P1", schema="HR", kind="PROCEDURE", source=source)
    ddl, issues = pc.convert_procedure_or_function(routine)
    assert "v_a := SUBSTR(v_clob, 1, 32767);" in ddl  # both defaults applied
    assert "v_b := SUBSTR(v_clob, 1, 20);" in ddl  # offset defaults to 1


def test_dbms_random_value_ranged_form_not_mapped_still_flagged():
    # Only the no-arg form is in scope for the random() mapping; the ranged
    # 2-arg form has no direct Postgres equivalent and must still be flagged.
    source = (
        "PROCEDURE p1 IS\n"
        "  v_r NUMBER;\n"
        "BEGIN\n"
        "  v_r := DBMS_RANDOM.VALUE(1, 100);\n"
        "END p1;"
    )
    routine = Routine(name="P1", schema="HR", kind="PROCEDURE", source=source)
    ddl, issues = pc.convert_procedure_or_function(routine)
    assert "DBMS_RANDOM.VALUE(1, 100)" in ddl
    assert any(i.severity == "error" and "DBMS_" in i.message for i in issues)


def test_bare_procedure_call_statement_gets_call_keyword():
    """Real bug, reported from the field: Oracle allows an unqualified
    procedure call as a standalone statement (`feature_pl_create_row(p_id,
    p_name);`) with no leading keyword -- valid there because PL/SQL has no
    way to call a *function* and discard its result other than assigning
    it, so a bare statement can only ever be a procedure call. PL/pgSQL has
    no bare-call statement form at all; every statement must start with a
    keyword its parser recognizes, so this reached "Apply DDL to Target"
    verbatim and failed with `syntax error at or near
    "feature_pl_create_row"`. The fix: CALL is the direct equivalent for
    exactly this shape."""
    source = (
        "PROCEDURE process_row(p_id IN NUMBER, p_name IN VARCHAR2) IS\n"
        "BEGIN\n"
        "  feature_pl_create_row(p_id, p_name);\n"
        "  feature_pl_pkg.log_event('created ' || p_name);\n"
        "END process_row;"
    )
    routine = Routine(name="PROCESS_ROW", schema="HR", kind="PROCEDURE", source=source)
    ddl, issues = pc.convert_procedure_or_function(routine)
    assert "CALL feature_pl_create_row(p_id, p_name);" in ddl
    assert "CALL feature_pl_pkg.log_event('created ' || p_name);" in ddl
    result = pc.convert_routine(routine, "PostgreSQL")
    assert result.status != ConversionStatus.MANUAL


def test_bare_procedure_call_with_nested_function_call_arguments():
    """The balanced-paren scan must not stop at the first ')' when an
    argument is itself a function call."""
    source = (
        "PROCEDURE process_row(p_id IN NUMBER) IS\n"
        "BEGIN\n"
        "  feature_pl_create_row(NVL(p_id, 0), UPPER('x'));\n"
        "END process_row;"
    )
    routine = Routine(name="PROCESS_ROW", schema="HR", kind="PROCEDURE", source=source)
    ddl, issues = pc.convert_procedure_or_function(routine)
    assert "CALL feature_pl_create_row(COALESCE(p_id, 0), UPPER('x'));" in ddl


def test_call_not_added_when_the_call_is_part_of_a_larger_statement():
    """A function call used inside an assignment or an IF condition is not
    a standalone statement and must not be prefixed with CALL -- only a
    call that *is* the entire statement qualifies."""
    source = (
        "PROCEDURE process_row(p_id IN NUMBER) IS\n"
        "  v_ok NUMBER;\n"
        "BEGIN\n"
        "  v_ok := feature_pl_validate(p_id);\n"
        "  IF feature_pl_validate(p_id) = 1 THEN\n"
        "    NULL;\n"
        "  END IF;\n"
        "END process_row;"
    )
    routine = Routine(name="PROCESS_ROW", schema="HR", kind="PROCEDURE", source=source)
    ddl, issues = pc.convert_procedure_or_function(routine)
    assert "CALL" not in ddl
    assert "v_ok := feature_pl_validate(p_id);" in ddl
    assert "IF feature_pl_validate(p_id) = 1 THEN" in ddl


def test_call_not_added_to_a_parameterized_cursor_open():
    """`OPEN cur(param);` -- opening an already-declared parameterized
    cursor -- must not be mistaken for a bare procedure call; it is valid,
    unrelated PL/pgSQL syntax on its own."""
    source = (
        "PROCEDURE list_by_category IS\n"
        "  CURSOR cur_by_category(cp_category VARCHAR2) IS\n"
        "    SELECT product_id FROM products WHERE category = cp_category;\n"
        "BEGIN\n"
        "  OPEN cur_by_category('BOOKS');\n"
        "  CLOSE cur_by_category;\n"
        "END list_by_category;"
    )
    routine = Routine(name="LIST_BY_CATEGORY", schema="HR", kind="PROCEDURE", source=source)
    ddl, issues = pc.convert_procedure_or_function(routine)
    assert "CALL" not in ddl
    assert "OPEN cur_by_category('BOOKS');" in ddl


def test_pipe_row_is_flagged_manual_not_mechanically_rewritten():
    """Oracle's pipelined-table-function idiom (`PIPE ROW(...)` inside a
    `FUNCTION ... PIPELINED`) has no PL/pgSQL equivalent statement -- a
    Postgres set-returning function needs a completely different shape
    (RETURNS SETOF/TABLE with RETURN NEXT/RETURN QUERY), which cannot be
    safely retrofitted onto an arbitrary existing function body. Left
    unconverted, this reached "Apply DDL to Target" and failed with
    `syntax error at or near "PIPE"`, with no issue ever raised to explain
    why -- this must now be flagged MANUAL instead."""
    source = (
        "FUNCTION get_rows RETURN feature_pl_record_tab PIPELINED IS\n"
        "BEGIN\n"
        "  PIPE ROW(feature_pl_record_type(1, 'a'));\n"
        "  RETURN;\n"
        "END get_rows;"
    )
    routine = Routine(name="GET_ROWS", schema="HR", kind="FUNCTION", source=source)
    ddl, issues = pc.convert_procedure_or_function(routine)
    assert any(
        i.severity == "error" and "PIPE ROW" in i.message for i in issues
    )
    result = pc.convert_routine(routine, "PostgreSQL")
    assert result.status == ConversionStatus.MANUAL
