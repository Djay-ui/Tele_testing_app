"""
Oracle -> PostgreSQL, Oracle -> MySQL, Oracle -> SQL Server, and
Oracle -> Db2 data type mapping -- plus the reverse direction (MySQL ->
pivot, PostgreSQL -> pivot, SQL Server -> pivot, Db2 -> pivot, MongoDB ->
pivot) used when one of those engines is the *source* instead of Oracle.
See from_mysql()/from_postgres()/from_sqlserver()/from_db2()/
from_mongodb()'s own docstrings for how the reverse direction fits into
the rest of this module. from_mongodb() is the odd one out among these:
every other from_<engine>() reverse-maps a type string read straight off
a real catalog view, whereas MongoDB has no fixed schema at all, so its
"type string" is actually a BSON type name *inferred* by sampling
documents (see tgdatabridge.core.mongo_source_introspector) rather than
something a catalog ever declared.

Each forward mapper (to_postgres/to_mysql/to_sqlserver/to_db2/to_oracle) takes a raw Oracle type string such as "VARCHAR2(100)",
"NUMBER(10,2)", "NUMBER(4)", "CLOB", "TIMESTAMP(6) WITH TIME ZONE" and
returns (target_type, issues) where issues is a list of ConversionIssue.
to_oracle() is the odd one out among these: since this pivot type
representation already *is* Oracle's own native syntax, it's close to an
identity function rather than a real cross-dialect translation -- see its
own docstring.

Precision-based NUMBER handling follows the same rules AWS SCT documents:
NUMBER with scale 0 and precision <= 4 becomes a small integer type,
<= 9 a standard integer, <= 18 a big integer, otherwise an exact
numeric/decimal type. NUMBER with no precision/scale at all is treated
as an unbounded numeric, which is flagged as a warning because it maps
to arbitrary-precision types differently across engines.
"""
from __future__ import annotations

import re
from typing import List, Optional, Tuple

from tgdatabridge.core.schema_model import ConversionIssue

_NUMBER_RE = re.compile(r"^NUMBER\s*(\((\d+)(?:\s*,\s*(-?\d+))?\))?$", re.IGNORECASE)
_SIZED_RE = re.compile(r"^([A-Z0-9_ ]+?)\s*\((\d+)(?:\s*,\s*(\d+))?\)$", re.IGNORECASE)
_TIMESTAMP_RE = re.compile(r"^TIMESTAMP(\s*\(\d+\))?(\s+WITH(\s+LOCAL)?\s+TIME\s+ZONE)?$", re.IGNORECASE)


def _timestamp_precision(match, default: int, maximum: int) -> int:
    """The fractional-seconds digits a pivot TIMESTAMP asks for.

    `TIMESTAMP(3)` gives 3; a bare `TIMESTAMP` gives `default`, because
    Oracle's bare TIMESTAMP means TIMESTAMP(6) and the pivot is
    Oracle-flavoured. Clamped to `maximum` -- a target asked for more
    digits than it supports rejects the DDL outright, and rounding down
    to the most that engine can store is both what the user wanted and
    the only thing that works.
    """
    raw = match.group(1)
    if raw is None:
        return min(default, maximum)
    digits = int(raw.strip().strip("()").strip())
    return max(0, min(digits, maximum))


def _clean(raw_type: str) -> str:
    return " ".join(raw_type.strip().upper().split())


def _map_number(precision, scale) -> Tuple[str, List[ConversionIssue]]:
    issues: List[ConversionIssue] = []
    if precision is None:
        issues.append(ConversionIssue(
            "warning",
            "NUMBER with no precision/scale mapped to an unbounded numeric type; "
            "verify range and precision requirements on the target.",
        ))
        return "NUMERIC", issues

    precision = int(precision)
    scale = int(scale) if scale is not None else 0

    if scale > 0:
        return f"NUMERIC({precision},{scale})", issues
    if precision <= 4:
        return "SMALLINT", issues
    if precision <= 9:
        return "INTEGER", issues
    if precision <= 18:
        return "BIGINT", issues
    issues.append(ConversionIssue(
        "info", f"NUMBER({precision}) exceeds 64-bit integer range; mapped to NUMERIC({precision}).",
    ))
    return f"NUMERIC({precision})", issues


def _map_number_mysql(precision, scale) -> Tuple[str, List[ConversionIssue]]:
    issues: List[ConversionIssue] = []
    if precision is None:
        issues.append(ConversionIssue(
            "warning",
            "NUMBER with no precision/scale mapped to DECIMAL(65,30) (MySQL's maximum); "
            "verify range requirements on the target.",
        ))
        return "DECIMAL(65,30)", issues

    precision = int(precision)
    scale = int(scale) if scale is not None else 0

    if scale > 0:
        return f"DECIMAL({precision},{scale})", issues
    if precision <= 4:
        return "SMALLINT", issues
    if precision <= 9:
        return "INT", issues
    if precision <= 18:
        return "BIGINT", issues
    issues.append(ConversionIssue(
        "info", f"NUMBER({precision}) exceeds 64-bit integer range; mapped to DECIMAL({precision},0).",
    ))
    return f"DECIMAL({precision},0)", issues


def to_postgres(raw_type: str) -> Tuple[str, List[ConversionIssue]]:
    t = _clean(raw_type)
    issues: List[ConversionIssue] = []

    m = _NUMBER_RE.match(t)
    if m:
        return _map_number(m.group(2), m.group(3))

    if _TIMESTAMP_RE.match(t):
        return ("TIMESTAMPTZ" if "TIME ZONE" in t else "TIMESTAMP"), issues

    m = _SIZED_RE.match(t)
    base = m.group(1).strip() if m else t
    size = m.group(2) if m else None

    simple = {
        "VARCHAR2": lambda: (f"VARCHAR({size})" if size else "VARCHAR"),
        "NVARCHAR2": lambda: (f"VARCHAR({size})" if size else "VARCHAR"),
        "CHAR": lambda: (f"CHAR({size})" if size else "CHAR(1)"),
        "NCHAR": lambda: (f"CHAR({size})" if size else "CHAR(1)"),
        "VARCHAR": lambda: (f"VARCHAR({size})" if size else "VARCHAR"),
        "DATE": lambda: "TIMESTAMP",
        "FLOAT": lambda: "DOUBLE PRECISION",
        "BINARY_FLOAT": lambda: "REAL",
        "BINARY_DOUBLE": lambda: "DOUBLE PRECISION",
        "CLOB": lambda: "TEXT",
        "NCLOB": lambda: "TEXT",
        "LONG": lambda: "TEXT",
        "BLOB": lambda: "BYTEA",
        "LONG RAW": lambda: "BYTEA",
        "RAW": lambda: (f"BYTEA" if not size else "BYTEA"),
        "XMLTYPE": lambda: "XML",
        "BOOLEAN": lambda: "BOOLEAN",
        # SYS_REFCURSOR is Oracle's predefined *weak* ref cursor type --
        # usable directly as a variable/parameter/return type with no
        # local `TYPE ... IS REF CURSOR` declaration of its own (that
        # locally-declared form is a different, unsupported case already
        # flagged by convert_declare_block's unsupported_type_vars check).
        # Before this mapping existed, SYS_REFCURSOR fell all the way
        # through to the "no mapping rule" fallback below and silently
        # became TEXT -- a real migration then hit `OPEN p_cursor FOR
        # SELECT ...;` against a TEXT variable and failed at "Apply DDL to
        # Target" with 'variable "p_cursor" must be of type cursor or
        # refcursor', instead of ever being flagged during conversion.
        # PostgreSQL's own built-in REFCURSOR type is the direct, correct
        # equivalent: both are an opaque handle to an open cursor that can
        # be OPENed, FETCHed from, and returned to a caller.
        "SYS_REFCURSOR": lambda: "REFCURSOR",
        "ROWID": lambda: "VARCHAR(18)",
        "UROWID": lambda: "VARCHAR(4000)",
        "INTERVAL YEAR TO MONTH": lambda: "INTERVAL YEAR TO MONTH",
        "INTERVAL DAY TO SECOND": lambda: "INTERVAL DAY TO SECOND",
        "SYS.XMLTYPE": lambda: "XML",
    }

    if base in simple:
        result = simple[base]()
        if base == "ROWID":
            issues.append(ConversionIssue(
                "warning", "ROWID has no PostgreSQL equivalent; mapped to VARCHAR(18) as a stand-in. "
                           "Review any code that relies on ROWID semantics."))
        if base in ("RAW",) and size:
            issues.append(ConversionIssue(
                "info", f"RAW({size}) mapped to BYTEA; PostgreSQL BYTEA is unbounded, length constraint dropped."))
        return result, issues

    issues.append(ConversionIssue(
        "error", f"No mapping rule for Oracle type '{raw_type}'. Defaulting to TEXT; manual review required."))
    return "TEXT", issues


