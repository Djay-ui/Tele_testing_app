import pytest

from tgdatabridge.core import ddl_generator
from tgdatabridge.core.schema_model import (
    Column, ConversionIssue, ConversionStatus, Constraint, Index, Partition,
    PartitionScheme, Routine, Schema, Sequence, Table, View,
)
from tgdatabridge.utils.sql_split import split_sql_statements


def _sample_table() -> Table:
    return Table(
        name="EMPLOYEES",
        schema="HR",
        columns=[
            Column(name="EMP_ID", data_type="NUMBER(9)", nullable=False, identity=True),
            Column(name="NAME", data_type="VARCHAR2(100)", nullable=False),
            Column(name="HIRE_DATE", data_type="DATE", nullable=True),
            Column(name="SALARY", data_type="NUMBER(10,2)", nullable=True),
        ],
        constraints=[
            Constraint(name="PK_EMPLOYEES", kind="PRIMARY KEY", columns=["EMP_ID"]),
        ],
        indexes=[
            Index(name="IDX_EMP_NAME", columns=["NAME"], unique=False),
        ],
    )


def test_generate_table_ddl_postgres_contains_expected_types():
    # Every quoted Postgres identifier (table, column, constraint, index) is
    # lowercased on purpose — see ddl_generator._quote_pg's docstring: Postgres
    # folds *unquoted* identifiers (in views/routine bodies copied through
    # from Oracle) to lowercase, so our quoted names must match that folding.
    table = _sample_table()
    ddl, issues = ddl_generator.generate_table_ddl_postgres(table)
    # IF NOT EXISTS makes re-running "Apply DDL to Target" against a target
    # that already has this table a no-op instead of "relation already exists".
    assert 'CREATE TABLE IF NOT EXISTS "employees"' in ddl
    assert "INTEGER" in ddl  # EMP_ID NUMBER(9)
    assert "VARCHAR(100)" in ddl
    assert "TIMESTAMP" in ddl  # HIRE_DATE
    assert "NUMERIC(10,2)" in ddl  # SALARY
    assert "GENERATED ALWAYS AS IDENTITY" in ddl
    assert 'PRIMARY KEY ("emp_id")' in ddl
    assert 'CREATE  INDEX IF NOT EXISTS "idx_emp_name"' in ddl or 'CREATE INDEX IF NOT EXISTS "idx_emp_name"' in ddl


def test_generate_table_ddl_mysql_contains_expected_types():
    table = _sample_table()
    ddl, issues = ddl_generator.generate_table_ddl_mysql(table)
    assert "CREATE TABLE IF NOT EXISTS `EMPLOYEES`" in ddl
    assert "AUTO_INCREMENT" in ddl
    assert "DECIMAL(10,2)" in ddl
    assert "DATETIME" in ddl
    assert "ENGINE=InnoDB" in ddl


def test_sequence_ddl_postgres_is_native():
    seq = Sequence(name="EMP_SEQ", schema="HR", start_value=1, increment_by=1)
    ddl, issues = ddl_generator.generate_sequence_ddl_postgres(seq)
    assert "CREATE SEQUENCE" in ddl
    assert issues == []


def test_sequence_ddl_postgres_clamps_oracle_default_maxvalue():
    # Oracle sequences created without an explicit MAXVALUE default to
    # 9999999999999999999999999999, which is outside PostgreSQL's bigint
    # sequence range and would otherwise make CREATE SEQUENCE fail with
    # "value ... is out of range for type bigint".
    seq = Sequence(
        name="EMP_SEQ", schema="HR", start_value=1, increment_by=1,
        min_value=1, max_value=9999999999999999999999999999,
    )
    ddl, issues = ddl_generator.generate_sequence_ddl_postgres(seq)
    assert "MAXVALUE" not in ddl
    assert "MINVALUE 1" in ddl
    assert any("bigint" in i.message for i in issues)


def test_sequence_ddl_postgres_keeps_in_range_values():
    seq = Sequence(name="EMP_SEQ", schema="HR", start_value=1, increment_by=1,
                    min_value=1, max_value=1000000)
    ddl, issues = ddl_generator.generate_sequence_ddl_postgres(seq)
    assert "MAXVALUE 1000000" in ddl
    assert issues == []


def test_sequence_ddl_mysql_emulated_with_warning():
    seq = Sequence(name="EMP_SEQ", schema="HR", start_value=1, increment_by=1)
    ddl, issues = ddl_generator.generate_sequence_ddl_mysql(seq)
    assert "CREATE TABLE" in ddl
    assert any(i.severity == "warning" for i in issues)


def test_generate_schema_ddl_end_to_end():
    schema = Schema(name="HR", source_engine="Oracle", target_engine="PostgreSQL")
    schema.tables.append(_sample_table())
    schema.sequences.append(Sequence(name="EMP_SEQ", schema="HR"))
    ddl_text, issues = ddl_generator.generate_schema_ddl(schema, "PostgreSQL")
    assert "CREATE SEQUENCE" in ddl_text
    assert "CREATE TABLE" in ddl_text
    assert schema.tables[0].status is not None


def test_table_ddl_never_contains_inline_foreign_key():
    # FKs must never appear inside CREATE TABLE (postgres: no trailing ALTER
    # TABLE either) — they're generated separately and applied only after
    # every table exists. See generate_foreign_key_ddl_postgres/_mysql.
    account = Table(
        name="ACCOUNT", schema="HR",
        columns=[Column(name="ACCOUNT_ID", data_type="NUMBER(9)", nullable=False),
                 Column(name="CUSTOMER_ID", data_type="NUMBER(9)", nullable=False)],
        constraints=[
            Constraint(name="PK_ACCOUNT", kind="PRIMARY KEY", columns=["ACCOUNT_ID"]),
            Constraint(name="FK_ACCOUNT_CUSTOMER", kind="FOREIGN KEY",
                       columns=["CUSTOMER_ID"], ref_table="CUSTOMER", ref_columns=["CUSTOMER_ID"]),
        ],
    )
    pg_ddl, _ = ddl_generator.generate_table_ddl_postgres(account)
    assert "FOREIGN KEY" not in pg_ddl
    assert "ALTER TABLE" not in pg_ddl

    my_ddl, _ = ddl_generator.generate_table_ddl_mysql(account)
    assert "FOREIGN KEY" not in my_ddl


def test_foreign_key_ddl_generated_separately():
    account = Table(
        name="ACCOUNT", schema="HR",
        columns=[Column(name="CUSTOMER_ID", data_type="NUMBER(9)")],
        constraints=[
            Constraint(name="FK_ACCOUNT_CUSTOMER", kind="FOREIGN KEY",
                       columns=["CUSTOMER_ID"], ref_table="CUSTOMER", ref_columns=["CUSTOMER_ID"]),
        ],
    )
    pg_fk = ddl_generator.generate_foreign_key_ddl_postgres(account)
    assert 'ALTER TABLE "account" ADD CONSTRAINT "fk_account_customer"' in pg_fk
    assert 'FOREIGN KEY ("customer_id") REFERENCES "customer" ("customer_id")' in pg_fk

    my_fk = ddl_generator.generate_foreign_key_ddl_mysql(account)
    assert "ALTER TABLE `ACCOUNT` ADD CONSTRAINT `FK_ACCOUNT_CUSTOMER`" in my_fk


def test_schema_ddl_defers_fk_until_after_all_tables_regardless_of_order():
    # Reproduces the exact real-world failure: the child table (ACCOUNT, with
    # a FK to CUSTOMER) is introspected/listed *before* its parent CUSTOMER.
    # A naive per-table emission puts ACCOUNT's FK right after ACCOUNT's own
    # CREATE TABLE, before CUSTOMER exists yet -> "relation CUSTOMER does not
    # exist". All CREATE TABLEs must come before any ALTER TABLE ADD FK.
    account = Table(
        name="ACCOUNT", schema="HR",
        columns=[Column(name="ACCOUNT_ID", data_type="NUMBER(9)", nullable=False),
                 Column(name="CUSTOMER_ID", data_type="NUMBER(9)", nullable=False)],
        constraints=[
            Constraint(name="PK_ACCOUNT", kind="PRIMARY KEY", columns=["ACCOUNT_ID"]),
            Constraint(name="FK_ACCOUNT_CUSTOMER", kind="FOREIGN KEY",
                       columns=["CUSTOMER_ID"], ref_table="CUSTOMER", ref_columns=["CUSTOMER_ID"]),
        ],
    )
    customer = Table(
        name="CUSTOMER", schema="HR",
        columns=[Column(name="CUSTOMER_ID", data_type="NUMBER(9)", nullable=False)],
        constraints=[Constraint(name="PK_CUSTOMER", kind="PRIMARY KEY", columns=["CUSTOMER_ID"])],
    )
    schema = Schema(name="HR", source_engine="Oracle", target_engine="PostgreSQL")
    schema.tables = [account, customer]  # child listed before parent, on purpose

    ddl_text, issues = ddl_generator.generate_schema_ddl(schema, "PostgreSQL")

    create_account_pos = ddl_text.index('CREATE TABLE IF NOT EXISTS "account"')
    create_customer_pos = ddl_text.index('CREATE TABLE IF NOT EXISTS "customer"')
    fk_pos = ddl_text.index('ALTER TABLE "account" ADD CONSTRAINT "fk_account_customer"')

    assert create_account_pos < fk_pos
    assert create_customer_pos < fk_pos


def test_sequence_ddl_postgres_uses_if_not_exists():
    # Lets "Apply DDL to Target" be re-run against a target that already has
    # this sequence from a previous (partially-failed) run without erroring.
    seq = Sequence(name="EMP_SEQ", schema="HR", start_value=1, increment_by=1)
    ddl, issues = ddl_generator.generate_sequence_ddl_postgres(seq)
    assert 'CREATE SEQUENCE IF NOT EXISTS "emp_seq"' in ddl


def test_foreign_key_ddl_postgres_wrapped_to_tolerate_rerun():
    # ALTER TABLE ADD CONSTRAINT has no IF NOT EXISTS in Postgres; wrap in a
    # DO block that swallows "constraint already exists" (duplicate_object)
    # so re-applying the same DDL is safe.
    account = Table(
        name="ACCOUNT", schema="HR",
        columns=[Column(name="CUSTOMER_ID", data_type="NUMBER(9)")],
        constraints=[
            Constraint(name="FK_ACCOUNT_CUSTOMER", kind="FOREIGN KEY",
                       columns=["CUSTOMER_ID"], ref_table="CUSTOMER", ref_columns=["CUSTOMER_ID"]),
        ],
    )
    pg_fk = ddl_generator.generate_foreign_key_ddl_postgres(account)
    assert pg_fk.startswith("DO $$ BEGIN")
    assert "EXCEPTION" in pg_fk
    assert "WHEN duplicate_object THEN NULL;" in pg_fk
    assert pg_fk.rstrip().endswith("END $$;")
    assert 'ALTER TABLE "account" ADD CONSTRAINT "fk_account_customer"' in pg_fk


