"""Tests for tgdatabridge.core.db2_introspector -- reading Db2 (LUW)'s SYSCAT.*
catalog views into a Schema, for using Db2 as a *source* engine. Mirrors
tests/test_sqlserver_introspector.py's fake-connector approach -- pure
data-shape/mapping tests, not integration tests against a live database."""
from tgdatabridge.core.db2_introspector import _fmt_data_type, introspect_schema


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
    references=(), checks=(), indexes=(), sequences=(),
    views=(), routines=(), triggers=(),
):
    return _FakeConn([
        ("FROM SYSCAT.TABLES WHERE TABSCHEMA", tables),
        ("FROM SYSCAT.COLUMNS WHERE TABSCHEMA", columns),
        ("SELECT CONSTNAME, TABNAME, TYPE FROM SYSCAT.TABCONST", table_constraints),
        ("FROM SYSCAT.KEYCOLUSE WHERE TABSCHEMA = %(schema)s ORDER BY TABNAME, CONSTNAME, COLSEQ", key_columns),
        ("FROM SYSCAT.REFERENCES", references),
        ("FROM SYSCAT.CHECKS", checks),
        ("FROM SYSCAT.INDEXES i", indexes),
        ("FROM SYSCAT.SEQUENCES", sequences),
        ("FROM SYSCAT.VIEWS", views),
        ("FROM SYSCAT.ROUTINES", routines),
        ("FROM SYSCAT.TRIGGERS", triggers),
    ])


# --------------------------------------------------------- _fmt_data_type

def test_fmt_data_type_varchar_uses_length():
    assert _fmt_data_type(("varchar", 255, None)) == "varchar(255)"


def test_fmt_data_type_decimal_uses_length_and_scale():
    assert _fmt_data_type(("decimal", 10, 2)) == "decimal(10,2)"


def test_fmt_data_type_decimal_no_scale():
    assert _fmt_data_type(("decimal", 8, 0)) == "decimal(8)"


def test_fmt_data_type_graphic_sized():
    assert _fmt_data_type(("graphic", 10, None)) == "graphic(10)"


def test_fmt_data_type_decfloat_with_length():
    assert _fmt_data_type(("decfloat", 16, None)) == "decfloat(16)"


def test_fmt_data_type_plain_passthrough():
    assert _fmt_data_type(("timestamp", None, None)) == "timestamp"


# ------------------------------------------------------------ introspect_schema

def test_introspect_basic_tables_and_columns():
    conn = _conn(
        tables=[("EMPLOYEES", "emp table")],
        columns=[
            ("EMPLOYEES", "ID", "integer", None, None, "N", None, "Y"),
            ("EMPLOYEES", "NAME", "varchar", 100, None, "N", None, "N"),
        ],
    )
    schema = introspect_schema(conn, "APP")
    assert schema.source_engine == "Db2"
    assert [t.name for t in schema.tables] == ["EMPLOYEES"]
    table = schema.tables[0]
    assert table.comment == "emp table"
    assert [c.name for c in table.columns] == ["ID", "NAME"]
    assert table.columns[0].data_type == "NUMBER(10)"
    assert table.columns[0].identity is True
    assert table.columns[1].data_type == "VARCHAR2(100)"
    assert table.columns[1].identity is False


def test_introspect_column_reverse_mapping_issue_lands_on_source_issues():
    conn = _conn(
        tables=[("EMPLOYEES", None)],
        columns=[("EMPLOYEES", "ID_CARD", "graphic", 10, None, "Y", None, "N")],
    )
    schema = introspect_schema(conn, "APP")
    col = schema.tables[0].columns[0]
    assert col.data_type == "CHAR(10)"
    assert col.issues == []
    assert any(i.severity == "info" for i in col.source_issues)


