"""Tests for tgdatabridge.core.connect_by_rewriter -- the best-effort CONNECT BY
-> recursive CTE rewrite shared by ddl_generator (views) and each of
plsql_converter.py/tsql_converter.py/db2_converter.py (routine bodies)."""
from tgdatabridge.core.connect_by_rewriter import find_and_rewrite


def test_basic_query_rewritten_to_recursive_cte():
    query = (
        "SELECT employee_id, manager_id, last_name\n"
        "FROM employees\n"
        "START WITH manager_id IS NULL\n"
        "CONNECT BY PRIOR employee_id = manager_id"
    )
    new_text, issues = find_and_rewrite(query, cte_keyword="WITH RECURSIVE")
    assert "WITH RECURSIVE cb_cte_1 (employee_id, manager_id, last_name) AS (" in new_text
    assert "FROM employees\n  WHERE manager_id IS NULL" in new_text
    assert "UNION ALL" in new_text
    assert "JOIN cb_cte_1 ON _cb_t.manager_id = cb_cte_1.employee_id" in new_text
    assert new_text.strip().endswith("SELECT employee_id, manager_id, last_name\nFROM cb_cte_1")
    assert len(issues) == 1
    assert issues[0].severity == "info"
    assert "recursive CTE" in issues[0].message


def test_prior_on_right_hand_side_produces_same_join():
    query = (
        "SELECT employee_id, manager_id\n"
        "FROM employees\n"
        "START WITH manager_id IS NULL\n"
        "CONNECT BY manager_id = PRIOR employee_id"
    )
    new_text, issues = find_and_rewrite(query)
    assert "JOIN cb_cte_1 ON _cb_t.manager_id = cb_cte_1.employee_id" in new_text
    assert len(issues) == 1


def test_table_alias_reused_in_recursive_term_instead_of_synthetic_alias():
    query = (
        "SELECT e.employee_id, e.manager_id\n"
        "FROM employees e\n"
        "START WITH e.manager_id IS NULL\n"
        "CONNECT BY PRIOR e.employee_id = e.manager_id"
    )
    new_text, issues = find_and_rewrite(query)
    assert "_cb_t" not in new_text
    assert "FROM employees e\n  JOIN cb_cte_1 ON e.manager_id = cb_cte_1.employee_id" in new_text


def test_order_by_is_preserved_after_the_cte():
    query = (
        "SELECT employee_id, manager_id\n"
        "FROM employees\n"
        "START WITH manager_id IS NULL\n"
        "CONNECT BY PRIOR employee_id = manager_id\n"
        "ORDER BY employee_id"
    )
    new_text, _issues = find_and_rewrite(query)
    assert new_text.strip().endswith("FROM cb_cte_1\nORDER BY employee_id")


def test_level_pseudocolumn_tracked_through_recursion():
    query = (
        "SELECT employee_id, manager_id, LEVEL\n"
        "FROM employees\n"
        "START WITH manager_id IS NULL\n"
        "CONNECT BY PRIOR employee_id = manager_id"
    )
    new_text, _issues = find_and_rewrite(query)
    assert "employee_id, manager_id, LEVEL) AS (" in new_text
    assert "SELECT employee_id, manager_id, 1\n" in new_text  # anchor: level starts at 1
    assert "cb_cte_1.LEVEL + 1" in new_text  # recursive: level increments


def test_level_with_explicit_alias_uses_that_alias_as_the_cte_column():
    query = (
        "SELECT employee_id, LEVEL AS depth\n"
        "FROM employees\n"
        "START WITH manager_id IS NULL\n"
        "CONNECT BY PRIOR employee_id = manager_id"
    )
    new_text, _issues = find_and_rewrite(query)
    assert "employee_id, depth) AS (" in new_text
    assert "cb_cte_1.depth + 1" in new_text


def test_nocycle_is_stripped_and_flagged_with_an_info_issue():
    query = (
        "SELECT employee_id, manager_id\n"
        "FROM employees\n"
        "START WITH manager_id IS NULL\n"
        "CONNECT BY NOCYCLE PRIOR employee_id = manager_id"
    )
    new_text, issues = find_and_rewrite(query)
    assert "NOCYCLE" not in new_text
    assert len(issues) == 2
    assert any("NOCYCLE" in i.message for i in issues)


def test_sqlserver_and_db2_use_plain_with_not_with_recursive():
    query = (
        "SELECT employee_id, manager_id\n"
        "FROM employees\n"
        "START WITH manager_id IS NULL\n"
        "CONNECT BY PRIOR employee_id = manager_id"
    )
    new_text, _issues = find_and_rewrite(query, cte_keyword="WITH")
    assert new_text.startswith("WITH cb_cte_1")
    assert "RECURSIVE" not in new_text