def test_generate_schema_ddl_reports_progress_across_every_object_kind():
    # On a very large schema (a big Oracle banking core can easily have
    # 20k+ objects across tables/views/sequences/routines), "Convert
    # Schema" can take a while; progress_cb is what lets the GUI show a
    # real N/total instead of an indeterminate spinner for the whole step.
    schema = Schema(name="HR", source_engine="Oracle", target_engine="PostgreSQL")
    schema.sequences = [Sequence(name="S1", schema="HR"), Sequence(name="S2", schema="HR")]
    schema.tables = [_sample_table()]
    schema.views = [View(name="V1", schema="HR", definition="SELECT 1")]
    schema.routines = [Routine(
        name="P1", schema="HR", kind="PROCEDURE",
        source="PROCEDURE p1 IS BEGIN NULL; END p1;",
    )]

    calls = []
    ddl_generator.generate_schema_ddl(
        schema, "PostgreSQL", progress_cb=lambda done, total: calls.append((done, total)))

    total_objects = len(schema.sequences) + len(schema.tables) + len(schema.views) + len(schema.routines)
    assert len(calls) == total_objects
    assert calls[-1] == (total_objects, total_objects)
    # done increments by exactly 1 each call, never skips or repeats
    assert [c[0] for c in calls] == list(range(1, total_objects + 1))


def test_generate_schema_ddl_progress_cb_is_optional():
    # Existing callers that don't pass progress_cb must be unaffected.
    schema = Schema(name="HR", source_engine="Oracle", target_engine="PostgreSQL")
    schema.tables = [_sample_table()]
    ddl_text, issues = ddl_generator.generate_schema_ddl(schema, "PostgreSQL")
    assert "CREATE TABLE" in ddl_text


def test_comment_only_routine_blocks_do_not_merge_into_the_next_statement():
    # A PACKAGE spec has no Postgres equivalent and renders as a pure
    # comment with no terminating ';' of its own. Without a forced
    # terminator, the statement splitter used by "Apply DDL to Target"
    # never flushes it and it silently merges into the *next* routine's
    # CREATE FUNCTION/PROCEDURE statement -- corrupting that statement's
    # reported line numbers and grouping two unrelated objects under one
    # "Statement N" entry. Regression test for exactly this, found while
    # diagnosing a real "Statement 20/37 failed" report against PKG_BANK.
    spec_source = "PACKAGE PKG_BANK AS\n  PROCEDURE P;\nEND PKG_BANK;"
    body_source = (
        "PACKAGE BODY PKG_BANK AS\n"
        "  PROCEDURE P IS\n"
        "  BEGIN\n"
        "    NULL;\n"
        "  END P;\n"
        "END PKG_BANK;"
    )
    schema = Schema(name="HR", source_engine="Oracle", target_engine="PostgreSQL")
    schema.routines = [
        Routine(name="PKG_BANK", schema="HR", kind="PACKAGE", source=spec_source),
        Routine(name="PKG_BANK", schema="HR", kind="PACKAGE BODY", source=body_source),
    ]
    ddl_text, _ = ddl_generator.generate_schema_ddl(schema, "PostgreSQL")
    statements = split_sql_statements(ddl_text)
    real_statements = [s for s in statements if "CREATE" in s]
    # The PACKAGE spec's comment-only placeholder must have been flushed
    # and dropped as its own (empty) statement, not glued onto the front of
    # the PACKAGE BODY's CREATE PROCEDURE statement.
    assert len(real_statements) == 1
    assert "no Postgres equivalent" not in real_statements[0]


def test_non_oracle_routine_placeholder_with_begin_end_body_splits_cleanly():
    # Regression test for a real bug found while building the MongoDB
    # target: the non-Oracle-source MANUAL placeholder's own header text
    # ("...this tool's stored-routine converters...") contains an English
    # contraction apostrophe, and its /* ... */-wrapped original source is
    # commonly a real BEGIN...END block. Before sql_split.py learned about
    # "--" and "/* */" comments, that stray apostrophe desynced the quote
    # tracker for the rest of the document, and even once that was fixed,
    # the unprotected "/* */" wrapper let the BEGIN...END's internal ';'
    # split the placeholder into a dangling '*/'-only fragment. Neither may
    # happen: the placeholder must come through as exactly one statement,
    # and the table that follows it must not be swallowed or corrupted.
    routine = Routine(
        name="RAISE_SALARY",
        schema="HR",
        kind="PROCEDURE",
        # Db2, not MySQL: a MySQL source has had a real converter since
        # Round 20, so it no longer produces a placeholder to test with.
        source_engine="Db2",
        source="PROCEDURE raise_salary() BEGIN UPDATE t SET x = 1; END",
    )
    schema = Schema(name="HR", source_engine="Db2", target_engine="PostgreSQL")
    schema.routines = [routine]
    schema.tables = [_sample_table()]
    ddl_text, _ = ddl_generator.generate_schema_ddl(schema, "PostgreSQL")
    statements = split_sql_statements(ddl_text)
    assert not any(s.strip() == "*/" for s in statements)
    create_table_stmts = [s for s in statements if "CREATE TABLE" in s]
    assert len(create_table_stmts) == 1
    placeholder_stmts = [s for s in statements if "MANUAL CONVERSION REQUIRED" in s]
    assert len(placeholder_stmts) == 1
    assert "BEGIN UPDATE t SET x = 1; END" in placeholder_stmts[0]


def test_postgres_identifiers_are_lowercased_to_match_unquoted_references():
    # This is the exact bug that produced 'relation "customer" does not
    # exist': a view's SELECT text is copied through from Oracle mostly
    # verbatim, with unquoted references like "FROM CUSTOMER". PostgreSQL
    # folds any *unquoted* identifier to lowercase when parsing that FROM
    # clause, so our own quoted object names must already be lowercase or
    # they'll never match. Every _quote_pg caller must produce lowercase.
    table = Table(name="CUSTOMER", schema="HR",
                  columns=[Column(name="CUSTOMER_ID", data_type="NUMBER(9)")])
    table_ddl, _ = ddl_generator.generate_table_ddl_postgres(table)
    assert 'CREATE TABLE IF NOT EXISTS "customer"' in table_ddl
    assert '"CUSTOMER"' not in table_ddl  # never the original uppercase, quoted

    view = View(name="VW_CUSTOMER", schema="HR",
                definition="SELECT CUSTOMER_ID FROM CUSTOMER")
    view_ddl, _ = ddl_generator.generate_view_ddl(view, "PostgreSQL")
    assert 'CREATE OR REPLACE VIEW "vw_customer"' in view_ddl
    # the raw, unquoted "FROM CUSTOMER" is left as-is here — Postgres itself
    # folds it to lowercase at parse time, which is what makes it resolve
    # to our lowercase-quoted "customer" table above
    assert "FROM CUSTOMER" in view_ddl


# ---------------------------------------------------------- SQL Server target


def test_generate_table_ddl_sqlserver_contains_expected_types_and_guard():
    table = _sample_table()
    ddl, issues = ddl_generator.generate_table_ddl_sqlserver(table)
    assert "IF OBJECT_ID(N'[EMPLOYEES]', N'U') IS NULL" in ddl
    assert "BEGIN" in ddl and ddl.rstrip().endswith("END;")
    assert "INT" in ddl  # EMP_ID NUMBER(9)
    assert "VARCHAR(100)" in ddl
    assert "DATETIME2" in ddl  # HIRE_DATE
    assert "DECIMAL(10,2)" in ddl  # SALARY
    assert "IDENTITY(1,1)" in ddl
    assert 'PRIMARY KEY ([EMP_ID])' in ddl
    assert "IF NOT EXISTS (SELECT 1 FROM sys.indexes" in ddl


def test_generate_table_ddl_sqlserver_schema_qualified():
    table = _sample_table()
    ddl, issues = ddl_generator.generate_table_ddl_sqlserver(table, schema="dbo")
    assert "[dbo].[EMPLOYEES]" in ddl


def test_table_ddl_sqlserver_never_contains_inline_foreign_key():
    account = Table(
        name="ACCOUNT", schema="HR",
        columns=[Column(name="ACCOUNT_ID", data_type="NUMBER(9)", nullable=False),
                 Column(name="CUSTOMER_ID", data_type="NUMBER(9)", nullable=False)],
        constraints=[
            Constraint(name="PK_ACCOUNT", kind="PRIMARY KEY", columns=["ACCOUNT_ID"]),
            Constraint(name="FK_ACCOUNT_CUSTOMER", kind="FOREIGN KEY",
                       columns=["CUSTOMER_ID"], ref_table="CUSTOMER", ref_columns=["CUSTOMER_ID"]),
        ],
    )
    ddl, _ = ddl_generator.generate_table_ddl_sqlserver(account)
    assert "FOREIGN KEY" not in ddl


def test_foreign_key_ddl_sqlserver_wrapped_to_tolerate_rerun():
    account = Table(
        name="ACCOUNT", schema="HR",
        columns=[Column(name="CUSTOMER_ID", data_type="NUMBER(9)")],
        constraints=[
            Constraint(name="FK_ACCOUNT_CUSTOMER", kind="FOREIGN KEY",
                       columns=["CUSTOMER_ID"], ref_table="CUSTOMER", ref_columns=["CUSTOMER_ID"]),
        ],
    )
    fk = ddl_generator.generate_foreign_key_ddl_sqlserver(account)
    assert "IF NOT EXISTS (SELECT 1 FROM sys.foreign_keys WHERE name = 'FK_ACCOUNT_CUSTOMER')" in fk
    assert "ALTER TABLE [ACCOUNT] ADD CONSTRAINT [FK_ACCOUNT_CUSTOMER]" in fk
    assert "FOREIGN KEY ([CUSTOMER_ID]) REFERENCES [CUSTOMER] ([CUSTOMER_ID])" in fk


def test_sequence_ddl_sqlserver_is_native_and_guarded():
    seq = Sequence(name="EMP_SEQ", schema="HR", start_value=1, increment_by=1)
    ddl, issues = ddl_generator.generate_sequence_ddl_sqlserver(seq)
    assert "CREATE SEQUENCE [EMP_SEQ] AS BIGINT" in ddl
    assert "IF NOT EXISTS (SELECT 1 FROM sys.sequences WHERE name = 'EMP_SEQ')" in ddl
    assert issues == []


def test_sequence_ddl_sqlserver_clamps_oracle_default_maxvalue():
    seq = Sequence(
        name="EMP_SEQ", schema="HR", start_value=1, increment_by=1,
        min_value=1, max_value=9999999999999999999999999999,
    )
    ddl, issues = ddl_generator.generate_sequence_ddl_sqlserver(seq)
    assert "MAXVALUE" not in ddl
    assert "MINVALUE 1" in ddl
    assert any("BIGINT" in i.message for i in issues)


def test_view_ddl_sqlserver_uses_create_or_alter():
    view = View(name="VW_CUSTOMER", schema="HR", definition="SELECT CUSTOMER_ID FROM CUSTOMER")
    ddl, issues = ddl_generator.generate_view_ddl(view, "SQL Server")
    assert ddl.startswith("CREATE OR ALTER VIEW [VW_CUSTOMER]")


def test_view_ddl_sqlserver_schema_qualified():
    view = View(name="VW_CUSTOMER", schema="HR", definition="SELECT 1")
    ddl, issues = ddl_generator.generate_view_ddl(view, "SQL Server", schema="dbo")
    assert "[dbo].[VW_CUSTOMER]" in ddl


def test_generate_schema_ddl_sqlserver_end_to_end_and_dispatch():
    schema = Schema(name="HR", source_engine="Oracle", target_engine="SQL Server")
    schema.tables.append(_sample_table())
    schema.sequences.append(Sequence(name="EMP_SEQ", schema="HR"))
    schema.views.append(View(name="V1", schema="HR", definition="SELECT 1"))
    schema.routines.append(Routine(
        name="P1", schema="HR", kind="PROCEDURE",
        source="PROCEDURE p1 IS BEGIN NULL; END p1;",
    ))
    ddl_text, issues = ddl_generator.generate_schema_ddl(schema, "SQL Server", target_schema="dbo")
    assert "CREATE SEQUENCE [dbo].[EMP_SEQ]" in ddl_text
    assert "[dbo].[EMPLOYEES]" in ddl_text
    assert "CREATE OR ALTER VIEW [dbo].[V1]" in ddl_text
    assert "CREATE OR ALTER PROCEDURE [P1]" in ddl_text
    assert "EXEC('CREATE SCHEMA '" in ddl_text


