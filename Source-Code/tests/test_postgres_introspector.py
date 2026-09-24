"""Tests for tgdatabridge.core.postgres_introspector -- reading PostgreSQL's
information_schema (plus a couple of pg_catalog queries) into a Schema,
for using PostgreSQL as a *source* engine. Mirrors
tests/test_mysql_introspector.py's fake-connector approach -- pure data-
shape/mapping tests, not integration tests against a live database."""
from tgdatabridge.core.postgres_introspector import _fmt_data_type, introspect_schema


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
    tables=(), table_comments=(), columns=(), table_constraints=(), key_columns=(),
    referential_constraints=(), check_constraints=(), indexes=(), sequences=(),
    views=(), routines=(), triggers=(),
):
    # `routines` now describes pg_proc rows:
    #   (name, kind, body, arguments, result)
    # and `triggers` gains the trigger function's name:
    #   (name, table, event, timing, orientation, function_name)
    return _FakeConn([
        ("FROM information_schema.tables", tables),
        ("obj_description(c.oid, 'pg_class')", table_comments),
        ("FROM information_schema.columns", columns),
        ("SELECT constraint_name, table_name, constraint_type FROM information_schema.table_constraints", table_constraints),
        ("FROM information_schema.key_column_usage WHERE table_schema = %(schema)s ORDER BY table_name, constraint_name, ordinal_position", key_columns),
        ("FROM information_schema.referential_constraints", referential_constraints),
        ("information_schema.check_constraints", check_constraints),
        ("FROM pg_index ix", indexes),
        ("FROM information_schema.sequences", sequences),
        ("FROM information_schema.views", views),
        ("FROM pg_catalog.pg_proc p", routines),
        ("FROM information_schema.triggers t", triggers),
    ])


# --------------------------------------------------------- _fmt_data_type

def test_fmt_data_type_varchar_uses_character_maximum_length():
    assert _fmt_data_type(("character varying", "varchar", 255, None, None, None)) == "character varying(255)"


def test_fmt_data_type_numeric_uses_precision_and_scale():
    assert _fmt_data_type(("numeric", "numeric", None, 10, 2, None)) == "numeric(10,2)"


def test_fmt_data_type_numeric_no_scale():
    assert _fmt_data_type(("numeric", "numeric", None, 8, 0, None)) == "numeric(8)"


def test_fmt_data_type_unqualified_numeric():
    assert _fmt_data_type(("numeric", "numeric", None, None, None, None)) == "numeric"


def test_fmt_data_type_array_uses_udt_name_element_type():
    assert _fmt_data_type(("ARRAY", "_int4", None, None, None, None)) == "int4[]"


def test_fmt_data_type_user_defined_uses_udt_name():
    assert _fmt_data_type(("USER-DEFINED", "mood", None, None, None, None)) == "mood"


def test_fmt_data_type_interval_uses_interval_type():
    assert _fmt_data_type(("interval", "interval", None, None, None, "DAY TO SECOND")) == "interval day to second"


def test_fmt_data_type_interval_no_qualifier():
    assert _fmt_data_type(("interval", "interval", None, None, None, None)) == "interval"


def test_fmt_data_type_plain_passthrough():
    assert _fmt_data_type(("boolean", "bool", None, None, None, None)) == "boolean"


# ------------------------------------------------------------ introspect_schema

def test_introspect_basic_tables_and_columns():
    conn = _conn(
        tables=[("employees",)],
        columns=[
            ("employees", "id", "integer", "int4", None, 10, 0, None, "NO", "nextval('employees_id_seq'::regclass)", "NO"),
            ("employees", "name", "character varying", "varchar", 100, None, None, None, "NO", None, "NO"),
        ],
    )
    schema = introspect_schema(conn, "public")
    assert schema.source_engine == "PostgreSQL"
    assert [t.name for t in schema.tables] == ["employees"]
    table = schema.tables[0]
    assert [c.name for c in table.columns] == ["id", "name"]
    assert table.columns[0].data_type == "NUMBER(10)"
    assert table.columns[1].data_type == "VARCHAR2(100)"


