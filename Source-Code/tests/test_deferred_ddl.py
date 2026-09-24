"""Tests for two-phase DDL generation -- SCALE.md section 1.3.

Loading into a table that has no indexes and no constraints is
substantially faster (nothing is maintained per row), and building each
index once over the finished table produces a denser index. So
`generate_schema_ddl_phased` splits the generated DDL into a **pre-load**
script (bare tables) and a **post-load** script (constraints, indexes,
foreign keys, triggers).

The property that matters most here is that nothing is *lost* in the
split: every object the single-script path creates must still be created
by exactly one of the two phases. A dropped index is invisible until
someone runs a slow query in production months later, so several tests
below compare the two paths object by object rather than eyeballing the
SQL.

Plain functions, no pytest -- see tests/test_cli_config.py's note.
"""
from tgdatabridge.core import ddl_generator as dg
from tgdatabridge.core.schema_model import (
    Column, Constraint, Index, Routine, Schema, Table, View,
)

ENGINES = ("PostgreSQL", "MySQL", "SQL Server", "DB2", "Oracle", "MongoDB")
SQL_ENGINES = ("PostgreSQL", "MySQL", "SQL Server", "DB2", "Oracle")


def _employees():
    table = Table(name="EMPLOYEES", schema="HR")
    table.columns = [
        Column(name="EMP_ID", data_type="NUMBER(10)", nullable=False),
        Column(name="DEPT_ID", data_type="NUMBER(10)"),
        Column(name="EMAIL", data_type="VARCHAR2(100)"),
        Column(name="SALARY", data_type="NUMBER(10,2)"),
    ]
    table.constraints = [
        Constraint(name="EMP_PK", kind="PRIMARY KEY", columns=["EMP_ID"]),
        Constraint(name="EMP_EMAIL_UQ", kind="UNIQUE", columns=["EMAIL"]),
        Constraint(name="EMP_SAL_CK", kind="CHECK", columns=["SALARY"], check_condition="SALARY > 0"),
        Constraint(name="EMP_DEPT_FK", kind="FOREIGN KEY", columns=["DEPT_ID"],
                   ref_table="DEPARTMENTS", ref_columns=["DEPT_ID"]),
    ]
    table.indexes = [Index(name="EMP_DEPT_IDX", columns=["DEPT_ID"])]
    return table


def _departments():
    table = Table(name="DEPARTMENTS", schema="HR")
    table.columns = [Column(name="DEPT_ID", data_type="NUMBER(10)", nullable=False)]
    table.constraints = [Constraint(name="DEPT_PK", kind="PRIMARY KEY", columns=["DEPT_ID"])]
    return table


def _trigger():
    return Routine(
        name="TRG_AUDIT", schema="HR", kind="TRIGGER", table_name="EMPLOYEES",
        timing="BEFORE", events=["INSERT"], row_level=True,
        source=("CREATE OR REPLACE TRIGGER trg_audit\nBEFORE INSERT ON employees\n"
                "FOR EACH ROW\nBEGIN\n  :NEW.email := LOWER(:NEW.email);\nEND;"),
    )


def _function():
    return Routine(
        name="FN_CALC", schema="HR", kind="FUNCTION",
        source="CREATE FUNCTION fn_calc RETURN NUMBER IS BEGIN RETURN 1; END;",
    )


def _schema():
    """A fresh Schema each call -- DDL generation mutates the objects it's
    given (Column.target_type, Table.status, Routine.converted_source), so
    reusing one instance across both phases would compare a converted
    schema against an already-converted one."""
    schema = Schema(name="HR", source_engine="Oracle")
    schema.tables = [_employees(), _departments()]
    schema.views = [View(name="V_EMP", schema="HR", definition="SELECT emp_id FROM employees")]
    schema.routines = [_function(), _trigger()]
    return schema


def _phased(engine, target_schema="app"):
    return dg.generate_schema_ddl_phased(_schema(), engine, target_schema)


def _lower(text):
    return text.lower()


# ------------------------------------------------ pre-load holds nothing back

def test_pre_load_creates_every_table_on_every_engine():
    for engine in ENGINES:
        pre, _, _ = _phased(engine)
        assert "employees" in _lower(pre), engine
        assert "departments" in _lower(pre), engine


def test_pre_load_has_no_constraints():
    for engine in SQL_ENGINES:
        pre, _, _ = _phased(engine)
        lowered = _lower(pre)
        assert "primary key" not in lowered, engine
        assert "emp_email_uq" not in lowered, engine
        assert "emp_sal_ck" not in lowered, engine
        assert "foreign key" not in lowered, engine


def test_pre_load_has_no_indexes():
    for engine in SQL_ENGINES:
        pre, _, _ = _phased(engine)
        assert "emp_dept_idx" not in _lower(pre), engine


def test_pre_load_keeps_not_null_and_column_types():
    # NOT NULL is deliberately not deferred: it costs nothing per row, and
    # adding it afterwards forces a full re-scan to prove it holds.
    for engine in SQL_ENGINES:
        pre, _, _ = _phased(engine)
        assert "not null" in _lower(pre), engine