def test_generate_schema_ddl_sqlserver_defers_fk_until_after_all_tables():
    account = Table(
        name="ACCOUNT", schema="HR",
        columns=[Column(name="ACCOUNT_ID", data_type="NUMBER(9)", nullable=False),
                 Column(name="CUSTOMER_ID", data_type="NUMBER(9)", nullable=False)],
        constraints=[
            Constraint(name="PK_ACCOUNT", kind="PRIMARY KEY", columns=["ACCOUNT_ID"]),
            Constraint(name="FK_ACCOUNT_CUSTOMER", kind="FOREIGN KEY",
                       columns=["CUSTOMER_ID"], ref_table="CUSTOMER", ref_columns=["CUSTOMER_ID"]),
        ],
    )
    customer = Table(
        name="CUSTOMER", schema="HR",
        columns=[Column(name="CUSTOMER_ID", data_type="NUMBER(9)", nullable=False)],
        constraints=[Constraint(name="PK_CUSTOMER", kind="PRIMARY KEY", columns=["CUSTOMER_ID"])],
    )
    schema = Schema(name="HR", source_engine="Oracle", target_engine="SQL Server")
    schema.tables = [account, customer]  # child listed before parent, on purpose

    ddl_text, issues = ddl_generator.generate_schema_ddl(schema, "SQL Server")

    create_account_pos = ddl_text.index("IF OBJECT_ID(N'[ACCOUNT]'")
    create_customer_pos = ddl_text.index("IF OBJECT_ID(N'[CUSTOMER]'")
    fk_pos = ddl_text.index("ADD CONSTRAINT [FK_ACCOUNT_CUSTOMER]")

    assert create_account_pos < fk_pos
    assert create_customer_pos < fk_pos


def test_generate_schema_ddl_postgres_and_mysql_unaffected_by_sqlserver_addition():
    # The is_postgres/is_sqlserver restructuring must not change a single
    # byte of output for the two pre-existing target engines.
    schema = Schema(name="HR", source_engine="Oracle", target_engine="PostgreSQL")
    schema.tables.append(_sample_table())
    schema.sequences.append(Sequence(name="EMP_SEQ", schema="HR"))
    pg_ddl, _ = ddl_generator.generate_schema_ddl(schema, "PostgreSQL")
    assert 'CREATE TABLE IF NOT EXISTS "employees"' in pg_ddl
    assert 'CREATE SEQUENCE IF NOT EXISTS "emp_seq"' in pg_ddl

    schema2 = Schema(name="HR", source_engine="Oracle", target_engine="MySQL")
    schema2.tables.append(_sample_table())
    my_ddl, _ = ddl_generator.generate_schema_ddl(schema2, "MySQL")
    assert "CREATE TABLE IF NOT EXISTS `EMPLOYEES`" in my_ddl


def test_generate_schema_ddl_sqlserver_end_to_end_with_row_loop_and_select_into():
    # Regression test for a real bug caught by an end-to-end smoke test: a
    # routine with a row/cursor FOR loop followed by an unrelated routine
    # with a genuine SELECT...INTO, run through the full generate_schema_ddl
    # -> split_sql_statements pipeline, must produce exactly one statement
    # per object with no scrambled/cross-contaminated SQL and no errors.
    schema = Schema(name="HR", source_engine="Oracle", target_engine="SQL Server")
    schema.tables.append(_sample_table())
    schema.routines.append(Routine(
        name="GET_TOTAL", schema="HR", kind="FUNCTION",
        source=(
            "FUNCTION GET_TOTAL (P_ID IN NUMBER) RETURN NUMBER IS\n"
            "    V_TOTAL NUMBER := 0;\n"
            "BEGIN\n"
            "    FOR REC IN (SELECT SALARY FROM EMPLOYEES WHERE EMP_ID = P_ID) LOOP\n"
            "        V_TOTAL := V_TOTAL + REC.SALARY;\n"
            "    END LOOP;\n"
            "    RETURN V_TOTAL;\n"
            "END GET_TOTAL;\n"
        ),
    ))
    schema.routines.append(Routine(
        name="GET_NAME", schema="HR", kind="PROCEDURE",
        source=(
            "PROCEDURE GET_NAME (P_ID IN NUMBER, P_NAME OUT VARCHAR2) IS\n"
            "BEGIN\n"
            "    SELECT NAME INTO P_NAME FROM EMPLOYEES WHERE EMP_ID = P_ID;\n"
            "END GET_NAME;\n"
        ),
    ))

    ddl_text, issues = ddl_generator.generate_schema_ddl(schema, "SQL Server")
    statements = split_sql_statements(ddl_text)
    assert any("CREATE TABLE" in s for s in statements)
    routine_statements = [s for s in statements if "CREATE OR ALTER" in s]
    assert len(routine_statements) == 2  # each routine its own statement, not merged together

    func_stmt = next(s for s in routine_statements if "GET_TOTAL" in s)
    assert func_stmt.index("OPEN [REC_cursor];") < func_stmt.index("WHILE @@FETCH_STATUS = 0")
    assert "RETURN @V_TOTAL;" in func_stmt

    proc_stmt = next(s for s in routine_statements if "GET_NAME" in s)
    assert "SELECT @P_NAME = NAME FROM EMPLOYEES WHERE EMP_ID = @P_ID;" in proc_stmt

    assert not any(i.severity == "error" for i in issues)


# ------------------------------------------------------------------- DB2 target


def test_generate_table_ddl_db2_contains_expected_types_and_guard():
    table = _sample_table()
    ddl, issues = ddl_generator.generate_table_ddl_db2(table)
    assert "SYSCAT.TABLES WHERE" in ddl and "TABNAME = 'EMPLOYEES'" in ddl
    assert "EXECUTE IMMEDIATE" in ddl
    assert ddl.rstrip().endswith("END;")
    assert "INTEGER" in ddl  # EMP_ID NUMBER(9)
    assert "VARCHAR(100)" in ddl
    assert "TIMESTAMP" in ddl  # HIRE_DATE
    assert "DECIMAL(10,2)" in ddl  # SALARY
    assert "GENERATED ALWAYS AS IDENTITY" in ddl
    assert 'PRIMARY KEY ("EMP_ID")' in ddl
    assert "SYSCAT.INDEXES WHERE" in ddl


def test_generate_table_ddl_db2_schema_qualified():
    table = _sample_table()
    ddl, issues = ddl_generator.generate_table_ddl_db2(table, schema="app")
    assert '"APP"."EMPLOYEES"' in ddl
    assert "TABSCHEMA = 'APP'" in ddl


def test_table_ddl_db2_never_contains_inline_foreign_key():
    account = Table(
        name="ACCOUNT", schema="HR",
        columns=[Column(name="ACCOUNT_ID", data_type="NUMBER(9)", nullable=False),
                 Column(name="CUSTOMER_ID", data_type="NUMBER(9)", nullable=False)],
        constraints=[
            Constraint(name="PK_ACCOUNT", kind="PRIMARY KEY", columns=["ACCOUNT_ID"]),
            Constraint(name="FK_ACCOUNT_CUSTOMER", kind="FOREIGN KEY",
                       columns=["CUSTOMER_ID"], ref_table="CUSTOMER", ref_columns=["CUSTOMER_ID"]),
        ],
    )
    ddl, _ = ddl_generator.generate_table_ddl_db2(account)
    assert "FOREIGN KEY" not in ddl


def test_foreign_key_ddl_db2_wrapped_to_tolerate_rerun():
    account = Table(
        name="ACCOUNT", schema="HR",
        columns=[Column(name="CUSTOMER_ID", data_type="NUMBER(9)")],
        constraints=[
            Constraint(name="FK_ACCOUNT_CUSTOMER", kind="FOREIGN KEY",
                       columns=["CUSTOMER_ID"], ref_table="CUSTOMER", ref_columns=["CUSTOMER_ID"]),
        ],
    )
    fk = ddl_generator.generate_foreign_key_ddl_db2(account)
    assert "SYSCAT.TABCONST WHERE" in fk and "CONSTNAME = 'FK_ACCOUNT_CUSTOMER'" in fk
    assert 'ALTER TABLE "ACCOUNT" ADD CONSTRAINT "FK_ACCOUNT_CUSTOMER"' in fk
    assert 'FOREIGN KEY ("CUSTOMER_ID") REFERENCES "CUSTOMER" ("CUSTOMER_ID")' in fk


def test_sequence_ddl_db2_is_native_and_guarded():
    seq = Sequence(name="EMP_SEQ", schema="HR", start_value=1, increment_by=1)
    ddl, issues = ddl_generator.generate_sequence_ddl_db2(seq)
    assert 'CREATE SEQUENCE "EMP_SEQ" AS BIGINT' in ddl
    assert "SYSCAT.SEQUENCES WHERE" in ddl and "SEQNAME = 'EMP_SEQ'" in ddl
    assert issues == []


def test_sequence_ddl_db2_clamps_oracle_default_maxvalue():
    seq = Sequence(
        name="EMP_SEQ", schema="HR", start_value=1, increment_by=1,
        min_value=1, max_value=9999999999999999999999999999,
    )
    ddl, issues = ddl_generator.generate_sequence_ddl_db2(seq)
    assert "MAXVALUE" not in ddl
    assert "MINVALUE 1" in ddl
    assert any("BIGINT" in i.message for i in issues)


def test_view_ddl_db2_uses_create_or_replace():
    view = View(name="VW_CUSTOMER", schema="HR", definition="SELECT CUSTOMER_ID FROM CUSTOMER")
    ddl, issues = ddl_generator.generate_view_ddl(view, "DB2")
    assert ddl.startswith('CREATE OR REPLACE VIEW "VW_CUSTOMER"')


def test_view_ddl_db2_schema_qualified():
    view = View(name="VW_CUSTOMER", schema="HR", definition="SELECT 1")
    ddl, issues = ddl_generator.generate_view_ddl(view, "DB2", schema="app")
    assert '"APP"."VW_CUSTOMER"' in ddl


def test_generate_schema_ddl_db2_end_to_end_and_dispatch():
    schema = Schema(name="HR", source_engine="Oracle", target_engine="DB2")
    schema.tables.append(_sample_table())
    schema.sequences.append(Sequence(name="EMP_SEQ", schema="HR"))
    schema.views.append(View(name="V1", schema="HR", definition="SELECT 1"))
    schema.routines.append(Routine(
        name="P1", schema="HR", kind="PROCEDURE",
        source="PROCEDURE p1 IS BEGIN NULL; END p1;",
    ))
    ddl_text, issues = ddl_generator.generate_schema_ddl(schema, "DB2", target_schema="app")
    assert 'CREATE SEQUENCE "APP"."EMP_SEQ"' in ddl_text
    assert '"APP"."EMPLOYEES"' in ddl_text
    assert 'CREATE OR REPLACE VIEW "APP"."V1"' in ddl_text
    assert 'CREATE OR REPLACE PROCEDURE "P1"' in ddl_text
    assert "SYSCAT.SCHEMATA WHERE SCHEMANAME = 'APP'" in ddl_text


