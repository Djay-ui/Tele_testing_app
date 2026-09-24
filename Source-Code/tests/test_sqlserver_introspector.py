"""Tests for tgdatabridge.core.sqlserver_introspector -- reading SQL Server's
information_schema (plus sys.* catalog views) into a Schema, for using SQL
Server as a *source* engine. Mirrors tests/test_postgres_introspector.py's
fake-connector approach -- pure data-shape/mapping tests, not integration
tests against a live database."""
from tgdatabridge.core.sqlserver_introspector import _fmt_data_type, introspect_schema


class _FakeConn:
    def __init__(self, routes):
        self._routes = sorted(routes, key=lambda kv: -len(kv[0]))

    def execute(self, sql, params=None):
        sql_norm = " ".join(sql.split())
        for key, rows in self._routes:
            if key in sql_norm:
                return rows
        raise AssertionError(f"No fake route registered for query:\n{sql}")


def _conn(
    tables=(), columns=(), table_constraints=(), key_columns=(),
    referential_constraints=(), check_constraints=(), indexes=(), sequences=(),
    views=(), routines=(), triggers=(),
):
    return _FakeConn([
        ("FROM information_schema.tables", tables),
        ("FROM sys.columns c", columns),
        ("SELECT constraint_name, table_name, constraint_type FROM information_schema.table_constraints", table_constraints),
        ("FROM information_schema.key_column_usage WHERE table_schema = %(schema)s ORDER BY table_name, constraint_name, ordinal_position", key_columns),
        ("FROM information_schema.referential_constraints", referential_constraints),
        ("information_schema.check_constraints", check_constraints),
        ("FROM sys.indexes i", indexes),
        ("FROM sys.sequences s", sequences),
        ("FROM information_schema.views", views),
        ("FROM information_schema.routines", routines),
        ("FROM sys.triggers tr", triggers),
    ])


# --------------------------------------------------------- _fmt_data_type

def test_fmt_data_type_varchar_uses_max_length_directly():
    assert _fmt_data_type(("varchar", 255, None, None)) == "varchar(255)"


def test_fmt_data_type_varchar_max():
    assert _fmt_data_type(("varchar", -1, None, None)) == "varchar(max)"


def test_fmt_data_type_nvarchar_halves_byte_length_to_char_count():
    assert _fmt_data_type(("nvarchar", 200, None, None)) == "nvarchar(100)"


def test_fmt_data_type_nvarchar_max():
    assert _fmt_data_type(("nvarchar", -1, None, None)) == "nvarchar(max)"


def test_fmt_data_type_decimal_uses_precision_and_scale():
    assert _fmt_data_type(("decimal", None, 10, 2)) == "decimal(10,2)"


def test_fmt_data_type_decimal_no_scale():
    assert _fmt_data_type(("decimal", None, 8, 0)) == "decimal(8)"


def test_fmt_data_type_datetime2_uses_precision():
    assert _fmt_data_type(("datetime2", None, None, None)) == "datetime2"


def test_fmt_data_type_plain_passthrough():
    assert _fmt_data_type(("int", None, None, None)) == "int"


# ------------------------------------------------------------ introspect_schema

def test_introspect_basic_tables_and_columns():
    conn = _conn(
        tables=[("employees",)],
        columns=[
            ("employees", "id", "int", None, 10, 0, False, True, None),
            ("employees", "name", "varchar", 100, None, None, False, False, None),
        ],
    )
    schema = introspect_schema(conn, "dbo")
    assert schema.source_engine == "SQL Server"
    assert [t.name for t in schema.tables] == ["employees"]
    table = schema.tables[0]
    assert [c.name for c in table.columns] == ["id", "name"]
    assert table.columns[0].data_type == "NUMBER(10)"
    assert table.columns[0].identity is True
    assert table.columns[1].data_type == "VARCHAR2(100)"
    assert table.columns[1].identity is False


def test_introspect_column_default_definition_is_captured():
    conn = _conn(
        tables=[("employees",)],
        columns=[("employees", "active", "bit", None, None, None, False, False, "((1))")],
    )
    schema = introspect_schema(conn, "dbo")
    assert schema.tables[0].columns[0].default == "((1))"


def test_introspect_column_reverse_mapping_issue_lands_on_source_issues():
    conn = _conn(
        tables=[("employees",)],
        columns=[("employees", "id_card", "uniqueidentifier", None, None, None, True, False, None)],
    )
    schema = introspect_schema(conn, "dbo")
    col = schema.tables[0].columns[0]
    assert col.data_type == "VARCHAR2(36)"
    assert col.issues == []
    assert any(i.severity == "warning" for i in col.source_issues)


def test_introspect_constraints_keyed_by_bare_name_no_disambiguation_needed():
    conn = _conn(
        tables=[("employees",), ("departments",)],
        columns=[
            ("employees", "id", "int", None, 10, 0, False, True, None),
            ("departments", "id", "int", None, 10, 0, False, True, None),
        ],
        table_constraints=[
            ("PK_employees", "employees", "PRIMARY KEY"),
            ("PK_departments", "departments", "PRIMARY KEY"),
        ],
        key_columns=[
            ("PK_employees", "employees", "id", 1),
            ("PK_departments", "departments", "id", 1),
        ],
    )
    schema = introspect_schema(conn, "dbo")
    emp = next(t for t in schema.tables if t.name == "employees")
    dept = next(t for t in schema.tables if t.name == "departments")
    assert emp.constraints[0].name == "PK_employees"
    assert dept.constraints[0].name == "PK_departments"
    assert emp.constraints[0].columns == ["id"]


