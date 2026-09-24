"""
Regression tests for SQL Server -> MySQL/PostgreSQL conversion.

Each test here corresponds to a failure that was reproduced against a real
MySQL/MariaDB and PostgreSQL server before the fix, so the assertions are
about SQL that provably did or did not run, not about formatting.
"""
import re

import pytest

from tgdatabridge.core import ddl_generator, tsql_dialect
from tgdatabridge.core.plsql_converter import convert_routine
from tgdatabridge.core.schema_model import (
    Column, Constraint, ConversionStatus, Index, Routine, Schema, Table, View,
)
from tgdatabridge.utils.sql_split import has_executable_sql, split_sql_statements


# --------------------------------------------------------------- defaults

@pytest.mark.parametrize("target,expected", [
    ("MySQL", "CURRENT_TIMESTAMP"),
    ("PostgreSQL", "CURRENT_TIMESTAMP"),
])
def test_getdate_default_is_translated(target, expected):
    # The exact failure in the bug report: MySQL 3770 "Default value
    # expression of column 'CreatedDate' contains a disallowed function:
    # getdate", PostgreSQL "function getdate() does not exist".
    clause, issues = tsql_dialect.translate_default(
        "(getdate())", "SQL Server", target, "CreatedDate", "DATETIME")
    assert clause == expected
    assert not [i for i in issues if i.severity == "error"]


def test_sqlserver_wrapping_parens_are_stripped_from_literals():
    assert tsql_dialect.translate_default("((0))", "SQL Server", "MySQL", "n", "INT")[0] == "0"
    assert tsql_dialect.translate_default(
        "(N'unknown')", "SQL Server", "PostgreSQL", "s", "VARCHAR(10)")[0] == "'unknown'"


def test_bit_default_becomes_boolean_on_postgres():
    # PostgreSQL: 'column "isactive" is of type boolean but default
    # expression is of type integer'.
    assert tsql_dialect.translate_default(
        "((1))", "SQL Server", "PostgreSQL", "IsActive", "BOOLEAN")[0] == "TRUE"
    assert tsql_dialect.translate_default(
        "((0))", "SQL Server", "PostgreSQL", "IsActive", "BOOLEAN")[0] == "FALSE"
    # MySQL keeps TINYINT(1), where 0/1 is already right.
    assert tsql_dialect.translate_default(
        "((1))", "SQL Server", "MySQL", "IsActive", "TINYINT(1)")[0] == "1"


def test_mysql_rejects_nondeterministic_defaults_so_they_are_dropped_loudly():
    clause, issues = tsql_dialect.translate_default(
        "(newid())", "SQL Server", "MySQL", "RowGuid", "CHAR(36)")
    assert clause is None
    # A warning, not an error: the column and its table still convert and
    # create correctly, so the object tree must not mark the whole table
    # "Requires manual conversion" over a dropped default.
    assert any(i.severity == "warning" and "RowGuid" in i.message for i in issues)


def test_a_dropped_default_does_not_fail_the_whole_table():
    t = Table(name="EmployeeSalaryAudit", schema="dbo", columns=[
        Column(name="AuditID", data_type="NUMBER(19)", nullable=False, identity=True),
        Column(name="ChangedBy", data_type="VARCHAR2(128)", default="(suser_sname())"),
    ])
    ddl, _ = ddl_generator.generate_table_ddl_mysql(t, source_engine="SQL Server")
    assert "suser_sname" not in ddl.lower()
    assert t.status == ConversionStatus.AUTOMATIC_WITH_WARNINGS, t.status


def test_postgres_accepts_uuid_and_user_defaults():
    assert tsql_dialect.translate_default(
        "(newid())", "SQL Server", "PostgreSQL", "g", "UUID")[0] == "gen_random_uuid()"
    assert tsql_dialect.translate_default(
        "(suser_sname())", "SQL Server", "PostgreSQL", "u", "VARCHAR(128)")[0] == "CURRENT_USER"


def test_mysql_expression_default_is_parenthesised_but_literals_are_not():
    # MySQL 8 requires an expression default to be parenthesised, and
    # requires CURRENT_TIMESTAMP *not* to be.
    assert tsql_dialect.translate_default(
        "(getdate())", "SQL Server", "MySQL", "d", "DATETIME")[0] == "CURRENT_TIMESTAMP"
    assert tsql_dialect.translate_default(
        "(abs(-1))", "SQL Server", "MySQL", "n", "INT")[0] == "(abs(-1))"