def test_generate_schema_ddl_db2_defers_fk_until_after_all_tables():
    account = Table(
        name="ACCOUNT", schema="HR",
        columns=[Column(name="ACCOUNT_ID", data_type="NUMBER(9)", nullable=False),
                 Column(name="CUSTOMER_ID", data_type="NUMBER(9)", nullable=False)],
        constraints=[
            Constraint(name="PK_ACCOUNT", kind="PRIMARY KEY", columns=["ACCOUNT_ID"]),
            Constraint(name="FK_ACCOUNT_CUSTOMER", kind="FOREIGN KEY",
                       columns=["CUSTOMER_ID"], ref_table="CUSTOMER", ref_columns=["CUSTOMER_ID"]),
        ],
    )
    customer = Table(
        name="CUSTOMER", schema="HR",
        columns=[Column(name="CUSTOMER_ID", data_type="NUMBER(9)", nullable=False)],
        constraints=[Constraint(name="PK_CUSTOMER", kind="PRIMARY KEY", columns=["CUSTOMER_ID"])],
    )
    schema = Schema(name="HR", source_engine="Oracle", target_engine="DB2")
    schema.tables = [account, customer]  # child listed before parent, on purpose

    ddl_text, issues = ddl_generator.generate_schema_ddl(schema, "DB2")

    create_account_pos = ddl_text.index("TABNAME = 'ACCOUNT'")
    create_customer_pos = ddl_text.index("TABNAME = 'CUSTOMER'")
    fk_pos = ddl_text.index('ADD CONSTRAINT "FK_ACCOUNT_CUSTOMER"')

    assert create_account_pos < fk_pos
    assert create_customer_pos < fk_pos


def test_generate_schema_ddl_postgres_mysql_sqlserver_unaffected_by_db2_addition():
    # The is_postgres/is_sqlserver/is_db2 restructuring must not change a
    # single byte of output for the three pre-existing target engines.
    schema = Schema(name="HR", source_engine="Oracle", target_engine="PostgreSQL")
    schema.tables.append(_sample_table())
    schema.sequences.append(Sequence(name="EMP_SEQ", schema="HR"))
    pg_ddl, _ = ddl_generator.generate_schema_ddl(schema, "PostgreSQL")
    assert 'CREATE TABLE IF NOT EXISTS "employees"' in pg_ddl
    assert 'CREATE SEQUENCE IF NOT EXISTS "emp_seq"' in pg_ddl

    schema2 = Schema(name="HR", source_engine="Oracle", target_engine="MySQL")
    schema2.tables.append(_sample_table())
    my_ddl, _ = ddl_generator.generate_schema_ddl(schema2, "MySQL")
    assert "CREATE TABLE IF NOT EXISTS `EMPLOYEES`" in my_ddl

    schema3 = Schema(name="HR", source_engine="Oracle", target_engine="SQL Server")
    schema3.tables.append(_sample_table())
    sql_ddl, _ = ddl_generator.generate_schema_ddl(schema3, "SQL Server", target_schema="dbo")
    assert "IF OBJECT_ID(N'[dbo].[EMPLOYEES]', N'U') IS NULL" in sql_ddl


def test_generate_schema_ddl_db2_end_to_end_with_row_loop_and_select_into():
    # Regression test mirroring the SQL Server one: a routine with a
    # row/cursor FOR loop followed by an unrelated routine with a genuine
    # SELECT...INTO, run through the full generate_schema_ddl ->
    # split_sql_statements pipeline, must produce exactly one statement per
    # object with no scrambled/cross-contaminated SQL and no errors. This
    # specifically exercises the "END FOR" bare-END false-positive fix in
    # both db2_converter.py's own block scanners and sql_split.py.
    schema = Schema(name="HR", source_engine="Oracle", target_engine="DB2")
    schema.tables.append(_sample_table())
    schema.routines.append(Routine(
        name="GET_TOTAL", schema="HR", kind="FUNCTION",
        source=(
            "FUNCTION GET_TOTAL (P_ID IN NUMBER) RETURN NUMBER IS\n"
            "    V_TOTAL NUMBER := 0;\n"
            "BEGIN\n"
            "    FOR REC IN (SELECT SALARY FROM EMPLOYEES WHERE EMP_ID = P_ID) LOOP\n"
            "        V_TOTAL := V_TOTAL + REC.SALARY;\n"
            "    END LOOP;\n"
            "    RETURN V_TOTAL;\n"
            "END GET_TOTAL;\n"
        ),
    ))
    schema.routines.append(Routine(
        name="GET_NAME", schema="HR", kind="PROCEDURE",
        source=(
            "PROCEDURE GET_NAME (P_ID IN NUMBER, P_NAME OUT VARCHAR2) IS\n"
            "BEGIN\n"
            "    SELECT NAME INTO P_NAME FROM EMPLOYEES WHERE EMP_ID = P_ID;\n"
            "END GET_NAME;\n"
        ),
    ))

    ddl_text, issues = ddl_generator.generate_schema_ddl(schema, "DB2")
    statements = split_sql_statements(ddl_text)
    assert any("SYSCAT.TABLES" in s for s in statements)
    routine_statements = [s for s in statements if "CREATE OR REPLACE" in s and ("PROCEDURE" in s or "FUNCTION" in s)]
    assert len(routine_statements) == 2  # each routine its own statement, not merged together

    func_stmt = next(s for s in routine_statements if "GET_TOTAL" in s)
    assert "FOR REC AS REC_cur CURSOR FOR" in func_stmt
    assert "END FOR" in func_stmt
    assert "RETURN V_TOTAL;" in func_stmt

    proc_stmt = next(s for s in routine_statements if "GET_NAME" in s)
    # Db2 supports SELECT ... INTO natively -- must survive unrewritten
    assert "SELECT NAME INTO P_NAME FROM EMPLOYEES WHERE EMP_ID = P_ID;" in proc_stmt

    assert not any(i.severity == "error" for i in issues)


# -------------------------------------------------------------------- MongoDB


def test_generate_table_ddl_mongodb_contains_expected_bson_types_and_validator():
    table = _sample_table()
    ddl, issues = ddl_generator.generate_table_ddl_mongodb(table)
    assert 'db.createCollection("EMPLOYEES"' in ddl
    assert '"$jsonSchema"' in ddl
    assert '"EMP_ID"' in ddl and '"bsonType": "int"' in ddl
    assert '"NAME"' in ddl and '"bsonType": "string"' in ddl
    assert '"HIRE_DATE"' in ddl and '"bsonType": "date"' in ddl
    assert '"SALARY"' in ddl and '"bsonType": "decimal"' in ddl
    assert '"required"' in ddl
    assert '"EMP_ID"' in ddl  # required (NOT NULL)


def test_generate_table_ddl_mongodb_pk_and_index_create_index_calls():
    table = _sample_table()
    ddl, _ = ddl_generator.generate_table_ddl_mongodb(table)
    assert 'db["EMPLOYEES"].createIndex({"EMP_ID": 1}' in ddl
    assert '"unique": true' in ddl and '"name": "PK_EMPLOYEES"' in ddl
    assert 'db["EMPLOYEES"].createIndex({"NAME": 1}' in ddl
    assert '"name": "IDX_EMP_NAME"' in ddl


def test_generate_table_ddl_mongodb_identity_and_default_flagged_info():
    table = Table(
        name="T1", schema="HR",
        columns=[Column(name="ID", data_type="NUMBER(9)", nullable=False, identity=True),
                 Column(name="STATUS", data_type="VARCHAR2(20)", nullable=True, default="'ACTIVE'")],
    )
    ddl, issues = ddl_generator.generate_table_ddl_mongodb(table)
    assert any("auto-increment" in i.message for i in issues)
    assert any("DEFAULT" in i.message for i in issues)


def test_check_constraint_numeric_translates_to_mongo_bounds():
    table = Table(
        name="T1", schema="HR",
        columns=[Column(name="SALARY", data_type="NUMBER(10,2)", nullable=True)],
        constraints=[Constraint(name="CK_SALARY", kind="CHECK", check_condition="salary > 0")],
    )
    ddl, issues = ddl_generator.generate_table_ddl_mongodb(table)
    # lowercase 'salary' in the CHECK text must still match the uppercase
    # SALARY column -- see _find_property_case_insensitive.
    assert '"exclusiveMinimum": 0' in ddl
    assert not any("could not be automatically translated" in i.message for i in issues)


def test_check_constraint_in_list_translates_to_mongo_enum():
    table = Table(
        name="T1", schema="HR",
        columns=[Column(name="STATUS", data_type="VARCHAR2(20)", nullable=True)],
        constraints=[Constraint(
            name="CK_STATUS", kind="CHECK",
            check_condition="status IN ('ACTIVE','INACTIVE')",
        )],
    )
    ddl, issues = ddl_generator.generate_table_ddl_mongodb(table)
    assert '"enum"' in ddl and "ACTIVE" in ddl and "INACTIVE" in ddl


def test_check_constraint_unrecognized_shape_flags_warning():
    table = Table(
        name="T1", schema="HR",
        columns=[Column(name="AMOUNT", data_type="NUMBER(10,2)", nullable=True)],
        constraints=[Constraint(
            name="CK_AMOUNT", kind="CHECK",
            check_condition="MOD(amount, 5) = 0",
        )],
    )
    ddl, issues = ddl_generator.generate_table_ddl_mongodb(table)
    assert any("could not be automatically translated" in i.message for i in issues)


def test_generate_table_ddl_mongodb_foreign_key_flags_warning_not_enforced():
    account = Table(
        name="ACCOUNT", schema="HR",
        columns=[Column(name="ACCOUNT_ID", data_type="NUMBER(9)", nullable=False),
                 Column(name="CUSTOMER_ID", data_type="NUMBER(9)", nullable=False)],
        constraints=[
            Constraint(name="PK_ACCOUNT", kind="PRIMARY KEY", columns=["ACCOUNT_ID"]),
            Constraint(name="FK_ACCOUNT_CUSTOMER", kind="FOREIGN KEY",
                       columns=["CUSTOMER_ID"], ref_table="CUSTOMER", ref_columns=["CUSTOMER_ID"]),
        ],
    )
    ddl, issues = ddl_generator.generate_table_ddl_mongodb(account)
    assert "FOREIGN KEY" not in ddl  # never a real constraint in the collection DDL itself
    assert any("no enforcement in MongoDB" in i.message for i in issues)

    fk_comment = ddl_generator.generate_foreign_key_ddl_mongodb(account)
    assert fk_comment.startswith("-- NOTE:")
    assert "ACCOUNT.CUSTOMER_ID references CUSTOMER.CUSTOMER_ID" in fk_comment
    assert "db." not in fk_comment  # documentation-only, nothing for execute_ddl to parse


def test_generate_sequence_ddl_mongodb_uses_counters_collection():
    seq = Sequence(name="EMP_SEQ", schema="HR", start_value=1, increment_by=1)
    ddl, issues = ddl_generator.generate_sequence_ddl_mongodb(seq)
    assert 'db.createCollection("counters")' in ddl
    assert 'db["counters"].updateOne({"_id": "EMP_SEQ"}' in ddl
    assert '"$setOnInsert": {"seq": 1}' in ddl
    assert '"upsert": true' in ddl
    assert any(i.severity == "warning" for i in issues)


def test_generate_view_ddl_mongodb_is_always_manual_placeholder():
    view = View(name="V_ACTIVE_EMP", schema="HR", definition="SELECT * FROM EMPLOYEES WHERE ACTIVE = 1")
    ddl, issues = ddl_generator.generate_view_ddl(view, "MongoDB")
    assert "MANUAL CONVERSION REQUIRED for VIEW V_ACTIVE_EMP" in ddl
    assert "/*" in ddl and "*/" in ddl
    assert "SELECT * FROM EMPLOYEES" in ddl
    assert any(i.severity == "error" for i in issues)


