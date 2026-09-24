"""The target-table shape check -- tgdatabridge.core.target_shape.

The failure it prevents, seen in the field
------------------------------------------
An Excel -> MySQL migration of a four-sheet workbook: three sheets copied
and validated cleanly, the fourth failed with

    1054 (42S22): Unknown column 'employee_id' in 'field list'

The target database already contained a table called `employees` from
earlier work. Every generated CREATE TABLE uses IF NOT EXISTS so that
re-applying a schema is a no-op, so `CREATE TABLE IF NOT EXISTS
\\`Employees\\`` matched that table, changed nothing, and reported
success -- and the insert then went at the *old* table's columns.

Two things made it hard to diagnose. The DDL step said it worked, and it
is platform-dependent: MySQL on Windows folds table names to lower case
(lower_case_table_names=1), so "Employees" and "employees" are one table
there and two on Linux. The same migration passes on one machine and
fails on another.

These tests use fake connectors so they run with no database installed,
matching the rest of the suite. The behaviour was additionally verified
end to end against a real MariaDB with lower_case_table_names=1 (which
reproduces the exact 1054) and a real PostgreSQL.
"""
import pytest

from tgdatabridge.core.target_shape import ShapeProblem, check_table_shape


class _FakeTarget:
    """Answers SELECTs the way a database would: a query naming a column
    the table doesn't have raises, everything else returns no rows."""

    schema_name = None

    def __init__(self, tables):
        self.tables = {name: set(cols) for name, cols in tables.items()}
        self.queries = []

    def execute(self, sql, params=None):
        self.queries.append(sql)
        table = sql.split(" FROM ")[1].split(" WHERE ")[0].strip()
        bare = table.split(".")[-1].strip('`"[]')
        if bare not in self.tables:
            raise RuntimeError(f"1146 (42S02): Table '{bare}' doesn't exist")
        selected = sql.split("SELECT ", 1)[1].split(" FROM ")[0].strip()
        if selected == "*":
            return []
        for column in (c.strip().strip('`"[]') for c in selected.split(",")):
            if column not in self.tables[bare]:
                raise RuntimeError(f"1054 (42S22): Unknown column '{column}' in 'field list'")
        return []


class _MySQLishTarget(_FakeTarget):
    pass


class _MongoTarget:
    schema_name = None

    def execute(self, sql, params=None):
        raise NotImplementedError("no SQL dialect exists for MongoDB")


# ------------------------------------------------------------- the defect

def test_a_matching_table_reports_no_problem():
    target = _MySQLishTarget({"Employees": ["employee_id", "full_name"]})
    assert check_table_shape(target, "Employees", ["employee_id", "full_name"]) is None


def test_a_preexisting_table_with_other_columns_is_caught():
    """The field failure exactly: the table is there, its columns are not
    the ones we're about to write."""
    target = _MySQLishTarget({"Employees": ["emp_no", "first_name", "last_name"]})
    problem = check_table_shape(
        target, "Employees", ["employee_id", "full_name", "department", "hire_date"])

    assert problem is not None
    assert problem.exists is True
    assert problem.missing_columns == ["employee_id", "full_name", "department", "hire_date"]


def test_the_message_names_the_table_the_columns_and_the_way_out():
    """A message that only says "unknown column" is what sent someone to
    a log file for an hour. This one has to be actionable on its own."""
    target = _MySQLishTarget({"Employees": ["emp_no"]})
    problem = check_table_shape(target, "Employees", ["employee_id", "full_name"])
    message = problem.message

    assert "Employees" in message
    assert "employee_id" in message and "full_name" in message
    assert "CREATE TABLE IF NOT EXISTS" in message
    assert "Drop or rename" in message
    # The platform trap is the part nobody guesses.
    assert "case-insensitively" in message


def test_only_the_genuinely_missing_columns_are_listed():
    target = _MySQLishTarget({"Employees": ["employee_id", "full_name"]})
    problem = check_table_shape(
        target, "Employees", ["employee_id", "full_name", "department"])
    assert problem.missing_columns == ["department"]


