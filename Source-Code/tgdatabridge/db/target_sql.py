"""Addressing a *target* table, and the three statements an incremental
sync needs: look rows up by key, upsert them, delete them.

Two things make this its own module rather than inline strings.

**Every target engine names its objects differently, and this tool
already decided how.** `ddl_generator` lowercases and double-quotes on
PostgreSQL, uppercases and double-quotes on Oracle and Db2, backticks
case-preserved on MySQL, brackets case-preserved on SQL Server -- and each
connector's `insert_batch` repeats that convention so its INSERT resolves
to the object the DDL created. A fourth and fifth copy of that reasoning
in the sync code would be a fourth and fifth chance to get it subtly
wrong; the failure mode is `relation "Employees" does not exist` from a
statement that looks perfectly correct.

**Every driver binds parameters differently.** psycopg and
mysql-connector take `%s`, pyodbc and ibm_db take `?`, python-oracledb
takes `:1`. Choosing the wrong one produces either a syntax error or --
worse, on the engines where the wrong marker is still valid SQL -- a
literal string where a value was meant.

The upsert itself is the point of the module. An incremental run does not
know whether a changed source row is new to the target or already there,
and finding out with a SELECT per row would be unusable at any real size.
Every engine has a merge-on-key form:

    PostgreSQL   INSERT ... ON CONFLICT (key) DO UPDATE SET ...
    MySQL        INSERT ... ON DUPLICATE KEY UPDATE ...
    Oracle       MERGE INTO ... USING (SELECT :1 ... FROM dual) ...
    SQL Server   MERGE ... USING (VALUES (?, ...)) ...
    Db2          MERGE INTO ... USING (VALUES (?, ...)) ...

all of which are one round trip per batch and atomic per row.
"""
from __future__ import annotations

from typing import Callable, List, Optional, Sequence

from tgdatabridge.utils.identifiers import quote_backtick, quote_bracket, quote_double


def dialect_of(target) -> str:
    """Which engine `target` is, from the connector's class name -- the
    same trick source_sql.quoter_for and target_shape._quote_for use, so
    no engine string has to be threaded down here."""
    name = type(target).__name__.lower()
    for key in ("postgres", "mysql", "sqlserver", "oracle", "db2", "mongo"):
        if key in name:
            return key
    return "postgres"


def identifier(target, name: str) -> str:
    """One object name, spelled the way this tool's DDL created it."""
    dialect = dialect_of(target)
    if dialect == "postgres":
        return quote_double(name.lower())
    if dialect == "mysql":
        return quote_backtick(name)
    if dialect == "sqlserver":
        return quote_bracket(name)
    return quote_double(name.upper())          # Oracle, Db2


def qualified(target, table: str) -> str:
    """The table, schema-qualified where the connector qualifies its own
    inserts -- SQL Server, Db2 and Oracle do, because the connecting user
    and the schema the DDL was generated for are not necessarily the
    same account."""
    dialect = dialect_of(target)
    schema = getattr(getattr(target, "params", None), "schema", None)
    if schema and dialect in ("sqlserver", "oracle", "db2"):
        return f"{identifier(target, schema)}.{identifier(target, table)}"
    return identifier(target, table)


def placeholder(target, position: int) -> str:
    """One bind marker. `position` is 1-based, and only Oracle uses it."""
    dialect = dialect_of(target)
    if dialect in ("postgres", "mysql"):
        return "%s"
    if dialect == "oracle":
        return f":{position}"
    return "?"                                  # pyodbc, ibm_db


def placeholders(target, count: int, start: int = 1) -> str:
    return ", ".join(placeholder(target, start + i) for i in range(count))


def select_by_keys_sql(target, table: str, columns: Sequence[str],
                       key_columns: Sequence[str], key_count: int) -> str:
    """`SELECT <columns> FROM t WHERE (k1, k2) IN ((?, ?), (?, ?), ...)`.

    A row-value IN list rather than one statement per key: an incremental
    run asks this once per batch of a few thousand keys, and per-key round
    trips are what make a sync take longer than the full migration it is
    meant to avoid.
    """
    select = ", ".join(identifier(target, c) for c in columns)
    keys = ", ".join(identifier(target, c) for c in key_columns)
    width = len(key_columns)
    tuples = []
    position = 1
    for _ in range(key_count):
        tuples.append(f"({placeholders(target, width, position)})")
        position += width
    left = keys if width == 1 else f"({keys})"
    return (f"SELECT {select} FROM {qualified(target, table)} "
            f"WHERE {left} IN ({', '.join(tuples)})")