def test_introspect_serial_default_column_is_identity_and_default_is_not_leaked():
    conn = _conn(
        tables=[("employees",)],
        columns=[
            ("employees", "id", "integer", "int4", None, 10, 0, None, "NO", "nextval('employees_id_seq'::regclass)", "NO"),
        ],
    )
    schema = introspect_schema(conn, "public")
    col = schema.tables[0].columns[0]
    assert col.identity is True
    assert col.default is None  # nextval(...) plumbing isn't a meaningful "default" on the target


def test_introspect_generated_identity_column_is_identity():
    conn = _conn(
        tables=[("employees",)],
        columns=[
            ("employees", "id", "integer", "int4", None, 10, 0, None, "NO", None, "YES"),
        ],
    )
    schema = introspect_schema(conn, "public")
    assert schema.tables[0].columns[0].identity is True


def test_introspect_column_reverse_mapping_issue_lands_on_source_issues():
    conn = _conn(
        tables=[("employees",)],
        columns=[("employees", "id_card", "uuid", "uuid", None, None, None, None, "YES", None, "NO")],
    )
    schema = introspect_schema(conn, "public")
    col = schema.tables[0].columns[0]
    assert col.data_type == "VARCHAR2(36)"
    assert col.issues == []
    assert any(i.severity == "warning" for i in col.source_issues)


def test_introspect_table_comment_is_best_effort():
    conn = _conn(
        tables=[("employees",)],
        table_comments=[("employees", "the employee master table")],
    )
    schema = introspect_schema(conn, "public")
    assert schema.tables[0].comment == "the employee master table"


def test_introspect_table_comment_missing_pg_catalog_access_is_tolerated():
    class _RaisingConn(_FakeConn):
        def execute(self, sql, params=None):
            if "obj_description" in sql:
                raise Exception("permission denied for table pg_description")
            return super().execute(sql, params)

    base = _conn(tables=[("employees",)])
    conn = _RaisingConn(base._routes)
    schema = introspect_schema(conn, "public")
    assert schema.tables[0].comment is None


def test_introspect_constraints_keyed_by_bare_name_no_disambiguation_needed():
    # Unlike MySQL, PostgreSQL constraint names are already unique within a
    # schema -- no "PRIMARY"-style collision to work around.
    conn = _conn(
        tables=[("employees",), ("departments",)],
        columns=[
            ("employees", "id", "integer", "int4", None, 10, 0, None, "NO", None, "NO"),
            ("departments", "id", "integer", "int4", None, 10, 0, None, "NO", None, "NO"),
        ],
        table_constraints=[
            ("employees_pkey", "employees", "PRIMARY KEY"),
            ("departments_pkey", "departments", "PRIMARY KEY"),
        ],
        key_columns=[
            ("employees_pkey", "employees", "id", 1),
            ("departments_pkey", "departments", "id", 1),
        ],
    )
    schema = introspect_schema(conn, "public")
    emp = next(t for t in schema.tables if t.name == "employees")
    dept = next(t for t in schema.tables if t.name == "departments")
    assert emp.constraints[0].name == "employees_pkey"
    assert dept.constraints[0].name == "departments_pkey"
    assert emp.constraints[0].columns == ["id"]


def test_introspect_foreign_key_resolves_ref_table_and_columns_via_referential_constraints():
    conn = _conn(
        tables=[("employees",), ("departments",)],
        columns=[
            ("employees", "dept_id", "integer", "int4", None, 10, 0, None, "YES", None, "NO"),
            ("departments", "id", "integer", "int4", None, 10, 0, None, "NO", None, "NO"),
        ],
        table_constraints=[
            ("departments_pkey", "departments", "PRIMARY KEY"),
            ("fk_emp_dept", "employees", "FOREIGN KEY"),
        ],
        key_columns=[
            ("departments_pkey", "departments", "id", 1),
            ("fk_emp_dept", "employees", "dept_id", 1),
        ],
        referential_constraints=[("fk_emp_dept", "departments_pkey")],
    )
    schema = introspect_schema(conn, "public")
    emp = next(t for t in schema.tables if t.name == "employees")
    fk = next(c for c in emp.constraints if c.kind == "FOREIGN KEY")
    assert fk.columns == ["dept_id"]
    assert fk.ref_table == "departments"
    assert fk.ref_columns == ["id"]