def test_introspect_constraints_keyed_by_bare_name_no_disambiguation_needed():
    conn = _conn(
        tables=[("EMPLOYEES", None), ("DEPARTMENTS", None)],
        columns=[
            ("EMPLOYEES", "ID", "integer", None, None, "N", None, "Y"),
            ("DEPARTMENTS", "ID", "integer", None, None, "N", None, "Y"),
        ],
        table_constraints=[
            ("PK_EMPLOYEES", "EMPLOYEES", "P"),
            ("PK_DEPARTMENTS", "DEPARTMENTS", "P"),
        ],
        key_columns=[
            ("PK_EMPLOYEES", "EMPLOYEES", "ID", 1),
            ("PK_DEPARTMENTS", "DEPARTMENTS", "ID", 1),
        ],
    )
    schema = introspect_schema(conn, "APP")
    emp = next(t for t in schema.tables if t.name == "EMPLOYEES")
    dept = next(t for t in schema.tables if t.name == "DEPARTMENTS")
    assert emp.constraints[0].name == "PK_EMPLOYEES"
    assert emp.constraints[0].kind == "PRIMARY KEY"
    assert dept.constraints[0].name == "PK_DEPARTMENTS"
    assert emp.constraints[0].columns == ["ID"]


def test_introspect_foreign_key_resolves_ref_table_and_columns_directly():
    # SYSCAT.REFERENCES gives the referenced table name directly (no
    # second lookup needed, unlike postgres/sqlserver's referential_
    # constraints join).
    conn = _conn(
        tables=[("EMPLOYEES", None), ("DEPARTMENTS", None)],
        columns=[
            ("EMPLOYEES", "DEPT_ID", "integer", None, None, "Y", None, "N"),
            ("DEPARTMENTS", "ID", "integer", None, None, "N", None, "Y"),
        ],
        table_constraints=[
            ("PK_DEPARTMENTS", "DEPARTMENTS", "P"),
            ("FK_EMP_DEPT", "EMPLOYEES", "F"),
        ],
        key_columns=[
            ("PK_DEPARTMENTS", "DEPARTMENTS", "ID", 1),
            ("FK_EMP_DEPT", "EMPLOYEES", "DEPT_ID", 1),
        ],
        references=[("FK_EMP_DEPT", "DEPARTMENTS", "PK_DEPARTMENTS")],
    )
    schema = introspect_schema(conn, "APP")
    emp = next(t for t in schema.tables if t.name == "EMPLOYEES")
    fk = next(c for c in emp.constraints if c.kind == "FOREIGN KEY")
    assert fk.columns == ["DEPT_ID"]
    assert fk.ref_table == "DEPARTMENTS"
    assert fk.ref_columns == ["ID"]


def test_introspect_check_constraints():
    conn = _conn(
        tables=[("EMPLOYEES", None)],
        checks=[("CK_SALARY", "EMPLOYEES", "(SALARY > 0)")],
    )
    schema = introspect_schema(conn, "APP")
    cons = schema.tables[0].constraints[0]
    assert cons.kind == "CHECK"
    assert cons.name == "CK_SALARY"
    assert cons.check_condition == "(SALARY > 0)"


def test_introspect_index_columns_ordered_and_unique_flagged():
    conn = _conn(
        tables=[("EMPLOYEES", None)],
        indexes=[
            ("EMPLOYEES", "IX_EMP_DEPT_NAME", "D", "DEPT_ID"),
            ("EMPLOYEES", "IX_EMP_DEPT_NAME", "D", "NAME"),
            ("EMPLOYEES", "UQ_EMP_EMAIL", "U", "EMAIL"),
        ],
    )
    schema = introspect_schema(conn, "APP")
    idx_by_name = {i.name: i for i in schema.tables[0].indexes}
    assert idx_by_name["IX_EMP_DEPT_NAME"].columns == ["DEPT_ID", "NAME"]
    assert idx_by_name["IX_EMP_DEPT_NAME"].unique is False
    assert idx_by_name["UQ_EMP_EMAIL"].unique is True