def test_a_missing_table_is_reported_differently_from_a_mismatched_one():
    """Different cause, different fix -- "run Apply DDL" rather than
    "drop the existing table"."""
    target = _MySQLishTarget({"Departments": ["id"]})
    problem = check_table_shape(target, "Employees", ["employee_id"])
    assert problem.exists is False
    assert "does not exist" in problem.message
    assert "Apply DDL" in problem.message


# ---------------------------------------------------------- not applicable

def test_mongodb_is_skipped_rather_than_failed():
    """A collection has no fixed columns to be missing, and its execute()
    raises. Reporting a problem here would block every MongoDB
    migration."""
    assert check_table_shape(_MongoTarget(), "Employees", ["employee_id"]) is None


def test_a_connector_without_execute_is_skipped():
    class _NoExecute:
        schema_name = None
    assert check_table_shape(_NoExecute(), "Employees", ["employee_id"]) is None


def test_no_columns_is_skipped():
    target = _MySQLishTarget({"Employees": ["employee_id"]})
    assert check_table_shape(target, "Employees", []) is None


def test_the_check_never_raises_even_on_a_hostile_connector():
    """It runs before every table's migration; a check that could throw
    would be worse than the bug it prevents."""
    class _Exploding:
        schema_name = None

        def execute(self, sql, params=None):
            raise RuntimeError("connection lost")

    problem = check_table_shape(_Exploding(), "Employees", ["employee_id"])
    assert problem is not None and problem.exists is False


# ------------------------------------------------------- migrate/plan wiring

class _FakeSource:
    def count_rows(self, table, schema=None):
        return 2

    def fetch_batches_table(self, table, batch_size=5000):
        yield [c.name for c in table.columns], [(1, "a"), (2, "b")]


def _table():
    from tgdatabridge.core.schema_model import Column, Table
    return Table(
        name="Employees", schema="emp",
        columns=[Column(name="employee_id", data_type="NUMBER(9)"),
                 Column(name="full_name", data_type="VARCHAR2(50)")])


def test_migrate_table_refuses_before_writing_any_rows():
    """Failing fast matters: the alternative writes some tables, fails on
    this one, and leaves a half-migrated target plus a driver error."""
    from tgdatabridge.core.migrator import migrate_table

    target = _MySQLishTarget({"Employees": ["emp_no"]})
    target.insert_batch = lambda *a, **k: pytest.fail("must not write to a mismatched table")

    result = migrate_table(_FakeSource(), target, _table())
    assert result.succeeded is False
    assert result.rows_copied == 0
    assert "already existed with a different set of columns" in result.error


def test_dry_run_reports_it_as_not_ready():
    """Dry Run exists to answer "would this work?" before anything is
    written, so it has to notice this."""
    from tgdatabridge.core.migrator import plan_table

    target = _MySQLishTarget({"Employees": ["emp_no"]})
    target.count_rows = lambda table, schema=None: 0

    plan = plan_table(_FakeSource(), target, _table())
    assert plan.ready is False
    assert any("different set of columns" in w for w in plan.warnings)


def test_a_good_target_still_migrates():
    from tgdatabridge.core.migrator import migrate_table

    written = []
    target = _MySQLishTarget({"Employees": ["employee_id", "full_name"]})
    target.insert_batch = lambda table, cols, rows: written.extend(rows)

    result = migrate_table(_FakeSource(), target, _table(), validate=False)
    assert result.succeeded is True
    assert result.rows_copied == 2
    assert len(written) == 2


# ------------------------------- preflight before "Apply DDL to Target"