def _map_number_sqlserver(precision, scale) -> Tuple[str, List[ConversionIssue]]:
    issues: List[ConversionIssue] = []
    if precision is None:
        issues.append(ConversionIssue(
            "warning",
            "NUMBER with no precision/scale mapped to DECIMAL(38,10) (SQL Server's maximum "
            "precision is 38); verify range requirements on the target.",
        ))
        return "DECIMAL(38,10)", issues

    precision = int(precision)
    scale = int(scale) if scale is not None else 0

    if scale > 0:
        return f"DECIMAL({precision},{scale})", issues
    if precision <= 4:
        return "SMALLINT", issues
    if precision <= 9:
        return "INT", issues
    if precision <= 18:
        return "BIGINT", issues
    issues.append(ConversionIssue(
        "info", f"NUMBER({precision}) exceeds 64-bit integer range; mapped to DECIMAL({precision},0).",
    ))
    return f"DECIMAL({precision},0)", issues


def to_sqlserver(raw_type: str) -> Tuple[str, List[ConversionIssue]]:
    t = _clean(raw_type)
    issues: List[ConversionIssue] = []

    m = _NUMBER_RE.match(t)
    if m:
        return _map_number_sqlserver(m.group(2), m.group(3))

    if _TIMESTAMP_RE.match(t):
        if "TIME ZONE" in t:
            return "DATETIMEOFFSET", issues
        return "DATETIME2", issues

    m = _SIZED_RE.match(t)
    base = m.group(1).strip() if m else t
    size = m.group(2) if m else None

    # SQL Server's VARCHAR/NVARCHAR/VARBINARY only accept an explicit size up
    # to 8000 (4000 characters for NVARCHAR, which is 2 bytes/char); above
    # that -- or when Oracle gave no size at all -- MAX is the only valid
    # explicit-length alternative.
    def _sized(kind: str, max_explicit: int, default_max: str = "MAX") -> str:
        if size and int(size) <= max_explicit:
            return f"{kind}({size})"
        return f"{kind}({default_max})"

    simple = {
        # NVARCHAR2/NCHAR/NCLOB map to the N-prefixed Unicode types, unlike
        # the Postgres/MySQL mappers -- SQL Server's plain VARCHAR/CHAR are
        # collation-code-page text, not Unicode, while Oracle's N* types are
        # explicitly Unicode; NVARCHAR/NCHAR is the faithful equivalent.
        "VARCHAR2": lambda: _sized("VARCHAR", 8000),
        "NVARCHAR2": lambda: _sized("NVARCHAR", 4000),
        "CHAR": lambda: (f"CHAR({size})" if size else "CHAR(1)"),
        "NCHAR": lambda: (f"NCHAR({size})" if size else "NCHAR(1)"),
        "VARCHAR": lambda: _sized("VARCHAR", 8000),
        "DATE": lambda: "DATETIME2",
        "FLOAT": lambda: "FLOAT",
        "BINARY_FLOAT": lambda: "REAL",
        "BINARY_DOUBLE": lambda: "FLOAT",
        "CLOB": lambda: "NVARCHAR(MAX)",
        "NCLOB": lambda: "NVARCHAR(MAX)",
        "LONG": lambda: "NVARCHAR(MAX)",
        "BLOB": lambda: "VARBINARY(MAX)",
        "LONG RAW": lambda: "VARBINARY(MAX)",
        "RAW": lambda: _sized("VARBINARY", 8000),
        "XMLTYPE": lambda: "XML",
        "BOOLEAN": lambda: "BIT",
        "ROWID": lambda: "VARCHAR(18)",
        "UROWID": lambda: "VARCHAR(4000)",
        "SYS.XMLTYPE": lambda: "XML",
    }

    if base in simple:
        result = simple[base]()
        if base == "ROWID":
            issues.append(ConversionIssue(
                "warning", "ROWID has no SQL Server equivalent; mapped to VARCHAR(18) as a stand-in. "
                           "Review any code that relies on ROWID semantics."))
        if base == "UROWID":
            issues.append(ConversionIssue(
                "warning", "UROWID has no SQL Server equivalent; mapped to VARCHAR(4000) as a stand-in."))
        return result, issues

    if base in ("INTERVAL YEAR TO MONTH", "INTERVAL DAY TO SECOND"):
        issues.append(ConversionIssue(
            "error",
            f"SQL Server has no native INTERVAL type; '{raw_type}' mapped to NVARCHAR(30) as a "
            "placeholder. Interval arithmetic must be rewritten by hand using DATEADD/DATEDIFF.",
        ))
        return "NVARCHAR(30)", issues

    issues.append(ConversionIssue(
        "error", f"No mapping rule for Oracle type '{raw_type}'. Defaulting to NVARCHAR(MAX); manual review required."))
    return "NVARCHAR(MAX)", issues


def _map_number_db2(precision, scale) -> Tuple[str, List[ConversionIssue]]:
    issues: List[ConversionIssue] = []
    if precision is None:
        issues.append(ConversionIssue(
            "warning",
            "NUMBER with no precision/scale mapped to DECIMAL(31,9) (Db2's maximum "
            "precision is 31); verify range requirements on the target.",
        ))
        return "DECIMAL(31,9)", issues

    precision = int(precision)
    scale = int(scale) if scale is not None else 0

    if scale > 0:
        return f"DECIMAL({precision},{scale})", issues
    if precision <= 4:
        return "SMALLINT", issues
    if precision <= 9:
        return "INTEGER", issues
    if precision <= 18:
        return "BIGINT", issues
    if precision > 31:
        issues.append(ConversionIssue(
            "info", f"NUMBER({precision}) exceeds Db2's maximum DECIMAL precision (31); "
                    f"mapped to DECIMAL(31,0), values may be truncated -- review the source data.",
        ))
        return "DECIMAL(31,0)", issues
    issues.append(ConversionIssue(
        "info", f"NUMBER({precision}) exceeds 64-bit integer range; mapped to DECIMAL({precision},0).",
    ))
    return f"DECIMAL({precision},0)", issues


