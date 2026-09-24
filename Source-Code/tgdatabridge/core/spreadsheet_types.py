"""Cell-value classification/coercion and identifier sanitization shared by
the Excel/CSV source's introspector (tgdatabridge.core.spreadsheet_introspector)
and its connector (tgdatabridge.db.spreadsheet_connector).

This lives in its own module purely for layering: the introspector is in
`tgdatabridge/core/` and the connector is in `tgdatabridge/db/`, both need the exact
same rules for "what type is this cell, really?", and neither should have
to import the other at runtime to get them (the introspector only imports
the connector under TYPE_CHECKING, mirroring mongo_source_introspector).
Nothing here imports anything outside the standard library.

Why a spreadsheet needs a classification step at all
----------------------------------------------------
Every other source engine in this tool reports its own column types from
a real catalog view. A spreadsheet has no catalog and, for CSV, no types
whatsoever -- every value arrives as a string. So types are *inferred* by
sampling real cells, the same approach mongo_source_introspector takes
for MongoDB's schemaless documents.

Three inference decisions worth knowing about, all deliberately
conservative because guessing wrong here silently corrupts data:

  - **Text that looks numeric is treated as numeric** (`"42"` -> int),
    because "numbers stored as text" is the single most common thing
    wrong with a real spreadsheet, and leaving such a column as VARCHAR2
    would make every downstream numeric comparison on the target wrong.
  - **...except when it has a leading zero** (`"01234"`, `"007"`). That
    is virtually always a zip code, account number, product code or
    similar identifier where the zero is meaningful data, and converting
    it to an int destroys it irreversibly. These stay strings.
  - **Only unambiguous ISO-8601-style dates are recognized** as dates.
    `"2024-03-07"` is a date; `"03/07/2024"` is left as a string, because
    there is no way to know from the file whether that is March 7th or
    July 3rd, and a tool that silently picks one will silently produce a
    wrong migration for half the world. A string column of dates is
    obviously imperfect but it is *honestly* imperfect -- the user can
    see it in the assessment report and fix the source or the target
    column by hand.

Mixed-type columns resolve to text, not to the majority type
------------------------------------------------------------
Where a column's sampled values disagree, `combine_types` falls back to
"string" for any disagreement that isn't a pure numeric widening (int +
float -> float). This deliberately differs from
mongo_source_introspector's "most common type wins": in MongoDB a field
with mixed types is a genuine data-modelling question, but in a
spreadsheet a column of numbers with a few "N/A" cells in it is just
text with numbers in it -- and picking the majority type there would make
the *migration itself fail* on the minority rows rather than merely
producing an imperfect column type. Failing safe (a wider type) beats
failing loudly on row 40,000 of a long-running migration.
"""
from __future__ import annotations

import datetime
import decimal
import re
from typing import Any, Optional, Tuple

# The type-name vocabulary produced by classify_value() and understood by
# type_mapping.from_spreadsheet(). "null" is not a type -- it's the
# absence of an observation, tracked separately for nullability.
NULL = "null"
BOOL = "bool"
INT = "int"
FLOAT = "float"
DECIMAL = "decimal"
DATETIME = "datetime"
DATE = "date"
TIME = "time"
BINARY = "binary"
STRING = "string"

_NUMERIC_TYPES = (INT, FLOAT, DECIMAL)

# "01234", "007", "+00.5" -- a numeric-looking string whose leading zero
# carries meaning (zip/account/product codes). "0", "0.5" and "-0.25" are
# deliberately NOT matched: a bare zero and a fractional value below one
# are ordinary numbers, not codes.
_LEADING_ZERO_RE = re.compile(r"^[+-]?0[0-9]")

_INT_RE = re.compile(r"^[+-]?[0-9]+$")
# Rejects "nan"/"inf"/"infinity", which float() would otherwise accept and
# turn into values no database column can store.
_FLOAT_RE = re.compile(r"^[+-]?(?:[0-9]+\.[0-9]*|\.[0-9]+|[0-9]+)(?:[eE][+-]?[0-9]+)?$")

_TRUE_WORDS = {"true", "yes"}
_FALSE_WORDS = {"false", "no"}

# Unambiguous, ISO-8601-shaped only -- see this module's docstring for why
# "%d/%m/%Y"-style formats are deliberately absent.
_DATETIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M")
_DATE_FORMATS = ("%Y-%m-%d",)
_TIME_FORMATS = ("%H:%M:%S", "%H:%M")


