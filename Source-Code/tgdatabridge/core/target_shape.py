"""Does the target table actually have the columns we're about to write?

The failure this exists to prevent
----------------------------------
Every target's generated DDL uses `CREATE TABLE IF NOT EXISTS` (or the
per-engine equivalent) so that re-running "Apply DDL to Target" is a
no-op rather than an error. That is the right behaviour for the case it
was designed for -- the same schema applied twice -- but it has a sharp
edge: if a table of that name already exists with a *different* shape,
the statement succeeds and changes nothing. Apply DDL reports success,
and the mismatch only surfaces later as a driver error from the middle
of the data migration:

    1054 (42S22): Unknown column 'employee_id' in 'field list'

which says nothing about the actual cause. Worse, it is
platform-dependent: MySQL on Windows folds table names to lower case
(`lower_case_table_names=1`), so a spreadsheet sheet named "Employees"
collides with a pre-existing `employees` table there, while on Linux the
two are distinct and everything works. A migration that runs cleanly on
one machine can fail on another with an error that mentions neither the
table's history nor the fact that the DDL step quietly declined to do
anything.

This module answers the question directly, before any rows are written,
and turns that into a message naming the table, the columns that are
missing, and what to do about it.

How it checks
-------------
Deliberately with `SELECT <columns> FROM <table> WHERE 1=0` through the
connector's ordinary `execute()`, rather than by adding a
`list_columns()` method to all six connectors. Every SQL engine this tool
targets accepts a false-predicate select, no engine returns rows for it,
and it costs one trivial round trip per table. When it fails, each column
is probed individually to name precisely which ones are absent -- N more
queries, but only ever on the path that is already about to fail.

MongoDB has no SQL and its `execute()` raises NotImplementedError; a
document collection also has no fixed set of columns to be missing, so
the check reports "not applicable" there rather than a false problem.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence


@dataclass
class ShapeProblem:
    """What's wrong with a target table, in terms a person can act on."""

    table_name: str
    exists: bool = True
    missing_columns: List[str] = field(default_factory=list)
    detail: str = ""
    # Set only by _check_partition_shape below: the table exists with the
    # right columns, but isn't partitioned the way the source (and the
    # generated DDL) expects. A distinct flag rather than overloading
    # missing_columns == [] -- an ordinary column-shape problem can also
    # legitimately have no columns it could individually confirm missing
    # (see check_table_shape's "(unknown)" fallback), so that emptiness
    # alone can't be used to tell the two cases apart.
    not_partitioned: bool = False

    @property
    def message(self) -> str:
        if not self.exists:
            return (
                f"Target table '{self.table_name}' does not exist. Run "
                f"\"3. Apply DDL to Target\" before migrating data."
            )
        if self.not_partitioned:
            return (
                f"Target table '{self.table_name}' already exists but is a plain, "
                f"unpartitioned table, while the source table is partitioned and the "
                f"generated DDL declares it PARTITION BY. \"CREATE TABLE IF NOT EXISTS\" "
                f"left the existing plain table unchanged (PostgreSQL has no ALTER TABLE "
                f"that turns an existing table into a partitioned one), so every "
                f"'CREATE TABLE ... PARTITION OF {self.table_name} ...' statement that "
                f"follows fails with '\"{self.table_name}\" is not partitioned'. "
                f"Drop the existing '{self.table_name}' table (its partitions will be "
                f"recreated fresh by this schema), or migrate into a different "
                f"database/schema, then re-run."
            )
        missing = ", ".join(self.missing_columns) or "(unknown)"
        return (
            f"Target table '{self.table_name}' already existed with a different set of "
            f"columns, so \"CREATE TABLE IF NOT EXISTS\" left it unchanged and the data "
            f"doesn't fit it. Missing on the target: {missing}. "
            f"Drop or rename the existing '{self.table_name}' table, or migrate into a "
            f"different database/schema, then re-run. "
            f"(On Windows, MySQL matches table names case-insensitively, so 'Employees' "
            f"and 'employees' are the same table.)"
        )


