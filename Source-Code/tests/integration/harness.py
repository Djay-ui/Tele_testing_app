"""The two things every conformance test needs: a source that hands over
known rows, and a way to get generated DDL onto a real server.

`InMemorySource` stands in for a source database. It is the *only* fake
in this suite, and it is a fake on purpose: the question these tests
answer is "does what this tool generates actually work against a real
engine", and pinning the input rows to a literal list is what makes a
failure attributable. Reading the rows back afterwards goes through a
real connector (see the `source_reader` fixture), so the source side of
every connector is still exercised against a live server -- just in the
second half of the arc rather than the first.
"""
from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

from tgdatabridge.core.ddl_generator import generate_schema_ddl
from tgdatabridge.utils.sql_split import split_sql_statements


class InMemorySource:
    """A source connector backed by a Python list.

    Implements just the surface `migrator.migrate_table` uses:
    `fetch_batches(sql, batch_size)` and `count_rows`. The SQL string is
    accepted and ignored -- migrate_table builds it, and asserting on its
    exact text belongs in the unit suite, not here.
    """

    def __init__(self, columns: Sequence[str], rows: Sequence[Tuple]):
        self.columns = list(columns)
        self.rows = list(rows)
        self.statements: List[str] = []

    def fetch_batches(self, sql: str, batch_size: int = 5000):
        self.statements.append(sql)
        for start in range(0, len(self.rows), batch_size):
            yield self.columns, self.rows[start:start + batch_size]

    def count_rows(self, table: str, schema: Optional[str] = None) -> int:
        return len(self.rows)

    def close(self) -> None:
        pass


def apply_schema(target, schema, engine_label: str, namespace: str) -> List[str]:
    """Generate DDL for `schema` against `engine_label` and run it on
    `target`, one statement at a time. Returns the statements executed,
    so a failing test can print exactly what the server was asked to do.

    Statements are applied individually rather than as one script because
    that is what the tool itself does (`main_window._apply_ddl` and
    `cli/runner.py` both split first), and because a server error then
    names the statement that caused it instead of a 40-line blob.
    """
    ddl, _issues = generate_schema_ddl(schema, engine_label, _qualifier(engine_label, namespace))
    statements = split_sql_statements(ddl)
    for statement in statements:
        target.execute_ddl(statement)
    return statements


def _qualifier(engine_label: str, namespace: str) -> Optional[str]:
    """Which engines want the scratch namespace threaded into the DDL as
    an explicit qualifier.

    PostgreSQL pins a search_path when it connects and MySQL's connection
    is already bound to one database, so for those two the generated DDL
    must stay *unqualified* -- adding a qualifier would make the DDL
    address a schema the connector isn't pointing at. SQL Server, Db2 and
    Oracle have no search_path equivalent, so every object reference has
    to carry the schema. MongoDB has no schemas at all.
    """
    engine = engine_label.lower().replace(" ", "")
    if engine.startswith(("postgres", "mysql", "mongo")):
        return None
    if engine.startswith("db2") or engine.startswith("oracle"):
        return namespace.upper()
    return namespace


def target_schema_for(engine_label: str, namespace: str) -> Optional[str]:
    """What to pass as `schema=` to count_rows/checksum_rows/validate.

    Same split as `_qualifier`, for the same reason: on PostgreSQL and
    MySQL the connection already resolves the name, and passing a schema
    there would qualify a table that shouldn't be.
    """
    return _qualifier(engine_label, namespace)


def normalise(value):
    """Compare values across engines without pretending differences that
    matter don't exist.

    Only two normalisations are applied, both because the *engine's* type
    system genuinely has no narrower representation:

    * `Decimal` and `float` are compared as `Decimal` of the same string,
      because MySQL's connector returns DECIMAL as Decimal and some
      drivers hand back float for the same column.
    * A `date` is widened to a `datetime` at midnight, because an Oracle
      DATE maps to a timestamp on every target -- that is the documented
      mapping, not a rounding error.

    Anything else -- a truncated microsecond, a str that came back bytes,
    a NULL that came back empty-string -- is left alone so the assertion
    fails, which is the whole point.
    """
    import datetime
    import decimal

    if isinstance(value, float):
        return decimal.Decimal(str(value))
    if isinstance(value, datetime.datetime):
        return value
    if isinstance(value, datetime.date):
        return datetime.datetime(value.year, value.month, value.day)
    return value


def read_back(source_reader, engine_label: str, namespace: str,
              table_name: str, columns: Sequence[str]) -> List[Tuple]:
    """Every row currently in the target table, as a list of tuples in
    `columns` order, read through a real connector."""
    if engine_label.lower().startswith("mongo"):
        collection = source_reader.db[table_name]
        return [tuple(doc.get(c) for c in columns)
                for doc in collection.find({}, {"_id": 0})]

    from tgdatabridge.utils.identifiers import quote_backtick, quote_bracket, quote_double

    engine = engine_label.lower().replace(" ", "")
    if engine.startswith("mysql"):
        quote = quote_backtick
    elif engine.startswith("sqlserver"):
        quote = quote_bracket
    else:
        quote = quote_double

    fold = str
    if engine.startswith("postgres"):
        fold = str.lower
    elif engine.startswith(("oracle", "db2")):
        fold = str.upper

    qualifier = _qualifier(engine_label, namespace)
    ref = quote(fold(table_name))
    if qualifier:
        ref = f"{quote(fold(qualifier))}.{ref}"
    col_list = ", ".join(quote(fold(c)) for c in columns)

    rows: List[Tuple] = []
    for _cols, batch in source_reader.fetch_batches(
            f"SELECT {col_list} FROM {ref}", batch_size=1000):
        rows.extend(batch)
    return rows