def to_db2(raw_type: str) -> Tuple[str, List[ConversionIssue]]:
    t = _clean(raw_type)
    issues: List[ConversionIssue] = []

    m = _NUMBER_RE.match(t)
    if m:
        return _map_number_db2(m.group(2), m.group(3))

    if _TIMESTAMP_RE.match(t):
        if "TIME ZONE" in t:
            issues.append(ConversionIssue(
                "warning", "Db2 LUW has no default TIMESTAMP WITH TIME ZONE type; storing as "
                           "TIMESTAMP. Time zone conversion logic must be handled in application code."))
        return "TIMESTAMP", issues

    m = _SIZED_RE.match(t)
    base = m.group(1).strip() if m else t
    size = m.group(2) if m else None

    simple = {
        "VARCHAR2": lambda: (f"VARCHAR({size})" if size else "VARCHAR(4000)"),
        "NVARCHAR2": lambda: (f"VARCHAR({size})" if size else "VARCHAR(4000)"),
        "CHAR": lambda: (f"CHAR({size})" if size else "CHAR(1)"),
        "NCHAR": lambda: (f"CHAR({size})" if size else "CHAR(1)"),
        "VARCHAR": lambda: (f"VARCHAR({size})" if size else "VARCHAR(4000)"),
        "DATE": lambda: "TIMESTAMP",
        "FLOAT": lambda: "DOUBLE",
        "BINARY_FLOAT": lambda: "REAL",
        "BINARY_DOUBLE": lambda: "DOUBLE",
        "CLOB": lambda: "CLOB",
        "NCLOB": lambda: "CLOB",
        "LONG": lambda: "CLOB",
        "BLOB": lambda: "BLOB",
        "LONG RAW": lambda: "BLOB",
        "RAW": lambda: (f"VARCHAR({size}) FOR BIT DATA" if size else "BLOB"),
        "XMLTYPE": lambda: "XML",
        "BOOLEAN": lambda: "BOOLEAN",
        "ROWID": lambda: "VARCHAR(18)",
        "UROWID": lambda: "VARCHAR(4000)",
        "SYS.XMLTYPE": lambda: "XML",
    }

    if base in simple:
        result = simple[base]()
        if base == "ROWID":
            issues.append(ConversionIssue(
                "warning", "Oracle ROWID semantics have no direct Db2 equivalent; mapped to "
                           "VARCHAR(18) as a stand-in. Review any code that relies on ROWID semantics."))
        if base == "UROWID":
            issues.append(ConversionIssue(
                "warning", "UROWID has no Db2 equivalent; mapped to VARCHAR(4000) as a stand-in."))
        if base == "NCLOB":
            issues.append(ConversionIssue(
                "info", "NCLOB mapped to CLOB; if the target database is not Unicode, double-byte "
                        "characters may not round-trip -- verify the database codeset."))
        return result, issues

    if base in ("INTERVAL YEAR TO MONTH", "INTERVAL DAY TO SECOND"):
        issues.append(ConversionIssue(
            "error",
            f"Db2 has no native INTERVAL type; '{raw_type}' mapped to VARCHAR(30) as a "
            "placeholder. Interval arithmetic must be rewritten by hand using date/time functions.",
        ))
        return "VARCHAR(30)", issues

    issues.append(ConversionIssue(
        "error", f"No mapping rule for Oracle type '{raw_type}'. Defaulting to CLOB; manual review required."))
    return "CLOB", issues


def to_mysql(raw_type: str) -> Tuple[str, List[ConversionIssue]]:
    t = _clean(raw_type)
    issues: List[ConversionIssue] = []

    m = _NUMBER_RE.match(t)
    if m:
        return _map_number_mysql(m.group(2), m.group(3))

    m = _TIMESTAMP_RE.match(t)
    if m:
        # MySQL's bare DATETIME keeps *whole seconds only*: fractional
        # precision is opt-in as DATETIME(n), n in 0..6. Oracle's bare
        # TIMESTAMP means TIMESTAMP(6), so mapping it to plain DATETIME
        # silently discarded microseconds on every row -- data loss that
        # nothing reported, because the row counts still matched and the
        # target genuinely contained a value.
        #
        # It surfaced instead as a checksum mismatch, i.e. as the
        # unhelpful "Unvalidated" verdict, and only once the integration
        # suite ran the round trip against a real server
        # (tests/integration/test_known_regressions.py). MySQL is the only
        # one of the five SQL targets with this default: PostgreSQL's
        # TIMESTAMP and Db2's are both 6 digits, and SQL Server's
        # DATETIME2 is 7.
        precision = _timestamp_precision(m, default=6, maximum=6)
        mapped = f"DATETIME({precision})" if precision else "DATETIME"
        if "TIME ZONE" in t:
            issues.append(ConversionIssue(
                "warning", f"MySQL TIMESTAMP has no explicit time zone type; storing as {mapped}. "
                           "Time zone conversion logic must be handled in application code."))
        return mapped, issues

    m = _SIZED_RE.match(t)
    base = m.group(1).strip() if m else t
    size = m.group(2) if m else None

    simple = {
        "VARCHAR2": lambda: (f"VARCHAR({size})" if size else "VARCHAR(4000)"),
        "NVARCHAR2": lambda: (f"VARCHAR({size})" if size else "VARCHAR(4000)"),
        "CHAR": lambda: (f"CHAR({size})" if size else "CHAR(1)"),
        "NCHAR": lambda: (f"CHAR({size})" if size else "CHAR(1)"),
        "VARCHAR": lambda: (f"VARCHAR({size})" if size else "VARCHAR(4000)"),
        "DATE": lambda: "DATETIME",
        "FLOAT": lambda: "DOUBLE",
        "BINARY_FLOAT": lambda: "FLOAT",
        "BINARY_DOUBLE": lambda: "DOUBLE",
        "CLOB": lambda: "LONGTEXT",
        "NCLOB": lambda: "LONGTEXT",
        "LONG": lambda: "LONGTEXT",
        "BLOB": lambda: "LONGBLOB",
        "LONG RAW": lambda: "LONGBLOB",
        "RAW": lambda: (f"VARBINARY({size})" if size else "VARBINARY(255)"),
        "XMLTYPE": lambda: "LONGTEXT",
        "BOOLEAN": lambda: "TINYINT(1)",
        "ROWID": lambda: "VARCHAR(18)",
        "UROWID": lambda: "VARCHAR(4000)",
    }

    if base in simple:
        result = simple[base]()
        if base == "ROWID":
            issues.append(ConversionIssue(
                "warning", "ROWID has no MySQL equivalent; mapped to VARCHAR(18) as a stand-in."))
        if base == "XMLTYPE":
            issues.append(ConversionIssue(
                "warning", "MySQL has no native XML type; XMLTYPE mapped to LONGTEXT. "
                           "XML-specific functions used in the source must be rewritten."))
        return result, issues

    issues.append(ConversionIssue(
        "error", f"No mapping rule for Oracle type '{raw_type}'. Defaulting to TEXT; manual review required."))
    return "TEXT", issues


_ORACLE_KNOWN_BASES = {
    "VARCHAR2", "NVARCHAR2", "CHAR", "NCHAR", "VARCHAR", "NUMBER", "FLOAT",
    "BINARY_FLOAT", "BINARY_DOUBLE", "CLOB", "NCLOB", "LONG", "BLOB",
    "LONG RAW", "RAW", "DATE", "TIMESTAMP", "XMLTYPE", "SYS.XMLTYPE",
    "BOOLEAN", "ROWID", "UROWID", "INTERVAL YEAR TO MONTH", "INTERVAL DAY TO SECOND",
}


def to_oracle(raw_type: str) -> Tuple[str, List[ConversionIssue]]:
    """Map the shared pivot type back onto real Oracle syntax for an Oracle
    *target*. Unlike every other to_*() forward mapper above, this is close
    to an identity function: the pivot representation this whole tool uses
    internally (VARCHAR2(100), NUMBER(10,2), CLOB, TIMESTAMP(6) WITH TIME
    ZONE, ...) already *is* Oracle's own native type syntax -- that's the
    entire reason it was chosen as the shared pivot in the first place, so
    every to_postgres/to_mysql/to_sqlserver/to_db2/to_mongodb forward
    mapper above only has to translate *out* of Oracle syntax, never into
    it. This still normalizes whitespace/casing the same way every other
    mapper does (`_clean`), and still validates the base type against the
    known pivot vocabulary rather than blindly trusting it, since a raw
    type here could originate from any of the five from_<engine>() reverse
    mappers just as easily as from a real Oracle source."""
    t = _clean(raw_type)
    issues: List[ConversionIssue] = []

    m = _NUMBER_RE.match(t)
    if m:
        precision, scale = m.group(2), m.group(3)
        if precision is None:
            return "NUMBER", issues
        return (f"NUMBER({precision},{scale})" if scale is not None else f"NUMBER({precision})"), issues

    if _TIMESTAMP_RE.match(t):
        return t, issues  # already valid Oracle TIMESTAMP[(n)][ WITH [LOCAL] TIME ZONE] syntax as-is

    m = _SIZED_RE.match(t)
    base = m.group(1).strip() if m else t
    size = m.group(2) if m else None
    scale = m.group(3) if m else None

    if base == "SYS.XMLTYPE":
        base = "XMLTYPE"  # collapse the fully-qualified spelling some sources emit onto the plain one

    if base not in _ORACLE_KNOWN_BASES:
        issues.append(ConversionIssue(
            "error", f"No mapping rule for pivot type '{raw_type}'. Defaulting to CLOB; manual review required."))
        return "CLOB", issues

    if base == "BOOLEAN":
        issues.append(ConversionIssue(
            "info",
            "Native BOOLEAN table columns require Oracle Database 23c or later; on an older Oracle "
            "target, replace this column with NUMBER(1) (or CHAR(1) 'Y'/'N') and adjust any "
            "referencing PL/SQL by hand.",
        ))
        return "BOOLEAN", issues

    if size is not None:
        return (f"{base}({size},{scale})" if scale is not None else f"{base}({size})"), issues
    return base, issues


