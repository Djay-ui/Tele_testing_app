"""Tests for the ddl_generator.py side of the function-based-index fix
(the "column \"sys_nc00011$\" does not exist" bug).

introspector.py substitutes the real expression (e.g. 'LOWER("EMAIL")') in
place of Oracle's hidden SYS_NC#####$ column name in Index.columns -- see
tests/test_introspector.py for that half. This file covers what
ddl_generator.py does with an Index.columns entry that is an expression
rather than a plain column name: it must render the expression unquoted
(not quoted as if it were a single identifier, which would send a target
looking for a column literally named 'lower("email")' or similar) and
raise a ConversionIssue warning about it, for every SQL dialect. MongoDB
has no equivalent of a function-based index, so its two index-generation
sites skip such an index entirely instead.
"""
from tgdatabridge.core import ddl_generator
from tgdatabridge.core.ddl_generator import (
    _index_column_sql,
    _is_index_expression,
    _mongo_index_has_expression_column,
    _quote_db2,
    _quote_mysql,
    _quote_oracle,
    _quote_pg,
    _quote_sqlserver,
)
from tgdatabridge.core.schema_model import Column, Constraint, Index, Table


def _table_with_expression_index() -> Table:
    return Table(
        name="CUSTOMERS",
        schema="HR",
        columns=[
            Column(name="ID", data_type="NUMBER(9)", nullable=False),
            Column(name="EMAIL", data_type="VARCHAR2(100)", nullable=False),
        ],
        constraints=[
            Constraint(name="PK_CUSTOMERS", kind="PRIMARY KEY", columns=["ID"]),
        ],
        indexes=[
            Index(name="IX_CUSTOMERS_EMAIL_LOWER", columns=['LOWER("EMAIL")'], unique=False),
        ],
    )


# --------------------------------------------------------- unit-level checks


def test_is_index_expression_detects_parentheses():
    assert _is_index_expression('LOWER("EMAIL")') is True
    assert _is_index_expression("EMAIL") is False


def test_index_column_sql_passes_plain_column_through_quote_fn():
    assert _index_column_sql(_quote_pg, "EMAIL") == _quote_pg("EMAIL")
    assert _index_column_sql(_quote_oracle, "EMAIL") == _quote_oracle("EMAIL")


def test_index_column_sql_strips_quotes_from_an_expression_and_skips_quote_fn():
    # The whole point of the fix: an expression is never handed to a
    # _quote_* function (which would wrap the *entire expression string*
    # in one pair of quotes, as if "LOWER(\"EMAIL\")" were itself a column
    # name) -- it is rendered unquoted so every target's own unquoted-
    # identifier folding applies to EMAIL inside it, exactly as it would
    # for a plain column reference this tool created.
    result = _index_column_sql(_quote_pg, 'LOWER("EMAIL")')
    assert result == "LOWER(EMAIL)"
    assert '"' not in result


def test_index_column_sql_strips_quotes_for_every_dialect():
    for quote_fn in (_quote_pg, _quote_mysql, _quote_sqlserver, _quote_db2, _quote_oracle):
        result = _index_column_sql(quote_fn, 'LOWER("EMAIL")')
        assert result == "LOWER(EMAIL)", quote_fn.__name__


# ---------------------------------------------- end-to-end DDL generation


def test_postgres_ddl_never_quotes_sys_nc_and_uses_bare_expression():
    table = _table_with_expression_index()
    ddl, issues = ddl_generator.generate_table_ddl_postgres(table)
    assert "sys_nc" not in ddl.lower()
    assert "LOWER(EMAIL)" in ddl
    assert any(issue.severity == "warning" for issue in issues)
    assert any("IX_CUSTOMERS_EMAIL_LOWER" in issue.message for issue in issues)


def test_oracle_ddl_never_quotes_sys_nc_and_uses_bare_expression():
    table = _table_with_expression_index()
    ddl, issues = ddl_generator.generate_table_ddl_oracle(table)
    assert "sys_nc" not in ddl.lower()
    assert "LOWER(EMAIL)" in ddl
    assert any(issue.severity == "warning" for issue in issues)


