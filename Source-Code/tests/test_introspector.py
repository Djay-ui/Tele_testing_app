"""Tests for tgdatabridge.core.introspector -- reading Oracle's data
dictionary into a Schema, for using Oracle as a *source* engine.

Uses a fake connector whose execute() dispatches on a short, deliberately
distinctive substring registered for each query the introspector issues,
rather than a real Oracle connection -- these are pure data-shape/mapping
tests, not integration tests against a live database. Mirrors the same
pattern tests/test_mysql_introspector.py uses.
"""
from tgdatabridge.core.introspector import introspect_schema
from tgdatabridge.core.schema_model import Schema


class _FakeConn:
    def __init__(self, routes):
        # routes: list of (distinctive_substring, rows) checked most-specific
        # (longest substring) first, so a query matching more than one
        # registered substring still resolves to the intended one.
        self._routes = sorted(routes, key=lambda kv: -len(kv[0]))

    def execute(self, sql, params=None):
        sql_norm = " ".join(sql.split())
        for key, rows in self._routes:
            if key in sql_norm:
                return rows
        raise AssertionError(f"No fake route registered for query:\n{sql}")


def _conn(
    tables=(), columns=(), identity_cols=(), constraints=(), cons_columns=(),
    ref_constraints=(), indexes=(), ind_expressions=(), ind_columns=(),
    sequences=(), views=(), part_tables=(), part_key_columns=(), tab_partitions=(),
    mviews=(),
):
    return _FakeConn([
        ("FROM all_tables t", tables),
        ("FROM all_tab_columns", columns),
        ("FROM all_tab_identity_cols", identity_cols),
        ("FROM all_constraints WHERE owner = :owner AND constraint_type IN", constraints),
        ("FROM all_cons_columns", cons_columns),
        ("FROM all_constraints WHERE owner = :owner AND constraint_type = 'R'", ref_constraints),
        ("FROM all_indexes", indexes),
        ("FROM all_ind_expressions", ind_expressions),
        ("FROM all_ind_columns", ind_columns),
        ("FROM all_sequences", sequences),
        ("FROM all_views", views),
        ("FROM all_part_tables", part_tables),
        ("FROM all_part_key_columns", part_key_columns),
        ("FROM all_tab_partitions", tab_partitions),
        ("FROM all_mviews", mviews),
    ])


def _schema(**kwargs) -> Schema:
    conn = _conn(**kwargs)
    return introspect_schema(conn, "HR", include_routines=False)


# --------------------------------------------------- function-based indexes


def test_plain_index_column_is_carried_through_unchanged():
    schema = _schema(
        tables=[("CUSTOMERS", None)],
        columns=[("CUSTOMERS", "EMAIL", "VARCHAR2", 100, None, None, "Y", None)],
        indexes=[("CUSTOMERS", "IX_CUSTOMERS_EMAIL", "NONUNIQUE")],
        ind_columns=[("IX_CUSTOMERS_EMAIL", "EMAIL", 1)],
    )
    idx = schema.tables[0].indexes[0]
    assert idx.columns == ["EMAIL"]


def test_function_based_index_resolves_hidden_column_to_its_real_expression():
    # The exact real-world bug: ALL_IND_COLUMNS reports a function-based
    # index's column as a hidden virtual column (SYS_NC00011$), which is
    # meaningless on any target -- CREATE INDEX ... ("sys_nc00011$") fails
    # with `column "sys_nc00011$" does not exist` the moment it's applied,
    # because no target this tool creates ever has such a column. The real
    # expression (LOWER("EMAIL")) is only in ALL_IND_EXPRESSIONS, keyed by
    # the same index name and column position.
    schema = _schema(
        tables=[("CUSTOMERS", None)],
        columns=[("CUSTOMERS", "EMAIL", "VARCHAR2", 100, None, None, "Y", None)],
        indexes=[("CUSTOMERS", "IX_CUSTOMERS_EMAIL_LOWER", "NONUNIQUE")],
        ind_columns=[("IX_CUSTOMERS_EMAIL_LOWER", "SYS_NC00011$", 1)],
        ind_expressions=[("IX_CUSTOMERS_EMAIL_LOWER", 1, 'LOWER("EMAIL")')],
    )
    idx = schema.tables[0].indexes[0]
    assert idx.columns == ['LOWER("EMAIL")']
    assert "SYS_NC00011$" not in idx.columns[0]


def test_composite_index_mixes_a_plain_column_and_an_expression_correctly():
    # A two-column index where only the second column is function-based --
    # position-keyed matching must apply the substitution to exactly the
    # right slot, not the whole index.
    schema = _schema(
        tables=[("CUSTOMERS", None)],
        columns=[
            ("CUSTOMERS", "STATUS", "VARCHAR2", 20, None, None, "Y", None),
            ("CUSTOMERS", "EMAIL", "VARCHAR2", 100, None, None, "Y", None),
        ],
        indexes=[("CUSTOMERS", "IX_STATUS_EMAIL_LOWER", "NONUNIQUE")],
        ind_columns=[
            ("IX_STATUS_EMAIL_LOWER", "STATUS", 1),
            ("IX_STATUS_EMAIL_LOWER", "SYS_NC00012$", 2),
        ],
        ind_expressions=[("IX_STATUS_EMAIL_LOWER", 2, 'LOWER("EMAIL")')],
    )
    idx = schema.tables[0].indexes[0]
    assert idx.columns == ["STATUS", 'LOWER("EMAIL")']


