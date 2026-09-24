"""Applying a converted schema twice must not abort on the second run.

The reported failure: a target that already had the tables and their
foreign keys stopped at

    Statement 7/21 failed: 1826 (HY000): Duplicate foreign key constraint
    name 'FK_Attendance_Employees'

leaving the rest of the script -- views, functions, procedures, triggers
-- unapplied.
"""
from tgdatabridge.core.ddl_generator import _replace_prefix, generate_schema_ddl
from tgdatabridge.core.schema_model import (
    Column, Constraint, ConversionStatus, Routine, Schema, Table,
)
from tgdatabridge.utils.ddl_errors import describe_skip, is_already_exists_error


class _Err(Exception):
    """Stands in for a driver exception: the drivers differ only in where
    they put the code, which is exactly what the matcher has to cope
    with."""

    def __init__(self, message, errno=None, sqlstate=None):
        super().__init__(message)
        if errno is not None:
            self.errno = errno
        if sqlstate is not None:
            self.sqlstate = sqlstate


# --------------------------------------------------------- the matcher

def test_mysql_duplicate_foreign_key_is_already_exists():
    exc = _Err("1826 (HY000): Duplicate foreign key constraint name "
               "'FK_Attendance_Employees'", errno=1826, sqlstate="HY000")
    assert is_already_exists_error(exc)


def test_mysql_duplicate_procedure_and_trigger():
    assert is_already_exists_error(
        _Err("PROCEDURE usp_GetEmployees already exists", errno=1304))
    assert is_already_exists_error(_Err("Trigger already exists", errno=1359))
    assert is_already_exists_error(
        _Err("Table 'Employees' already exists", errno=1050))
    assert is_already_exists_error(_Err("Duplicate key name 'IX_x'", errno=1061))


def test_mariadb_reports_the_duplicate_as_1005_errno_121():
    # MySQL 8 says 1826; MariaDB says 1005 with InnoDB sub-code 121.
    # Both mean the constraint is already there.
    assert is_already_exists_error(_Err(
        "1005 (HY000): Can't create table `db`.`EmployeeAttendance` "
        "(errno: 121 \"Duplicate key on write or update\")", errno=1005))


def test_postgres_duplicate_object_sqlstate():
    assert is_already_exists_error(
        _Err('constraint "fk_attendance_employees" for relation '
             '"employeeattendance" already exists', sqlstate="42710"))
    assert is_already_exists_error(
        _Err('relation "employees" already exists', sqlstate="42P07"))


def test_sqlserver_and_oracle_codes():
    assert is_already_exists_error(
        _Err("There is already an object named 'vw_x' in the database.",
             errno=2714))
    assert is_already_exists_error(
        _Err("ORA-00955: name is already used by an existing object"))


def test_a_real_failure_is_not_mistaken_for_already_exists():
    # The 3734 case fixed earlier: the referenced table is the wrong
    # shape. Swallowing this would re-introduce a half-applied schema
    # reported as a success.
    assert not is_already_exists_error(_Err(
        "3734 (HY000): Failed to add the foreign key constraint. Missing "
        "column 'EmployeeID' for constraint 'FK_Attendance_Employees' in "
        "the referenced table 'employees'", errno=3734))
    # errno 150: a genuine type mismatch between the two columns.
    assert not is_already_exists_error(_Err(
        "1005 (HY000): Can't create table (errno: 150 \"Foreign key "
        "constraint is incorrectly formed\")", errno=1005))
    assert not is_already_exists_error(_Err(
        'relation "employees" does not exist', sqlstate="42P01"))
    assert not is_already_exists_error(_Err(
        "1064 (42000): You have an error in your SQL syntax", errno=1064))


def test_describe_skip_names_the_object():
    assert "FK_Attendance_Employees" in describe_skip(
        "ALTER TABLE `EmployeeAttendance` ADD CONSTRAINT "
        "`FK_Attendance_Employees` FOREIGN KEY (`EmployeeID`) "
        "REFERENCES `Employees` (`EmployeeID`)")
    assert describe_skip("CREATE TRIGGER `trg_x` AFTER UPDATE ON t "
                         "FOR EACH ROW BEGIN END").startswith("trigger ")
    assert describe_skip("CREATE INDEX `IX_a` ON t (a)").startswith("index ")


# ------------------------------------------- routines are re-appliable

def _routine(kind, body, name="usp_GetEmployees"):
    r = Routine(name=name, schema="dbo", kind=kind, source="",
                source_engine="SQL Server")
    r.converted_source = body
    r.status = ConversionStatus.AUTOMATIC
    return r