def test_mysql_ddl_never_quotes_sys_nc_and_uses_bare_expression():
    table = _table_with_expression_index()
    ddl, issues = ddl_generator.generate_table_ddl_mysql(table)
    assert "sys_nc" not in ddl.lower()
    assert "LOWER(EMAIL)" in ddl
    assert any(issue.severity == "warning" for issue in issues)


def test_sqlserver_ddl_never_quotes_sys_nc_and_uses_bare_expression():
    table = _table_with_expression_index()
    ddl, issues = ddl_generator.generate_table_ddl_sqlserver(table)
    assert "sys_nc" not in ddl.lower()
    assert "LOWER(EMAIL)" in ddl
    assert any(issue.severity == "warning" for issue in issues)


def test_db2_ddl_never_quotes_sys_nc_and_uses_bare_expression():
    table = _table_with_expression_index()
    ddl, issues = ddl_generator.generate_table_ddl_db2(table)
    assert "sys_nc" not in ddl.lower()
    assert "LOWER(EMAIL)" in ddl
    assert any(issue.severity == "warning" for issue in issues)


def test_deferred_postgres_ddl_also_uses_bare_expression():
    # The deferred path (defer_constraints=True, indexes built after data
    # load) is a completely separate function with its own _quote_pg
    # call site -- must get the same fix, not just the inline path.
    table = _table_with_expression_index()
    deferred = ddl_generator.generate_deferred_ddl_postgres(table)
    assert "sys_nc" not in deferred.lower()
    assert "LOWER(EMAIL)" in deferred


def test_deferred_oracle_ddl_also_uses_bare_expression():
    table = _table_with_expression_index()
    deferred = ddl_generator.generate_deferred_ddl_oracle(table)
    assert "sys_nc" not in deferred.lower()
    assert "LOWER(EMAIL)" in deferred


def test_regular_plain_column_index_is_unaffected_by_the_fix():
    # Regression guard: an ordinary index (no expression) must render
    # exactly as before, with no warning issue about it.
    table = Table(
        name="CUSTOMERS",
        schema="HR",
        columns=[Column(name="STATUS", data_type="VARCHAR2(20)", nullable=True)],
        indexes=[Index(name="IX_STATUS", columns=["STATUS"], unique=False)],
    )
    ddl, issues = ddl_generator.generate_table_ddl_postgres(table)
    assert '"status"' in ddl
    assert not any("IX_STATUS" in issue.message for issue in issues)


# --------------------------------------------------------------- MongoDB


def test_mongo_index_has_expression_column_detects_it():
    idx = Index(name="IX_CUSTOMERS_EMAIL_LOWER", columns=['LOWER("EMAIL")'], unique=False)
    assert _mongo_index_has_expression_column(idx) is True
    plain = Index(name="IX_STATUS", columns=["STATUS"], unique=False)
    assert _mongo_index_has_expression_column(plain) is False


def test_mongo_table_ddl_skips_expression_index_and_raises_a_warning():
    table = _table_with_expression_index()
    ddl, issues = ddl_generator.generate_table_ddl_mongodb(table)
    assert "createIndex" not in ddl or "IX_CUSTOMERS_EMAIL_LOWER" not in ddl
    assert any(
        issue.severity == "warning" and "IX_CUSTOMERS_EMAIL_LOWER" in issue.message
        for issue in issues
    )


def test_mongo_deferred_ddl_silently_skips_expression_index():
    # The deferred Mongo path has no `issues` list in scope at all (it
    # returns a plain str) -- it must skip the expression-based index
    # without raising or erroring, relying on generate_table_ddl_mongodb's
    # own warning (emitted separately, from the same schema-conversion
    # pass) to have told the user about it already.
    table = _table_with_expression_index()
    deferred = ddl_generator.generate_deferred_ddl_mongodb(table)
    assert "IX_CUSTOMERS_EMAIL_LOWER" not in deferred