def test_oracle_sysdate_default_still_works():
    # The pre-existing Oracle path must be unaffected.
    assert tsql_dialect.translate_default(
        "SYSDATE", "Oracle", "PostgreSQL", "d", "TIMESTAMP")[0] == "CURRENT_TIMESTAMP"


def test_non_relational_targets_pass_defaults_through_unchanged():
    for target in ("Oracle", "SQL Server", "Db2"):
        assert tsql_dialect.translate_default(
            "(getdate())", "SQL Server", target, "d", "DATE")[0] == "(getdate())"


# ------------------------------------------------------------ expressions

def test_isnull_and_len_map_per_target():
    assert "IFNULL(" in tsql_dialect.translate_expression("ISNULL(a,0)", "MySQL")[0]
    assert "COALESCE(" in tsql_dialect.translate_expression("ISNULL(a,0)", "PostgreSQL")[0]
    assert "CHAR_LENGTH(" in tsql_dialect.translate_expression("LEN(a)", "MySQL")[0]
    assert "LENGTH(" in tsql_dialect.translate_expression("LEN(a)", "PostgreSQL")[0]


def test_bracket_identifiers_and_dbo_prefix_are_removed():
    out, _ = tsql_dialect.translate_expression("SELECT * FROM [dbo].[Employees]", "MySQL")
    assert "`Employees`" in out
    assert "dbo" not in out


def test_top_becomes_limit():
    out, _ = tsql_dialect.translate_expression("SELECT TOP 5 a FROM t ORDER BY a", "MySQL")
    assert out.rstrip().endswith("LIMIT 5")
    assert "TOP" not in out


def test_string_concat_plus_is_rewritten_only_when_a_literal_proves_it_is_text():
    mysql, _ = tsql_dialect.translate_expression("SELECT a + ' ' + b, qty + 1 FROM t", "MySQL")
    assert "CONCAT(a, ' ', b)" in mysql
    assert "qty + 1" in mysql          # arithmetic left alone
    pg, _ = tsql_dialect.translate_expression("SELECT a + ' ' + b, qty + 1 FROM t", "PostgreSQL")
    assert "a || ' ' || b" in pg
    assert "qty + 1" in pg


def test_pure_arithmetic_is_never_touched():
    for target in ("MySQL", "PostgreSQL"):
        out, _ = tsql_dialect.translate_expression("SELECT a + b + c FROM t", target)
        assert out == "SELECT a + b + c FROM t"


def test_string_literals_are_never_rewritten():
    out, _ = tsql_dialect.translate_expression("SELECT 'call GETDATE() here' AS s", "MySQL")
    assert "'call GETDATE() here'" in out


def test_unsupported_constructs_are_reported_not_guessed():
    _out, issues = tsql_dialect.translate_expression("SELECT * FROM OPENQUERY(x, 'y')", "MySQL")
    assert any(i.severity == "error" for i in issues)


# --------------------------------------------------------------- routines

FUNCTION_SRC = """CREATE FUNCTION dbo.fn_AnnualSalary (@Monthly DECIMAL(10,2))
RETURNS DECIMAL(18,2)
AS
BEGIN
    DECLARE @Annual DECIMAL(18,2);
    SET @Annual = ISNULL(@Monthly, 0) * 12;
    RETURN @Annual;
END"""

PROCEDURE_SRC = """CREATE PROCEDURE [dbo].[usp_ByDept]
    @DepartmentID INT,
    @MinSalary DECIMAL(10,2) = 0
AS
BEGIN
    SET NOCOUNT ON;
    IF @DepartmentID IS NULL
    BEGIN
        RAISERROR('DepartmentID is required', 16, 1);
        RETURN;
    END
    SELECT TOP 100 e.EmployeeID, ISNULL(e.Salary,0) AS Salary
    FROM dbo.Employees e
    WHERE e.DepartmentID = @DepartmentID
    ORDER BY e.Salary DESC;
END"""