class _FakeTarget:
    """Answers SELECT ... WHERE 1=0 probes from a fixed table->columns map."""

    schema_name = None

    def __init__(self, tables):
        self.tables = {t.lower(): {c.lower() for c in cols} for t, cols in tables.items()}

    def execute(self, sql, params=None):
        import re as _re
        m = _re.match(r"SELECT (.+?) FROM [`\"\[]?(\w+)[`\"\]]? WHERE 1=0", sql, _re.IGNORECASE)
        if not m:
            raise AssertionError(f"unexpected probe: {sql}")
        cols, table = m.group(1), m.group(2).lower()
        if table not in self.tables:
            raise RuntimeError(f"Table '{table}' doesn't exist")
        if cols.strip() == "*":
            return []
        for c in cols.split(","):
            name = c.strip().strip('`"[]').lower()
            if name not in self.tables[table]:
                raise RuntimeError(f"Unknown column '{name}' in 'field list'")
        return []


def _preflight_table(name, columns):
    from tgdatabridge.core.schema_model import Column, Table
    return Table(name=name, schema="dbo",
                 columns=[Column(name=c, data_type="NUMBER(10)") for c in columns])


def test_apply_preflight_is_silent_when_the_target_is_empty():
    # The normal case: nothing exists yet, because creating it is the
    # whole point of the step about to run.
    from tgdatabridge.core.target_shape import check_before_apply_ddl
    target = _FakeTarget({})
    assert check_before_apply_ddl(target, [_preflight_table("Employees", ["EmployeeID", "FirstName"])]) == []


def test_apply_preflight_is_silent_when_the_existing_table_matches():
    from tgdatabridge.core.target_shape import check_before_apply_ddl
    target = _FakeTarget({"Employees": ["EmployeeID", "FirstName"]})
    assert check_before_apply_ddl(target, [_preflight_table("Employees", ["EmployeeID", "FirstName"])]) == []


def test_apply_preflight_catches_the_stale_table_behind_error_3734():
    # The real failure: prod_db already had an `employees` table without
    # EmployeeID, so CREATE TABLE IF NOT EXISTS did nothing and the FK
    # pass later died with "Missing column 'EmployeeID' ... in the
    # referenced table 'employees'".
    from tgdatabridge.core.target_shape import check_before_apply_ddl
    target = _FakeTarget({"Employees": ["Id", "FirstName"]})
    problems = check_before_apply_ddl(
        target, [_preflight_table("Employees", ["EmployeeID", "FirstName"])])
    assert len(problems) == 1
    assert problems[0].exists
    assert problems[0].missing_columns == ["EmployeeID"]
    assert "Employees" in problems[0].message
    assert "EmployeeID" in problems[0].message


def test_apply_preflight_matches_table_names_case_insensitively():
    # MySQL on Windows folds table names, so `Employees` and `employees`
    # are the same table -- which is exactly how the stale one hides.
    from tgdatabridge.core.target_shape import check_before_apply_ddl
    target = _FakeTarget({"employees": ["Id"]})
    problems = check_before_apply_ddl(
        target, [_preflight_table("Employees", ["EmployeeID"])])
    assert len(problems) == 1 and problems[0].missing_columns == ["EmployeeID"]


# --------------------------- partition-shape mismatch ("is not partitioned")

class _FakePostgresTarget(_FakeTarget):
    """Same SELECT ... WHERE 1=0 column probing as _FakeTarget, plus
    answering the pg_partitioned_table existence query
    _check_partition_shape issues. The class name has to contain
    "postgres" -- that check (like _quote_for/_fold before it) is gated on
    the connector's own class name rather than an explicit engine
    parameter, matching this module's existing style."""

    def __init__(self, tables, partitioned=()):
        super().__init__(tables)
        self.partitioned = {t.lower() for t in partitioned}

    def execute(self, sql, params=None):
        if "pg_partitioned_table" in sql:
            import re as _re
            m = _re.search(r"c\.relname = '([^']+)'", sql)
            name = m.group(1) if m else None
            return [(1,)] if name and name in self.partitioned else []
        return super().execute(sql, params)