def classify_value(value: Any) -> Tuple[str, Any]:
    """Classify one cell into `(type_name, normalized_value)`.

    `type_name` is one of this module's constants. `normalized_value` is
    what should actually be migrated for that cell: the parsed number or
    date for text that was recognized as one, the original object
    otherwise, and always None for an empty/blank cell.

    An all-whitespace string counts as blank (spreadsheets are full of
    cells containing a single space); a string that is kept *as* a string
    is returned unchanged rather than stripped, so no data is silently
    altered on the way through.
    """
    if value is None:
        return NULL, None
    # bool must precede int: bool is an int subclass in Python.
    if isinstance(value, bool):
        return BOOL, value
    if isinstance(value, int):
        return INT, value
    if isinstance(value, float):
        return FLOAT, value
    if isinstance(value, decimal.Decimal):
        return DECIMAL, value
    # datetime must precede date: datetime is a date subclass.
    if isinstance(value, datetime.datetime):
        return DATETIME, value
    if isinstance(value, datetime.date):
        return DATE, value
    if isinstance(value, datetime.time):
        return TIME, value
    if isinstance(value, (bytes, bytearray)):
        return BINARY, bytes(value)
    if isinstance(value, str):
        return _classify_text(value)
    # Anything else (an openpyxl formula object a workbook opened without
    # data_only could yield, some exotic cell type) is stringified rather
    # than guessed at.
    return STRING, str(value)


def _classify_text(text: str) -> Tuple[str, Any]:
    stripped = text.strip()
    if not stripped:
        return NULL, None

    lowered = stripped.lower()
    if lowered in _TRUE_WORDS:
        return BOOL, True
    if lowered in _FALSE_WORDS:
        return BOOL, False

    if not _LEADING_ZERO_RE.match(stripped):
        if _INT_RE.match(stripped):
            try:
                return INT, int(stripped)
            except ValueError:  # pragma: no cover - regex already guarantees this parses
                pass
        elif _FLOAT_RE.match(stripped):
            try:
                return FLOAT, float(stripped)
            except ValueError:  # pragma: no cover - as above
                pass

    for fmt in _DATETIME_FORMATS:
        parsed = _try_strptime(stripped, fmt)
        if parsed is not None:
            return DATETIME, parsed
    for fmt in _DATE_FORMATS:
        parsed = _try_strptime(stripped, fmt)
        if parsed is not None:
            return DATE, parsed.date()
    for fmt in _TIME_FORMATS:
        parsed = _try_strptime(stripped, fmt)
        if parsed is not None:
            return TIME, parsed.time()

    # Kept as the caller wrote it, not as `stripped` -- see classify_value.
    return STRING, text


def _try_strptime(text: str, fmt: str) -> Optional[datetime.datetime]:
    try:
        return datetime.datetime.strptime(text, fmt)
    except ValueError:
        return None


def combine_types(observed) -> str:
    """Reduce every type name observed in one column down to the single
    type that column should be given. `observed` is any iterable of type
    names (NULL entries are ignored -- they say nothing about type, only
    about nullability).

    Returns NULL when nothing was ever observed (an entirely empty
    column). That's deliberately *not* collapsed to STRING here: "every
    sampled row was blank" and "this column holds text" are genuinely
    different findings, and type_mapping.from_spreadsheet has a dedicated
    branch for the former that both picks a safe wide type and warns the
    user that nothing could be inferred. Folding it into STRING would
    silently drop that warning.
    """
    kinds = {t for t in observed if t != NULL}
    if not kinds:
        return NULL
    if len(kinds) == 1:
        return next(iter(kinds))
    # Pure numeric disagreement is a genuine widening, not a conflict:
    # a column of ints with some decimals in it is a decimal column.
    if kinds <= set(_NUMERIC_TYPES):
        if DECIMAL in kinds:
            return DECIMAL
        return FLOAT
    # A date column with times in it (or vice versa) widens to datetime,
    # which can represent both.
    if kinds <= {DATE, DATETIME}:
        return DATETIME
    # Everything else -- numbers mixed with text, dates mixed with
    # numbers, booleans mixed with anything -- falls back to text.
    return STRING


def is_conflict(observed) -> bool:
    """True only when a column's values disagreed in a way that forced
    combine_types to fall back to text.

    Deliberately *not* true for the widenings combine_types resolves
    cleanly -- a money column holding `100.50`, `0` and `-42.75` is an
    int/float mix by the letter of the rules and an ordinary decimal
    column by any reasonable reading, and warning about it would put a
    scary "review this" note on almost every numeric column of almost
    every real spreadsheet. A warning that fires constantly is a warning
    nobody reads, so this fires only when something genuinely was lost:
    a column that had to become text because its values could not all be
    represented any other way.
    """
    kinds = {t for t in observed if t != NULL}
    if len(kinds) <= 1:
        return False
    if kinds <= set(_NUMERIC_TYPES):
        return False
    if kinds <= {DATE, DATETIME}:
        return False
    return True