TRIGGER_SRC = """CREATE TRIGGER dbo.trg_SalaryAudit
ON dbo.Employees
AFTER UPDATE
AS
BEGIN
    SET NOCOUNT ON;
    IF UPDATE(Salary)
    BEGIN
        INSERT INTO dbo.SalaryAudit (EmployeeID, OldSalary, NewSalary, ChangedBy)
        SELECT i.EmployeeID, d.Salary AS OldSalary, i.Salary AS NewSalary, SUSER_SNAME()
        FROM inserted i
        INNER JOIN deleted d ON i.EmployeeID = d.EmployeeID
        WHERE ISNULL(i.Salary, 0) <> ISNULL(d.Salary, 0);
    END
END"""


def _routine(kind, name, source, **kw):
    return Routine(name=name, schema="dbo", kind=kind, source=source,
                   source_engine="SQL Server", **kw)


@pytest.mark.parametrize("target", ["MySQL", "PostgreSQL"])
def test_function_converts_automatically(target):
    r = _routine("FUNCTION", "fn_AnnualSalary", FUNCTION_SRC)
    convert_routine(r, target)
    assert r.status == ConversionStatus.AUTOMATIC, [i.message for i in r.issues]
    assert "fn_AnnualSalary".lower() in r.converted_source.lower()
    assert "@" not in r.converted_source          # no T-SQL variable sigils survive
    assert "v_Monthly" in r.converted_source


@pytest.mark.parametrize("target", ["MySQL", "PostgreSQL"])
def test_procedure_converts_and_keeps_no_tsql_isms(target):
    r = _routine("PROCEDURE", "usp_ByDept", PROCEDURE_SRC)
    convert_routine(r, target)
    assert r.status != ConversionStatus.MANUAL, [i.message for i in r.issues]
    body = r.converted_source
    assert "SET NOCOUNT" not in body
    assert "RAISERROR" not in body
    assert "TOP 100" not in body and "LIMIT 100" in body
    assert "ISNULL(" not in body
    assert "dbo." not in body


def test_procedure_early_return_uses_leave_on_mysql():
    # MySQL rejects a bare RETURN outside a stored *function*, so the body
    # has to be labelled and exited with LEAVE.
    r = _routine("PROCEDURE", "usp_ByDept", PROCEDURE_SRC)
    convert_routine(r, "MySQL")
    assert "proc_exit: BEGIN" in r.converted_source
    assert "LEAVE proc_exit;" in r.converted_source
    assert "END proc_exit;" in r.converted_source


def test_procedure_returning_rows_becomes_a_refcursor_function_on_postgres():
    r = _routine("PROCEDURE", "usp_ByDept", PROCEDURE_SRC)
    convert_routine(r, "PostgreSQL")
    assert "RETURNS refcursor" in r.converted_source
    assert "OPEN result_cursor FOR" in r.converted_source
    # A bare RETURN is illegal in a value-returning function.
    assert "\n  RETURN;" not in r.converted_source


@pytest.mark.parametrize("target", ["MySQL", "PostgreSQL"])
def test_trigger_pseudo_tables_become_new_and_old(target):
    r = _routine("TRIGGER", "trg_SalaryAudit", TRIGGER_SRC,
                 table_name="Employees", timing="AFTER", events=["UPDATE"])
    convert_routine(r, target)
    assert r.status == ConversionStatus.AUTOMATIC, [i.message for i in r.issues]
    body = r.converted_source
    assert "inserted" not in body.lower().replace("insert into", "")
    assert "NEW." in body and "OLD." in body
    assert "VALUES (" in body            # set-based INSERT..SELECT became row-level
    assert " AS OldSalary" not in body   # SELECT aliases stripped for VALUES


def test_postgres_trigger_emits_function_plus_create_trigger():
    r = _routine("TRIGGER", "trg_SalaryAudit", TRIGGER_SRC,
                 table_name="Employees", timing="AFTER", events=["UPDATE"])
    convert_routine(r, "PostgreSQL")
    assert "RETURNS trigger" in r.converted_source
    assert "EXECUTE FUNCTION" in r.converted_source


def test_instead_of_trigger_is_still_flagged_manual():
    src = TRIGGER_SRC.replace("AFTER UPDATE", "INSTEAD OF UPDATE")
    r = _routine("TRIGGER", "trg_SalaryAudit", src, table_name="Employees")
    convert_routine(r, "MySQL")
    assert r.status == ConversionStatus.MANUAL
    assert any("INSTEAD OF" in i.message for i in r.issues)