def test_generate_view_ddl_mongodb_source_is_manual_regardless_of_target():
    # A MongoDB-sourced view's `definition` is a JSON aggregation pipeline,
    # not SQL -- this must short-circuit to MANUAL unconditionally, even
    # for a *SQL* target where the normal SQL-text heuristics below would
    # otherwise run against text that was never SQL in the first place.
    view = View(
        name="V_RECENT_ORDERS", schema="APP",
        definition='{"viewOn": "orders", "pipeline": [{"$match": {"status": "SYSDATE"}}]}',
        source_engine="MongoDB",
    )
    ddl, issues = ddl_generator.generate_view_ddl(view, "PostgreSQL")
    assert "MANUAL CONVERSION REQUIRED for VIEW V_RECENT_ORDERS" in ddl
    assert "aggregation pipeline" in ddl
    assert any(i.severity == "error" for i in issues)
    assert view.status == ConversionStatus.MANUAL


def test_generate_view_ddl_mongodb_source_does_not_run_connect_by_rewrite():
    # The pipeline text below contains "CONNECT BY" only incidentally (as
    # if it were literal JSON string content) -- if the MongoDB-source
    # guard didn't short-circuit before the CONNECT BY rewrite/Oracle
    # marker scan, this could misfire against non-SQL text.
    view = View(
        name="V1", schema="APP", definition='{"pipeline": [{"$match": {"note": "CONNECT BY x"}}]}',
        source_engine="MongoDB",
    )
    ddl, issues = ddl_generator.generate_view_ddl(view, "PostgreSQL")
    assert "MANUAL CONVERSION REQUIRED" in ddl
    assert "CONNECT BY x" in ddl  # preserved verbatim inside the /* ... */ block, not rewritten


def test_generate_view_ddl_default_source_engine_is_unaffected():
    # Every existing caller that builds a View without passing
    # source_engine keeps working exactly as before (Oracle source, full
    # SQL-text heuristics still run rather than short-circuiting to the
    # MongoDB-source MANUAL placeholder).
    # Uses the outer-join marker (+) rather than ROWNUM here: ROWNUM's
    # top-N idiom is now actually rewritten to LIMIT (see
    # test_postgres_sql_expression_gaps.py), so it no longer proves the
    # SQL-text heuristics ran at all -- (+) still does, since it stays
    # flagged rather than guessed at.
    view = View(name="V1", schema="APP", definition="SELECT * FROM T a, U b WHERE a.id = b.id(+)")
    assert view.source_engine == "Oracle"
    ddl, issues = ddl_generator.generate_view_ddl(view, "PostgreSQL")
    assert "CREATE OR REPLACE VIEW" in ddl
    assert "MANUAL CONVERSION REQUIRED" not in ddl
    assert any("(+)" in i.message for i in issues)


def test_generate_schema_ddl_mongodb_end_to_end_dispatch():
    schema = Schema(name="HR", source_engine="Oracle", target_engine="MongoDB")
    schema.tables.append(_sample_table())
    schema.sequences.append(Sequence(name="EMP_SEQ", schema="HR"))
    schema.views.append(View(name="V1", schema="HR", definition="SELECT 1"))
    schema.routines.append(Routine(
        name="P1", schema="HR", kind="PROCEDURE",
        source="PROCEDURE p1 IS BEGIN NULL; END p1;",
    ))
    ddl_text, issues = ddl_generator.generate_schema_ddl(schema, "MongoDB")
    assert 'db.createCollection("EMPLOYEES"' in ddl_text
    assert 'db["counters"].updateOne({"_id": "EMP_SEQ"}' in ddl_text
    assert "MANUAL CONVERSION REQUIRED for VIEW V1" in ddl_text
    assert "MANUAL CONVERSION REQUIRED for PROCEDURE P1" in ddl_text


def test_generate_schema_ddl_mongodb_placeholder_with_begin_end_splits_cleanly():
    # Regression test: a routine whose original source is a real
    # BEGIN...END block, wrapped in the MongoDB-always-manual placeholder's
    # /* ... */, must come through split_sql_statements as exactly one
    # statement per object -- no dangling '*/' fragment, no swallowed
    # semicolons, no merge with the table that follows it. See sql_split.py's
    # module docstring and this file's
    # test_non_oracle_routine_placeholder_with_begin_end_body_splits_cleanly
    # for the underlying bug this exercises.
    schema = Schema(name="HR", source_engine="Oracle", target_engine="MongoDB")
    schema.routines.append(Routine(
        name="RAISE_SALARY", schema="HR", kind="PROCEDURE",
        source="PROCEDURE raise_salary IS BEGIN UPDATE employees SET salary = salary * 1.1; END raise_salary;",
    ))
    schema.tables.append(_sample_table())
    ddl_text, _ = ddl_generator.generate_schema_ddl(schema, "MongoDB")
    statements = split_sql_statements(ddl_text)
    assert not any(s.strip() == "*/" for s in statements)
    assert len([s for s in statements if 'db.createCollection("EMPLOYEES"' in s]) == 1
    placeholder = next(s for s in statements if "MANUAL CONVERSION REQUIRED for PROCEDURE" in s)
    assert "BEGIN UPDATE employees SET salary = salary * 1.1; END raise_salary;" in placeholder


def test_generate_schema_ddl_postgres_mysql_sqlserver_db2_unaffected_by_mongodb_addition():
    schema = Schema(name="HR", source_engine="Oracle", target_engine="PostgreSQL")
    schema.tables.append(_sample_table())
    pg_ddl, _ = ddl_generator.generate_schema_ddl(schema, "PostgreSQL")
    assert 'CREATE TABLE IF NOT EXISTS "employees"' in pg_ddl

    schema2 = Schema(name="HR", source_engine="Oracle", target_engine="DB2")
    schema2.tables.append(_sample_table())
    db2_ddl, _ = ddl_generator.generate_schema_ddl(schema2, "DB2")
    assert "SYSCAT.TABLES WHERE" in db2_ddl


# --------------------------------------------- CONNECT BY view rewriting

_HIERARCHICAL_VIEW_SQL = (
    "SELECT employee_id, manager_id, last_name\n"
    "FROM employees\n"
    "START WITH manager_id IS NULL\n"
    "CONNECT BY PRIOR employee_id = manager_id"
)


def test_view_ddl_postgres_rewrites_connect_by_and_drops_the_manual_marker():
    view = View(name="VW_ORG_CHART", schema="HR", definition=_HIERARCHICAL_VIEW_SQL)
    ddl, issues = ddl_generator.generate_view_ddl(view, "PostgreSQL")
    assert "WITH RECURSIVE cb_cte_1" in ddl
    assert "CONNECT BY" not in ddl.upper()
    # the unconditional "must be rewritten by hand" marker no longer fires,
    # since the literal CONNECT BY text is gone by the time it's scanned
    assert not any("has no direct equivalent" in i.message for i in issues)
    assert any(i.severity == "info" and "recursive CTE" in i.message for i in issues)
    # an info-only issue list never degrades status to MANUAL
    assert not any(i.severity in ("warning", "error") for i in issues)


def test_view_ddl_sqlserver_and_db2_use_plain_with_keyword():
    view_sqlserver = View(name="VW_ORG_CHART", schema="HR", definition=_HIERARCHICAL_VIEW_SQL)
    ddl_sqlserver, _ = ddl_generator.generate_view_ddl(view_sqlserver, "SQL Server")
    assert "WITH cb_cte_1" in ddl_sqlserver
    assert "WITH RECURSIVE" not in ddl_sqlserver

    view_db2 = View(name="VW_ORG_CHART", schema="HR", definition=_HIERARCHICAL_VIEW_SQL)
    ddl_db2, _ = ddl_generator.generate_view_ddl(view_db2, "DB2")
    assert "WITH cb_cte_1" in ddl_db2
    assert "WITH RECURSIVE" not in ddl_db2


def test_view_ddl_mysql_uses_with_recursive_too():
    view = View(name="VW_ORG_CHART", schema="HR", definition=_HIERARCHICAL_VIEW_SQL)
    ddl, _ = ddl_generator.generate_view_ddl(view, "MySQL")
    assert "WITH RECURSIVE cb_cte_1" in ddl


def test_view_ddl_connect_by_falls_back_to_manual_marker_when_unsupported():
    # SELECT * disqualifies it from the auto-rewrite (see connect_by_rewriter.py)
    view = View(
        name="VW_ORG_CHART", schema="HR",
        definition="SELECT * FROM employees START WITH manager_id IS NULL CONNECT BY PRIOR employee_id = manager_id",
    )
    ddl, issues = ddl_generator.generate_view_ddl(view, "PostgreSQL")
    assert "WITH RECURSIVE" not in ddl
    assert "CONNECT BY" in ddl.upper()
    assert any("has no direct equivalent" in i.message for i in issues)


def _mysql_sourced_table() -> Table:
    # Mimics what mysql_introspector.introspect_schema leaves on a column:
    # source_issues populated from type_mapping.from_mysql, col.issues still
    # empty because target-side DDL generation hasn't run yet.
    col = Column(name="STATUS", data_type="VARCHAR2(1)", nullable=True)
    col.source_issues = [ConversionIssue("warning", "MySQL ENUM has no Oracle/target equivalent; mapped to VARCHAR2 and the value list constraint was dropped.")]
    return Table(name="ORDERS", schema="APP", columns=[col])


def test_source_issues_are_carried_into_col_issues_on_first_ddl_generation():
    table = _mysql_sourced_table()
    _, issues = ddl_generator.generate_table_ddl_postgres(table)
    col = table.columns[0]
    assert any("MySQL ENUM" in i.message for i in col.issues)
    assert any("MySQL ENUM" in i.message for i in issues)


# -------------------------------------------------------------------- oracle


def test_generate_table_ddl_oracle_contains_expected_types_and_guard():
    table = _sample_table()
    ddl, issues = ddl_generator.generate_table_ddl_oracle(table)
    assert "EXECUTE IMMEDIATE" in ddl
    assert "EXCEPTION" in ddl and "SQLCODE != -955" in ddl
    assert ddl.rstrip().endswith("END;")
    # Unlike every other target, the pivot types pass through unchanged.
    assert "NUMBER(9)" in ddl  # EMP_ID
    assert "VARCHAR2(100)" in ddl  # NAME
    assert "DATE" in ddl  # HIRE_DATE
    assert "NUMBER(10,2)" in ddl  # SALARY
    assert "GENERATED ALWAYS AS IDENTITY" in ddl
    assert 'PRIMARY KEY ("EMP_ID")' in ddl
    assert issues == []


def test_generate_table_ddl_oracle_schema_qualified():
    table = _sample_table()
    ddl, _issues = ddl_generator.generate_table_ddl_oracle(table, schema="app")
    assert '"APP"."EMPLOYEES"' in ddl


def test_generate_table_ddl_oracle_creates_guarded_index():
    table = _sample_table()
    ddl, _issues = ddl_generator.generate_table_ddl_oracle(table)
    assert 'CREATE INDEX "IDX_EMP_NAME" ON "EMPLOYEES" ("NAME")' in ddl


