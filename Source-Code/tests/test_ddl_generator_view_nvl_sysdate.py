"""Tests for the fix to an Oracle-sourced view carrying NVL(...)/SYSDATE
straight through into target DDL (the "function nvl(numeric, integer)
does not exist" bug).

Oracle's NVL() and SYSDATE never existed on any of the four SQL targets
this tool generates DDL for. generate_view_ddl already *detected* both
(via the oracle_markers scan) and reported a warning about them, but
never actually rewrote the view text -- so a view using either was
reported "Converted automatically" (or "automatic, with warnings") and
then failed outright the moment "Apply DDL to Target" actually ran it.
Stored routines/triggers already got the real rewrite (see
sql_translator.py's _SUBSTITUTIONS and plsql_converter.py) -- this closes
the same gap for views. DECODE()/(+) are deliberately NOT rewritten here:
unlike NVL(a, b) -> COALESCE(a, b) and SYSDATE -> CURRENT_TIMESTAMP, both
lossless renames, those two need real restructuring a human should
review, so they stay flagged rather than guessed at. ROWNUM's top-N idiom
used to be in that same flagged-only list -- it moved to a real rewrite
in a later round; see test_postgres_sql_expression_gaps.py for that fix
and this file's own test below for the regression guard proving it.
"""
from tgdatabridge.core import ddl_generator
from tgdatabridge.core.schema_model import View


def _oracle_view(definition: str) -> View:
    return View(name="FEATURE_TEST_CATEGORY_V", schema="HR", definition=definition)


# --------------------------------------------------- NVL -> COALESCE


def test_postgres_view_rewrites_nvl_to_coalesce():
    v = _oracle_view("SELECT category, SUM(NVL(amount,0)) total_amount FROM orders GROUP BY category")
    ddl, issues = ddl_generator.generate_view_ddl(v, "PostgreSQL")
    assert "NVL(" not in ddl.upper()
    assert "COALESCE(amount,0)" in ddl
    assert not any("NVL" in issue.message for issue in issues)


def test_mysql_view_rewrites_nvl_to_coalesce():
    v = _oracle_view("SELECT NVL(amount, 0) amt FROM orders")
    ddl, issues = ddl_generator.generate_view_ddl(v, "MySQL")
    assert "NVL(" not in ddl.upper()
    assert "COALESCE(amount, 0)" in ddl


def test_sqlserver_view_rewrites_nvl_to_coalesce():
    v = _oracle_view("SELECT NVL(amount, 0) amt FROM orders")
    ddl, issues = ddl_generator.generate_view_ddl(v, "SQL Server", schema="dbo")
    assert "NVL(" not in ddl.upper()
    assert "COALESCE(amount, 0)" in ddl


def test_db2_view_rewrites_nvl_to_coalesce():
    v = _oracle_view("SELECT NVL(amount, 0) amt FROM orders")
    ddl, issues = ddl_generator.generate_view_ddl(v, "Db2")
    assert "NVL(" not in ddl.upper()
    assert "COALESCE(amount, 0)" in ddl


def test_nvl_rewrite_is_case_insensitive_and_handles_whitespace_before_paren():
    v = _oracle_view("SELECT nvl (amount, 0) amt FROM orders")
    ddl, issues = ddl_generator.generate_view_ddl(v, "PostgreSQL")
    assert "coalesce" in ddl.lower()
    assert "nvl" not in ddl.lower()


def test_multiple_nvl_calls_in_one_view_are_all_rewritten():
    v = _oracle_view(
        "SELECT category, SUM(NVL(amount,0)) total_amount, SUM(NVL(quantity,0)) total_quantity, "
        "AVG(NVL(amount,0)) average_amount FROM orders GROUP BY category"
    )
    ddl, issues = ddl_generator.generate_view_ddl(v, "PostgreSQL")
    assert ddl.upper().count("NVL(") == 0
    assert ddl.count("COALESCE(") == 3


# --------------------------------------------------- SYSDATE -> CURRENT_TIMESTAMP


def test_postgres_view_rewrites_sysdate_to_current_timestamp():
    v = _oracle_view("SELECT id, SYSDATE AS loaded_at FROM orders")
    ddl, issues = ddl_generator.generate_view_ddl(v, "PostgreSQL")
    assert "SYSDATE" not in ddl.upper()
    assert "CURRENT_TIMESTAMP" in ddl
    assert not any("SYSDATE" in issue.message for issue in issues)


def test_mysql_view_rewrites_sysdate_to_current_timestamp():
    v = _oracle_view("SELECT id, SYSDATE AS loaded_at FROM orders")
    ddl, issues = ddl_generator.generate_view_ddl(v, "MySQL")
    assert "SYSDATE" not in ddl.upper()
    assert "CURRENT_TIMESTAMP" in ddl


# --------------------------------------------------- what stays flagged, not rewritten


def test_decode_is_still_flagged_but_not_rewritten():
    v = _oracle_view("SELECT DECODE(status, 'A', 'Active', 'Inactive') s FROM orders")
    ddl, issues = ddl_generator.generate_view_ddl(v, "PostgreSQL")
    assert "DECODE(" in ddl.upper()
    assert any("DECODE" in issue.message for issue in issues)


def test_rownum_topn_is_now_rewritten_to_limit():
    # No longer flagged-only as of the ROWNUM/LISTAGG/REGEXP_*/date-
    # arithmetic round -- see test_postgres_sql_expression_gaps.py for
    # the full set of cases this rewrite covers and deliberately doesn't.
    v = _oracle_view("SELECT id FROM orders WHERE ROWNUM <= 10")
    ddl, issues = ddl_generator.generate_view_ddl(v, "PostgreSQL")
    assert "LIMIT 10" in ddl
    assert "ROWNUM" not in ddl.upper()


def test_oracle_outer_join_operator_is_still_flagged_but_not_rewritten():
    v = _oracle_view("SELECT a.id FROM orders a, customers b WHERE a.cust_id = b.id(+)")
    ddl, issues = ddl_generator.generate_view_ddl(v, "PostgreSQL")
    assert "(+)" in ddl
    assert any("(+)" in issue.message for issue in issues)


# --------------------------------------------------- unaffected paths


def test_oracle_target_keeps_nvl_and_sysdate_verbatim():
    # An Oracle-sourced view going to an Oracle target needs no rewrite at
    # all -- NVL/SYSDATE are native there. This mirrors the early-return
    # path in generate_view_ddl for an Oracle-to-Oracle view.
    v = _oracle_view("SELECT NVL(amount, 0), SYSDATE FROM orders")
    ddl, issues = ddl_generator.generate_view_ddl(v, "Oracle")
    assert "NVL(" in ddl.upper()
    assert "SYSDATE" in ddl.upper()
    assert issues == []


def test_non_oracle_sourced_view_is_not_touched_by_this_substitution():
    # A view whose text isn't Oracle SQL at all must not have this
    # Oracle-specific rewrite applied to it -- gated on source_engine, the
    # same way the SQL-Server-source translation a few lines above it in
    # generate_view_ddl is gated on its own source_engine check. (A
    # PostgreSQL-sourced view containing the literal text "NVL(" is
    # contrived, but it proves the gate itself, not just that the pattern
    # happens not to match.)
    v = View(name="V", schema="S", definition="SELECT NVL(amount, 0) amt FROM t",
             source_engine="PostgreSQL")
    ddl, issues = ddl_generator.generate_view_ddl(v, "MySQL")
    assert "NVL(" in ddl.upper()