def to_mongodb(raw_type: str) -> Tuple[str, List[ConversionIssue]]:
    """Map an Oracle pivot type to a BSON type name usable inside a
    $jsonSchema validator's "bsonType" field (see
    ddl_generator.generate_table_ddl_mongodb). MongoDB is schema-flexible
    by design -- there is no CREATE TABLE column-type declaration the way
    every other target has -- so this only decides what goes in the
    *optional* validator this tool generates for it, not something the
    database itself would reject a mismatched value against without one."""
    t = _clean(raw_type)
    issues: List[ConversionIssue] = []

    m = _NUMBER_RE.match(t)
    if m:
        precision, scale = m.group(2), m.group(3)
        if precision is None:
            issues.append(ConversionIssue(
                "warning",
                "NUMBER with no precision/scale mapped to BSON decimal (Decimal128); the inserting "
                "application code must construct real Decimal128 values (not plain JS numbers) for "
                "this field, or precision will silently be lost.",
            ))
            return "decimal", issues
        precision = int(precision)
        scale = int(scale) if scale is not None else 0
        if scale > 0:
            issues.append(ConversionIssue(
                "info",
                f"NUMBER({precision},{scale}) mapped to BSON decimal (Decimal128); the inserting "
                "application code must construct real Decimal128 values (not plain JS numbers) for "
                "this field, or precision will silently be lost.",
            ))
            return "decimal", issues
        if precision <= 9:
            return "int", issues
        if precision <= 18:
            return "long", issues
        issues.append(ConversionIssue(
            "info", f"NUMBER({precision}) exceeds BSON long's 64-bit range; mapped to decimal (Decimal128).",
        ))
        return "decimal", issues

    if _TIMESTAMP_RE.match(t):
        if "TIME ZONE" in t:
            issues.append(ConversionIssue(
                "info",
                "BSON date values are always stored as a UTC instant; the original TIME ZONE "
                "offset itself is not preserved as separate, queryable data (only the point in "
                "time it represents is).",
            ))
        return "date", issues

    m = _SIZED_RE.match(t)
    base = m.group(1).strip() if m else t

    simple = {
        "VARCHAR2": lambda: "string",
        "NVARCHAR2": lambda: "string",
        "CHAR": lambda: "string",
        "NCHAR": lambda: "string",
        "VARCHAR": lambda: "string",
        "DATE": lambda: "date",
        "FLOAT": lambda: "double",
        "BINARY_FLOAT": lambda: "double",
        "BINARY_DOUBLE": lambda: "double",
        "CLOB": lambda: "string",
        "NCLOB": lambda: "string",
        "LONG": lambda: "string",
        "BLOB": lambda: "binData",
        "LONG RAW": lambda: "binData",
        "RAW": lambda: "binData",
        "XMLTYPE": lambda: "string",
        "BOOLEAN": lambda: "bool",
        "ROWID": lambda: "string",
        "UROWID": lambda: "string",
        "SYS.XMLTYPE": lambda: "string",
    }

    if base in simple:
        result = simple[base]()
        if base == "ROWID":
            issues.append(ConversionIssue(
                "warning", "ROWID has no MongoDB equivalent; mapped to a plain string as a stand-in. "
                           "Review any code that relies on ROWID semantics."))
        if base == "UROWID":
            issues.append(ConversionIssue(
                "warning", "UROWID has no MongoDB equivalent; mapped to a plain string as a stand-in."))
        if base in ("CLOB", "NCLOB", "LONG"):
            issues.append(ConversionIssue(
                "info", "MongoDB documents (and therefore any single field) have a hard 16MB size "
                        "limit; very large CLOB values may need to be stored via GridFS instead of "
                        "inline in the document."))
        if base == "XMLTYPE":
            issues.append(ConversionIssue(
                "info", "MongoDB has no native XML type; XMLTYPE mapped to a plain string. "
                        "XML-specific query/indexing functions used in the source must be rewritten."))
        return result, issues

    if base in ("INTERVAL YEAR TO MONTH", "INTERVAL DAY TO SECOND"):
        issues.append(ConversionIssue(
            "error",
            f"MongoDB has no native INTERVAL type; '{raw_type}' mapped to a plain string as a "
            "placeholder. Interval arithmetic must be rewritten by hand in application code.",
        ))
        return "string", issues

    issues.append(ConversionIssue(
        "error", f"No mapping rule for Oracle type '{raw_type}'. Defaulting to string; manual review required."))
    return "string", issues


_MYSQL_SIZED_RE = re.compile(r"^([A-Za-z_]+)\s*\((\d+)(?:\s*,\s*(\d+))?\)$")
_MYSQL_ENUM_SET_RE = re.compile(r"^(ENUM|SET)\s*\((.+)\)$", re.IGNORECASE | re.DOTALL)

# MySQL's own NUMERIC_PRECISION for each unsigned-agnostic integer type,
# reused directly as the pivot NUMBER(p)'s precision -- same widths
# MySQL's own information_schema.COLUMNS reports for these types.
_MYSQL_INT_WIDTHS = {
    "TINYINT": 3, "SMALLINT": 5, "MEDIUMINT": 7, "INT": 10, "INTEGER": 10, "BIGINT": 19,
}