def test_table_ddl_oracle_never_contains_inline_foreign_key():
    account = Table(
        name="ACCOUNT", schema="HR",
        columns=[Column(name="ACCOUNT_ID", data_type="NUMBER(9)", nullable=False),
                 Column(name="CUSTOMER_ID", data_type="NUMBER(9)", nullable=False)],
        constraints=[
            Constraint(name="PK_ACCOUNT", kind="PRIMARY KEY", columns=["ACCOUNT_ID"]),
            Constraint(name="FK_ACCOUNT_CUSTOMER", kind="FOREIGN KEY",
                       columns=["CUSTOMER_ID"], ref_table="CUSTOMER", ref_columns=["CUSTOMER_ID"]),
        ],
    )
    ddl, _ = ddl_generator.generate_table_ddl_oracle(account)
    assert "FOREIGN KEY" not in ddl


def test_foreign_key_ddl_oracle_wrapped_to_tolerate_rerun():
    account = Table(
        name="ACCOUNT", schema="HR",
        columns=[Column(name="CUSTOMER_ID", data_type="NUMBER(9)")],
        constraints=[
            Constraint(name="FK_ACCOUNT_CUSTOMER", kind="FOREIGN KEY",
                       columns=["CUSTOMER_ID"], ref_table="CUSTOMER", ref_columns=["CUSTOMER_ID"]),
        ],
    )
    fk = ddl_generator.generate_foreign_key_ddl_oracle(account)
    assert "SQLCODE != -2264 AND SQLCODE != -2275" in fk
    assert 'ALTER TABLE "ACCOUNT" ADD CONSTRAINT "FK_ACCOUNT_CUSTOMER"' in fk
    assert 'FOREIGN KEY ("CUSTOMER_ID") REFERENCES "CUSTOMER" ("CUSTOMER_ID")' in fk


def test_sequence_ddl_oracle_is_native_and_guarded():
    seq = Sequence(name="EMP_SEQ", schema="HR", start_value=1, increment_by=1)
    ddl, issues = ddl_generator.generate_sequence_ddl_oracle(seq)
    assert 'CREATE SEQUENCE "EMP_SEQ" START WITH 1 INCREMENT BY 1' in ddl
    assert "NOCYCLE" in ddl
    assert "EXECUTE IMMEDIATE" in ddl and "SQLCODE != -955" in ddl
    assert issues == []


def test_sequence_ddl_oracle_never_clamps_large_minmax():
    # Unlike Postgres/SQL Server/Db2 above, Oracle's own sequence range
    # (28-digit precision) never needs the bigint-range clamping those
    # three apply -- Oracle's own huge default MAXVALUE should pass
    # straight through unmodified.
    seq = Sequence(
        name="EMP_SEQ", schema="HR", start_value=1, increment_by=1,
        min_value=1, max_value=9999999999999999999999999999,
    )
    ddl, issues = ddl_generator.generate_sequence_ddl_oracle(seq)
    assert "MAXVALUE 9999999999999999999999999999" in ddl
    assert "MINVALUE 1" in ddl
    assert issues == []


def test_sequence_ddl_oracle_cycle():
    seq = Sequence(name="EMP_SEQ", schema="HR", cycle=True)
    ddl, _issues = ddl_generator.generate_sequence_ddl_oracle(seq)
    assert " CYCLE" in ddl and "NOCYCLE" not in ddl


def test_view_ddl_oracle_source_is_passthrough_automatic():
    view = View(name="VW_CUSTOMER", schema="HR", definition="SELECT CUSTOMER_ID FROM CUSTOMER")
    assert view.source_engine == "Oracle"  # default
    ddl, issues = ddl_generator.generate_view_ddl(view, "Oracle")
    assert ddl == 'CREATE OR REPLACE VIEW "VW_CUSTOMER" AS\nSELECT CUSTOMER_ID FROM CUSTOMER;'
    assert issues == []
    assert view.status == ConversionStatus.AUTOMATIC


def test_view_ddl_oracle_source_passthrough_preserves_connect_by_and_rownum_unrewritten():
    # These ARE valid, idiomatic Oracle syntax -- an Oracle target must
    # not run the CONNECT-BY-rewrite/oracle_markers scan against them the
    # way every non-Oracle SQL target does.
    view = View(
        name="VW_ORG_CHART", schema="HR",
        definition="SELECT employee_id FROM employees WHERE ROWNUM <= 10 "
                   "START WITH manager_id IS NULL CONNECT BY PRIOR employee_id = manager_id",
    )
    ddl, issues = ddl_generator.generate_view_ddl(view, "Oracle")
    assert "CONNECT BY PRIOR employee_id = manager_id" in ddl
    assert "ROWNUM <= 10" in ddl
    assert issues == []
    assert view.status == ConversionStatus.AUTOMATIC


def test_view_ddl_oracle_schema_qualified():
    view = View(name="VW_CUSTOMER", schema="HR", definition="SELECT 1")
    ddl, _issues = ddl_generator.generate_view_ddl(view, "Oracle", schema="app")
    assert '"APP"."VW_CUSTOMER"' in ddl


def test_view_ddl_oracle_non_oracle_source_is_flagged_manual():
    view = View(
        name="VW_CUSTOMER", schema="HR", definition="SELECT customer_id FROM customer LIMIT 10",
        source_engine="MySQL",
    )
    ddl, issues = ddl_generator.generate_view_ddl(view, "Oracle")
    assert "MANUAL CONVERSION REQUIRED for VIEW VW_CUSTOMER" in ddl
    assert "Source engine is MySQL, not Oracle" in ddl
    assert "SELECT customer_id FROM customer LIMIT 10" in ddl
    assert any(i.severity == "error" for i in issues)
    assert view.status == ConversionStatus.MANUAL


def test_generate_schema_ddl_oracle_end_to_end_and_dispatch():
    schema = Schema(name="HR", source_engine="Oracle", target_engine="Oracle")
    schema.tables.append(_sample_table())
    schema.sequences.append(Sequence(name="EMP_SEQ", schema="HR"))
    schema.views.append(View(name="V1", schema="HR", definition="SELECT 1"))
    schema.routines.append(Routine(
        name="P1", schema="HR", kind="PROCEDURE",
        source="PROCEDURE p1 IS BEGIN NULL; END p1;",
    ))
    ddl_text, issues = ddl_generator.generate_schema_ddl(schema, "Oracle", target_schema="app")
    assert 'CREATE SEQUENCE "APP"."EMP_SEQ"' in ddl_text
    assert '"APP"."EMPLOYEES"' in ddl_text
    assert 'CREATE OR REPLACE VIEW "APP"."V1"' in ddl_text
    assert "CREATE OR REPLACE PROCEDURE p1 IS BEGIN NULL; END p1;" in ddl_text
    # Oracle has no CREATE SCHEMA statement -- unlike Postgres/SQL Server/
    # Db2's own end-to-end dispatch tests, target_schema must only ever
    # appear as a qualifier, never as a leading CREATE SCHEMA/EXEC/
    # SYSCAT.SCHEMATA-guarded statement.
    assert "CREATE SCHEMA" not in ddl_text
    assert "sys.schemas" not in ddl_text
    assert "SYSCAT.SCHEMATA" not in ddl_text


def test_generate_schema_ddl_oracle_defers_fk_until_after_all_tables():
    account = Table(
        name="ACCOUNT", schema="HR",
        columns=[Column(name="ACCOUNT_ID", data_type="NUMBER(9)", nullable=False),
                 Column(name="CUSTOMER_ID", data_type="NUMBER(9)", nullable=False)],
        constraints=[
            Constraint(name="PK_ACCOUNT", kind="PRIMARY KEY", columns=["ACCOUNT_ID"]),
            Constraint(name="FK_ACCOUNT_CUSTOMER", kind="FOREIGN KEY",
                       columns=["CUSTOMER_ID"], ref_table="CUSTOMER", ref_columns=["CUSTOMER_ID"]),
        ],
    )
    schema = Schema(name="HR", source_engine="Oracle", target_engine="Oracle")
    schema.tables.append(account)
    ddl_text, _issues = ddl_generator.generate_schema_ddl(schema, "Oracle")
    tables_idx = ddl_text.index("-- Tables")
    fk_idx = ddl_text.index("-- Foreign Keys")
    assert tables_idx < fk_idx
    assert ddl_text.index('ADD CONSTRAINT "FK_ACCOUNT_CUSTOMER"') > fk_idx


def test_generate_schema_ddl_postgres_mysql_sqlserver_db2_mongodb_unaffected_by_oracle_addition():
    schema = Schema(name="HR", source_engine="Oracle", target_engine="PostgreSQL")
    schema.tables.append(_sample_table())
    for target in ("PostgreSQL", "MySQL", "SQL Server", "DB2", "MongoDB"):
        ddl_text, _issues = ddl_generator.generate_schema_ddl(schema, target)
        assert ddl_text  # still produces output; no exception, no cross-talk from the new Oracle branch


def test_source_issues_are_not_duplicated_across_repeated_ddl_generation_calls():
    table = _mysql_sourced_table()
    ddl_generator.generate_table_ddl_postgres(table)
    ddl_generator.generate_table_ddl_postgres(table)
    col = table.columns[0]
    mysql_enum_issues = [i for i in col.issues if "MySQL ENUM" in i.message]
    assert len(mysql_enum_issues) == 1


def test_source_issues_survive_switching_target_engine():
    table = _mysql_sourced_table()
    ddl_generator.generate_table_ddl_postgres(table)
    _, mysql_issues = ddl_generator.generate_table_ddl_mysql(table)
    col = table.columns[0]
    assert any("MySQL ENUM" in i.message for i in col.issues)
    assert any("MySQL ENUM" in i.message for i in mysql_issues)
    mysql_enum_issues = [i for i in col.issues if "MySQL ENUM" in i.message]
    assert len(mysql_enum_issues) == 1


def test_oracle_sourced_table_has_no_source_issues_merge_effect():
    # An Oracle-sourced column always has an empty source_issues list, so the
    # merge is a no-op and behavior for existing Oracle-source schemas is
    # unchanged.
    table = _sample_table()
    _, issues = ddl_generator.generate_table_ddl_postgres(table)
    for col in table.columns:
        assert col.source_issues == []


# --------------------------------------------------------------- rollback script


def _fk_table(name, fk_to=None):
    constraints = [Constraint(name=f"PK_{name}", kind="PRIMARY KEY", columns=[f"{name}_ID"])]
    if fk_to:
        constraints.append(Constraint(
            name=f"FK_{name}_{fk_to}", kind="FOREIGN KEY",
            columns=[f"{fk_to}_ID"], ref_table=fk_to, ref_columns=[f"{fk_to}_ID"],
        ))
    return Table(
        name=name, schema="HR",
        columns=[Column(name=f"{name}_ID", data_type="NUMBER")],
        constraints=constraints,
    )


def _rollback_schema() -> Schema:
    account = _fk_table("ACCOUNT", fk_to="CUSTOMER")
    customer = _fk_table("CUSTOMER")
    view = View(name="VW_ACCOUNT", schema="HR", definition="SELECT * FROM ACCOUNT", source_engine="Oracle")
    seq = Sequence(name="ACCOUNT_SEQ", schema="HR", start_value=1, increment_by=1)
    proc = Routine(name="RECALC_BALANCE", schema="HR", kind="PROCEDURE", source="PROCEDURE RECALC_BALANCE IS BEGIN NULL; END;")
    func = Routine(name="GET_BALANCE", schema="HR", kind="FUNCTION", source="FUNCTION GET_BALANCE RETURN NUMBER IS BEGIN RETURN 0; END;")
    trig = Routine(
        name="TRG_ACCOUNT_AUDIT", schema="HR", kind="TRIGGER", table_name="ACCOUNT",
        timing="AFTER", events=["INSERT"], row_level=True,
        source="BEGIN NULL; END;",
    )
    pkg_body = Routine(name="PKG_BANK", schema="HR", kind="PACKAGE BODY", source="PACKAGE BODY PKG_BANK IS END PKG_BANK;")
    return Schema(
        name="HR", tables=[account, customer], views=[view], sequences=[seq],
        routines=[proc, func, trig, pkg_body],
    )