def test_select_star_is_left_untouched():
    query = (
        "SELECT * FROM employees "
        "START WITH manager_id IS NULL "
        "CONNECT BY PRIOR employee_id = manager_id"
    )
    new_text, issues = find_and_rewrite(query)
    assert new_text == query
    assert issues == []


def test_unaliased_expression_column_is_left_untouched():
    query = (
        "SELECT employee_id, salary * 1.1 "
        "FROM employees "
        "START WITH manager_id IS NULL "
        "CONNECT BY PRIOR employee_id = manager_id"
    )
    new_text, issues = find_and_rewrite(query)
    assert new_text == query
    assert issues == []


def test_extra_where_clause_is_left_untouched():
    query = (
        "SELECT employee_id, manager_id FROM employees "
        "WHERE department_id = 10 "
        "START WITH manager_id IS NULL "
        "CONNECT BY PRIOR employee_id = manager_id"
    )
    new_text, issues = find_and_rewrite(query)
    assert new_text == query
    assert issues == []


def test_order_siblings_by_is_left_untouched():
    query = (
        "SELECT employee_id, manager_id FROM employees "
        "START WITH manager_id IS NULL "
        "CONNECT BY PRIOR employee_id = manager_id "
        "ORDER SIBLINGS BY employee_id"
    )
    new_text, issues = find_and_rewrite(query)
    assert new_text == query
    assert issues == []


def test_multiple_conditions_in_connect_by_is_left_untouched():
    query = (
        "SELECT employee_id, manager_id FROM employees "
        "START WITH manager_id IS NULL "
        "CONNECT BY PRIOR employee_id = manager_id AND status = 'ACTIVE'"
    )
    new_text, issues = find_and_rewrite(query)
    assert new_text == query
    assert issues == []


def test_sys_connect_by_path_is_left_untouched():
    query = (
        "SELECT employee_id, SYS_CONNECT_BY_PATH(last_name, '/') AS path FROM employees "
        "START WITH manager_id IS NULL "
        "CONNECT BY PRIOR employee_id = manager_id"
    )
    new_text, issues = find_and_rewrite(query)
    assert new_text == query
    assert issues == []


def test_joined_multi_table_from_clause_is_left_untouched():
    query = (
        "SELECT e.employee_id, e.manager_id FROM employees e, departments d "
        "WHERE e.department_id = d.department_id "
        "START WITH e.manager_id IS NULL "
        "CONNECT BY PRIOR e.employee_id = e.manager_id"
    )
    new_text, issues = find_and_rewrite(query)
    assert new_text == query
    assert issues == []


def test_text_without_connect_by_is_returned_unchanged():
    text = "SELECT * FROM employees WHERE department_id = 10"
    new_text, issues = find_and_rewrite(text)
    assert new_text == text
    assert issues == []


def test_multiple_independent_connect_by_queries_each_get_their_own_cte_name():
    text = (
        "SELECT employee_id, manager_id FROM employees "
        "START WITH manager_id IS NULL CONNECT BY PRIOR employee_id = manager_id;\n"
        "SELECT category_id, parent_id FROM categories "
        "START WITH parent_id IS NULL CONNECT BY PRIOR category_id = parent_id;"
    )
    new_text, issues = find_and_rewrite(text)
    assert "cb_cte_1" in new_text
    assert "cb_cte_2" in new_text
    assert len(issues) == 2


def test_rewrite_stops_at_statement_terminating_semicolon():
    text = (
        "CURSOR c1 IS SELECT employee_id, manager_id FROM employees "
        "START WITH manager_id IS NULL CONNECT BY PRIOR employee_id = manager_id;\n"
        "v_id NUMBER;"
    )
    new_text, issues = find_and_rewrite(text)
    assert "v_id NUMBER;" in new_text
    assert new_text.count(";") >= 1
    assert len(issues) == 1


def test_rewrite_stops_at_enclosing_close_paren_for_a_for_loop_header():
    text = (
        "FOR rec IN (SELECT employee_id, manager_id FROM employees "
        "START WITH manager_id IS NULL CONNECT BY PRIOR employee_id = manager_id) LOOP\n"
        "  NULL;\n"
        "END LOOP;"
    )
    new_text, issues = find_and_rewrite(text)
    assert new_text.startswith("FOR rec IN (WITH RECURSIVE cb_cte_1")
    assert new_text.rstrip().endswith(") LOOP\n  NULL;\nEND LOOP;")
    assert len(issues) == 1
