"""Tests for tgdatabridge.core.mysql_introspector -- reading MySQL's
information_schema into a Schema, for using MySQL as a *source* engine.

Uses a fake connector whose execute() dispatches on a short, deliberately
distinctive substring registered for each query the introspector issues,
rather than a real MySQL connection -- these are pure data-shape/mapping
tests, not integration tests against a live database."""
from tgdatabridge.core.mysql_introspector import _fmt_data_type, introspect_schema
from tgdatabridge.core.schema_model import ConversionIssue


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
    tables=(), columns=(), table_constraints=(), key_columns=(), fk_columns=(),
    check_constraints=(), indexes=(), views=(), routines=(), triggers=(),
    parameters=(),
):
    return _FakeConn([
        ("FROM information_schema.tables", tables),
        ("FROM information_schema.columns", columns),
        ("SELECT constraint_name, table_name, constraint_type FROM information_schema.table_constraints", table_constraints),
        ("FROM information_schema.key_column_usage WHERE table_schema = %(schema)s ORDER BY table_name, constraint_name, ordinal_position", key_columns),
        ("referenced_table_name IS NOT NULL", fk_columns),
        ("information_schema.check_constraints", check_constraints),
        ("FROM information_schema.statistics", indexes),
        ("FROM information_schema.views", views),
        ("FROM information_schema.parameters", parameters),
        ("FROM information_schema.routines", routines),
        ("FROM information_schema.triggers", triggers),
    ])


# --------------------------------------------------------- _fmt_data_type

def test_fmt_data_type_varchar_uses_character_maximum_length():
    assert _fmt_data_type(("varchar", 255, None, None, "varchar(255)")) == "varchar(255)"


def test_fmt_data_type_decimal_uses_precision_and_scale():
    assert _fmt_data_type(("decimal", None, 10, 2, "decimal(10,2)")) == "decimal(10,2)"


def test_fmt_data_type_decimal_no_scale():
    assert _fmt_data_type(("decimal", None, 8, 0, "decimal(8,0)")) == "decimal(8)"


def test_fmt_data_type_enum_uses_column_type_verbatim():
    assert _fmt_data_type(("enum", None, None, None, "enum('a','b')")) == "enum('a','b')"


def test_fmt_data_type_unsigned_int_preserves_unsigned_modifier():
    assert _fmt_data_type(("int", None, 10, 0, "int(10) unsigned")) == "int unsigned"


def test_fmt_data_type_plain_int_has_no_modifier():
    assert _fmt_data_type(("int", None, 10, 0, "int(10)")) == "int"


# ------------------------------------------------------------ introspect_schema

def test_introspect_basic_tables_and_columns():
    conn = _conn(
        tables=[("EMPLOYEES", "emp table")],
        columns=[
            ("EMPLOYEES", "id", "int", None, 10, 0, "int(10)", "NO", None, "auto_increment", None),
            ("EMPLOYEES", "name", "varchar", 100, None, None, "varchar(100)", "NO", None, "", None),
        ],
    )
    schema = introspect_schema(conn, "mydb")
    assert schema.source_engine == "MySQL"
    assert [t.name for t in schema.tables] == ["EMPLOYEES"]
    table = schema.tables[0]
    assert table.comment == "emp table"
    assert [c.name for c in table.columns] == ["id", "name"]
    assert table.columns[0].data_type == "NUMBER(10)"
    assert table.columns[0].identity is True
    assert table.columns[1].data_type == "VARCHAR2(100)"
    assert table.columns[1].identity is False


def test_introspect_column_reverse_mapping_issue_lands_on_source_issues():
    conn = _conn(
        tables=[("EMPLOYEES", None)],
        columns=[
            ("EMPLOYEES", "status", "enum", None, None, None, "enum('A','B')", "YES", None, "", None),
        ],
    )
    schema = introspect_schema(conn, "mydb")
    col = schema.tables[0].columns[0]
    assert col.data_type.startswith("VARCHAR2(")
    assert col.issues == []  # target-side issues only get set during DDL generation
    assert any(i.severity == "warning" for i in col.source_issues)


def test_introspect_primary_keys_are_disambiguated_per_table():
    # MySQL's PK constraint/index is always literally named "PRIMARY" on
    # every table -- must not collide across tables.
    conn = _conn(
        tables=[("EMPLOYEES", None), ("DEPARTMENTS", None)],
        columns=[
            ("EMPLOYEES", "id", "int", None, 10, 0, "int(10)", "NO", None, "", None),
            ("DEPARTMENTS", "id", "int", None, 10, 0, "int(10)", "NO", None, "", None),
        ],
        table_constraints=[
            ("PRIMARY", "EMPLOYEES", "PRIMARY KEY"),
            ("PRIMARY", "DEPARTMENTS", "PRIMARY KEY"),
        ],
        key_columns=[
            ("PRIMARY", "EMPLOYEES", "id", 1),
            ("PRIMARY", "DEPARTMENTS", "id", 1),
        ],
        indexes=[
            ("EMPLOYEES", "PRIMARY", 0, "id", 1),
            ("DEPARTMENTS", "PRIMARY", 0, "id", 1),
        ],
    )
    schema = introspect_schema(conn, "mydb")
    emp = next(t for t in schema.tables if t.name == "EMPLOYEES")
    dept = next(t for t in schema.tables if t.name == "DEPARTMENTS")
    assert emp.constraints[0].name == "EMPLOYEES_PK"
    assert dept.constraints[0].name == "DEPARTMENTS_PK"
    assert emp.constraints[0].name != dept.constraints[0].name
    assert emp.constraints[0].columns == ["id"]
    # the matching index is renamed the same way, so ddl_generator's
    # "skip an index that's really just the PK/UNIQUE constraint" dedup
    # logic (which matches by name) still recognizes them as the same object
    assert emp.indexes[0].name == "EMPLOYEES_PK"
    assert emp.indexes[0].unique is True