def check_table_shape(
    target, table_name: str, columns: Sequence[str], schema: Optional[str] = None,
    quote: Optional[callable] = None,
) -> Optional[ShapeProblem]:
    """None when the target table can accept these columns, or a
    ShapeProblem describing why it can't.

    Never raises: a connector that can't answer (MongoDB, or any engine
    whose `execute` is unavailable) is reported as "no problem found"
    rather than blocking a migration over a check that couldn't run. The
    migration's own error handling remains the backstop.
    """
    execute = getattr(target, "execute", None)
    if execute is None or not columns:
        return None

    quote = quote or _quote_for(target)
    qualified = _qualified(target, table_name, schema, quote)

    # Does the table exist at all? A missing table and a mismatched one
    # need different advice, so they're distinguished up front.
    try:
        _consume(execute(f"SELECT * FROM {qualified} WHERE 1=0"))
    except NotImplementedError:
        return None                      # MongoDB -- no SQL, no fixed columns
    except Exception:  # noqa: BLE001
        return ShapeProblem(table_name=table_name, exists=False)

    # Folded exactly as the DDL generator created them and as
    # insert_batch addresses them -- lower-case on PostgreSQL, upper-case
    # on Oracle/Db2. The table name was already folded by _qualified
    # above; the columns were not, and that omission made this check
    # report every mixed-case column as missing.
    #
    # The effect was severe and completely misleading. On a PostgreSQL
    # target, `atd_flexCustomerFlag` is created (quoted) as
    # `atd_flexcustomerflag`, so probing for `"atd_flexCustomerFlag"`
    # fails -- and the table was refused before a single row was written,
    # with a message blaming a pre-existing table that in fact did not
    # exist. Any table with a camelCase column failed this way, on an
    # empty database, every time; only tables whose columns happened to
    # be all lower-case got through. The rows themselves fit perfectly:
    # postgres_connector.insert_batch lower-cases the same names.
    probes = [(c, quote(_fold(target, c))) for c in columns]
    column_list = ", ".join(probe for _name, probe in probes)
    try:
        _consume(execute(f"SELECT {column_list} FROM {qualified} WHERE 1=0"))
        return None
    except NotImplementedError:
        return None
    except Exception as exc:  # noqa: BLE001
        detail = str(exc)

    # The table is there but the column list isn't accepted. Probe one at
    # a time so the message can name exactly what's missing rather than
    # making the user diff two schemas by hand.
    missing: List[str] = []
    for name, probe in probes:
        try:
            _consume(execute(f"SELECT {probe} FROM {qualified} WHERE 1=0"))
        except Exception:  # noqa: BLE001
            missing.append(name)
    return ShapeProblem(table_name=table_name, missing_columns=missing, detail=detail)


def _check_partition_shape(target, table, schema: Optional[str] = None) -> Optional[ShapeProblem]:
    """A second, distinct way "CREATE TABLE IF NOT EXISTS" silently does
    nothing: the target already has a table of this name, with all the
    right columns, but as a plain table -- while the source table is
    RANGE/LIST-partitioned and ddl_generator.generate_table_ddl_postgres
    (via _plan_partition_ddl_postgres) is about to emit it as
    `CREATE TABLE IF NOT EXISTS x (...) PARTITION BY ...` followed by one
    `CREATE TABLE ... PARTITION OF x ...` per partition.

    Unlike a plain column-shape mismatch (check_table_shape, above), this
    one passes that check outright -- the columns really do match, since
    partitioning changes how a table's rows are stored, not what columns
    it has. So a stale plain table left over from a run before this tool
    supported partitioning (or from a run where this same table happened
    to fall back to unpartitioned, see _plan_partition_ddl_postgres's own
    "declined to partition" cases) reads as "fine" to check_table_shape,
    and the mismatch only surfaces many statements later, once for *every*
    partition, as PostgreSQL's own

        ERROR: "orders" is not partitioned

    -- a real, reported case: 34 consecutive statement failures for one
    table, each one individually cryptic, none of them naming the actual
    cause (the parent's own CREATE TABLE "succeeded" -- IF NOT EXISTS just
    quietly kept the pre-existing plain table). Reported here as one clear
    ShapeProblem instead, before "Apply DDL to Target" runs a single
    statement, exactly like check_table_shape does for a column mismatch --
    and it plugs into the very same "Leave them out" / "Replace them..."
    recovery flow in main_window.py's _offer_preflight_actions, no GUI
    changes needed.

    PostgreSQL-only: this tool only ever generates native declarative-
    partitioning DDL for a PostgreSQL target (see
    ddl_generator._plan_partition_ddl_postgres's own docstring for why
    every other target engine here migrates a partitioned source table as
    a single ordinary table instead, which this check would have nothing
    to say about).
    """
    if table.partition_scheme is None:
        return None
    if "postgres" not in type(target).__name__.lower():
        return None
    execute = getattr(target, "execute", None)
    if execute is None:
        return None

    # Only a table this tool actually plans to emit PARTITION BY for can
    # have this specific mismatch -- a scheme _plan_partition_ddl_postgres
    # declines (composite/HASH, an unresolvable RANGE boundary, ...) is
    # migrated as a single ordinary table on purpose, so an existing plain
    # table there is correct, not stale.
    from tgdatabridge.core.ddl_generator import _plan_partition_ddl_postgres
    plan, _reason = _plan_partition_ddl_postgres(table)
    if plan is None:
        return None

    quote = _quote_for(target)
    qualified = _qualified(target, table.name, schema, quote)
    try:
        _consume(execute(f"SELECT * FROM {qualified} WHERE 1=0"))
    except NotImplementedError:
        return None
    except Exception:  # noqa: BLE001
        return None  # doesn't exist yet -- check_table_shape already covers this case

    folded_name = _fold(target, table.name)
    try:
        result = execute(
            "SELECT 1 FROM pg_partitioned_table pt JOIN pg_class c ON c.oid = pt.partrelid "
            f"WHERE c.relname = '{folded_name}'"
        )
        rows = list(result) if result is not None else []
    except Exception:  # noqa: BLE001
        # Can't tell either way (e.g. no permission on pg_catalog, though
        # that would be unusual) -- don't block the run over a check that
        # itself failed; the original "is not partitioned" errors remain
        # the backstop, same as check_table_shape's own NotImplementedError
        # handling above.
        return None
    if rows:
        return None  # already partitioned on the target -- no mismatch

    return ShapeProblem(table_name=table.name, not_partitioned=True)