def from_mysql(raw_type: str) -> Tuple[str, List[ConversionIssue]]:
    """Reverse-map a MySQL native column type (as reconstructed by
    tgdatabridge.core.mysql_introspector._fmt_data_type, e.g. "varchar(255)",
    "decimal(10,2)", "enum('a','b')", "int(10) unsigned") into this tool's
    shared Oracle-flavored pivot type string -- so the *existing*
    to_postgres/to_mysql/to_sqlserver/to_db2 forward-mappers can convert a
    MySQL-sourced column to any target exactly the same way they already
    convert an Oracle-sourced one, with no changes needed on that side at
    all. Returns (pivot_type, issues); a non-empty `issues` list here
    means the conversion is lossy or a total guess and should end up on
    Column.source_issues (not Column.issues) so it survives being
    re-combined with each target engine's own issues every time DDL is
    (re)generated -- see Column.source_issues's own docstring."""
    issues: List[ConversionIssue] = []

    # "unsigned"/"zerofill" carry no meaning in the pivot's Oracle-style
    # NUMBER (which has no unsigned concept) or in any of the forward
    # mappers, so they're stripped before matching rather than handled
    # type-by-type below; unsigned's extra positive-range headroom is
    # flagged once here since it's silently lost either way.
    t = raw_type.strip()
    if re.search(r"\bunsigned\b", t, re.IGNORECASE):
        issues.append(ConversionIssue(
            "info",
            f"MySQL type '{raw_type}' is UNSIGNED; the pivot representation used by this tool has no "
            "unsigned-integer concept, so the extra positive-range headroom (roughly double the signed "
            "range) is not preserved -- widen the target column by hand if values can exceed the signed range.",
        ))
    t = re.sub(r"\s+(unsigned|zerofill)\b", "", t, flags=re.IGNORECASE).strip()
    upper = t.upper()

    # MySQL's own convention for a boolean column is TINYINT(1) (there is
    # no native BOOLEAN storage type -- BOOL/BOOLEAN are just aliases for
    # TINYINT(1)); map it back to the pivot's own BOOLEAN rather than a
    # generic small integer so it round-trips as a real boolean on targets
    # that have one (PostgreSQL/Db2 BOOLEAN, SQL Server BIT).
    if re.match(r"^TINYINT\s*\(\s*1\s*\)$", upper):
        return "BOOLEAN", issues

    m = _MYSQL_SIZED_RE.match(t)
    base = m.group(1).upper() if m else upper
    size1 = m.group(2) if m else None
    size2 = m.group(3) if m else None

    if base in _MYSQL_INT_WIDTHS:
        return f"NUMBER({_MYSQL_INT_WIDTHS[base]})", issues

    if base in ("DECIMAL", "NUMERIC"):
        precision = size1 or "10"
        scale = size2 or "0"
        return f"NUMBER({precision},{scale})", issues

    if base == "FLOAT":
        return "BINARY_FLOAT", issues
    if base in ("DOUBLE", "REAL"):
        return "BINARY_DOUBLE", issues

    if base == "CHAR":
        return (f"CHAR({size1})" if size1 else "CHAR(1)"), issues
    if base == "VARCHAR":
        return (f"VARCHAR2({size1})" if size1 else "VARCHAR2(4000)"), issues
    if base == "BINARY":
        return (f"RAW({size1})" if size1 else "RAW(1)"), issues
    if base == "VARBINARY":
        return (f"RAW({size1})" if size1 else "RAW(4000)"), issues

    if base in ("TEXT", "TINYTEXT", "MEDIUMTEXT", "LONGTEXT"):
        return "CLOB", issues
    if base in ("BLOB", "TINYBLOB", "MEDIUMBLOB", "LONGBLOB"):
        return "BLOB", issues

    if base == "DATE":
        issues.append(ConversionIssue(
            "info",
            "MySQL DATE (date-only, with no time component) was mapped through the shared pivot as "
            "Oracle-style DATE, which always implies a time component -- the target column may end up "
            "able to store a time-of-day this source column never had; harmless unless application code "
            "specifically depends on the target rejecting one.",
        ))
        return "DATE", issues
    if base in ("DATETIME", "TIMESTAMP"):
        return "TIMESTAMP", issues
    if base == "TIME":
        issues.append(ConversionIssue(
            "warning",
            "MySQL TIME (time-of-day with no date component) has no equivalent in the shared pivot; "
            "mapped to VARCHAR2(20) as a text stand-in -- review any time arithmetic on the target, "
            "it will need to be rewritten against whatever real type replaces this.",
        ))
        return "VARCHAR2(20)", issues
    if base == "YEAR":
        return "NUMBER(4)", issues

    if base == "BIT":
        width = int(size1) if size1 else 1
        if width == 1:
            return "BOOLEAN", issues
        issues.append(ConversionIssue(
            "info",
            f"MySQL BIT({width}) mapped to RAW({(width + 7) // 8}) (byte-packed); bit-level access must "
            "be rewritten by hand against whatever the target's own bit/binary type looks like.",
        ))
        return f"RAW({(width + 7) // 8})", issues

    if base == "JSON":
        issues.append(ConversionIssue(
            "info",
            "MySQL JSON mapped to CLOB in the shared pivot; the target's own native JSON type and "
            "validation, if it has one, is not applied automatically -- add it by hand if needed.",
        ))
        return "CLOB", issues

    if base in ("GEOMETRY", "POINT", "LINESTRING", "POLYGON", "MULTIPOINT",
                "MULTILINESTRING", "MULTIPOLYGON", "GEOMETRYCOLLECTION"):
        issues.append(ConversionIssue(
            "error",
            f"MySQL spatial type '{raw_type}' has no equivalent in the shared pivot; mapped to BLOB as "
            "a raw byte stand-in. Spatial functions/indexes used against this column must be rewritten "
            "by hand for whatever spatial support (or lack of it) the target offers.",
        ))
        return "BLOB", issues

    enum_set_m = _MYSQL_ENUM_SET_RE.match(t)
    if enum_set_m:
        kind = enum_set_m.group(1).upper()
        literals = [lit.strip() for lit in enum_set_m.group(2).split(",") if lit.strip()]
        longest = max((len(lit.strip("'")) for lit in literals), default=1)
        width = longest if kind == "ENUM" else min(max(sum(len(lit) + 1 for lit in literals), 1), 1000)
        issues.append(ConversionIssue(
            "warning",
            f"MySQL {kind}({', '.join(literals) or '...'}) has no equivalent; mapped to VARCHAR2({width}) "
            f"with the {kind.lower()}'s allowed-value constraint dropped -- add a CHECK constraint on the "
            "target by hand if those values still need to be enforced.",
        ))
        return f"VARCHAR2({width})", issues

    issues.append(ConversionIssue(
        "error", f"No mapping rule for MySQL type '{raw_type}'. Defaulting to CLOB; manual review required."))
    return "CLOB", issues


_PG_SIZED_RE = re.compile(r"^([a-z_ ]+?)\s*\((\d+)(?:\s*,\s*(\d+))?\)$")

# PostgreSQL's own NUMERIC_PRECISION for each fixed-width integer type,
# reused directly as the pivot NUMBER(p)'s precision -- same widths
# PostgreSQL's own information_schema.COLUMNS reports for these types.
_PG_INT_WIDTHS = {"smallint": 5, "int2": 5, "integer": 10, "int": 10, "int4": 10, "bigint": 19, "int8": 19}


