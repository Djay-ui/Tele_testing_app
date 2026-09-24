"""Tests for four PostgreSQL-target conversion gaps identified in a
customer-provided gap analysis (Postgresql_changes.txt) comparing this
tool against ora2pg: ROWNUM's top-N idiom, LISTAGG, the Oracle REGEXP_*
function family, and Oracle's date-arithmetic functions (ADD_MONTHS,
LAST_DAY, TRUNC(date, 'fmt')). All four are shared between a view's own
SELECT text (ddl_generator.generate_view_ddl) and a routine's embedded
SELECTs (plsql_converter.convert_body) since both reach the PostgreSQL
target through the same SQL text -- tested at both call sites below.

Every rewrite here follows this tool's established "don't guess" rule:
a shape it can convert with full confidence is rewritten; anything murkier
(extra arguments, an unrecognized format model, a missing clause) is left
untouched and flagged for manual review instead.
"""
from tgdatabridge.core import ddl_generator
from tgdatabridge.core import plsql_converter as pc
from tgdatabridge.core.schema_model import Routine, View


def _oracle_view(definition: str) -> View:
    return View(name="V1", schema="HR", definition=definition)


def _routine(source: str) -> Routine:
    return Routine(name="P1", schema="HR", kind="PROCEDURE", source=source)


# --------------------------------------------------------------- ROWNUM top-N


def test_rownum_topn_only_condition_becomes_limit():
    issues = []
    out = pc.rewrite_rownum_topn("SELECT * FROM t WHERE ROWNUM <= 10", issues)
    assert "LIMIT 10" in out
    assert "ROWNUM" not in out
    assert any(i.severity == "info" for i in issues)


def test_rownum_topn_after_other_conditions_becomes_limit():
    issues = []
    out = pc.rewrite_rownum_topn("SELECT * FROM t WHERE x = 1 AND ROWNUM <= 10", issues)
    assert "WHERE x = 1" in out
    assert "LIMIT 10" in out
    assert "ROWNUM" not in out


def test_rownum_topn_strict_less_than_subtracts_one():
    issues = []
    out = pc.rewrite_rownum_topn("SELECT * FROM t WHERE ROWNUM < 10", issues)
    assert "LIMIT (10 - 1)" in out


def test_rownum_equals_one_becomes_limit_one():
    issues = []
    out = pc.rewrite_rownum_topn("SELECT * FROM t WHERE ROWNUM = 1", issues)
    assert "LIMIT 1" in out


def test_rownum_topn_on_an_ordered_outer_query_the_classic_pagination_idiom():
    issues = []
    out = pc.rewrite_rownum_topn(
        "SELECT * FROM (SELECT * FROM t ORDER BY y) WHERE ROWNUM <= 10", issues,
    )
    assert "ORDER BY y)" in out
    assert "LIMIT 10" in out


def test_rownum_as_the_first_of_several_conditions_is_left_untouched():
    # Genuinely different shape (ROWNUM followed by more AND-ed
    # conditions) -- not attempted, left for the oracle_markers/manual
    # marker scan to flag instead of guessed at.
    issues = []
    out = pc.rewrite_rownum_topn("SELECT * FROM t WHERE ROWNUM <= 10 AND x = 1", issues)
    assert out == "SELECT * FROM t WHERE ROWNUM <= 10 AND x = 1"
    assert issues == []


def test_view_ddl_postgres_target_rewrites_rownum_topn_to_limit():
    v = _oracle_view("SELECT id FROM orders WHERE ROWNUM <= 10")
    ddl, issues = ddl_generator.generate_view_ddl(v, "PostgreSQL")
    assert "LIMIT 10" in ddl
    assert "ROWNUM" not in ddl.upper()


def test_view_ddl_non_postgres_target_leaves_rownum_untouched():
    # The new rewrites are scoped to a PostgreSQL target only -- MySQL/SQL
    # Server/Db2 keep today's flagged-but-unrewritten behavior, since
    # LIMIT/OFFSET syntax (and the other three gaps) differ per target
    # and this round's fix doc is PostgreSQL-specific.
    v = _oracle_view("SELECT id FROM orders WHERE ROWNUM <= 10")
    ddl, issues = ddl_generator.generate_view_ddl(v, "MySQL")
    assert "ROWNUM" in ddl.upper()
    assert any("ROWNUM" in i.message for i in issues)