def test_generate_rollback_ddl_postgres_drops_everything_with_if_exists():
    schema = _rollback_schema()
    rollback = ddl_generator.generate_rollback_ddl(schema, "PostgreSQL")
    assert 'DROP TABLE IF EXISTS "account" CASCADE;' in rollback
    assert 'DROP TABLE IF EXISTS "customer" CASCADE;' in rollback
    assert 'DROP VIEW IF EXISTS "vw_account" CASCADE;' in rollback
    assert 'DROP SEQUENCE IF EXISTS "account_seq";' in rollback
    assert 'DROP PROCEDURE IF EXISTS "recalc_balance" CASCADE;' in rollback
    assert 'DROP FUNCTION IF EXISTS "get_balance" CASCADE;' in rollback
    assert 'DROP TRIGGER IF EXISTS "trg_account_audit" ON "account";' in rollback


def test_generate_rollback_ddl_postgres_drops_a_materialized_view_with_the_right_verb():
    # Confirmed directly against a live PostgreSQL 16 server: DROP VIEW IF
    # EXISTS on a materialized view errors outright ("\"x\" is not a
    # view") -- IF EXISTS does not save it, because the object it finds is
    # the wrong *kind*, not merely present-or-absent.
    schema = Schema(name="HR", views=[View(
        name="ACTIVE_EMP_MV", schema="HR", definition="SELECT 1", source_engine="Oracle",
        is_materialized=True,
    )])
    rollback = ddl_generator.generate_rollback_ddl(schema, "PostgreSQL")
    assert 'DROP MATERIALIZED VIEW IF EXISTS "active_emp_mv" CASCADE;' in rollback
    assert 'DROP VIEW IF EXISTS "active_emp_mv"' not in rollback


def test_generate_rollback_ddl_drops_child_table_before_parent():
    schema = _rollback_schema()
    rollback = ddl_generator.generate_rollback_ddl(schema, "PostgreSQL")
    assert rollback.index('DROP TABLE IF EXISTS "account"') < rollback.index('DROP TABLE IF EXISTS "customer"')


def test_generate_rollback_ddl_routines_and_views_come_before_tables():
    # Reverse dependency order: whatever might reference a table (a view,
    # a trigger) is dropped before the table itself.
    schema = _rollback_schema()
    rollback = ddl_generator.generate_rollback_ddl(schema, "PostgreSQL")
    assert rollback.index('DROP TRIGGER IF EXISTS "trg_account_audit"') < rollback.index('DROP TABLE IF EXISTS "account"')
    assert rollback.index('DROP VIEW IF EXISTS "vw_account"') < rollback.index('DROP TABLE IF EXISTS "account"')


def test_generate_rollback_ddl_package_body_gets_a_manual_note_for_postgres():
    schema = _rollback_schema()
    rollback = ddl_generator.generate_rollback_ddl(schema, "PostgreSQL")
    assert "PACKAGE BODY PKG_BANK has no direct equivalent" in rollback
    assert 'DROP PACKAGE' not in rollback  # Postgres has no PACKAGE object at all


def test_generate_rollback_ddl_mysql_uses_backtick_quoting():
    schema = _rollback_schema()
    rollback = ddl_generator.generate_rollback_ddl(schema, "MySQL")
    assert "DROP TABLE IF EXISTS `ACCOUNT`;" in rollback
    assert "DROP VIEW IF EXISTS `VW_ACCOUNT`;" in rollback
    assert "DROP PROCEDURE IF EXISTS `RECALC_BALANCE`;" in rollback
    assert "DROP FUNCTION IF EXISTS `GET_BALANCE`;" in rollback
    assert "DROP TRIGGER IF EXISTS `TRG_ACCOUNT_AUDIT`;" in rollback
    # sequences are emulated via a "<name>_SEQ" helper table for MySQL
    assert "DROP TABLE IF EXISTS `ACCOUNT_SEQ_SEQ`;" in rollback


def test_generate_rollback_ddl_sqlserver_uses_object_id_guards_and_schema_qualifies():
    schema = _rollback_schema()
    rollback = ddl_generator.generate_rollback_ddl(schema, "SQL Server", target_schema="dbo")
    assert "IF OBJECT_ID('dbo.ACCOUNT', 'U') IS NOT NULL" in rollback
    assert "DROP TABLE [dbo].[ACCOUNT];" in rollback
    assert "IF OBJECT_ID('dbo.VW_ACCOUNT', 'V') IS NOT NULL" in rollback
    assert "IF EXISTS (SELECT 1 FROM sys.sequences WHERE name = 'ACCOUNT_SEQ')" in rollback
    assert "IF OBJECT_ID('dbo.RECALC_BALANCE', 'P') IS NOT NULL" in rollback
    assert "IF OBJECT_ID('dbo.GET_BALANCE', 'FN') IS NOT NULL" in rollback
    assert "IF OBJECT_ID('dbo.TRG_ACCOUNT_AUDIT', 'TR') IS NOT NULL" in rollback


def test_generate_rollback_ddl_db2_uses_syscat_existence_checks():
    schema = _rollback_schema()
    rollback = ddl_generator.generate_rollback_ddl(schema, "DB2", target_schema="app")
    assert "SYSCAT.TABLES WHERE TABSCHEMA = 'APP' AND TABNAME = 'ACCOUNT'" in rollback
    assert "SYSCAT.SEQUENCES WHERE SEQSCHEMA = 'APP' AND SEQNAME = 'ACCOUNT_SEQ'" in rollback
    assert "SYSCAT.ROUTINES WHERE ROUTINESCHEMA = 'APP' AND ROUTINENAME = 'RECALC_BALANCE'" in rollback
    assert "SYSCAT.TRIGGERS WHERE TRIGSCHEMA = 'APP' AND TRIGNAME = 'TRG_ACCOUNT_AUDIT'" in rollback
    assert 'DROP TABLE "APP"."ACCOUNT"' in rollback


def test_generate_rollback_ddl_oracle_uses_execute_immediate_with_ignore_codes():
    schema = _rollback_schema()
    rollback = ddl_generator.generate_rollback_ddl(schema, "Oracle", target_schema="app")
    assert 'DROP TABLE "APP"."ACCOUNT" CASCADE CONSTRAINTS' in rollback
    assert "SQLCODE != -942" in rollback  # table/view
    assert "SQLCODE != -2289" in rollback  # sequence
    assert "SQLCODE != -4043" in rollback  # procedure/function
    assert "EXECUTE IMMEDIATE" in rollback
    # Oracle-sourced PACKAGE BODY targeting Oracle has a direct equivalent
    # (unlike every other target) -- it should NOT get the "no direct
    # equivalent" manual note the other engines' tests assert on, and gets
    # a real "DROP PACKAGE BODY" rather than falling through to the generic
    # PROCEDURE/FUNCTION drop.
    assert "has no direct equivalent" not in rollback
    assert 'DROP PACKAGE BODY "APP"."PKG_BANK"' in rollback


def test_generate_rollback_ddl_mongodb_emits_drop_calls_and_placeholder_notes():
    schema = _rollback_schema()
    rollback = ddl_generator.generate_rollback_ddl(schema, "MongoDB")
    assert 'db["ACCOUNT"].drop();' in rollback
    assert 'db["CUSTOMER"].drop();' in rollback
    assert "VIEW VW_ACCOUNT is always a MANUAL CONVERSION REQUIRED placeholder" in rollback
    assert "TRIGGER TRG_ACCOUNT_AUDIT is always a MANUAL CONVERSION REQUIRED placeholder" in rollback
    assert 'counters' in rollback  # sequence emulation note mentions the counters helper collection


def test_generate_rollback_ddl_is_idempotent_to_generate_repeatedly():
    # Calling this twice must produce byte-identical output -- it must not
    # mutate the Schema/Table/Routine objects the way DDL generation
    # itself does (status/issues fields), since this is meant to be safely
    # regenerable after Convert Schema without side effects.
    schema = _rollback_schema()
    first = ddl_generator.generate_rollback_ddl(schema, "PostgreSQL")
    second = ddl_generator.generate_rollback_ddl(schema, "PostgreSQL")
    assert first == second


# ------------------------------------------------------------ materialized views
#
# ora2pg gap-analysis Tier 1 item #2. An Oracle materialized view's
# backing container table looks, to the introspector's plain
# all_tables/all_tab_columns pass, exactly like an ordinary table -- its
# defining query, BUILD_MODE and REFRESH_MODE/METHOD only exist in
# ALL_MVIEWS (see tests/test_introspector.py for that half of the fix).
# generate_view_ddl is the other half: what actually gets emitted once a
# View is marked is_materialized.

def _mview(**overrides) -> View:
    defaults = dict(
        name="ACTIVE_EMP_MV", schema="HR", source_engine="Oracle",
        definition="SELECT emp_id, NVL(bonus, 0) AS bonus FROM employees WHERE status = 'A'",
        is_materialized=True, mview_build_mode="IMMEDIATE",
        mview_refresh_mode="DEMAND", mview_refresh_method="FORCE",
    )
    defaults.update(overrides)
    return View(**defaults)


def test_materialized_view_postgres_uses_with_no_data():
    # WITH NO DATA unconditionally, regardless of Oracle's BUILD_MODE: see
    # the docstring at its call site in generate_view_ddl for why running
    # the query immediately would be actively wrong in this tool's own
    # apply-then-migrate-data pipeline order, not just unsupported.
    ddl, issues = ddl_generator.generate_view_ddl(_mview(), "PostgreSQL")
    assert ddl.startswith('CREATE MATERIALIZED VIEW IF NOT EXISTS "active_emp_mv" AS')
    assert ddl.rstrip().endswith("WITH NO DATA;")
    assert "COALESCE(bonus, 0)" in ddl  # still goes through the normal Oracle-rewrite pipeline
    assert any(i.severity == "info" and "REFRESH MATERIALIZED VIEW" in i.message for i in issues)
    assert any("FORCE" in i.message for i in issues)  # source refresh_method surfaced for context


def test_materialized_view_postgres_status_is_automatic_not_warning():
    # The REFRESH reminder is informational, not a defect -- an MV that
    # migrates cleanly shouldn't show up in the report the same way a real
    # gap would.
    mv = _mview()
    ddl_generator.generate_view_ddl(mv, "PostgreSQL")
    assert mv.status == ConversionStatus.AUTOMATIC


def test_materialized_view_postgres_is_idempotent_to_reapply():
    ddl, _ = ddl_generator.generate_view_ddl(_mview(), "PostgreSQL")
    assert "IF NOT EXISTS" in ddl


def test_materialized_view_with_unknown_refresh_settings_still_gets_a_reminder():
    mv = _mview(mview_build_mode=None, mview_refresh_mode=None, mview_refresh_method=None)
    _ddl, issues = ddl_generator.generate_view_ddl(mv, "PostgreSQL")
    info = next(i for i in issues if i.severity == "info")
    assert "unknown" in info.message