def delete_by_keys_sql(target, table: str, key_columns: Sequence[str],
                       key_count: int) -> str:
    keys = ", ".join(identifier(target, c) for c in key_columns)
    width = len(key_columns)
    tuples = []
    position = 1
    for _ in range(key_count):
        tuples.append(f"({placeholders(target, width, position)})")
        position += width
    left = keys if width == 1 else f"({keys})"
    return f"DELETE FROM {qualified(target, table)} WHERE {left} IN ({', '.join(tuples)})"


def upsert_sql(target, table: str, columns: Sequence[str],
               key_columns: Sequence[str]) -> Optional[str]:
    """Insert the row, or overwrite it if its key is already there.

    None when the engine has no merge form this tool can build -- MongoDB,
    which has no SQL at all. The caller then falls back to delete + insert,
    which is correct but not atomic per row.
    """
    dialect = dialect_of(target)
    table_sql = qualified(target, table)
    cols = [identifier(target, c) for c in columns]
    keys = {c.lower() for c in key_columns}
    updatable = [identifier(target, c) for c in columns if c.lower() not in keys]

    if dialect == "postgres":
        conflict = ", ".join(identifier(target, c) for c in key_columns)
        if not updatable:
            # Key-only table: there is nothing to overwrite, so a conflict
            # means the row is already exactly right.
            action = "DO NOTHING"
        else:
            action = "DO UPDATE SET " + ", ".join(f"{c} = EXCLUDED.{c}" for c in updatable)
        return (f"INSERT INTO {table_sql} ({', '.join(cols)}) "
                f"VALUES ({placeholders(target, len(columns))}) "
                f"ON CONFLICT ({conflict}) {action}")

    if dialect == "mysql":
        if not updatable:
            # MySQL has no DO NOTHING; assigning a key column to itself is
            # the standard no-op and keeps the statement one round trip.
            assignment = f"{cols[0]} = {cols[0]}"
        else:
            assignment = ", ".join(f"{c} = VALUES({c})" for c in updatable)
        return (f"INSERT INTO {table_sql} ({', '.join(cols)}) "
                f"VALUES ({placeholders(target, len(columns))}) "
                f"ON DUPLICATE KEY UPDATE {assignment}")

    if dialect in ("oracle", "sqlserver", "db2"):
        source_columns = ", ".join(
            f"{placeholder(target, i + 1)} AS {c}" for i, c in enumerate(cols))
        using = (f"(SELECT {source_columns} FROM dual)" if dialect == "oracle"
                 else f"(SELECT {source_columns}"
                      + (" FROM SYSIBM.SYSDUMMY1)" if dialect == "db2" else ")"))
        on = " AND ".join(
            f"t.{identifier(target, c)} = s.{identifier(target, c)}" for c in key_columns)
        matched = (""
                   if not updatable else
                   " WHEN MATCHED THEN UPDATE SET "
                   + ", ".join(f"t.{c} = s.{c}" for c in updatable))
        insert_cols = ", ".join(cols)
        insert_vals = ", ".join(f"s.{c}" for c in cols)
        statement = (f"MERGE INTO {table_sql} t USING {using} s ON ({on})"
                     f"{matched} WHEN NOT MATCHED THEN "
                     f"INSERT ({insert_cols}) VALUES ({insert_vals})")
        # Oracle is the one of the three that does not want a terminator
        # inside an executemany.
        return statement if dialect == "oracle" else statement + ";"

    return None


def flatten_keys(rows: Sequence[Sequence]) -> List:
    """`[(1, 'a'), (2, 'b')]` -> `[1, 'a', 2, 'b']`, the shape an IN-list
    of row values binds as."""
    out: List = []
    for row in rows:
        out.extend(row)
    return out


def execute_many(target, sql: str, rows: Sequence[Sequence]) -> None:
    """`executemany` through whichever driver `target` wraps.

    Connectors expose `execute` and `execute_ddl` but nothing that binds
    many parameter sets, and adding a public method to six connectors to
    support one caller would be the wrong trade -- this reaches the cursor
    the same way each connector's own `insert_batch` does.
    """
    if not rows:
        return
    cursor = target._conn.cursor()
    try:
        if dialect_of(target) == "sqlserver":
            cursor.fast_executemany = True
        cursor.executemany(sql, [tuple(r) for r in rows])
    finally:
        cursor.close()


def execute_with(target, sql: str, params: Sequence) -> List[tuple]:
    """One parameterised statement, returning its rows (empty for a
    statement that returns none)."""
    cursor = target._conn.cursor()
    try:
        cursor.execute(sql, tuple(params))
        try:
            return list(cursor.fetchall())
        except Exception:  # noqa: BLE001 - a DELETE has nothing to fetch
            return []
    finally:
        cursor.close()


def counter(target) -> Callable[[str], int]:
    def count(table: str) -> int:
        rows = execute_with(target, f"SELECT COUNT(*) FROM {qualified(target, table)}", ())
        return int(rows[0][0]) if rows else 0

    return count