def test_dynamic_sql_is_still_flagged_manual():
    src = ("CREATE PROCEDURE dbo.p AS BEGIN DECLARE @s NVARCHAR(200); "
           "SET @s = N'SELECT 1'; EXEC(@s); END")
    r = _routine("PROCEDURE", "p", src)
    convert_routine(r, "MySQL")
    assert r.status == ConversionStatus.MANUAL
    assert any("Dynamic SQL" in i.message for i in r.issues)


def test_unparseable_routine_degrades_to_manual_not_a_crash():
    r = _routine("PROCEDURE", "p", "this is not T-SQL at all")
    convert_routine(r, "MySQL")
    assert r.status == ConversionStatus.MANUAL
    assert "MANUAL CONVERSION REQUIRED" in r.converted_source


def test_oracle_sourced_routines_still_use_the_oracle_converter():
    r = Routine(name="p", schema="s", kind="PROCEDURE",
                source="PROCEDURE p IS BEGIN NULL; END;", source_engine="Oracle")
    convert_routine(r, "PostgreSQL")
    # Whatever the Oracle path decides, it must not have gone through the
    # T-SQL front end (which would have failed to find a CREATE header).
    assert "not T-SQL" not in (r.converted_source or "")
    assert "SQL Server" not in (r.converted_source or "")


def test_sqlserver_routines_to_an_oracle_target_are_still_manual():
    r = _routine("FUNCTION", "fn_AnnualSalary", FUNCTION_SRC)
    convert_routine(r, "Oracle")
    assert r.status == ConversionStatus.MANUAL


# ------------------------------------------------------------------ views

def test_sqlserver_view_body_is_translated():
    v = View(name="vw_Emp", schema="dbo", source_engine="SQL Server", definition=(
        "SELECT e.ID, e.First + N' ' + e.Last AS FullName, ISNULL(e.Sal,0) AS Sal, GETDATE() AS AsOf "
        "FROM [dbo].[Employees] e"))
    ddl, issues = ddl_generator.generate_view_ddl(v, "MySQL")
    assert v.status == ConversionStatus.AUTOMATIC, [i.message for i in issues]
    assert "CONCAT(" in ddl and "IFNULL(" in ddl and "CURRENT_TIMESTAMP" in ddl
    assert "[dbo]" not in ddl


def test_oracle_sourced_view_is_not_run_through_the_tsql_translator():
    # An Oracle-sourced view must not be treated as if it were T-SQL (the
    # translate_expression() call a few lines above this in
    # generate_view_ddl is gated on source_engine == "SQL Server") -- but
    # SYSDATE is still rewritten to CURRENT_TIMESTAMP, by generate_view_ddl's
    # own separate, Oracle-specific substitution pass (see
    # test_ddl_generator_view_nvl_sysdate.py for that fix's own tests).
    v = View(name="V", schema="S", definition="SELECT SYSDATE FROM DUAL")
    ddl, _ = ddl_generator.generate_view_ddl(v, "PostgreSQL")
    assert "SYSDATE" not in ddl
    assert "CURRENT_TIMESTAMP" in ddl


# --------------------------------------------------- identity / defer bugs

def _schema_with_identity(target):
    s = Schema(name="dbo", source_engine="SQL Server", target_engine=target)
    t = Table(name="Audit", schema="dbo", columns=[
        Column(name="AuditID", data_type="NUMBER(19)", nullable=False, identity=True),
        Column(name="Note", data_type="VARCHAR2(50)"),
    ])
    t.constraints = [Constraint(name="PK_Audit", kind="PRIMARY KEY", columns=["AuditID"])]
    t.indexes = [Index(name="IX_Note", columns=["Note"])]
    s.tables = [t]
    return s