def test_introspect_check_constraints_excludes_auto_not_null_checks():
    conn = _conn(
        tables=[("employees",)],
        check_constraints=[
            ("employees_name_not_null", "employees", '"name" IS NOT NULL'),
            ("employees_salary_check", "employees", "salary > 0"),
        ],
    )
    schema = introspect_schema(conn, "public")
    names = [c.name for c in schema.tables[0].constraints]
    assert "employees_name_not_null" not in names
    assert "employees_salary_check" in names
    real_check = next(c for c in schema.tables[0].constraints if c.name == "employees_salary_check")
    assert real_check.kind == "CHECK"
    assert real_check.check_condition == "salary > 0"


def test_introspect_index_columns_ordered_and_unique_flagged():
    conn = _conn(
        tables=[("employees",)],
        indexes=[
            ("employees", "idx_emp_name_dept", False, "dept_id"),
            ("employees", "idx_emp_name_dept", False, "name"),
            ("employees", "employees_email_key", True, "email"),
        ],
    )
    schema = introspect_schema(conn, "public")
    idx_by_name = {i.name: i for i in schema.tables[0].indexes}
    assert idx_by_name["idx_emp_name_dept"].columns == ["dept_id", "name"]
    assert idx_by_name["idx_emp_name_dept"].unique is False
    assert idx_by_name["employees_email_key"].unique is True


def test_introspect_index_name_matches_pk_constraint_name_for_dedup():
    # PostgreSQL always names a PK's backing index the same as the
    # constraint -- ddl_generator's existing dedup (skip an index whose name
    # matches a PK/UNIQUE constraint on the same table) should Just Work
    # here with no renaming, unlike mysql_introspector's "PRIMARY" handling.
    conn = _conn(
        tables=[("employees",)],
        columns=[("employees", "id", "integer", "int4", None, 10, 0, None, "NO", None, "NO")],
        table_constraints=[("employees_pkey", "employees", "PRIMARY KEY")],
        key_columns=[("employees_pkey", "employees", "id", 1)],
        indexes=[("employees", "employees_pkey", True, "id")],
    )
    schema = introspect_schema(conn, "public")
    table = schema.tables[0]
    assert table.constraints[0].name == "employees_pkey"
    assert table.indexes[0].name == "employees_pkey"


def test_introspect_sequences_native_object():
    conn = _conn(
        sequences=[("employees_id_seq", 1, 1, 1, 2147483647, "NO")],
    )
    schema = introspect_schema(conn, "public")
    assert len(schema.sequences) == 1
    seq = schema.sequences[0]
    assert seq.name == "employees_id_seq"
    assert seq.start_value == 1
    assert seq.increment_by == 1
    assert seq.max_value == 2147483647
    assert seq.cycle is False


def test_introspect_views():
    conn = _conn(views=[("v_active_emp", "SELECT * FROM employees WHERE active")])
    schema = introspect_schema(conn, "public")
    assert len(schema.views) == 1
    assert schema.views[0].name == "v_active_emp"
    assert "active" in schema.views[0].definition


def test_introspect_routines_are_flagged_with_postgres_source_engine():
    conn = _conn(routines=[
        ("raise_salary", "FUNCTION", "BEGIN UPDATE t SET x = 1; END;",
         "p_id integer, p_pct numeric DEFAULT 1.5", "numeric"),
    ])
    schema = introspect_schema(conn, "public")
    assert len(schema.routines) == 1
    routine = schema.routines[0]
    assert routine.kind == "FUNCTION"
    assert routine.source_engine == "PostgreSQL"
    assert "UPDATE t" in routine.source