def test_pre_load_keeps_views_and_functions():
    for engine in SQL_ENGINES:
        pre, _, _ = _phased(engine)
        lowered = _lower(pre)
        assert "v_emp" in lowered, engine
        assert "fn_calc" in lowered, engine


def test_pre_load_omits_triggers():
    # A trigger that exists during a bulk load fires once per migrated row:
    # ruinous for throughput, and usually wrong, since the source rows
    # already reflect whatever the trigger does.
    for engine in SQL_ENGINES:
        pre, _, _ = _phased(engine)
        assert "trg_audit" not in _lower(pre), engine


# ------------------------------------------------- post-load has all of it

def test_post_load_has_every_constraint():
    for engine in SQL_ENGINES:
        _, post, _ = _phased(engine)
        lowered = _lower(post)
        assert "primary key" in lowered, engine
        assert "emp_email_uq" in lowered, engine
        assert "emp_sal_ck" in lowered, engine
        assert "foreign key" in lowered, engine


def test_post_load_has_the_secondary_index():
    for engine in ENGINES:
        _, post, _ = _phased(engine)
        assert "emp_dept_idx" in _lower(post), engine


def test_post_load_has_the_trigger():
    for engine in SQL_ENGINES:
        _, post, _ = _phased(engine)
        assert "trg_audit" in _lower(post), engine


def test_post_load_does_not_recreate_tables():
    for engine in SQL_ENGINES:
        _, post, _ = _phased(engine)
        assert "create table" not in _lower(post), engine


def test_post_load_does_not_repeat_views_or_functions():
    for engine in SQL_ENGINES:
        _, post, _ = _phased(engine)
        lowered = _lower(post)
        assert "v_emp" not in lowered, engine
        assert "fn_calc" not in lowered, engine


def test_primary_key_is_added_for_every_table():
    for engine in SQL_ENGINES:
        _, post, _ = _phased(engine)
        lowered = _lower(post)
        assert lowered.count("primary key") >= 2, engine  # EMPLOYEES and DEPARTMENTS


def test_primary_keys_are_named_except_on_mysql():
    # MySQL's primary key is always internally named "PRIMARY" -- a
    # constraint name there is accepted syntactically and then ignored, so
    # the deferred path emits ADD PRIMARY KEY without one, exactly as the
    # inline path does.
    for engine in ("PostgreSQL", "SQL Server", "DB2", "Oracle"):
        _, post, _ = _phased(engine)
        lowered = _lower(post)
        assert "emp_pk" in lowered, engine
        assert "dept_pk" in lowered, engine

    _, mysql_post, _ = _phased("MySQL")
    assert "add primary key" in _lower(mysql_post)
    assert "emp_pk" not in _lower(mysql_post)


# ---------------------------------------------- nothing is lost in the split

def _object_markers(text):
    """Which of the schema's objects appear anywhere in a script."""
    lowered = _lower(text)
    markers = ("employees", "departments", "v_emp", "fn_calc", "trg_audit",
               "emp_pk", "dept_pk", "emp_email_uq", "emp_sal_ck", "emp_dept_fk", "emp_dept_idx")
    return {m for m in markers if m in lowered}


def test_the_two_phases_together_cover_the_single_script():
    # The union of pre + post must mention every object the combined
    # script does. A dropped index is invisible until a slow query turns
    # up in production months later.
    for engine in SQL_ENGINES:
        combined, _ = dg.generate_schema_ddl(_schema(), engine, "app")
        pre, post, _ = _phased(engine)
        missing = _object_markers(combined) - (_object_markers(pre) | _object_markers(post))
        assert not missing, f"{engine} lost {missing}"


def test_no_object_is_created_in_both_phases():
    for engine in SQL_ENGINES:
        pre, post, _ = _phased(engine)
        overlap = _object_markers(pre) & _object_markers(post)
        # EMPLOYEES/DEPARTMENTS legitimately appear in both -- post-load's
        # ALTER TABLE statements name the table they alter.
        assert overlap <= {"employees", "departments"}, f"{engine} duplicated {overlap}"


def test_phased_reports_the_same_issues_as_the_single_script():
    for engine in SQL_ENGINES:
        _, combined_issues = dg.generate_schema_ddl(_schema(), engine, "app")
        _, _, phased_issues = _phased(engine)
        assert len(phased_issues) == len(combined_issues), engine


# ------------------------------------------------------ index deduplication

def test_an_index_backing_a_unique_constraint_is_not_created_twice():
    # The inline path skips an index whose name matches a PK/UNIQUE
    # constraint (the constraint creates it implicitly); the deferred path
    # has to skip the same ones or the post-load script fails on a
    # duplicate index name.
    schema = _schema()
    schema.tables[0].indexes.append(Index(name="EMP_EMAIL_UQ", columns=["EMAIL"], unique=True))
    for engine in SQL_ENGINES:
        post = dg.generate_deferred_ddl(schema.tables[0], engine, "app")
        assert "create" not in _lower(post).split("emp_email_uq")[-1][:20] or True
        # The real check: the unique constraint is added once, and no
        # separate CREATE INDEX names it.
        create_index_lines = [
            line for line in post.splitlines()
            if "create" in line.lower() and "index" in line.lower() and "emp_email_uq" in line.lower()
        ]
        assert create_index_lines == [], f"{engine} created a redundant index: {create_index_lines}"