def test_convert_body_rewrites_rownum_topn_in_an_embedded_cursor_select():
    src = (
        "PROCEDURE p1 IS\n"
        "  CURSOR c1 IS SELECT id FROM orders WHERE ROWNUM <= 5;\n"
        "BEGIN\n"
        "  NULL;\n"
        "END p1;"
    )
    ddl, issues = pc.convert_procedure_or_function(_routine(src))
    assert "LIMIT 5" in ddl
    assert "ROWNUM" not in ddl.upper()


# --------------------------------------------------------------- LISTAGG


def test_listagg_with_delimiter_becomes_string_agg():
    issues = []
    out = pc.rewrite_listagg(
        "SELECT LISTAGG(name, ', ') WITHIN GROUP (ORDER BY name) FROM employees", issues,
    )
    assert "STRING_AGG(name, ', ' ORDER BY name)" in out
    assert issues == []


def test_listagg_without_delimiter_defaults_to_empty_string():
    issues = []
    out = pc.rewrite_listagg(
        "SELECT LISTAGG(name) WITHIN GROUP (ORDER BY name DESC) FROM employees", issues,
    )
    assert "STRING_AGG(name, '' ORDER BY name DESC)" in out


def test_listagg_without_within_group_is_flagged_not_guessed():
    issues = []
    out = pc.rewrite_listagg("SELECT LISTAGG(name, ',') FROM employees", issues)
    assert "LISTAGG(name, ',')" in out
    assert any(i.severity == "error" and "LISTAGG" in i.message for i in issues)


def test_view_ddl_postgres_target_rewrites_listagg():
    v = _oracle_view(
        "SELECT dept_id, LISTAGG(name, ', ') WITHIN GROUP (ORDER BY name) names "
        "FROM employees GROUP BY dept_id",
    )
    ddl, issues = ddl_generator.generate_view_ddl(v, "PostgreSQL")
    assert "STRING_AGG(name, ', ' ORDER BY name)" in ddl


# --------------------------------------------------------------- REGEXP_*


def test_regexp_like_two_arg_becomes_tilde_operator():
    issues = []
    out = pc.rewrite_regexp_functions("SELECT * FROM t WHERE REGEXP_LIKE(name, '^A')", issues)
    assert "(name) ~ ('^A')" in out
    assert "REGEXP_LIKE" not in out


def test_regexp_like_case_insensitive_flag_becomes_tilde_star_operator():
    issues = []
    out = pc.rewrite_regexp_functions("SELECT * FROM t WHERE REGEXP_LIKE(name, '^a', 'i')", issues)
    assert "(name) ~* ('^a')" in out


def test_regexp_like_other_match_param_is_flagged_not_guessed():
    issues = []
    out = pc.rewrite_regexp_functions("SELECT * FROM t WHERE REGEXP_LIKE(name, '^a', 'm')", issues)
    assert "REGEXP_LIKE(name, '^a', 'm')" in out
    assert any(i.severity == "error" and "REGEXP_LIKE" in i.message for i in issues)


def test_regexp_replace_three_arg_form_needs_no_change():
    issues = []
    out = pc.rewrite_regexp_functions("SELECT REGEXP_REPLACE(name, '[0-9]+', 'X') FROM t", issues)
    assert "REGEXP_REPLACE(name, '[0-9]+', 'X')" in out
    assert issues == []


def test_regexp_replace_with_occurrence_argument_is_flagged_not_guessed():
    issues = []
    out = pc.rewrite_regexp_functions("SELECT REGEXP_REPLACE(name, '[0-9]+', 'X', 1, 0) FROM t", issues)
    assert "REGEXP_REPLACE(name, '[0-9]+', 'X', 1, 0)" in out
    assert any(i.severity == "error" and "REGEXP_REPLACE" in i.message for i in issues)


def test_regexp_substr_two_arg_becomes_substring_from():
    issues = []
    out = pc.rewrite_regexp_functions("SELECT REGEXP_SUBSTR(name, '[0-9]+') FROM t", issues)
    assert "substring(name from '[0-9]+')" in out


