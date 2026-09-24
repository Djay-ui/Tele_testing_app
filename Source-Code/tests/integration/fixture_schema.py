"""The schema every conformance test migrates, and the rows that go in it.

Why a fixed schema rather than one generated per test
-----------------------------------------------------
The point of this harness is that the *same* table is created, filled,
read back and validated on every engine, so a difference in behaviour
between engines shows up as a difference in the test result rather than
as a difference in the test. Each column below is here because something
has gone wrong with that shape before, or plausibly could:

``id``            NUMBER(9)      int4 on PostgreSQL -- the exact column
                                 class that binary COPY rejected when the
                                 dumper defaulted to numeric, turning a
                                 whole migration into "0 rows migrated".
``big_id``        NUMBER(18)     int8; a different OID, same failure mode.
``amount``        NUMBER(10,2)   decimal/numeric. Python Decimal must
                                 survive the round trip without becoming
                                 a float -- 1234.56 as a float is not
                                 1234.56.
``label``         VARCHAR2(100)  the ordinary case, and the one that
                                 carries the awkward text below.
``when_day``      DATE           Oracle's DATE has a time component, so
                                 every target maps it to a timestamp;
                                 coerce_for_pivot_type getting this wrong
                                 produced a *false* "Unvalidated" verdict
                                 that looked like data loss.
``when_exact``    TIMESTAMP(6)   microsecond precision, where engines
                                 differ most (MySQL truncates to seconds
                                 unless the column is declared with a
                                 fractional-seconds width).
``notes``         CLOB           LOB handling has its own batch size and
                                 its own code path in migrate_table.
``optional``      VARCHAR2(20)   nullable, with NULLs actually present.
``always_null``   VARCHAR2(20)   *every* row NULL. Type inference that
                                 samples values sees nothing here, and a
                                 column that is all-NULL has repeatedly
                                 been the one that breaks a bulk-load
                                 path (COPY in particular).

The text values include an embedded single quote, an embedded double
quote and a backtick, because those are the three delimiters this tool
quotes with, and a non-ASCII character to keep the encoding honest.
"""
from __future__ import annotations

import datetime
import decimal
from typing import List, Tuple

from tgdatabridge.core.schema_model import Column, Schema, Table

# The table name is deliberately lower case and unremarkable: case
# folding is exercised on purpose elsewhere (test_known_regressions), and
# mixing it in here would make an unrelated failure look like a type bug.
TABLE_NAME = "it_conformance"

COLUMNS = [
    Column(name="id", data_type="NUMBER(9)", nullable=False),
    Column(name="big_id", data_type="NUMBER(18)"),
    Column(name="amount", data_type="NUMBER(10,2)"),
    Column(name="label", data_type="VARCHAR2(100)"),
    Column(name="when_day", data_type="DATE"),
    Column(name="when_exact", data_type="TIMESTAMP(6)"),
    Column(name="notes", data_type="CLOB"),
    Column(name="optional", data_type="VARCHAR2(20)"),
    Column(name="always_null", data_type="VARCHAR2(20)"),
]

COLUMN_NAMES = [c.name for c in COLUMNS]


def sample_table(schema: str) -> Table:
    """A fresh Table each call -- Column carries mutable per-run state
    (``target_type``, ``issues``) that DDL generation overwrites, so
    sharing one instance between engines would let one engine's
    conversion results leak into the next one's."""
    return Table(
        name=TABLE_NAME,
        schema=schema,
        columns=[
            Column(name=c.name, data_type=c.data_type, nullable=c.nullable)
            for c in COLUMNS
        ],
    )


def sample_schema(schema: str, target_engine: str) -> Schema:
    return Schema(
        name=schema,
        source_engine="Oracle",
        target_engine=target_engine,
        tables=[sample_table(schema)],
    )


# --------------------------------------------------------------- the rows

_AWKWARD = [
    "plain",
    "O'Brien",                       # single quote -- the literal delimiter
    'say "hello"',                   # double quote -- the ANSI identifier delimiter
    "back`tick",                     # backtick -- the MySQL identifier delimiter
    "sem;icolon -- and a comment",   # what a naive statement splitter chokes on
    "unicode: café ☕",              # non-ASCII, to keep the encoding honest
]


def sample_rows(count: int = 60) -> List[Tuple]:
    """Deterministic rows. No randomness: a test that fails only on some
    runs is worse than no test, and a fixed seed is just randomness with
    extra steps when the failure has to be reproduced by hand."""
    rows = []
    base_day = datetime.date(2024, 1, 1)
    base_time = datetime.datetime(2024, 1, 1, 9, 30, 0)
    for i in range(count):
        rows.append((
            i + 1,                                              # id
            10_000_000_000 + i,                                 # big_id (> int4)
            decimal.Decimal("1234.56") + decimal.Decimal(i),    # amount
            _AWKWARD[i % len(_AWKWARD)],                        # label
            base_day + datetime.timedelta(days=i),              # when_day
            base_time + datetime.timedelta(seconds=i, microseconds=i * 7),
            ("lorem ipsum " * 20).strip() if i % 3 else None,   # notes (CLOB, some NULL)
            None if i % 4 == 0 else f"opt-{i}",                 # optional
            None,                                               # always_null
        ))
    return rows