@pytest.mark.parametrize("engine", ["MySQL", "SQL Server", "DB2"])
def test_materialized_view_on_a_non_postgres_target_is_flagged_manual(engine):
    # Contained-scope decision: only a PostgreSQL target gets a real
    # translation here, matching ora2pg's own Oracle-to-Postgres-only
    # focus. MySQL has no materialized-view concept, SQL Server's closest
    # analog (an indexed view) has different rules entirely, and Db2's
    # summary-table (MQT) translation hasn't been verified closely enough
    # to ship.
    mv = _mview()
    ddl, issues = ddl_generator.generate_view_ddl(mv, engine)
    assert ddl.startswith("-- MANUAL CONVERSION REQUIRED for MATERIALIZED VIEW ACTIVE_EMP_MV")
    assert "SELECT emp_id" in ddl  # original query kept verbatim for reference
    assert mv.status == ConversionStatus.MANUAL
    assert any(i.severity == "error" for i in issues)


def test_an_ordinary_view_is_unaffected_by_the_materialized_branch():
    view = View(name="V", schema="HR", source_engine="Oracle", definition="SELECT id FROM t")
    ddl, issues = ddl_generator.generate_view_ddl(view, "PostgreSQL")
    assert ddl == 'CREATE OR REPLACE VIEW "v" AS\nSELECT id FROM t;'
    assert issues == []


# -------------------------------------------------------- native partitioning
#
# ora2pg gap-analysis Tier 1 item #1 -- the single highest-value item in
# that report. Every DDL shape below was applied against a real, running
# PostgreSQL 16 server while this was built (not just eyeballed), because
# a wrong partition bound or an unmet unique-constraint restriction is
# exactly the kind of thing that looks right and fails at "Apply DDL to
# Target" -- see _plan_partition_ddl_postgres's own docstring.

def _partitioned_table(scheme: PartitionScheme, constraints=None, indexes=None) -> Table:
    return Table(
        name="SALES", schema="HR",
        columns=[
            Column(name="ID", data_type="NUMBER(9)", nullable=False),
            Column(name="SALE_DATE", data_type="DATE", nullable=False),
            Column(name="REGION", data_type="VARCHAR2(20)", nullable=False),
        ],
        constraints=constraints or [],
        indexes=indexes or [],
        partition_scheme=scheme,
    )


def test_range_partitioned_table_emits_partition_by_and_children():
    scheme = PartitionScheme(
        kind="RANGE", columns=["SALE_DATE"],
        partitions=[
            Partition(name="SALES_2024_01",
                      high_value="TO_DATE(' 2024-02-01 00:00:00', 'SYYYY-MM-DD HH24:MI:SS')", position=1),
            Partition(name="SALES_2024_02", high_value="MAXVALUE", position=2),
        ],
    )
    table = _partitioned_table(scheme)
    ddl, issues = ddl_generator.generate_table_ddl_postgres(table)
    assert 'PARTITION BY RANGE ("sale_date")' in ddl
    assert (
        'CREATE TABLE IF NOT EXISTS "sales_2024_01" PARTITION OF "sales" '
        "FOR VALUES FROM (MINVALUE) TO (' 2024-02-01 00:00:00');" in ddl
    )
    assert (
        'CREATE TABLE IF NOT EXISTS "sales_2024_02" PARTITION OF "sales" '
        "FOR VALUES FROM (' 2024-02-01 00:00:00') TO (MAXVALUE);" in ddl
    )
    assert issues == []
    assert table.status == ConversionStatus.AUTOMATIC


def test_list_partitioned_table_with_a_default_partition():
    scheme = PartitionScheme(
        kind="LIST", columns=["REGION"],
        partitions=[
            Partition(name="SALES_NY", high_value="'NY', 'NJ'", position=1),
            Partition(name="SALES_DEFAULT", high_value="DEFAULT", position=2),
        ],
    )
    table = _partitioned_table(scheme)
    ddl, issues = ddl_generator.generate_table_ddl_postgres(table)
    assert 'PARTITION BY LIST ("region")' in ddl
    assert 'CREATE TABLE IF NOT EXISTS "sales_ny" PARTITION OF "sales" FOR VALUES IN (\'NY\', \'NJ\');' in ddl
    assert 'CREATE TABLE IF NOT EXISTS "sales_default" PARTITION OF "sales" DEFAULT;' in ddl
    assert issues == []


def test_a_primary_key_not_covering_the_partition_key_is_dropped_and_flagged():
    # The exact failure confirmed live: "unique constraint on partitioned
    # table must include all partitioning columns" -- PostgreSQL 16
    # rejects this CREATE TABLE outright if the PK is emitted as-is.
    scheme = PartitionScheme(
        kind="RANGE", columns=["SALE_DATE"],
        partitions=[Partition(name="P1", high_value="MAXVALUE", position=1)],
    )
    table = _partitioned_table(
        scheme, constraints=[Constraint(name="PK_SALES", kind="PRIMARY KEY", columns=["ID"])])
    ddl, issues = ddl_generator.generate_table_ddl_postgres(table)
    assert "PK_SALES" not in ddl
    assert "PRIMARY KEY" not in ddl
    assert any(
        i.severity == "warning" and "PK_SALES" in i.message and "partition-key" in i.message
        for i in issues
    )
    assert table.status == ConversionStatus.AUTOMATIC_WITH_WARNINGS


def test_a_primary_key_covering_the_partition_key_is_kept():
    scheme = PartitionScheme(
        kind="RANGE", columns=["SALE_DATE"],
        partitions=[Partition(name="P1", high_value="MAXVALUE", position=1)],
    )
    table = _partitioned_table(
        scheme,
        constraints=[Constraint(name="PK_SALES", kind="PRIMARY KEY", columns=["ID", "SALE_DATE"])])
    ddl, issues = ddl_generator.generate_table_ddl_postgres(table)
    assert 'CONSTRAINT "pk_sales" PRIMARY KEY ("id", "sale_date")' in ddl
    assert issues == []


def test_a_unique_index_not_covering_the_partition_key_is_dropped_and_flagged():
    scheme = PartitionScheme(
        kind="RANGE", columns=["SALE_DATE"],
        partitions=[Partition(name="P1", high_value="MAXVALUE", position=1)],
    )
    table = _partitioned_table(
        scheme, indexes=[Index(name="UX_SALES_ID", columns=["ID"], unique=True)])
    ddl, issues = ddl_generator.generate_table_ddl_postgres(table)
    assert "UX_SALES_ID" not in ddl
    assert any(i.severity == "warning" and "UX_SALES_ID" in i.message for i in issues)


def test_a_non_unique_index_on_a_partitioned_table_is_unaffected():
    # Only a *unique* index/constraint has the partition-key-coverage
    # restriction in PostgreSQL -- a plain index on a partitioned table
    # (PG 11+) is created directly on the parent and cascades to every
    # partition automatically, present and future.
    scheme = PartitionScheme(
        kind="RANGE", columns=["SALE_DATE"],
        partitions=[Partition(name="P1", high_value="MAXVALUE", position=1)],
    )
    table = _partitioned_table(
        scheme, indexes=[Index(name="IX_SALES_REGION", columns=["REGION"], unique=False)])
    ddl, issues = ddl_generator.generate_table_ddl_postgres(table)
    assert 'CREATE INDEX IF NOT EXISTS "ix_sales_region" ON "sales" ("region");' in ddl
    assert issues == []


def test_hash_partitioning_falls_back_to_a_plain_table_with_a_warning():
    # Same scope decision ora2pg itself documents for Oracle HASH
    # partitioning: "explicitly unsupported and skipped with a warning."
    scheme = PartitionScheme(kind="HASH", columns=["ID"], partitions=[
        Partition(name="P1", high_value=None, position=1),
        Partition(name="P2", high_value=None, position=2),
    ])
    table = _partitioned_table(scheme)
    ddl, issues = ddl_generator.generate_table_ddl_postgres(table)
    assert "PARTITION BY" not in ddl
    assert "PARTITION OF" not in ddl
    assert 'CREATE TABLE IF NOT EXISTS "sales" (' in ddl
    assert any(i.severity == "warning" and "HASH-partitioned" in i.message for i in issues)


def test_composite_subpartitioned_scheme_falls_back_to_a_plain_table():
    scheme = PartitionScheme(
        kind="RANGE", columns=["SALE_DATE"], subpartitioning_type="HASH",
        partitions=[Partition(name="P1", high_value="MAXVALUE", position=1)],
    )
    table = _partitioned_table(scheme)
    ddl, issues = ddl_generator.generate_table_ddl_postgres(table)
    assert "PARTITION BY" not in ddl
    assert any(i.severity == "warning" and "RANGE-HASH" in i.message for i in issues)


def test_an_unrecognized_range_boundary_falls_back_to_a_plain_table():
    # A boundary this tool cannot confidently translate must never become
    # a partitioned table with a *guessed* boundary -- the whole table
    # falls back to being migrated unpartitioned instead, per
    # _plan_partition_ddl_postgres's own docstring.
    scheme = PartitionScheme(
        kind="RANGE", columns=["SALE_DATE"],
        partitions=[Partition(name="P1", high_value="SYS_EXTRACT_UTC(SYSTIMESTAMP)", position=1)],
    )
    table = _partitioned_table(scheme)
    ddl, issues = ddl_generator.generate_table_ddl_postgres(table)
    assert "PARTITION BY" not in ddl
    assert any(i.severity == "warning" and "P1" in i.message for i in issues)


def test_an_unpartitioned_table_is_completely_unaffected():
    table = _sample_table()
    assert table.partition_scheme is None
    ddl, issues = ddl_generator.generate_table_ddl_postgres(table)
    assert "PARTITION" not in ddl.upper()


def test_deferred_ddl_keeps_partition_children_in_the_immediate_ddl_not_deferred():
    # PostgreSQL has no way to declare a table partitioned after creation,
    # so this can never move to the post-load phase (SCALE.md section
    # 1.3) the way an ordinary index/constraint does -- every partition
    # must exist before the very first row is loaded.
    scheme = PartitionScheme(
        kind="RANGE", columns=["SALE_DATE"],
        partitions=[Partition(name="P1", high_value="MAXVALUE", position=1)],
    )
    table = _partitioned_table(
        scheme,
        constraints=[Constraint(name="PK_SALES", kind="PRIMARY KEY", columns=["ID", "SALE_DATE"])],
        indexes=[Index(name="IX_SALES_REGION", columns=["REGION"], unique=False)],
    )
    pre_ddl, _ = ddl_generator.generate_table_ddl_postgres(table, defer_constraints=True)
    assert "PARTITION BY RANGE" in pre_ddl
    assert 'PARTITION OF "sales"' in pre_ddl
    assert "PK_SALES" not in pre_ddl  # deferred like any other constraint
    assert "IX_SALES_REGION" not in pre_ddl  # deferred like any other index

    post_ddl = ddl_generator.generate_deferred_ddl_postgres(table)
    assert 'CONSTRAINT "pk_sales" PRIMARY KEY ("id", "sale_date")' in post_ddl
    assert "ix_sales_region" in post_ddl


def test_deferred_ddl_also_drops_a_pk_that_does_not_cover_the_partition_key():
    # The same restriction _plan_partition_ddl_postgres enforces in the
    # immediate CREATE TABLE path must hold for the deferred ALTER TABLE
    # path too, or a phased migration of this exact table would apply
    # cleanly and then fail on the *second* script.
    scheme = PartitionScheme(
        kind="RANGE", columns=["SALE_DATE"],
        partitions=[Partition(name="P1", high_value="MAXVALUE", position=1)],
    )
    table = _partitioned_table(
        scheme, constraints=[Constraint(name="PK_SALES", kind="PRIMARY KEY", columns=["ID"])])
    ddl_generator.generate_table_ddl_postgres(table, defer_constraints=True)
    post_ddl = ddl_generator.generate_deferred_ddl_postgres(table)
    assert "PK_SALES" not in post_ddl
    assert post_ddl == ""