def test_introspect_foreign_key_resolves_ref_table_and_columns():
    conn = _conn(
        tables=[("employees",), ("departments",)],
        columns=[
            ("employees", "dept_id", "int", None, 10, 0, True, False, None),
            ("departments", "id", "int", None, 10, 0, False, True, None),
        ],
        table_constraints=[
            ("PK_departments", "departments", "PRIMARY KEY"),
            ("FK_emp_dept", "employees", "FOREIGN KEY"),
        ],
        key_columns=[
            ("PK_departments", "departments", "id", 1),
            ("FK_emp_dept", "employees", "dept_id", 1),
        ],
        referential_constraints=[("FK_emp_dept", "PK_departments")],
    )
    schema = introspect_schema(conn, "dbo")
    emp = next(t for t in schema.tables if t.name == "employees")
    fk = next(c for c in emp.constraints if c.kind == "FOREIGN KEY")
    assert fk.columns == ["dept_id"]
    assert fk.ref_table == "departments"
    assert fk.ref_columns == ["id"]


def test_introspect_check_constraints():
    conn = _conn(
        tables=[("employees",)],
        check_constraints=[("CK_salary", "employees", "([salary]>(0))")],
    )
    schema = introspect_schema(conn, "dbo")
    cons = schema.tables[0].constraints[0]
    assert cons.kind == "CHECK"
    assert cons.name == "CK_salary"
    assert cons.check_condition == "([salary]>(0))"


def test_introspect_index_columns_ordered_and_unique_flagged():
    conn = _conn(
        tables=[("employees",)],
        indexes=[
            ("employees", "IX_emp_dept_name", False, "dept_id"),
            ("employees", "IX_emp_dept_name", False, "name"),
            ("employees", "UQ_emp_email", True, "email"),
        ],
    )
    schema = introspect_schema(conn, "dbo")
    idx_by_name = {i.name: i for i in schema.tables[0].indexes}
    assert idx_by_name["IX_emp_dept_name"].columns == ["dept_id", "name"]
    assert idx_by_name["IX_emp_dept_name"].unique is False
    assert idx_by_name["UQ_emp_email"].unique is True


def test_introspect_index_name_matches_pk_constraint_name_for_dedup():
    conn = _conn(
        tables=[("employees",)],
        columns=[("employees", "id", "int", None, 10, 0, False, True, None)],
        table_constraints=[("PK_employees", "employees", "PRIMARY KEY")],
        key_columns=[("PK_employees", "employees", "id", 1)],
        indexes=[("employees", "PK_employees", True, "id")],
    )
    schema = introspect_schema(conn, "dbo")
    table = schema.tables[0]
    assert table.constraints[0].name == "PK_employees"
    assert table.indexes[0].name == "PK_employees"


def test_introspect_sequences_native_object():
    conn = _conn(sequences=[("employees_id_seq", 1, 1, 1, 2147483647, False)])
    schema = introspect_schema(conn, "dbo")
    assert len(schema.sequences) == 1
    seq = schema.sequences[0]
    assert seq.name == "employees_id_seq"
    assert seq.start_value == 1
    assert seq.max_value == 2147483647
    assert seq.cycle is False


def test_introspect_views():
    conn = _conn(views=[("v_active_emp", "SELECT * FROM employees WHERE active = 1")])
    schema = introspect_schema(conn, "dbo")
    assert len(schema.views) == 1
    assert schema.views[0].name == "v_active_emp"
    assert "active" in schema.views[0].definition


def test_introspect_routines_are_flagged_with_sqlserver_source_engine():
    conn = _conn(routines=[("raise_salary", "PROCEDURE", "CREATE PROCEDURE raise_salary AS BEGIN UPDATE t SET x = 1; END")])
    schema = introspect_schema(conn, "dbo")
    assert len(schema.routines) == 1
    routine = schema.routines[0]
    assert routine.kind == "PROCEDURE"
    assert routine.source_engine == "SQL Server"
    assert "UPDATE t" in routine.source


def test_introspect_triggers_timing_and_events():
    conn = _conn(triggers=[
        ("trg_audit_after", "employees", False, "CREATE TRIGGER trg_audit_after ON employees AFTER INSERT AS ...", "INSERT"),
        ("trg_audit_instead_of", "employees", True, "CREATE TRIGGER trg_audit_instead_of ON employees INSTEAD OF DELETE AS ...", "DELETE"),
    ])
    schema = introspect_schema(conn, "dbo")
    by_name = {r.name: r for r in schema.routines}
    assert by_name["trg_audit_after"].timing == "AFTER"
    assert by_name["trg_audit_after"].events == ["INSERT"]
    assert by_name["trg_audit_instead_of"].timing == "INSTEAD OF"
    assert by_name["trg_audit_instead_of"].events == ["DELETE"]
    assert by_name["trg_audit_after"].source_engine == "SQL Server"
    assert by_name["trg_audit_after"].table_name == "employees"


def test_introspect_routines_skipped_when_include_routines_false():
    conn = _conn(
        routines=[("raise_salary", "PROCEDURE", "CREATE PROCEDURE raise_salary AS BEGIN RETURN; END")],
        triggers=[("trg_audit", "employees", False, "...", "INSERT")],
    )
    schema = introspect_schema(conn, "dbo", include_routines=False)
    assert schema.routines == []