def from_postgres(raw_type: str) -> Tuple[str, List[ConversionIssue]]:
    """Reverse-map a PostgreSQL native column type (as reconstructed by
    tgdatabridge.core.postgres_introspector._fmt_data_type, e.g.
    "character varying(255)", "numeric(10,2)", "timestamp with time zone",
    "integer[]") into this tool's shared Oracle-flavored pivot type string
    -- so the *existing* to_postgres/to_mysql/to_sqlserver/to_db2 forward-
    mappers can convert a PostgreSQL-sourced column to any target exactly
    the same way they already convert an Oracle-sourced one, with no
    changes needed on that side at all. Returns (pivot_type, issues); a
    non-empty `issues` list here means the conversion is lossy or a total
    guess and should end up on Column.source_issues (not Column.issues) so
    it survives being re-combined with each target engine's own issues
    every time DDL is (re)generated -- see Column.source_issues's own
    docstring. Mirrors from_mysql() above; see that function for the
    general design rationale."""
    issues: List[ConversionIssue] = []
    t = raw_type.strip().lower()

    m = _PG_SIZED_RE.match(t)
    base = m.group(1).strip() if m else t
    size1 = m.group(2) if m else None
    size2 = m.group(3) if m else None

    if base in _PG_INT_WIDTHS:
        return f"NUMBER({_PG_INT_WIDTHS[base]})", issues

    if base in ("numeric", "decimal"):
        if size1 is not None:
            scale = size2 or "0"
            return f"NUMBER({size1},{scale})", issues
        # Unqualified NUMERIC in Postgres is arbitrary-precision, same as an
        # Oracle NUMBER with no precision/scale at all -- a faithful,
        # lossless pivot representation, so no issue is raised here (each
        # forward mapper already warns about *that* case on its own, the
        # same way it would for a genuinely unbounded Oracle NUMBER).
        return "NUMBER", issues

    if base in ("real", "float4"):
        return "BINARY_FLOAT", issues
    if base in ("double precision", "float8", "float"):
        return "BINARY_DOUBLE", issues

    if base in ("character", "char", "bpchar"):
        return (f"CHAR({size1})" if size1 else "CHAR(1)"), issues
    if base in ("character varying", "varchar"):
        if size1 and int(size1) <= 4000:
            return f"VARCHAR2({size1})", issues
        issues.append(ConversionIssue(
            "info",
            f"PostgreSQL {'unbounded ' if not size1 else ''}character varying"
            f"{'(' + size1 + ')' if size1 else ''} exceeds Oracle VARCHAR2's practical 4000-character "
            "limit; mapped to CLOB instead of truncating.",
        ))
        return "CLOB", issues
    if base == "text":
        return "CLOB", issues
    if base == "bytea":
        return "BLOB", issues
    if base in ("boolean", "bool"):
        return "BOOLEAN", issues

    if base == "date":
        issues.append(ConversionIssue(
            "info",
            "PostgreSQL DATE (date-only, with no time component) was mapped through the shared pivot "
            "as Oracle-style DATE, which always implies a time component -- the target column may end "
            "up able to store a time-of-day this source column never had; harmless unless application "
            "code specifically depends on the target rejecting one.",
        ))
        return "DATE", issues
    if base in ("timestamp", "timestamp without time zone"):
        return "TIMESTAMP", issues
    if base in ("timestamptz", "timestamp with time zone"):
        return "TIMESTAMP WITH TIME ZONE", issues
    if base in ("time", "time without time zone"):
        issues.append(ConversionIssue(
            "warning",
            "PostgreSQL TIME (time-of-day with no date component) has no equivalent in the shared "
            "pivot; mapped to VARCHAR2(20) as a text stand-in -- review any time arithmetic on the "
            "target, it will need to be rewritten against whatever real type replaces this.",
        ))
        return "VARCHAR2(20)", issues
    if base in ("timetz", "time with time zone"):
        issues.append(ConversionIssue(
            "warning",
            "PostgreSQL TIME WITH TIME ZONE has no equivalent in the shared pivot; mapped to "
            "VARCHAR2(30) as a text stand-in, and the time zone offset is not preserved in a queryable "
            "form -- review any time/time zone arithmetic on the target.",
        ))
        return "VARCHAR2(30)", issues

    if base.startswith("interval"):
        month_ish = any(kw in base for kw in ("year", "month"))
        day_ish = any(kw in base for kw in ("day", "hour", "minute", "second"))
        if month_ish and not day_ish:
            return "INTERVAL YEAR TO MONTH", issues
        if day_ish and not month_ish:
            return "INTERVAL DAY TO SECOND", issues
        issues.append(ConversionIssue(
            "info",
            f"PostgreSQL '{raw_type}' is a general-purpose INTERVAL that can mix year/month and "
            "day/time fields at once; the pivot only has Oracle's split YEAR TO MONTH / DAY TO SECOND "
            "interval types, so this was mapped to INTERVAL DAY TO SECOND -- review any interval "
            "arithmetic that relies on the year/month fields.",
        ))
        return "INTERVAL DAY TO SECOND", issues

    if base == "uuid":
        issues.append(ConversionIssue(
            "warning",
            "PostgreSQL UUID has no equivalent in the shared pivot; mapped to VARCHAR2(36) as a text "
            "stand-in -- format validation and any UUID-generation defaults must be rewritten by hand "
            "against whatever the target offers.",
        ))
        return "VARCHAR2(36)", issues

    if base in ("json", "jsonb"):
        issues.append(ConversionIssue(
            "info",
            f"PostgreSQL {base.upper()} mapped to CLOB in the shared pivot; the target's own native "
            "JSON type and validation, if it has one, is not applied automatically -- add it by hand "
            "if needed.",
        ))
        return "CLOB", issues

    if base == "xml":
        return "XMLTYPE", issues

    if base == "money":
        issues.append(ConversionIssue(
            "info",
            "PostgreSQL MONEY mapped to NUMBER(19,4) in the shared pivot; MONEY's locale-dependent "
            "currency formatting and rounding behavior are not preserved -- review any code that "
            "depends on them.",
        ))
        return "NUMBER(19,4)", issues

    if base in ("inet", "cidr", "macaddr", "macaddr8"):
        issues.append(ConversionIssue(
            "warning",
            f"PostgreSQL {base.upper()} has no equivalent in the shared pivot; mapped to VARCHAR2(43) "
            "as a text stand-in -- address-aware comparison/containment operators used against this "
            "column must be rewritten by hand for whatever the target offers.",
        ))
        return "VARCHAR2(43)", issues

    if base in ("bit", "bit varying", "varbit"):
        width = int(size1) if size1 else 1
        if base == "bit" and width == 1:
            return "BOOLEAN", issues
        issues.append(ConversionIssue(
            "info",
            f"PostgreSQL {raw_type} mapped to RAW({max((width + 7) // 8, 1)}) (byte-packed); bit-level "
            "access must be rewritten by hand against whatever the target's own bit/binary type "
            "looks like.",
        ))
        return f"RAW({max((width + 7) // 8, 1)})", issues

    if base.endswith("[]"):
        issues.append(ConversionIssue(
            "error",
            f"PostgreSQL array type '{raw_type}' has no equivalent in the shared pivot; mapped to CLOB "
            "as a raw-text stand-in. Array indexing/containment operators used against this column "
            "must be rewritten by hand for whatever the target offers (most targets have no native "
            "array type either).",
        ))
        return "CLOB", issues

    issues.append(ConversionIssue(
        "error", f"No mapping rule for PostgreSQL type '{raw_type}'. Defaulting to CLOB; manual review required."))
    return "CLOB", issues


_SS_SIZED_RE = re.compile(r"^([a-z_][a-z_0-9]*)\s*\(([^,)]+)(?:\s*,\s*(\d+))?\)$", re.IGNORECASE)

# SQL Server's own precision for each fixed-width integer type, reused
# directly as the pivot NUMBER(p)'s precision.
_SS_INT_WIDTHS = {"tinyint": 3, "smallint": 5, "int": 10, "bigint": 19}


def from_sqlserver(raw_type: str) -> Tuple[str, List[ConversionIssue]]:
    """Reverse-map a SQL Server native column type (as reconstructed by
    tgdatabridge.core.sqlserver_introspector._fmt_data_type, e.g. "varchar(255)",
    "nvarchar(100)" -- already character-length, not the raw byte length
    sys.columns reports -- "decimal(10,2)", "nvarchar(max)",
    "datetimeoffset") into this tool's shared Oracle-flavored pivot type
    string -- so the *existing* to_postgres/to_mysql/to_sqlserver/to_db2
    forward-mappers can convert a SQL Server-sourced column to any target
    exactly the same way they already convert an Oracle-sourced one, with
    no changes needed on that side at all. Returns (pivot_type, issues); a
    non-empty `issues` list here means the conversion is lossy or a total
    guess and should end up on Column.source_issues (not Column.issues) --
    see Column.source_issues's own docstring. Mirrors from_mysql()/
    from_postgres() above; see those for the general design rationale."""
    issues: List[ConversionIssue] = []
    t = raw_type.strip().lower()

    m = _SS_SIZED_RE.match(t)
    base = m.group(1).lower() if m else t
    size1 = m.group(2).strip().lower() if m else None  # a digit string, or "max"
    size2 = m.group(3) if m else None

    if base in _SS_INT_WIDTHS:
        return f"NUMBER({_SS_INT_WIDTHS[base]})", issues

    if base in ("decimal", "numeric"):
        precision = size1 or "18"
        scale = size2 or "0"
        return f"NUMBER({precision},{scale})", issues

    if base == "money":
        return "NUMBER(19,4)", issues
    if base == "smallmoney":
        return "NUMBER(10,4)", issues

    if base == "bit":
        return "BOOLEAN", issues

    if base == "real":
        return "BINARY_FLOAT", issues
    if base == "float":
        return "BINARY_DOUBLE", issues

    if base in ("char", "nchar"):
        return (f"CHAR({size1})" if size1 else "CHAR(1)"), issues
    if base in ("varchar", "nvarchar"):
        if size1 == "max":
            return "CLOB", issues
        return (f"VARCHAR2({size1})" if size1 else "VARCHAR2(4000)"), issues
    if base in ("text", "ntext"):
        return "CLOB", issues

    if base == "binary":
        return (f"RAW({size1})" if size1 else "RAW(1)"), issues
    if base == "varbinary":
        if size1 == "max":
            return "BLOB", issues
        return (f"RAW({size1})" if size1 else "RAW(4000)"), issues
    if base == "image":
        return "BLOB", issues

    if base == "date":
        issues.append(ConversionIssue(
            "info",
            "SQL Server DATE (date-only, with no time component) was mapped through the shared pivot "
            "as Oracle-style DATE, which always implies a time component -- the target column may end "
            "up able to store a time-of-day this source column never had; harmless unless application "
            "code specifically depends on the target rejecting one.",
        ))
        return "DATE", issues
    if base in ("datetime", "datetime2", "smalldatetime"):
        return "TIMESTAMP", issues
    if base == "datetimeoffset":
        return "TIMESTAMP WITH TIME ZONE", issues
    if base == "time":
        issues.append(ConversionIssue(
            "warning",
            "SQL Server TIME (time-of-day with no date component) has no equivalent in the shared "
            "pivot; mapped to VARCHAR2(20) as a text stand-in -- review any time arithmetic on the "
            "target, it will need to be rewritten against whatever real type replaces this.",
        ))
        return "VARCHAR2(20)", issues

    if base == "uniqueidentifier":
        issues.append(ConversionIssue(
            "warning",
            "SQL Server UNIQUEIDENTIFIER has no equivalent in the shared pivot; mapped to VARCHAR2(36) "
            "as a text stand-in -- format validation and any GUID-generation defaults must be "
            "rewritten by hand against whatever the target offers.",
        ))
        return "VARCHAR2(36)", issues

    if base == "xml":
        return "XMLTYPE", issues

    if base in ("timestamp", "rowversion"):
        # SQL Server's type literally named TIMESTAMP (ROWVERSION is its
        # modern alias) is NOT a date/time type at all -- it's an
        # auto-updating 8-byte binary value used for optimistic
        # concurrency, unrelated to the SQL-standard TIMESTAMP everywhere
        # else in this file.
        issues.append(ConversionIssue(
            "warning",
            "SQL Server ROWVERSION/TIMESTAMP is an auto-updating 8-byte binary value for optimistic "
            "concurrency, not a date/time type -- it has no equivalent in the shared pivot and was "
            "mapped to RAW(8). Any concurrency-check logic built on it must be redesigned for "
            "whatever the target offers (e.g. a trigger-maintained version column).",
        ))
        return "RAW(8)", issues

    if base == "sql_variant":
        issues.append(ConversionIssue(
            "error",
            "SQL Server SQL_VARIANT (a column that can hold values of different data types per row) "
            "has no equivalent in the shared pivot; mapped to CLOB as a raw-text stand-in. Any code "
            "that branches on the stored value's runtime type must be rewritten by hand.",
        ))
        return "CLOB", issues

    if base == "hierarchyid":
        issues.append(ConversionIssue(
            "error",
            "SQL Server HIERARCHYID has no equivalent in the shared pivot; mapped to CLOB as a "
            "raw-text stand-in. Hierarchy-aware methods (GetAncestor, IsDescendantOf, etc.) used "
            "against this column must be rewritten by hand for whatever the target offers.",
        ))
        return "CLOB", issues

    if base in ("geometry", "geography"):
        issues.append(ConversionIssue(
            "error",
            f"SQL Server spatial type '{raw_type}' has no equivalent in the shared pivot; mapped to "
            "BLOB as a raw byte stand-in. Spatial functions/indexes used against this column must be "
            "rewritten by hand for whatever spatial support (or lack of it) the target offers.",
        ))
        return "BLOB", issues

    issues.append(ConversionIssue(
        "error", f"No mapping rule for SQL Server type '{raw_type}'. Defaulting to CLOB; manual review required."))
    return "CLOB", issues


