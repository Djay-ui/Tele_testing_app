"""Tests for skipping Oracle-internal indexes when generating target DDL
(the "function sys_op_map_nonnull(character varying) does not exist" bug).

Oracle automatically creates a unique index on a materialized view's
container table to support fast refresh -- named I_SNAP$_<mv name> -- built
on an expression that wraps every column in SYS_OP_MAP_NONNULL(...), an
undocumented, Oracle-internal function with no equivalent on any target.
introspector.py resolves this expression via ALL_IND_EXPRESSIONS the same
way it resolves a real function-based index's expression (see
tests/test_ddl_generator_function_index.py), but unlike LOWER(...)/
UPPER(...), this expression is never valid SQL anywhere else, and the
index behind it enforces nothing a real business rule ever depended on.
This file covers ddl_generator.py's decision to skip such an index
entirely (with an explanatory issue on the SQL dialects that track one)
rather than emit a CREATE INDEX that is guaranteed to fail on every target.
"""
from tgdatabridge.core import ddl_generator
from tgdatabridge.core.ddl_generator import (
    _has_oracle_internal_expression,
    _is_oracle_internal_expression,
)
from tgdatabridge.core.schema_model import Column, Index, Table


def _table_with_snapshot_index() -> Table:
    return Table(
        name="FEATURE_TEST_MV",
        schema="HR",
        columns=[
            Column(name="CATEGORY", data_type="VARCHAR2(50)", nullable=True),
        ],
        indexes=[
            Index(
                name="I_SNAP$_FEATURE_TEST_MV",
                columns=["SYS_OP_MAP_NONNULL(CATEGORY)"],
                unique=True,
            ),
        ],
    )


# --------------------------------------------------------- unit-level checks


def test_is_oracle_internal_expression_detects_sys_op_functions():
    assert _is_oracle_internal_expression("SYS_OP_MAP_NONNULL(CATEGORY)") is True
    assert _is_oracle_internal_expression("SYS_OP_C2C(NAME)") is True


def test_is_oracle_internal_expression_leaves_ordinary_expressions_alone():
    # A real, portable function-based index (the Round 29 case) must not
    # be caught by this net -- only Oracle's own SYS_OP_* functions are.
    assert _is_oracle_internal_expression('LOWER("EMAIL")') is False
    assert _is_oracle_internal_expression("EMAIL") is False


def test_has_oracle_internal_expression_on_an_index():
    snapshot_idx = Index(name="I_SNAP$_X", columns=["SYS_OP_MAP_NONNULL(CATEGORY)"], unique=True)
    assert _has_oracle_internal_expression(snapshot_idx) is True
    plain_idx = Index(name="IX_STATUS", columns=["STATUS"], unique=False)
    assert _has_oracle_internal_expression(plain_idx) is False
    function_idx = Index(name="IX_EMAIL_LOWER", columns=['LOWER("EMAIL")'], unique=False)
    assert _has_oracle_internal_expression(function_idx) is False


# ---------------------------------------------- end-to-end DDL generation


def test_postgres_ddl_skips_the_snapshot_index_and_raises_an_info_issue():
    table = _table_with_snapshot_index()
    ddl, issues = ddl_generator.generate_table_ddl_postgres(table)
    assert "sys_op_map_nonnull" not in ddl.lower()
    assert "i_snap" not in ddl.lower()
    assert any(
        issue.severity == "info" and "I_SNAP$_FEATURE_TEST_MV" in issue.message
        for issue in issues
    )
    # It must not ALSO get the generic "built on an expression" warning --
    # that message is misleading for an object that was never a real user
    # index in the first place.
    assert not any("not a plain column" in issue.message for issue in issues)


def test_oracle_ddl_skips_the_snapshot_index():
    table = _table_with_snapshot_index()
    ddl, issues = ddl_generator.generate_table_ddl_oracle(table)
    assert "sys_op_map_nonnull" not in ddl.lower()
    assert any(issue.severity == "info" for issue in issues)


def test_mysql_ddl_skips_the_snapshot_index():
    table = _table_with_snapshot_index()
    ddl, issues = ddl_generator.generate_table_ddl_mysql(table)
    assert "sys_op_map_nonnull" not in ddl.lower()
    assert any(issue.severity == "info" for issue in issues)


def test_sqlserver_ddl_skips_the_snapshot_index():
    table = _table_with_snapshot_index()
    ddl, issues = ddl_generator.generate_table_ddl_sqlserver(table)
    assert "sys_op_map_nonnull" not in ddl.lower()
    assert any(issue.severity == "info" for issue in issues)


def test_db2_ddl_skips_the_snapshot_index():
    table = _table_with_snapshot_index()
    ddl, issues = ddl_generator.generate_table_ddl_db2(table)
    assert "sys_op_map_nonnull" not in ddl.lower()
    assert any(issue.severity == "info" for issue in issues)


def test_deferred_postgres_ddl_also_skips_the_snapshot_index():
    table = _table_with_snapshot_index()
    deferred = ddl_generator.generate_deferred_ddl_postgres(table)
    assert "sys_op_map_nonnull" not in deferred.lower()
    assert "i_snap" not in deferred.lower()


def test_deferred_oracle_ddl_also_skips_the_snapshot_index():
    table = _table_with_snapshot_index()
    deferred = ddl_generator.generate_deferred_ddl_oracle(table)
    assert "sys_op_map_nonnull" not in deferred.lower()
    assert "i_snap" not in deferred.lower()


def test_deferred_mysql_ddl_also_skips_the_snapshot_index():
    table = _table_with_snapshot_index()
    deferred = ddl_generator.generate_deferred_ddl_mysql(table)
    assert "sys_op_map_nonnull" not in deferred.lower()


def test_deferred_sqlserver_ddl_also_skips_the_snapshot_index():
    table = _table_with_snapshot_index()
    deferred = ddl_generator.generate_deferred_ddl_sqlserver(table)
    assert "sys_op_map_nonnull" not in deferred.lower()


def test_deferred_db2_ddl_also_skips_the_snapshot_index():
    table = _table_with_snapshot_index()
    deferred = ddl_generator.generate_deferred_ddl_db2(table)
    assert "sys_op_map_nonnull" not in deferred.lower()


def test_a_real_function_based_index_is_unaffected_by_this_fix():
    # Regression guard: the Round 29 fix (a genuine LOWER(...)/UPPER(...)
    # function-based index) must still be carried through as-is, not
    # swept up by this new, narrower net for Oracle's own SYS_OP_* noise.
    table = Table(
        name="CUSTOMERS",
        schema="HR",
        columns=[Column(name="EMAIL", data_type="VARCHAR2(100)", nullable=False)],
        indexes=[Index(name="IX_CUSTOMERS_EMAIL_LOWER", columns=['LOWER("EMAIL")'], unique=False)],
    )
    ddl, issues = ddl_generator.generate_table_ddl_postgres(table)
    assert "LOWER(EMAIL)" in ddl
    assert any(issue.severity == "warning" for issue in issues)
    assert not any(issue.severity == "info" for issue in issues)


def test_a_plain_column_index_is_unaffected_by_this_fix():
    table = Table(
        name="CUSTOMERS",
        schema="HR",
        columns=[Column(name="STATUS", data_type="VARCHAR2(20)", nullable=True)],
        indexes=[Index(name="IX_STATUS", columns=["STATUS"], unique=False)],
    )
    ddl, issues = ddl_generator.generate_table_ddl_postgres(table)
    assert '"status"' in ddl
    assert issues == []