def coerce_for_pivot_type(value: Any, pivot_type: str) -> Any:
    """Convert one raw cell into a value that matches the column type the
    introspector settled on, for migration.

    This matters because inference happens on a *sample* while migration
    reads every row: a column inferred as text from a sample containing
    "N/A" must still receive `"42"` (a string) for a cell that classifies
    as an int, or the target's own type checking will reject the row.

    A value that cannot be coerced is returned **unchanged** rather than
    replaced with None. Silently nulling data the user asked to migrate
    would be the worst possible failure mode here -- letting the target
    driver raise its own error surfaces the problem instead of hiding it.
    """
    kind, normalized = classify_value(value)
    if kind == NULL:
        return None

    category = pivot_category(pivot_type)

    if category == "string":
        if isinstance(normalized, str):
            return normalized
        if isinstance(normalized, bool):
            return "true" if normalized else "false"
        if isinstance(normalized, (datetime.datetime, datetime.date, datetime.time)):
            return normalized.isoformat()
        return str(normalized)

    if category == "numeric":
        if isinstance(normalized, bool):
            return 1 if normalized else 0
        if isinstance(normalized, (int, float, decimal.Decimal)):
            return normalized
        return value

    if category == "bool":
        if isinstance(normalized, bool):
            return normalized
        if isinstance(normalized, (int, float)):
            return bool(normalized)
        return value

    if category == "datetime":
        # A column of mixed dates and datetimes types as TIMESTAMP (see
        # combine_types), so widen the plain dates to midnight datetimes
        # rather than handing a driver two different Python types for the
        # same column and hoping it copes.
        if _wants_timestamp(pivot_type) and type(normalized) is datetime.date:
            return datetime.datetime(normalized.year, normalized.month, normalized.day)
        if isinstance(normalized, (datetime.datetime, datetime.date, datetime.time)):
            return normalized
        return value

    if category == "binary":
        if isinstance(normalized, bytes):
            return normalized
        if isinstance(normalized, str):
            return normalized.encode("utf-8", errors="replace")
        return value

    return normalized


# --------------------------------------------------------- identifier naming

# Deliberately below Oracle's modern 128-char limit and above PostgreSQL's
# 63-char one is impossible, so this takes the tighter of the two: a name
# this tool generates must be valid on *every* target engine it supports.
_MAX_IDENTIFIER_LENGTH = 63
_NON_IDENTIFIER_RE = re.compile(r"[^A-Za-z0-9_]+")


def sanitize_identifier(raw: str, fallback: str) -> str:
    """Turn an arbitrary sheet name or header cell ("Order Items (2024)",
    "Unit Price $") into a legal, portable SQL identifier.

    ddl_generator quotes every identifier it emits, so exotic characters
    would technically survive -- but a quoted `"Unit Price $"` column is
    miserable to query by hand on the target afterwards, and the point of
    migrating a spreadsheet into a database is that people then use it as
    a database. Case is preserved (an all-caps sheet stays all-caps);
    only genuinely unusable characters are replaced.

    `fallback` is used when nothing usable survives (a header cell of
    "###", or a blank one).
    """
    collapsed = _NON_IDENTIFIER_RE.sub("_", (raw or "").strip()).strip("_")
    if not collapsed:
        return fallback
    # An identifier can't start with a digit on most engines.
    if collapsed[0].isdigit():
        collapsed = "_" + collapsed
    return collapsed[:_MAX_IDENTIFIER_LENGTH]


def _wants_timestamp(pivot_type: str) -> bool:
    """True for a pivot type whose target column carries a time component,
    so a plain date must be widened to a midnight datetime before it's
    written.

    DATE counts, which is easy to get wrong: this tool's pivot is
    Oracle-flavoured, and Oracle's DATE *is* a date-and-time type. Every
    target maps it accordingly -- DATETIME on MySQL, TIMESTAMP on
    PostgreSQL and Db2, DATETIME2 on SQL Server -- so a datetime.date
    written into one reads back as a datetime.datetime. Leaving the write
    as a bare date therefore produced a post-migration checksum mismatch
    on every spreadsheet column holding ISO dates: the data was correct
    and identical, but repr(date(2024,1,5)) and
    repr(datetime(2024,1,5,0,0)) differ, so validation reported a table
    as "Unvalidated" for no real reason. A false integrity warning is
    worse than none -- it teaches people to ignore the check.
    """
    normalized = (pivot_type or "").strip().upper()
    return normalized.startswith("TIMESTAMP") or normalized == "DATE"


def pivot_category(pivot_type: str) -> str:
    """Bucket one of this tool's Oracle-flavored pivot type strings (see
    Column.data_type) into the coarse category coerce_for_pivot_type
    needs. Unknown types fall back to "string", the widest bucket."""
    t = (pivot_type or "").strip().upper()
    if t.startswith(("NUMBER", "BINARY_DOUBLE", "BINARY_FLOAT", "FLOAT", "INTEGER", "DECIMAL", "NUMERIC")):
        return "numeric"
    if t.startswith("BOOLEAN"):
        return "bool"
    if t.startswith(("DATE", "TIMESTAMP", "INTERVAL")):
        return "datetime"
    if t.startswith(("BLOB", "RAW", "LONG RAW", "BFILE")):
        return "binary"
    return "string"