def test_introspect_foreign_key_resolves_ref_table_and_columns():
    conn = _conn(
        tables=[("EMPLOYEES", None), ("DEPARTMENTS", None)],
        columns=[
            ("EMPLOYEES", "dept_id", "int", None, 10, 0, "int(10)", "YES", None, "", None),
            ("DEPARTMENTS", "id", "int", None, 10, 0, "int(10)", "NO", None, "", None),
        ],
        table_constraints=[("fk_emp_dept", "EMPLOYEES", "FOREIGN KEY")],
        key_columns=[("fk_emp_dept", "EMPLOYEES", "dept_id", 1)],
        fk_columns=[("fk_emp_dept", "EMPLOYEES", "DEPARTMENTS", "id", 1)],
    )
    schema = introspect_schema(conn, "mydb")
    emp = next(t for t in schema.tables if t.name == "EMPLOYEES")
    fk = emp.constraints[0]
    assert fk.kind == "FOREIGN KEY"
    assert fk.columns == ["dept_id"]
    assert fk.ref_table == "DEPARTMENTS"
    assert fk.ref_columns == ["id"]


def test_introspect_non_primary_index_kept_as_is():
    conn = _conn(
        tables=[("EMPLOYEES", None)],
        columns=[("EMPLOYEES", "name", "varchar", 100, None, None, "varchar(100)", "YES", None, "", None)],
        indexes=[("EMPLOYEES", "idx_name", 1, "name", 1)],
    )
    schema = introspect_schema(conn, "mydb")
    idx = schema.tables[0].indexes[0]
    assert idx.name == "idx_name"
    assert idx.unique is False
    assert idx.columns == ["name"]


def test_introspect_check_constraints_missing_table_is_tolerated():
    # information_schema.CHECK_CONSTRAINTS doesn't exist at all on older
    # MySQL/MariaDB -- introspect_schema must not blow up if the fake
    # connector (or a real old server) raises for that query.
    class _RaisingConn(_FakeConn):
        def execute(self, sql, params=None):
            if "check_constraints" in sql:
                raise Exception("Table 'information_schema.check_constraints' doesn't exist")
            return super().execute(sql, params)

    base = _conn(tables=[("EMPLOYEES", None)])
    conn = _RaisingConn(base._routes)
    schema = introspect_schema(conn, "mydb")
    assert schema.tables[0].constraints == []


def test_introspect_no_sequences_ever():
    conn = _conn(tables=[("EMPLOYEES", None)])
    schema = introspect_schema(conn, "mydb")
    assert schema.sequences == []


def test_introspect_views():
    conn = _conn(views=[("V_ACTIVE_EMP", "select * from employees where active = 1")])
    schema = introspect_schema(conn, "mydb")
    assert len(schema.views) == 1
    assert schema.views[0].name == "V_ACTIVE_EMP"
    assert "active = 1" in schema.views[0].definition


def test_introspect_routines_are_flagged_with_mysql_source_engine():
    conn = _conn(routines=[("RAISE_SALARY", "PROCEDURE", "BEGIN UPDATE t SET x=1; END")])
    schema = introspect_schema(conn, "mydb")
    assert len(schema.routines) == 1
    routine = schema.routines[0]
    assert routine.kind == "PROCEDURE"
    assert routine.source_engine == "MySQL"
    assert "UPDATE t" in routine.source


def test_introspect_triggers_are_single_event_row_level():
    conn = _conn(triggers=[("TRG_AUDIT", "EMPLOYEES", "INSERT", "AFTER", "INSERT INTO audit_log VALUES (1)")])
    schema = introspect_schema(conn, "mydb")
    trigger = schema.routines[0]
    assert trigger.kind == "TRIGGER"
    assert trigger.source_engine == "MySQL"
    assert trigger.table_name == "EMPLOYEES"
    assert trigger.timing == "AFTER"
    assert trigger.events == ["INSERT"]
    assert trigger.row_level is True


def test_introspect_routines_skipped_when_include_routines_false():
    conn = _conn(
        routines=[("RAISE_SALARY", "PROCEDURE", "BEGIN NULL; END")],
        triggers=[("TRG_AUDIT", "EMPLOYEES", "INSERT", "AFTER", "...")],
    )
    schema = introspect_schema(conn, "mydb", include_routines=False)
    assert schema.routines == []
