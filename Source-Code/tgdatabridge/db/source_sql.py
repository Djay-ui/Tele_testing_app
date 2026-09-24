"""Quoting identifiers in the SQL this tool builds against a *source*.

Every SELECT the data migration runs is assembled here rather than
written by a person, and until now it was assembled out of bare names:

    SELECT id, name, primary, created_at FROM app.address_rel

`primary` is a reserved word. So are `key`, `order`, `group`, `index`,
`class`, `function`, `condition`, `year_month` and a hundred others, and
a real application schema is full of columns called exactly those things.
MySQL answers the statement above with

    1064 (42000): You have an error in your SQL syntax ... near
    'primary, created_at, updated_at, deleted_at FROM app.address_rel'

and the table is reported as failed while every table whose columns
happen to avoid the reserved list migrates fine -- which is why this
survived so long. The same statement is equally invalid on PostgreSQL,
Oracle, SQL Server and Db2; only the list of reserved words differs.

Quoting every identifier removes the question entirely: a quoted
identifier is never a keyword on any of them. What differs is only the
quote character, and each engine's own connector already knows it -- this
picks the same one from the source connector, so the migration's SQL and
the connector's own SQL agree.

Names are quoted exactly as the introspector read them. That matters:
Oracle and Db2 report upper-case, PostgreSQL lower-case, and quoting
makes the case significant, so re-casing here would turn a working query
into "column does not exist".
"""
from __future__ import annotations

from typing import Callable, Iterable, Optional

from tgdatabridge.utils.identifiers import quote_backtick, quote_bracket, quote_double


def quoter_for(source) -> Callable[[str], str]:
    """The identifier quoting `source`'s engine uses.

    Chosen from the connector's own class name, the same way
    target_shape._quote_for does, so no engine name has to be threaded
    through the migrator to reach here.
    """
    name = type(source).__name__.lower()
    if "mysql" in name:
        return quote_backtick
    if "sqlserver" in name:
        return quote_bracket
    return quote_double


def quote_columns(source, names: Iterable[str]) -> str:
    """`a, b, c` -> "a", "b", "c" (or `a`, `b`, `c`, or [a], [b], [c])."""
    quote = quoter_for(source)
    return ", ".join(quote(name) for name in names)


def qualified_table(source, table_name: str, schema: Optional[str] = None) -> str:
    """`schema.table`, both parts quoted; the table alone when there is no
    schema.

    The schema is quoted for the same reason as the columns, and for one
    more: a MySQL database can be named something like
    `3f8cddb7226647be97fe09fd0b094e5e`, which an unquoted identifier is
    not always allowed to be.
    """
    quote = quoter_for(source)
    if schema:
        return f"{quote(schema)}.{quote(table_name)}"
    return quote(table_name)