def test_bigint_identity_is_widened_to_a_type_the_target_accepts():
    # MySQL error 1063 "Incorrect column specifier"; PostgreSQL "identity
    # column type must be smallint, integer, or bigint".
    ddl, _ = ddl_generator.generate_table_ddl_mysql(
        _schema_with_identity("MySQL").tables[0], source_engine="SQL Server")
    assert "BIGINT AUTO_INCREMENT" in ddl
    assert "DECIMAL(19,0) AUTO_INCREMENT" not in ddl

    ddl, _ = ddl_generator.generate_table_ddl_postgres(
        _schema_with_identity("PostgreSQL").tables[0], source_engine="SQL Server")
    assert "BIGINT GENERATED ALWAYS AS IDENTITY" in ddl
    assert "NUMERIC(19) GENERATED" not in ddl


def test_mysql_keeps_the_primary_key_inline_when_constraints_are_deferred():
    # InnoDB error 1075: an AUTO_INCREMENT column must be part of a key at
    # CREATE TABLE time, so this one key cannot be deferred.
    table = _schema_with_identity("MySQL").tables[0]
    ddl, issues = ddl_generator.generate_table_ddl_mysql(
        table, defer_constraints=True, source_engine="SQL Server")
    assert "PRIMARY KEY (`AuditID`)" in ddl
    assert "IX_Note" not in ddl          # every *other* index is still deferred
    assert any("AUTO_INCREMENT" in i.message for i in issues)


def test_deferred_mysql_ddl_does_not_re_add_that_primary_key():
    # ...which would be error 1068, "Multiple primary key defined".
    table = _schema_with_identity("MySQL").tables[0]
    deferred = ddl_generator.generate_deferred_ddl_mysql(table, "SQL Server")
    assert "ADD PRIMARY KEY" not in deferred
    assert "IX_Note" in deferred


def test_mysql_table_without_identity_still_defers_its_primary_key():
    t = Table(name="Lookup", schema="dbo", columns=[
        Column(name="Code", data_type="VARCHAR2(10)", nullable=False)])
    t.constraints = [Constraint(name="PK_Lookup", kind="PRIMARY KEY", columns=["Code"])]
    ddl, _ = ddl_generator.generate_table_ddl_mysql(t, defer_constraints=True)
    assert "PRIMARY KEY" not in ddl
    assert "ADD PRIMARY KEY" in ddl_generator.generate_deferred_ddl_mysql(t)


# ---------------------------------------------------------- apply filtering

def test_comment_only_blocks_are_not_sent_to_a_server():
    placeholder = ("-- MANUAL CONVERSION REQUIRED for PROCEDURE p\n"
                   "/*\nCREATE PROCEDURE p AS BEGIN SELECT 1; END\n*/\n;")
    script = placeholder + '\nCREATE TABLE t (a INT);'
    statements = split_sql_statements(script)
    # The placeholder is still in the script the user reads and saves...
    assert len(statements) == 2
    # ...but is never executed (MySQL: "Query was empty"; psycopg refuses).
    assert [s for s in statements if has_executable_sql(s)] == [statements[1]]


def test_real_sql_is_always_executable():
    for sql in ("CREATE TABLE t (a INT)", "-- header\nSELECT 1", "/* note */ SELECT 1"):
        assert has_executable_sql(sql)


def test_full_schema_ddl_has_no_untranslated_tsql_left():
    s = _schema_with_identity("MySQL")
    s.tables[0].columns[1].default = "(getdate())"
    s.views = [View(name="V", schema="dbo", source_engine="SQL Server",
                    definition="SELECT ISNULL(a,0) FROM [dbo].[T]")]
    s.routines = [_routine("TRIGGER", "trg", TRIGGER_SRC, table_name="Employees",
                           timing="AFTER", events=["UPDATE"])]
    for target in ("MySQL", "PostgreSQL"):
        ddl, _ = ddl_generator.generate_schema_ddl(s, target)
        executable = "\n".join(
            s_ for s_ in split_sql_statements(ddl) if has_executable_sql(s_))
        for token in ("getdate(", "ISNULL(", "[dbo]", "SUSER_SNAME("):
            assert token not in executable, f"{token} survived for {target}"


# ------------------------------------------- MySQL fractional-seconds rule