def test_introspect_index_name_matches_pk_constraint_name_for_dedup():
    conn = _conn(
        tables=[("EMPLOYEES", None)],
        columns=[("EMPLOYEES", "ID", "integer", None, None, "N", None, "Y")],
        table_constraints=[("PK_EMPLOYEES", "EMPLOYEES", "P")],
        key_columns=[("PK_EMPLOYEES", "EMPLOYEES", "ID", 1)],
        indexes=[("EMPLOYEES", "PK_EMPLOYEES", "P", "ID")],
    )
    schema = introspect_schema(conn, "APP")
    table = schema.tables[0]
    assert table.constraints[0].name == "PK_EMPLOYEES"
    assert table.indexes[0].name == "PK_EMPLOYEES"
    assert table.indexes[0].unique is True


def test_introspect_sequences_native_object():
    conn = _conn(sequences=[("EMPLOYEES_ID_SEQ", 1, 1, 1, 2147483647, "N")])
    schema = introspect_schema(conn, "APP")
    assert len(schema.sequences) == 1
    seq = schema.sequences[0]
    assert seq.name == "EMPLOYEES_ID_SEQ"
    assert seq.start_value == 1
    assert seq.max_value == 2147483647
    assert seq.cycle is False


def test_introspect_views():
    conn = _conn(views=[("V_ACTIVE_EMP", "SELECT * FROM EMPLOYEES WHERE ACTIVE = 1")])
    schema = introspect_schema(conn, "APP")
    assert len(schema.views) == 1
    assert schema.views[0].name == "V_ACTIVE_EMP"
    assert "ACTIVE" in schema.views[0].definition


def test_introspect_routines_are_flagged_with_db2_source_engine():
    conn = _conn(routines=[("RAISE_SALARY", "P", "CREATE PROCEDURE RAISE_SALARY() BEGIN UPDATE T SET X = 1; END")])
    schema = introspect_schema(conn, "APP")
    assert len(schema.routines) == 1
    routine = schema.routines[0]
    assert routine.kind == "PROCEDURE"
    assert routine.source_engine == "Db2"
    assert "UPDATE T" in routine.source


def test_introspect_functions_kind_mapped_correctly():
    conn = _conn(routines=[("CALC_BONUS", "F", "CREATE FUNCTION CALC_BONUS() RETURNS INTEGER BEGIN RETURN 1; END")])
    schema = introspect_schema(conn, "APP")
    assert schema.routines[0].kind == "FUNCTION"


def test_introspect_triggers_one_row_per_trigger_no_fanout():
    # Unlike MySQL/PostgreSQL/SQL Server, a Db2 trigger fires on exactly
    # one event -- SYSCAT.TRIGGERS already has one row per trigger.
    conn = _conn(triggers=[
        ("TRG_AUDIT_BEFORE", "EMPLOYEES", "B", "I", "CREATE TRIGGER TRG_AUDIT_BEFORE ..."),
        ("TRG_AUDIT_AFTER", "EMPLOYEES", "A", "D", "CREATE TRIGGER TRG_AUDIT_AFTER ..."),
    ])
    schema = introspect_schema(conn, "APP")
    assert len(schema.routines) == 2
    by_name = {r.name: r for r in schema.routines}
    assert by_name["TRG_AUDIT_BEFORE"].timing == "BEFORE"
    assert by_name["TRG_AUDIT_BEFORE"].events == ["INSERT"]
    assert by_name["TRG_AUDIT_AFTER"].timing == "AFTER"
    assert by_name["TRG_AUDIT_AFTER"].events == ["DELETE"]
    assert by_name["TRG_AUDIT_BEFORE"].source_engine == "Db2"
    assert by_name["TRG_AUDIT_BEFORE"].table_name == "EMPLOYEES"


def test_introspect_routines_skipped_when_include_routines_false():
    conn = _conn(
        routines=[("RAISE_SALARY", "P", "CREATE PROCEDURE RAISE_SALARY() BEGIN RETURN; END")],
        triggers=[("TRG_AUDIT", "EMPLOYEES", "A", "I", "...")],
    )
    schema = introspect_schema(conn, "APP", include_routines=False)
    assert schema.routines == []