def test_index_with_no_matching_expression_row_keeps_its_column_name():
    # Regression guard: a table with *some* function-based indexes must not
    # cause an ordinary index's plain column to be second-guessed just
    # because ALL_IND_EXPRESSIONS has rows for a different index.
    schema = _schema(
        tables=[("CUSTOMERS", None)],
        columns=[("CUSTOMERS", "STATUS", "VARCHAR2", 20, None, None, "Y", None)],
        indexes=[("CUSTOMERS", "IX_STATUS", "NONUNIQUE")],
        ind_columns=[("IX_STATUS", "STATUS", 1)],
        ind_expressions=[("IX_OTHER_LOWER", 1, 'LOWER("NAME")')],
    )
    idx = schema.tables[0].indexes[0]
    assert idx.columns == ["STATUS"]


# ---------------------------------------------------------------- partitioning


def test_a_range_partitioned_table_gets_a_partition_scheme():
    schema = _schema(
        tables=[("SALES", None)],
        columns=[("SALES", "SALE_DATE", "DATE", None, None, None, "Y", None)],
        part_tables=[("SALES", "RANGE", "NONE")],
        part_key_columns=[("SALES", "SALE_DATE", 1)],
        tab_partitions=[
            ("SALES", "SALES_2024_01", "TO_DATE(' 2024-02-01 00:00:00', 'SYYYY-MM-DD HH24:MI:SS')", 1),
            ("SALES", "SALES_2024_02", "MAXVALUE", 2),
        ],
    )
    table = schema.tables[0]
    assert table.partition_scheme is not None
    scheme = table.partition_scheme
    assert scheme.kind == "RANGE"
    assert scheme.columns == ["SALE_DATE"]
    assert scheme.subpartitioning_type is None
    assert [p.name for p in scheme.partitions] == ["SALES_2024_01", "SALES_2024_02"]
    assert scheme.partitions[1].high_value == "MAXVALUE"


def test_a_composite_partitioning_scheme_keeps_its_subpartitioning_type():
    schema = _schema(
        tables=[("ORDERS", None)],
        columns=[("ORDERS", "REGION", "VARCHAR2", 20, None, None, "Y", None)],
        part_tables=[("ORDERS", "RANGE", "HASH")],
        part_key_columns=[("ORDERS", "ORDER_DATE", 1)],
        tab_partitions=[("ORDERS", "P1", "MAXVALUE", 1)],
    )
    scheme = schema.tables[0].partition_scheme
    assert scheme.kind == "RANGE"
    assert scheme.subpartitioning_type == "HASH"


def test_an_ordinary_unpartitioned_table_has_no_partition_scheme():
    schema = _schema(
        tables=[("CUSTOMERS", None)],
        columns=[("CUSTOMERS", "EMAIL", "VARCHAR2", 100, None, None, "Y", None)],
    )
    assert schema.tables[0].partition_scheme is None


# ----------------------------------------------------------- materialized views


def test_a_materialized_view_is_moved_out_of_tables_and_into_views():
    # The exact real-world gap this closes: ALL_MVIEWS is the only place an
    # MV's defining query lives -- its container table (which is where the
    # introspector's plain all_tables/all_tab_columns pass already picked
    # it up, indistinguishable from an ordinary table) has no query at all.
    # Left as a Table, migrating it would silently copy a data snapshot and
    # drop the query/refresh metadata on the floor.
    schema = _schema(
        tables=[("ACTIVE_EMP_MV", None), ("CUSTOMERS", None)],
        columns=[
            ("ACTIVE_EMP_MV", "EMP_ID", "NUMBER", None, 9, 0, "Y", None),
            ("CUSTOMERS", "EMAIL", "VARCHAR2", 100, None, None, "Y", None),
        ],
        mviews=[(
            "ACTIVE_EMP_MV", "SELECT emp_id FROM employees WHERE status = 'A'",
            "IMMEDIATE", "DEMAND", "FORCE",
        )],
    )
    table_names = [t.name for t in schema.tables]
    assert "ACTIVE_EMP_MV" not in table_names
    assert table_names == ["CUSTOMERS"]

    mviews = [v for v in schema.views if v.is_materialized]
    assert len(mviews) == 1
    mv = mviews[0]
    assert mv.name == "ACTIVE_EMP_MV"
    assert mv.definition == "SELECT emp_id FROM employees WHERE status = 'A'"
    assert mv.mview_build_mode == "IMMEDIATE"
    assert mv.mview_refresh_mode == "DEMAND"
    assert mv.mview_refresh_method == "FORCE"


def test_an_ordinary_view_is_not_marked_materialized():
    schema = _schema(views=[("V_ACTIVE", "SELECT * FROM t")])
    assert schema.views[0].is_materialized is False