@pytest.mark.parametrize("target_type,expected", [
    ("DATETIME(6)",  "CURRENT_TIMESTAMP(6)"),
    ("DATETIME(3)",  "CURRENT_TIMESTAMP(3)"),
    ("TIMESTAMP(6)", "CURRENT_TIMESTAMP(6)"),
    ("DATETIME",     "CURRENT_TIMESTAMP"),
    ("DATETIME(0)",  "CURRENT_TIMESTAMP"),
])
def test_mysql_default_matches_the_columns_fractional_precision(target_type, expected):
    # MySQL: "If a TIMESTAMP or DATETIME column definition includes an
    # explicit fractional seconds precision value, the same value must be
    # used throughout the column definition." Mismatched precision is
    # error 1067, "Invalid default value for 'CreatedDate'" -- which is
    # what a SQL Server `datetime` (-> DATETIME(6)) carrying (getdate())
    # produced on a real MySQL 8 server. MariaDB accepts the unqualified
    # form, so this must be driven by the rule, not by one server's
    # tolerance.
    clause, issues = tsql_dialect.translate_default(
        "(getdate())", "SQL Server", "MySQL", "CreatedDate", target_type)
    assert clause == expected
    assert not [i for i in issues if i.severity == "error"]


def test_postgres_timestamp_default_is_not_given_a_precision():
    # PostgreSQL has no such constraint, and CURRENT_TIMESTAMP(6) there
    # would be a different (and unnecessary) thing.
    clause, _ = tsql_dialect.translate_default(
        "(getdate())", "SQL Server", "PostgreSQL", "CreatedDate", "TIMESTAMP(6)")
    assert clause == "CURRENT_TIMESTAMP"


def test_generated_mysql_ddl_carries_matching_precision_end_to_end():
    t = Table(name="Departments", schema="dbo", columns=[
        Column(name="CreatedDate", data_type="DATE", default="(getdate())"),
    ])
    ddl, _ = ddl_generator.generate_table_ddl_mysql(t, source_engine="SQL Server")
    # Whatever precision the type mapping produced, the default must agree.
    m = re.search(r"`CreatedDate`\s+(\S+)\s+DEFAULT\s+(CURRENT_TIMESTAMP(?:\(\d+\))?)", ddl)
    assert m, ddl
    col_type, default = m.group(1), m.group(2)
    fsp = re.search(r"\((\d+)\)", col_type)
    if fsp and fsp.group(1) != "0":
        assert default == f"CURRENT_TIMESTAMP({fsp.group(1)})", ddl
    else:
        assert default == "CURRENT_TIMESTAMP", ddl


# ------------------------------------- foreign-key column type agreement

def _fk_schema(target):
    s = Schema(name="dbo", source_engine="SQL Server", target_engine=target)
    emp = Table(name="Employees", schema="dbo", columns=[
        # bigint IDENTITY -> widened to BIGINT so the target accepts it as
        # auto-increment (see _identity_type).
        Column(name="EmployeeID", data_type="NUMBER(19)", nullable=False, identity=True),
    ])
    emp.constraints = [Constraint(name="PK_Emp", kind="PRIMARY KEY", columns=["EmployeeID"])]
    att = Table(name="EmployeeAttendance", schema="dbo", columns=[
        Column(name="AttendanceID", data_type="NUMBER(19)", nullable=False, identity=True),
        # plain bigint -> would map to DECIMAL(19,0)/NUMERIC(19) on its own
        Column(name="EmployeeID", data_type="NUMBER(19)", nullable=False),
    ])
    att.constraints = [
        Constraint(name="PK_Att", kind="PRIMARY KEY", columns=["AttendanceID"]),
        Constraint(name="FK_Attendance_Employees", kind="FOREIGN KEY", columns=["EmployeeID"],
                   ref_table="Employees", ref_columns=["EmployeeID"]),
    ]
    s.tables = [att, emp]
    return s


@pytest.mark.parametrize("target,expected", [("MySQL", "BIGINT"), ("PostgreSQL", "BIGINT")])
def test_fk_column_type_matches_the_identity_column_it_references(target, expected):
    # Without this the referenced identity column is BIGINT while the
    # referencing column is DECIMAL(19,0)/NUMERIC(19), and the constraint
    # is rejected: 1005 (HY000) ... errno 150 "Foreign key constraint is
    # incorrectly formed".
    s = _fk_schema(target)
    ddl, _ = ddl_generator.generate_schema_ddl(s, target)
    att = next(t for t in s.tables if t.name == "EmployeeAttendance")
    emp = next(t for t in s.tables if t.name == "Employees")
    fk_col = next(c for c in att.columns if c.name == "EmployeeID")
    pk_col = next(c for c in emp.columns if c.name == "EmployeeID")
    assert pk_col.target_type == expected, ddl
    assert fk_col.target_type == expected, ddl
    assert "DECIMAL(19,0)" not in ddl and "NUMERIC(19)" not in ddl, ddl