_DB2_SIZED_RE = re.compile(r"^([a-z_][a-z_0-9]*)\s*\((\d+)(?:\s*,\s*(\d+))?\)$", re.IGNORECASE)

# Db2's own precision for each fixed-width integer type, reused directly as
# the pivot NUMBER(p)'s precision.
_DB2_INT_WIDTHS = {"smallint": 5, "integer": 10, "int": 10, "bigint": 19}


def from_db2(raw_type: str) -> Tuple[str, List[ConversionIssue]]:
    """Reverse-map a Db2 (LUW) native column type (as reconstructed by
    tgdatabridge.core.db2_introspector._fmt_data_type, e.g. "varchar(255)",
    "decimal(10,2)", "graphic(10)", "timestamp") into this tool's shared
    Oracle-flavored pivot type string -- so the *existing* to_postgres/
    to_mysql/to_sqlserver/to_db2 forward-mappers can convert a
    Db2-sourced column to any target exactly the same way they already
    convert an Oracle-sourced one, with no changes needed on that side at
    all. Returns (pivot_type, issues); a non-empty `issues` list here
    means the conversion is lossy or a total guess and should end up on
    Column.source_issues (not Column.issues) -- see Column.source_issues's
    own docstring. Mirrors from_mysql()/from_postgres()/from_sqlserver()
    above; see those for the general design rationale."""
    issues: List[ConversionIssue] = []
    t = raw_type.strip().lower()

    m = _DB2_SIZED_RE.match(t)
    base = m.group(1).lower() if m else t
    size1 = m.group(2) if m else None
    size2 = m.group(3) if m else None

    if base in _DB2_INT_WIDTHS:
        return f"NUMBER({_DB2_INT_WIDTHS[base]})", issues

    if base in ("decimal", "numeric", "dec"):
        precision = size1 or "31"
        scale = size2 or "0"
        return f"NUMBER({precision},{scale})", issues

    if base == "decfloat":
        issues.append(ConversionIssue(
            "info",
            f"Db2 {raw_type} is an arbitrary-precision decimal *floating-point* type (distinct from "
            "DECIMAL's fixed scale); the pivot has no exact equivalent, so this was mapped to an "
            "unbounded NUMBER -- review any code that depends on DECFLOAT's floating-scale behavior "
            "(e.g. NaN/Infinity handling), which NUMBER does not have.",
        ))
        return "NUMBER", issues

    if base == "real":
        return "BINARY_FLOAT", issues
    if base in ("double", "float", "double precision"):
        return "BINARY_DOUBLE", issues

    if base in ("char", "character"):
        return (f"CHAR({size1})" if size1 else "CHAR(1)"), issues
    if base == "varchar":
        return (f"VARCHAR2({size1})" if size1 else "VARCHAR2(4000)"), issues
    if base in ("clob",):
        return "CLOB", issues

    if base in ("graphic", "vargraphic", "dbclob"):
        issues.append(ConversionIssue(
            "info",
            f"Db2 {raw_type} is a double-byte character (DBCS/graphic) type for storing data in a "
            "fixed graphic character set; the pivot has no separate graphic-type concept, so this was "
            "mapped to a plain character type of the same declared length -- verify the target column "
            "is Unicode-capable if the source data includes non-Latin graphic characters.",
        ))
        if base == "graphic":
            return (f"CHAR({size1})" if size1 else "CHAR(1)"), issues
        return (f"VARCHAR2({size1})" if size1 else "VARCHAR2(4000)"), issues

    if base == "binary":
        return (f"RAW({size1})" if size1 else "RAW(1)"), issues
    if base == "varbinary":
        return (f"RAW({size1})" if size1 else "RAW(4000)"), issues
    if base == "blob":
        return "BLOB", issues

    if base == "date":
        issues.append(ConversionIssue(
            "info",
            "Db2 DATE (date-only, with no time component) was mapped through the shared pivot as "
            "Oracle-style DATE, which always implies a time component -- the target column may end up "
            "able to store a time-of-day this source column never had; harmless unless application "
            "code specifically depends on the target rejecting one.",
        ))
        return "DATE", issues
    if base == "timestamp":
        return "TIMESTAMP", issues
    if base == "time":
        issues.append(ConversionIssue(
            "warning",
            "Db2 TIME (time-of-day with no date component) has no equivalent in the shared pivot; "
            "mapped to VARCHAR2(20) as a text stand-in -- review any time arithmetic on the target, "
            "it will need to be rewritten against whatever real type replaces this.",
        ))
        return "VARCHAR2(20)", issues

    if base == "boolean":
        return "BOOLEAN", issues

    if base == "xml":
        return "XMLTYPE", issues

    if base == "rowid":
        # The pivot already has Oracle's own ROWID as a first-class type --
        # every to_* forward mapper already has a warning for it (a Db2
        # ROWID is a different opaque row-identifier scheme than Oracle's,
        # but both are equally non-portable, so reusing the existing
        # ROWID-has-no-target-equivalent handling is a faithful enough fit
        # rather than inventing a second, parallel warning here).
        return "ROWID", issues

    issues.append(ConversionIssue(
        "error", f"No mapping rule for Db2 type '{raw_type}'. Defaulting to CLOB; manual review required."))
    return "CLOB", issues


_MONGO_STRING_CLOB_THRESHOLD = 4000