def _range_partitioned_table(name="orders"):
    from tgdatabridge.core.schema_model import Column, Partition, PartitionScheme, Table
    scheme = PartitionScheme(
        kind="RANGE", columns=["order_date"],
        partitions=[Partition(name=f"{name}_p1", high_value="MAXVALUE", position=1)],
    )
    return Table(
        name=name, schema="dbo",
        columns=[Column(name="id", data_type="NUMBER(9)"),
                 Column(name="order_date", data_type="DATE")],
        partition_scheme=scheme,
    )


def test_partition_shape_catches_a_stale_plain_table():
    """The real bug, reported from the field: a migration re-run against a
    target where 'orders' already existed as a plain table (left over from
    before this tool supported native partitioning) reported "Apply DDL"
    as having applied 155/202 statements, with 34 of the failures all
    reading '"orders" is not partitioned' -- one per CREATE TABLE ...
    PARTITION OF orders statement, none of them naming the actual cause.
    check_table_shape alone can't catch this: the columns really do match,
    since partitioning doesn't add or remove any."""
    from tgdatabridge.core.target_shape import check_before_apply_ddl
    target = _FakePostgresTarget({"orders": ["id", "order_date"]}, partitioned=())
    problems = check_before_apply_ddl(target, [_range_partitioned_table("orders")])
    assert len(problems) == 1
    assert problems[0].table_name == "orders"
    assert problems[0].not_partitioned is True
    assert "is not partitioned" in problems[0].message
    assert "orders" in problems[0].message
    assert "Drop the existing" in problems[0].message


def test_partition_shape_is_silent_when_the_target_is_already_partitioned():
    from tgdatabridge.core.target_shape import check_before_apply_ddl
    target = _FakePostgresTarget({"orders": ["id", "order_date"]}, partitioned=("orders",))
    assert check_before_apply_ddl(target, [_range_partitioned_table("orders")]) == []


def test_partition_shape_is_silent_when_the_table_does_not_exist_yet():
    # Not a problem -- creating it (correctly partitioned) is the whole
    # point of the step about to run.
    from tgdatabridge.core.target_shape import check_before_apply_ddl
    target = _FakePostgresTarget({}, partitioned=())
    assert check_before_apply_ddl(target, [_range_partitioned_table("orders")]) == []


def test_partition_shape_is_skipped_for_a_non_postgres_target():
    # This tool only ever generates native declarative-partitioning DDL for
    # PostgreSQL; every other target migrates a partitioned source table as
    # a single ordinary table, so an existing plain table there is correct.
    from tgdatabridge.core.target_shape import check_before_apply_ddl
    target = _FakeTarget({"orders": ["id", "order_date"]})  # class name has no "postgres" in it
    assert check_before_apply_ddl(target, [_range_partitioned_table("orders")]) == []


def test_partition_shape_is_skipped_for_an_unpartitioned_table():
    from tgdatabridge.core.target_shape import check_before_apply_ddl
    from tgdatabridge.core.schema_model import Column, Table
    target = _FakePostgresTarget({"employees": ["id"]}, partitioned=())
    plain_table = Table(name="employees", schema="dbo", columns=[Column(name="id", data_type="NUMBER(9)")])
    assert check_before_apply_ddl(target, [plain_table]) == []


def test_partition_shape_is_skipped_when_the_scheme_falls_back_to_unpartitioned():
    # A HASH scheme is never turned into native PARTITION BY DDL (see
    # _plan_partition_ddl_postgres) -- migrated as a single ordinary table
    # on purpose, so an existing plain table there is not stale.
    from tgdatabridge.core.target_shape import check_before_apply_ddl
    from tgdatabridge.core.schema_model import Column, Partition, PartitionScheme, Table
    scheme = PartitionScheme(kind="HASH", columns=["id"], partitions=[
        Partition(name="p1", high_value=None, position=1),
    ])
    table = Table(name="orders", schema="dbo",
                  columns=[Column(name="id", data_type="NUMBER(9)")],
                  partition_scheme=scheme)
    target = _FakePostgresTarget({"orders": ["id"]}, partitioned=())
    assert check_before_apply_ddl(target, [table]) == []