def test_regexp_substr_with_position_argument_is_flagged_not_guessed():
    issues = []
    out = pc.rewrite_regexp_functions("SELECT REGEXP_SUBSTR(name, '[0-9]+', 1) FROM t", issues)
    assert "REGEXP_SUBSTR(name, '[0-9]+', 1)" in out
    assert any(i.severity == "error" and "REGEXP_SUBSTR" in i.message for i in issues)


def test_regexp_count_and_regexp_instr_are_always_flagged():
    issues = []
    out = pc.rewrite_regexp_functions(
        "SELECT REGEXP_COUNT(name, '[0-9]'), REGEXP_INSTR(name, '[0-9]') FROM t", issues,
    )
    assert "REGEXP_COUNT(name, '[0-9]')" in out
    assert "REGEXP_INSTR(name, '[0-9]')" in out
    messages = [i.message for i in issues]
    assert any("REGEXP_COUNT" in m for m in messages)
    assert any("REGEXP_INSTR" in m for m in messages)


def test_view_ddl_postgres_target_rewrites_regexp_like():
    v = _oracle_view("SELECT id FROM t WHERE REGEXP_LIKE(email, '^[a-z]+@')")
    ddl, issues = ddl_generator.generate_view_ddl(v, "PostgreSQL")
    assert "~ ('^[a-z]+@')" in ddl


# --------------------------------------------------------------- date arithmetic


def test_add_months_becomes_interval_arithmetic_with_a_caveat_warning():
    issues = []
    out = pc.rewrite_oracle_date_functions("SELECT ADD_MONTHS(hire_date, 3) FROM t", issues)
    assert "((hire_date) + ((3) * INTERVAL '1 month'))" in out
    assert any(i.severity == "warning" and "month-end" in i.message for i in issues)


def test_last_day_becomes_date_trunc_expression():
    issues = []
    out = pc.rewrite_oracle_date_functions("SELECT LAST_DAY(hire_date) FROM t", issues)
    assert "date_trunc('month', (hire_date))" in out
    assert "INTERVAL '1 month' - INTERVAL '1 day'" in out


def test_trunc_date_with_recognized_format_becomes_date_trunc():
    issues = []
    out = pc.rewrite_oracle_date_functions("SELECT TRUNC(hire_date, 'MM') FROM t", issues)
    assert "date_trunc('month', (hire_date))" in out
    assert issues == []


def test_trunc_date_with_unrecognized_format_is_flagged_not_guessed():
    issues = []
    out = pc.rewrite_oracle_date_functions("SELECT TRUNC(hire_date, 'DAY') FROM t", issues)
    assert "TRUNC(hire_date, 'DAY')" in out
    assert any(i.severity == "error" and "TRUNC" in i.message for i in issues)


def test_trunc_numeric_one_arg_is_left_completely_alone():
    issues = []
    out = pc.rewrite_oracle_date_functions("SELECT TRUNC(salary) FROM t", issues)
    assert out == "SELECT TRUNC(salary) FROM t"
    assert issues == []


def test_trunc_numeric_two_arg_with_integer_decimals_is_left_completely_alone():
    # The distinguishing signal is whether the 2nd argument is a quoted
    # string literal (a format model) -- an integer decimals argument
    # must never be mistaken for one.
    issues = []
    out = pc.rewrite_oracle_date_functions("SELECT TRUNC(salary, 2) FROM t", issues)
    assert out == "SELECT TRUNC(salary, 2) FROM t"
    assert issues == []


def test_view_ddl_postgres_target_rewrites_add_months_and_last_day():
    v = _oracle_view("SELECT ADD_MONTHS(hire_date, 1) a, LAST_DAY(hire_date) b FROM employees")
    ddl, issues = ddl_generator.generate_view_ddl(v, "PostgreSQL")
    assert "INTERVAL '1 month'" in ddl
    assert "date_trunc('month'" in ddl


def test_convert_body_rewrites_date_functions_in_a_routine():
    src = (
        "PROCEDURE p1 IS\n"
        "  v_d DATE;\n"
        "BEGIN\n"
        "  v_d := ADD_MONTHS(SYSDATE, 1);\n"
        "END p1;"
    )
    ddl, issues = pc.convert_procedure_or_function(_routine(src))
    assert "INTERVAL '1 month'" in ddl