def from_mongodb(bson_type: str, max_length: Optional[int] = None) -> Tuple[str, List[ConversionIssue]]:
    """Reverse-map a BSON scalar type name -- as inferred by
    tgdatabridge.core.mongo_source_introspector by *sampling* actual documents
    (MongoDB collections have no fixed schema for a real catalog view to
    report, unlike every other from_<engine>() reverse-mapper above) --
    into this tool's shared Oracle-flavored pivot type string, so the
    *existing* to_postgres/to_mysql/to_sqlserver/to_db2/to_mongodb forward
    mappers can convert a MongoDB-sourced column to any target exactly the
    same way they already convert an Oracle-sourced one, with no changes
    needed on that side at all.

    `bson_type` is one of the type names the introspector's sampler
    produces: "string", "int", "long", "double", "decimal" (Decimal128),
    "bool", "date", "objectId", "binData", or "unknown" (every sampled
    document had this field null/missing, so no real type could be
    observed). `max_length` is the longest observed string length for a
    "string" field (ignored for every other type) -- used to size the
    resulting VARCHAR2, mirroring how from_mysql/from_db2 reuse a source's
    own declared length rather than guessing one.

    Returns (pivot_type, issues); a non-empty `issues` list here means the
    conversion is lossy, a best-effort inference, or a total guess and
    should end up on Column.source_issues (not Column.issues) -- see
    Column.source_issues's own docstring."""
    issues: List[ConversionIssue] = []
    t = (bson_type or "").strip()

    if t == "string":
        if max_length is None or max_length > _MONGO_STRING_CLOB_THRESHOLD:
            issues.append(ConversionIssue(
                "info",
                "String field's longest sampled value exceeds VARCHAR2's practical 4000-char limit "
                "(or no length could be determined from the sample); mapped to CLOB instead -- a "
                "later document longer than anything sampled will still fit.",
            ))
            return "CLOB", issues
        # Padded so a slightly longer value than anything in the sample
        # still fits without truncation -- the sample is a subset of the
        # collection, not a full scan, so the observed max is a floor, not
        # a hard ceiling.
        sized = min(4000, max(1, int(max_length * 1.5) + 1))
        return f"VARCHAR2({sized})", issues

    if t == "int":
        return "NUMBER(9)", issues

    if t == "long":
        return "NUMBER(19)", issues

    if t == "double":
        return "BINARY_DOUBLE", issues

    if t == "decimal":
        issues.append(ConversionIssue(
            "info",
            "BSON Decimal128 has 34 significant decimal digits of precision with no separately "
            "declared scale; mapped to NUMBER(38,10) as a reasonable general-purpose fit -- narrow "
            "or widen the scale by hand if the application relies on a specific number of decimal "
            "places.",
        ))
        return "NUMBER(38,10)", issues

    if t == "bool":
        return "BOOLEAN", issues

    if t == "date":
        return "TIMESTAMP", issues

    if t == "objectId":
        # The pivot already has Oracle's own ROWID as a first-class type --
        # every to_* forward mapper already has a warning for it (a Mongo
        # ObjectId is a different opaque row-identifier scheme than
        # Oracle's, but both are equally non-portable, so reusing the
        # existing ROWID-has-no-target-equivalent handling is a faithful
        # enough fit rather than inventing a second, parallel warning
        # here -- same precedent as from_db2's own "rowid" case above).
        return "ROWID", issues

    if t == "binData":
        return "BLOB", issues

    issues.append(ConversionIssue(
        "warning",
        f"Field's sampled values were always null/missing (or an unrecognized BSON type "
        f"{bson_type!r} was observed), so no real type could be inferred; mapped to VARCHAR2(4000) "
        "as a safe stand-in -- review this field manually once the real data shape is known.",
    ))
    return "VARCHAR2(4000)", issues


_SPREADSHEET_STRING_CLOB_THRESHOLD = 4000


def from_spreadsheet(cell_type: str, max_length: Optional[int] = None) -> Tuple[str, List[ConversionIssue]]:
    """Reverse-map an inferred spreadsheet cell type -- as produced by
    tgdatabridge.core.spreadsheet_types.classify_value/combine_types from
    *sampling* real cells of an .xlsx/.csv file -- into this tool's shared
    Oracle-flavored pivot type string, so the existing to_postgres/
    to_mysql/to_sqlserver/to_db2/to_oracle/to_mongodb forward mappers
    convert a spreadsheet-sourced column to any target exactly the way
    they already convert an Oracle-sourced one, with no changes on that
    side at all. Same design as from_mongodb() above, and for the same
    reason: neither source has a catalog to read real types from.

    `cell_type` is one of spreadsheet_types' constants: "string", "int",
    "float", "decimal", "bool", "datetime", "date", "time", "binary", or
    "null" (the column was empty in every sampled row, so nothing was
    ever observed). `max_length` is the longest observed string length,
    used to size the resulting VARCHAR2 and ignored for every other type.

    Returns (pivot_type, issues); a non-empty `issues` list means the
    inference is a best-effort guess and belongs on Column.source_issues
    (not Column.issues) -- see Column.source_issues's own docstring.
    """
    issues: List[ConversionIssue] = []
    t = (cell_type or "").strip()

    if t == "string":
        if max_length is None or max_length > _SPREADSHEET_STRING_CLOB_THRESHOLD:
            issues.append(ConversionIssue(
                "info",
                "Text column's longest sampled value exceeds VARCHAR2's practical 4000-char limit "
                "(or no length could be determined from the sample); mapped to CLOB instead -- a "
                "later row longer than anything sampled will still fit.",
            ))
            return "CLOB", issues
        # Padded so a row slightly longer than anything sampled still fits.
        # Only a sample of the file was read, so the observed maximum is a
        # floor, not a ceiling -- same reasoning as from_mongodb's sizing.
        sized = min(4000, max(1, int(max_length * 1.5) + 1))
        return f"VARCHAR2({sized})", issues

    if t == "int":
        # Sized from the widest value actually seen (`max_length` is the
        # digit count for an integer column), but snapped to the two
        # precisions that map to a *native* integer type on every target:
        # NUMBER(9) -> INTEGER and NUMBER(18) -> BIGINT. Deliberately
        # coarse in both directions. Narrower buckets (SMALLINT for a
        # column of small numbers) would save nothing meaningful while
        # risking a real overflow, since only a sample of the file was
        # read; and NUMBER(19), the obvious "just fit any 64-bit int"
        # choice, is one digit past what every target can hold natively
        # and silently degrades to DECIMAL(19,0)/NUMERIC(19) everywhere.
        digits = max_length or 18
        if digits > 18:
            issues.append(ConversionIssue(
                "info",
                f"Integer column's widest sampled value has {digits} digits, beyond the 64-bit "
                "integer range every target engine supports natively; mapped to NUMBER(38), which "
                "becomes a DECIMAL/NUMERIC on the target rather than a true integer type.",
            ))
            return "NUMBER(38)", issues
        return "NUMBER(9)" if digits <= 9 else "NUMBER(18)", issues

    if t == "float":
        return "BINARY_DOUBLE", issues

    if t == "decimal":
        issues.append(ConversionIssue(
            "info",
            "Decimal column has no declared precision or scale in a spreadsheet; mapped to "
            "NUMBER(38,10) as a general-purpose fit -- narrow or widen the scale by hand if the "
            "application relies on a specific number of decimal places.",
        ))
        return "NUMBER(38,10)", issues

    if t == "bool":
        return "BOOLEAN", issues

    if t == "datetime":
        return "TIMESTAMP", issues

    if t == "date":
        return "DATE", issues

    if t == "time":
        issues.append(ConversionIssue(
            "info",
            "Time-of-day column has no standalone equivalent in this tool's Oracle-flavored pivot "
            "types; mapped to VARCHAR2(8) holding an ISO 'HH:MM:SS' string. Targets with a real "
            "TIME type (PostgreSQL, MySQL, SQL Server, Db2) can hold this natively -- change the "
            "generated column type by hand if you want that instead of text.",
        ))
        return "VARCHAR2(8)", issues

    if t == "binary":
        return "BLOB", issues

    issues.append(ConversionIssue(
        "warning",
        f"Column was empty in every sampled row (or an unrecognized cell type {cell_type!r} was "
        "observed), so no real type could be inferred; mapped to VARCHAR2(4000) as a safe "
        "stand-in -- review this column manually once the real data shape is known.",
    ))
    return "VARCHAR2(4000)", issues


def map_type(raw_type: str, target_engine: str) -> Tuple[str, List[ConversionIssue]]:
    if target_engine.lower().startswith("oracle"):
        return to_oracle(raw_type)
    if target_engine.lower().startswith("postgres"):
        return to_postgres(raw_type)
    if target_engine.lower().startswith("mysql"):
        return to_mysql(raw_type)
    if target_engine.lower().replace(" ", "").startswith("sqlserver"):
        return to_sqlserver(raw_type)
    if target_engine.lower().startswith("db2"):
        return to_db2(raw_type)
    if target_engine.lower().startswith("mongo"):
        return to_mongodb(raw_type)
    raise ValueError(f"Unsupported target engine: {target_engine}")