def test_the_alignment_is_reported_not_silent():
    s = _fk_schema("MySQL")
    issues = ddl_generator.align_foreign_key_column_types(s, "MySQL")
    assert any("EmployeeAttendance.EmployeeID" in i.message
               and "FK_Attendance_Employees" in i.message for i in issues)


def test_alignment_leaves_already_matching_columns_alone():
    s = Schema(name="dbo", source_engine="SQL Server", target_engine="MySQL")
    parent = Table(name="Departments", schema="dbo", columns=[
        Column(name="DepartmentID", data_type="NUMBER(10)", nullable=False)])
    child = Table(name="Employees", schema="dbo", columns=[
        Column(name="DepartmentID", data_type="NUMBER(10)")])
    child.constraints = [Constraint(name="FK_D", kind="FOREIGN KEY", columns=["DepartmentID"],
                                    ref_table="Departments", ref_columns=["DepartmentID"])]
    s.tables = [parent, child]
    assert ddl_generator.align_foreign_key_column_types(s, "MySQL") == []
    assert child.columns[0].target_type_override is None


def test_alignment_is_idempotent_across_repeated_generation():
    s = _fk_schema("MySQL")
    first, _ = ddl_generator.generate_schema_ddl(s, "MySQL")
    second, _ = ddl_generator.generate_schema_ddl(s, "MySQL")
    assert first == second


# ------------------------------------------- SQL Server view_definition

@pytest.mark.parametrize("definition,expected", [
    ("CREATE VIEW vw_X AS SELECT a FROM t", "SELECT a FROM t"),
    ("CREATE OR ALTER VIEW [dbo].[vw_X] AS\nSELECT a FROM t", "SELECT a FROM t"),
    ("CREATE VIEW vw_X (a, b) AS SELECT 1, 2", "SELECT 1, 2"),          # column list
    ("CREATE VIEW vw_X WITH SCHEMABINDING AS SELECT a FROM t", "SELECT a FROM t"),
    ("SELECT a FROM t", "SELECT a FROM t"),                              # already bare
    ("CREATE VIEW v AS SELECT 'as a value' AS c", "SELECT 'as a value' AS c"),
])
def test_create_view_header_is_stripped(definition, expected):
    assert tsql_dialect.strip_create_view_header(definition) == expected


def test_view_ddl_does_not_nest_create_inside_create():
    # SQL Server's information_schema.views.view_definition returns the
    # whole statement, so the generator used to emit
    #   CREATE OR REPLACE VIEW `v` AS CREATE VIEW v AS SELECT ...
    # -> 1064 (42000) syntax error at the second CREATE.
    v = View(name="vw_EmployeeDetails", schema="dbo", source_engine="SQL Server",
             definition=("CREATE VIEW vw_EmployeeDetails\nAS\nSELECT e.EmployeeID, "
                         "e.FirstName + ' ' + e.LastName AS EmployeeName FROM [dbo].[Employees] e"))
    for target in ("MySQL", "PostgreSQL"):
        import copy
        ddl, _ = ddl_generator.generate_view_ddl(copy.deepcopy(v), target)
        assert ddl.upper().count("CREATE") == 1, ddl
        assert "AS CREATE" not in ddl.upper().replace("\n", " "), ddl
        assert "SELECT" in ddl


def test_sqlserver_introspector_normalises_view_definition():
    from tgdatabridge.core.sqlserver_introspector import introspect_schema

    class _Conn:
        def execute(self, sql, params=None):
            low = sql.lower()
            if "information_schema.views" in low:
                return [("vw_X", "CREATE VIEW vw_X AS SELECT a FROM t")]
            if "information_schema.tables" in low:
                return []
            return []

    schema = introspect_schema(_Conn(), "dbo", include_routines=False)
    assert schema.views[0].definition == "SELECT a FROM t"