def test_mysql_routine_gets_a_drop_in_front():
    prefix = _replace_prefix(
        _routine("PROCEDURE", "CREATE PROCEDURE `usp_GetEmployees`()\nBEGIN\nEND"),
        "MySQL")
    assert "DROP PROCEDURE IF EXISTS" in prefix.upper()


def test_mysql_trigger_gets_a_drop_in_front():
    prefix = _replace_prefix(
        _routine("TRIGGER", "CREATE TRIGGER `trg_x` AFTER UPDATE ON t "
                            "FOR EACH ROW BEGIN END", name="trg_x"),
        "MySQL")
    assert "DROP TRIGGER IF EXISTS" in prefix.upper()


def test_no_drop_when_the_create_already_replaces():
    # PostgreSQL emits CREATE OR REPLACE FUNCTION, so a DROP would be
    # both redundant and -- with CASCADE -- destructive to dependent
    # views.
    prefix = _replace_prefix(
        _routine("FUNCTION", "CREATE OR REPLACE FUNCTION fn_x() RETURNS int AS $$"),
        "PostgreSQL")
    assert prefix == ""


def test_no_drop_for_a_manual_conversion_placeholder():
    r = Routine(name="usp_Odd", schema="dbo", kind="PROCEDURE", source="x")
    r.converted_source = None
    assert _replace_prefix(r, "MySQL") == ""


# ------------------------------------- the whole script, applied twice

def _schema_with_fk():
    schema = Schema(name="dbo", source_engine="SQL Server", target_engine="MySQL")
    parent = Table(name="Employees", schema="dbo", columns=[
        Column(name="EmployeeID", data_type="NUMBER(10)", nullable=False, identity=True),
        Column(name="FirstName", data_type="VARCHAR2(50)"),
    ], constraints=[
        Constraint(name="PK_Employees", kind="PRIMARY KEY", columns=["EmployeeID"]),
    ])
    child = Table(name="EmployeeAttendance", schema="dbo", columns=[
        Column(name="AttendanceID", data_type="NUMBER(10)", nullable=False, identity=True),
        Column(name="EmployeeID", data_type="NUMBER(10)", nullable=False),
    ], constraints=[
        Constraint(name="PK_Attendance", kind="PRIMARY KEY", columns=["AttendanceID"]),
        Constraint(name="FK_Attendance_Employees", kind="FOREIGN KEY",
                   columns=["EmployeeID"], ref_table="Employees",
                   ref_columns=["EmployeeID"]),
    ])
    schema.tables = [parent, child]
    schema.routines = [_routine(
        "PROCEDURE",
        "CREATE PROCEDURE `usp_GetEmployees`()\nBEGIN\n"
        "  SELECT * FROM `Employees`;\nEND")]
    return schema


def test_generated_mysql_script_can_be_run_again():
    """Everything that can be written idempotently, is. The one thing
    that cannot -- ADD CONSTRAINT -- is what the apply path's
    already-exists handling covers."""
    ddl, _issues = generate_schema_ddl(_schema_with_fk(), "MySQL")
    assert "CREATE TABLE IF NOT EXISTS" in ddl
    assert "DROP PROCEDURE IF EXISTS `usp_GetEmployees`" in ddl
    # and the FK is still emitted, once
    assert ddl.count("ADD CONSTRAINT `FK_Attendance_Employees`") == 1


def test_apply_loop_skips_an_existing_constraint_and_keeps_going():
    """The apply loop's contract, exercised without a GUI: a duplicate
    constraint is recorded and the remaining statements still run."""
    statements = [
        "CREATE TABLE IF NOT EXISTS `Employees` (`EmployeeID` BIGINT)",
        "ALTER TABLE `EmployeeAttendance` ADD CONSTRAINT "
        "`FK_Attendance_Employees` FOREIGN KEY (`EmployeeID`) "
        "REFERENCES `Employees` (`EmployeeID`)",
        "CREATE OR REPLACE VIEW `vw_x` AS SELECT 1",
    ]
    applied, skipped = [], []
    for stmt in statements:
        try:
            if "ADD CONSTRAINT" in stmt:
                raise _Err("1826 (HY000): Duplicate foreign key constraint "
                           "name 'FK_Attendance_Employees'", errno=1826)
            applied.append(stmt)
        except Exception as exc:  # noqa: BLE001
            assert is_already_exists_error(exc)
            skipped.append(describe_skip(stmt))
    assert len(applied) == 2, "the view after the constraint must still run"
    assert skipped == ["foreign key / constraint FK_Attendance_Employees"]