def test_introspect_reads_the_signature_that_information_schema_never_had():
    """routine_definition is the body alone, so a PostgreSQL function used
    to arrive with no parameter list at all -- nothing downstream could
    rebuild one for any target."""
    conn = _conn(routines=[
        ("raise_salary", "FUNCTION", "BEGIN NULL; END;",
         "p_id integer, INOUT p_pct numeric DEFAULT 1.5", "numeric"),
    ])
    routine = introspect_schema(conn, "public").routines[0]
    assert [(p.name, p.data_type, p.mode) for p in routine.parameters] == [
        ("p_id", "integer", "IN"), ("p_pct", "numeric", "INOUT")]
    assert routine.parameters[1].default == "1.5"
    assert routine.return_type == "numeric"


def test_a_trigger_function_is_not_also_listed_as_a_function():
    """It is the body of its trigger, not a routine anyone calls; listing
    it separately migrated it twice and showed the user a function they
    never wrote."""
    conn = _conn(routines=[
        ("trg_audit_fn", "FUNCTION", "BEGIN RETURN NEW; END;", "", "trigger"),
        ("real_fn", "FUNCTION", "BEGIN RETURN 1; END;", "", "integer"),
    ])
    assert [r.name for r in introspect_schema(conn, "public").routines] == ["real_fn"]


def test_a_trigger_carries_its_functions_body_not_execute_function():
    """information_schema.triggers.action_statement is literally
    "EXECUTE FUNCTION foo()" on PostgreSQL, so a trigger used to migrate
    as an empty shell."""
    conn = _conn(
        routines=[("trg_audit_fn", "FUNCTION",
                   "BEGIN INSERT INTO audit VALUES (NEW.id); RETURN NEW; END;",
                   "", "trigger")],
        triggers=[("trg_audit", "employees", "INSERT", "AFTER", "ROW", "trg_audit_fn")],
    )
    routine = [r for r in introspect_schema(conn, "public").routines
               if r.kind == "TRIGGER"][0]
    assert "INSERT INTO audit" in routine.source
    assert routine.trigger_function == "trg_audit_fn"


def test_a_multi_event_trigger_is_one_routine_not_one_per_event():
    """information_schema.triggers has a row per firing event. Passing
    those through gave the rest of the tool three copies of one trigger
    under one name."""
    conn = _conn(triggers=[
        ("trg_x", "employees", "INSERT", "BEFORE", "ROW", "trg_x_fn"),
        ("trg_x", "employees", "UPDATE", "BEFORE", "ROW", "trg_x_fn"),
        ("trg_x", "employees", "DELETE", "BEFORE", "ROW", "trg_x_fn"),
    ])
    routines = introspect_schema(conn, "public").routines
    assert len(routines) == 1
    assert sorted(routines[0].events) == ["DELETE", "INSERT", "UPDATE"]


def test_introspect_triggers_row_level_vs_statement_level():
    conn = _conn(triggers=[
        ("trg_audit_row", "employees", "INSERT", "AFTER", "ROW", "trg_audit_row_fn"),
        ("trg_audit_stmt", "employees", "DELETE", "AFTER", "STATEMENT", "trg_audit_stmt_fn"),
    ])
    schema = introspect_schema(conn, "public")
    by_name = {r.name: r for r in schema.routines}
    assert by_name["trg_audit_row"].row_level is True
    assert by_name["trg_audit_stmt"].row_level is False
    assert by_name["trg_audit_row"].source_engine == "PostgreSQL"


def test_introspect_routines_skipped_when_include_routines_false():
    conn = _conn(
        routines=[("raise_salary", "FUNCTION", "BEGIN NULL; END;", "", "integer")],
        triggers=[("trg_audit", "employees", "INSERT", "AFTER", "...", "ROW")],
    )
    schema = introspect_schema(conn, "public", include_routines=False)
    assert schema.routines == []