def check_before_apply_ddl(target, tables, schema: Optional[str] = None) -> List[ShapeProblem]:
    """Which of `tables` already exist on the target with the wrong shape.

    The same question check_table_shape answers per table, asked for a
    whole schema *before "Apply DDL to Target" runs a single statement*
    rather than before the data migration.

    That ordering matters, because the mismatch bites at DDL time first
    and much less legibly. `CREATE TABLE IF NOT EXISTS Employees (...)`
    against a database that already has an incompatible `employees`
    silently does nothing and reports success -- and then the foreign-key
    pass a few statements later fails with

        3734 (HY000): Failed to add the foreign key constraint. Missing
        column 'EmployeeID' for constraint 'FK_Attendance_Employees' in
        the referenced table 'employees'

    which points at the constraint rather than at the stale table that is
    actually the problem, and leaves the schema half-applied. Asking up
    front turns that into a message naming the table and the columns.

    Tables that do not exist yet are *not* problems here -- creating them
    is the entire point of the step about to run -- so only genuine
    shape mismatches are returned.
    """
    problems: List[ShapeProblem] = []
    for table in tables or []:
        columns = [c.name for c in getattr(table, "columns", [])]
        if not columns:
            continue
        problem = check_table_shape(target, table.name, columns, schema=schema)
        if problem is not None and problem.exists:
            problems.append(problem)
            continue
        # Columns are fine (or the table doesn't exist yet, which
        # _check_partition_shape independently confirms and no-ops on) --
        # still worth asking the second question: if the source is
        # partitioned, is the target table actually partitioned too? See
        # _check_partition_shape's own docstring for why a column-shape
        # match alone doesn't rule this out.
        partition_problem = _check_partition_shape(target, table, schema=schema)
        if partition_problem is not None:
            problems.append(partition_problem)
    return problems


def _consume(result) -> None:
    """Some connectors' execute() returns a cursor/iterator that only
    actually runs when consumed; others return None. Draining it makes
    both shapes behave the same."""
    if result is None:
        return
    try:
        list(result)
    except TypeError:
        pass


def _quote_for(target):
    """The identifier quoting the target engine uses, picked from the
    connector's own module name so this needs no engine parameter."""
    from tgdatabridge.utils.identifiers import quote_backtick, quote_bracket, quote_double

    name = type(target).__name__.lower()
    if "mysql" in name:
        return quote_backtick
    if "sqlserver" in name:
        return quote_bracket
    return quote_double


def _qualified(target, table_name: str, schema: Optional[str], quote) -> str:
    """Schema-qualify the way each connector's own insert_batch does, so
    this checks the table the migration will actually write to.

    MySQL is the exception: its connector never qualifies (the connection
    is already bound to one database), and PostgreSQL pins a search_path
    at connect time, so both are addressed bare.
    """
    engine = type(target).__name__.lower()
    if "mysql" in engine or "postgres" in engine:
        return quote(_fold(target, table_name))
    schema = schema or getattr(target, "schema_name", None)
    if schema:
        return f"{quote(_fold(target, schema))}.{quote(_fold(target, table_name))}"
    return quote(_fold(target, table_name))


def _fold(target, identifier: str) -> str:
    """Match each connector's own case folding -- ddl_generator creates
    PostgreSQL objects lower-cased and Oracle/Db2 objects upper-cased, so
    a check that didn't fold the same way would look for a table that
    isn't there."""
    engine = type(target).__name__.lower()
    if "postgres" in engine:
        return identifier.lower()
    if "oracle" in engine or "db2" in engine:
        return identifier.upper()
    return identifier