def test_deferrable_indexes_excludes_constraint_backed_ones():
    table = _employees()
    table.indexes.append(Index(name="EMP_EMAIL_UQ", columns=["EMAIL"], unique=True))
    names = [i.name for i in dg._deferrable_indexes(table)]
    assert names == ["EMP_DEPT_IDX"]


# --------------------------------------------------------- per-table helper

def test_generate_deferred_ddl_dispatches_every_engine():
    table = _employees()
    for engine in ENGINES:
        ddl = dg.generate_deferred_ddl(table, engine, "app")
        assert ddl.strip(), engine


def test_deferred_ddl_is_empty_for_a_table_with_nothing_to_defer():
    bare = Table(name="PLAIN", schema="HR")
    bare.columns = [Column(name="A", data_type="NUMBER(10)")]
    for engine in SQL_ENGINES:
        assert dg.generate_deferred_ddl(bare, engine, "app") == "", engine


def test_deferred_ddl_uses_alter_table_not_create_table():
    for engine in SQL_ENGINES:
        ddl = _lower(dg.generate_deferred_ddl(_employees(), engine, "app"))
        assert "alter table" in ddl, engine
        assert "create table" not in ddl, engine


# ------------------------------------------------------------ table flags

def test_defer_constraints_flag_on_each_table_generator():
    checks = (
        (dg.generate_table_ddl_postgres, ()),
        (dg.generate_table_ddl_mysql, ()),
        (dg.generate_table_ddl_sqlserver, ("app",)),
        (dg.generate_table_ddl_db2, ("app",)),
        (dg.generate_table_ddl_oracle, ("app",)),
    )
    for fn, extra in checks:
        inline, _ = fn(_employees(), *extra, False)
        deferred, _ = fn(_employees(), *extra, True)
        assert "primary key" in _lower(inline), fn.__name__
        assert "primary key" not in _lower(deferred), fn.__name__
        assert "emp_dept_idx" in _lower(inline), fn.__name__
        assert "emp_dept_idx" not in _lower(deferred), fn.__name__


def test_mongodb_defers_indexes_but_keeps_its_validator():
    # The $jsonSchema validator isn't index maintenance, and moving it
    # would need a separate collMod round trip for no gain.
    inline, _ = dg.generate_table_ddl_mongodb(_employees(), False)
    deferred, _ = dg.generate_table_ddl_mongodb(_employees(), True)
    assert "createindex" in _lower(inline)
    assert "createindex" not in _lower(deferred)
    assert "jsonschema" in _lower(deferred)


def test_default_behavior_is_unchanged():
    # Every pre-existing caller passes no flag at all and must get exactly
    # the single combined script it always did.
    for engine in SQL_ENGINES:
        default_sql, _ = dg.generate_schema_ddl(_schema(), engine, "app")
        explicit_sql, _ = dg.generate_schema_ddl(
            _schema(), engine, "app", defer_constraints=False, include_deferred=True)
        assert default_sql == explicit_sql, engine
        assert "primary key" in _lower(default_sql), engine


# ------------------------------------------------------------- postgres detail

def test_postgres_post_load_sets_the_search_path():
    # The script has to be self-contained if it's saved and run outside
    # this tool, matching the pre-load script's own leading statement.
    _, post, _ = _phased("PostgreSQL", "app")
    assert 'SET search_path TO "app"' in post


def test_postgres_deferred_constraints_are_idempotent():
    # Re-running a post-load script must be safe, like every other script
    # this tool generates.
    ddl = dg.generate_deferred_ddl_postgres(_employees())
    assert "duplicate_object" in ddl
    assert "IF NOT EXISTS" in ddl  # the CREATE INDEX half


def test_oracle_deferred_constraints_swallow_duplicate_constraint_errors():
    ddl = dg.generate_deferred_ddl_oracle(_employees(), "APP")
    assert "-2264" in ddl  # name already used by an existing constraint
    assert "-2261" in ddl  # such unique/primary key already exists


def test_sqlserver_and_db2_guard_on_their_catalogs():
    assert "sys.objects" in dg.generate_deferred_ddl_sqlserver(_employees(), "dbo")
    db2 = dg.generate_deferred_ddl_db2(_employees(), "HR")
    assert "SYSCAT.TABCONST" in db2
    assert "SYSCAT.INDEXES" in db2


def test_empty_schema_post_load_says_so_rather_than_emitting_nothing():
    empty = Schema(name="HR", source_engine="Oracle")
    post, _ = dg.generate_post_load_ddl(empty, "PostgreSQL", None)
    assert "Nothing to apply" in post
