"""
Transact-SQL (Microsoft SQL Server) -> target-dialect *expression* and
*query* translation.

This module is the shared, dialect-level half of SQL Server source
support. It knows nothing about procedure/function/trigger structure --
that lives in tsql_routine_converter.py, which calls into here for every
expression, predicate and SELECT it rewrites. It is also used directly by
ddl_generator for two things that are not routines at all:

  * column DEFAULT expressions (``(getdate())`` -> ``CURRENT_TIMESTAMP``),
    which previously reached the target verbatim and made every
    ``CREATE TABLE`` carrying one fail outright -- MySQL 3770
    "Default value expression of column 'CreatedDate' contains a
    disallowed function: getdate", PostgreSQL "function getdate() does
    not exist".
  * view bodies, whose ``[dbo].[Employees]``/``ISNULL``/``TOP n`` text was
    likewise copied through unchanged and could only ever fail on a
    MySQL/PostgreSQL target.

Design rules, in order of importance:

1. **Never guess silently.** Anything this module cannot translate with
   confidence is left as-is *and* reported as a ConversionIssue with
   severity "error", so the object is flagged rather than shipped broken.
   Partial-but-honest beats complete-but-wrong.
2. **Never touch string literals.** Every transform runs through
   `_apply_outside_strings`, so a `'GETDATE()'` inside a literal, or an
   apostrophe in a comment, is never rewritten or used to desync the
   tokenizer.
3. **Be idempotent.** Running a transform twice must not change the
   result; the DDL generator regenerates freely when the user switches
   target engine.
"""
from __future__ import annotations

import re
from typing import Callable, List, Optional, Tuple

from tgdatabridge.core.schema_model import ConversionIssue

# --------------------------------------------------------------- targets

MYSQL = "mysql"
POSTGRES = "postgres"


def normalize_target(target_engine: str) -> str:
    """Collapse a UI/engine label ("PostgreSQL", "MySQL", "SQL Server") to
    the short key used throughout this module. Anything that is not
    PostgreSQL is treated as MySQL-flavored only by explicit callers; this
    returns the raw lowercase key otherwise so callers can branch."""
    key = (target_engine or "").lower().replace(" ", "")
    if key.startswith("postgres"):
        return POSTGRES
    if key.startswith("mysql") or key.startswith("mariadb"):
        return MYSQL
    return key


# ------------------------------------------------------- literal-safe map

_STRING_LITERAL_RE = re.compile(r"N?'(?:[^']|'')*'", re.IGNORECASE)
_LINE_COMMENT_RE = re.compile(r"--[^\n]*")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
# One combined scanner so a transform skips literals AND comments in a
# single pass -- an apostrophe inside "-- don't do this" must not open a
# literal, and "GETDATE()" inside a comment must not be rewritten.
_INERT_RE = re.compile(
    r"(N?'(?:[^']|'')*')|(--[^\n]*)|(/\*.*?\*/)", re.IGNORECASE | re.DOTALL
)


def apply_outside_strings(text: str, func: Callable[[str], str]) -> str:
    """Apply `func` only to the parts of `text` that are neither inside a
    single-quoted literal nor inside a `--`/`/* */` comment."""
    out: List[str] = []
    pos = 0
    for m in _INERT_RE.finditer(text):
        out.append(func(text[pos:m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(func(text[pos:]))
    return "".join(out)


def scan_outside_strings(text: str) -> str:
    """Return `text` with every literal/comment blanked out to spaces, for
    *detection* passes that must not match inside them (the length and all
    other offsets are preserved, so an index into the result is a valid
    index into the original)."""
    def blank(m: re.Match) -> str:
        return " " * len(m.group(0))
    return _INERT_RE.sub(blank, text)


# ------------------------------------------------------ identifier quoting


_BRACKET_IDENT_RE = re.compile(r"\[([^\[\]]*)\]")


def _quote_mysql(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


def _quote_pg(name: str) -> str:
    # Lowercased for the same reason ddl_generator._quote_pg lowercases:
    # every object this tool creates on a Postgres target is created with a
    # lowercased quoted name, so references must match that or resolve to
    # nothing.
    return '"' + name.lower().replace('"', '""') + '"'


def quote_identifier(name: str, target: str) -> str:
    return _quote_pg(name) if target == POSTGRES else _quote_mysql(name)


def strip_schema_prefix(text: str, schema_names=("dbo",)) -> str:
    """Drop a leading `dbo.` / `[dbo].` qualifier from object references.

    Neither MySQL (where "schema" *is* the database) nor a PostgreSQL
    target created by this tool (everything lands in one search_path
    schema) has a `dbo` to resolve, so leaving the qualifier in place
    guarantees "table dbo.Employees doesn't exist" on the first statement
    that uses it."""
    def repl(match: re.Match) -> str:
        return ""
    pattern = re.compile(
        r"(?<![\w.])(?:\[(?:%s)\]|\b(?:%s)\b)\s*\.\s*"
        % ("|".join(re.escape(s) for s in schema_names),
           "|".join(re.escape(s) for s in schema_names)),
        re.IGNORECASE,
    )
    return apply_outside_strings(text, lambda chunk: pattern.sub(repl, chunk))


_CREATE_VIEW_RE = re.compile(
    r"^\s*CREATE\s+(?:OR\s+(?:ALTER|REPLACE)\s+)?VIEW\b", re.IGNORECASE)


def strip_create_view_header(definition: str) -> str:
    """Reduce a view definition to just its SELECT.

    `schema_model.View.definition` is documented as the view's *SELECT*
    text, and Oracle, MySQL and PostgreSQL all hand back exactly that.
    SQL Server does not: `information_schema.views.view_definition`
    returns the **entire original statement**, header and all --

        CREATE VIEW vw_EmployeeDetails AS SELECT e.EmployeeID, ...

    -- so wrapping it the way every generator does produces

        CREATE OR REPLACE VIEW `vw_EmployeeDetails` AS
        CREATE VIEW vw_EmployeeDetails AS SELECT ...

    which is error 1064, a syntax error at the second CREATE. Normalising
    here keeps that engine-specific quirk from leaking into the
    generators, and is a no-op for a definition that is already a bare
    SELECT.

    The closing `AS` is found by scanning at parenthesis depth zero with
    literals and comments blanked, so a column list -- `CREATE VIEW v (a,
    b) AS SELECT ...` -- or the word "as" inside a string cannot end the
    header early.
    """
    text = definition or ""
    m = _CREATE_VIEW_RE.match(text)
    if not m:
        return text

    scan = scan_outside_strings(text)
    depth = 0
    i = m.end()
    n = len(scan)
    while i < n:
        ch = scan[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif depth == 0 and (ch in "aA") and scan[i:i + 2].upper() == "AS":
            before = scan[i - 1] if i else " "
            after = scan[i + 2] if i + 2 < n else " "
            if not (before.isalnum() or before in "_$#") and \
               not (after.isalnum() or after in "_$#"):
                return text[i + 2:].strip()
        i += 1
    return text


def convert_bracket_identifiers(text: str, target: str) -> str:
    """`[Employee Name]` -> `` `Employee Name` `` / `"employee name"`.

    A bracketed identifier is unambiguous in T-SQL, so this is one of the
    few rewrites that is always safe."""
    def repl(match: re.Match) -> str:
        return quote_identifier(match.group(1), target)
    return apply_outside_strings(text, lambda chunk: _BRACKET_IDENT_RE.sub(repl, chunk))


# ------------------------------------------------------------ type names

# T-SQL type -> (mysql, postgres). Only the ones that actually differ need
# an entry; anything not listed passes through unchanged, which is correct
# for INT/BIGINT/SMALLINT/DECIMAL/NUMERIC/FLOAT/REAL/DATE/TIME.
_TYPE_MAP = {
    "NVARCHAR": ("VARCHAR", "VARCHAR"),
    "NCHAR": ("CHAR", "CHAR"),
    "NTEXT": ("LONGTEXT", "TEXT"),
    "TEXT": ("LONGTEXT", "TEXT"),
    "VARBINARY": ("VARBINARY", "BYTEA"),
    "BINARY": ("BINARY", "BYTEA"),
    "IMAGE": ("LONGBLOB", "BYTEA"),
    "DATETIME": ("DATETIME", "TIMESTAMP"),
    "DATETIME2": ("DATETIME", "TIMESTAMP"),
    "SMALLDATETIME": ("DATETIME", "TIMESTAMP"),
    "DATETIMEOFFSET": ("DATETIME", "TIMESTAMPTZ"),
    "MONEY": ("DECIMAL(19,4)", "NUMERIC(19,4)"),
    "SMALLMONEY": ("DECIMAL(10,4)", "NUMERIC(10,4)"),
    "BIT": ("TINYINT(1)", "BOOLEAN"),
    "UNIQUEIDENTIFIER": ("CHAR(36)", "UUID"),
    "TINYINT": ("TINYINT UNSIGNED", "SMALLINT"),
    "XML": ("LONGTEXT", "XML"),
    "SQL_VARIANT": ("LONGTEXT", "TEXT"),
    "ROWVERSION": ("BIGINT", "BIGINT"),
    "TIMESTAMP": ("BIGINT", "BIGINT"),  # T-SQL TIMESTAMP is a row version, not a time
}

_TYPE_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*(\(\s*[^()]*\s*\))?")


def convert_type_name(type_text: str, target: str) -> Tuple[str, List[ConversionIssue]]:
    """Translate one T-SQL type reference (as it appears in a DECLARE, a
    parameter list, a CAST or a CONVERT) into the target's spelling."""
    issues: List[ConversionIssue] = []
    text = (type_text or "").strip()
    if not text:
        return text, issues
    m = _TYPE_RE.match(text)
    if not m:
        return text, issues
    base = m.group(1).upper()
    args = (m.group(2) or "").strip()
    tail = text[m.end():].strip()

    mapped = _TYPE_MAP.get(base)
    if mapped is None:
        return text, issues

    replacement = mapped[1] if target == POSTGRES else mapped[0]
    # A mapping that already carries its own precision (MONEY -> DECIMAL(19,4))
    # must not then have the source's own "(...)" appended after it.
    if "(" in replacement:
        args = ""
    if args:
        inner = args[1:-1].strip()
        if inner.upper() == "MAX":
            # NVARCHAR(MAX)/VARBINARY(MAX) have no length equivalent.
            if target == POSTGRES:
                replacement = "TEXT" if base in ("NVARCHAR", "VARCHAR", "NCHAR", "CHAR") else "BYTEA"
            else:
                replacement = "LONGTEXT" if base in ("NVARCHAR", "VARCHAR", "NCHAR", "CHAR") else "LONGBLOB"
            args = ""
        elif base in ("NVARCHAR", "NCHAR") and inner.isdigit() and target == POSTGRES:
            pass  # length in characters on both sides
    if base == "BIT":
        issues.append(ConversionIssue(
            "info",
            "T-SQL BIT was mapped to %s; comparisons against 0/1 keep working."
            % ("BOOLEAN" if target == POSTGRES else "TINYINT(1)"),
        ))
    out = replacement + (args if args else "")
    return (out + (" " + tail if tail else "")), issues


# ------------------------------------------------------------- functions

def _fn(pattern: str) -> re.Pattern:
    return re.compile(pattern, re.IGNORECASE)


# Zero-argument / constant-like rewrites, applied as whole-word matches.
_SCALAR_CONSTANTS = {
    MYSQL: [
        (_fn(r"\bGETUTCDATE\s*\(\s*\)"), "UTC_TIMESTAMP()"),
        (_fn(r"\bSYSUTCDATETIME\s*\(\s*\)"), "UTC_TIMESTAMP(6)"),
        (_fn(r"\bGETDATE\s*\(\s*\)"), "CURRENT_TIMESTAMP"),
        (_fn(r"\bSYSDATETIME\s*\(\s*\)"), "CURRENT_TIMESTAMP(6)"),
        (_fn(r"\bSYSDATETIMEOFFSET\s*\(\s*\)"), "CURRENT_TIMESTAMP(6)"),
        (_fn(r"\bNEWID\s*\(\s*\)"), "UUID()"),
        (_fn(r"\bNEWSEQUENTIALID\s*\(\s*\)"), "UUID()"),
        (_fn(r"\bSUSER_SNAME\s*\(\s*\)"), "CURRENT_USER()"),
        (_fn(r"\bSUSER_NAME\s*\(\s*\)"), "CURRENT_USER()"),
        (_fn(r"\bUSER_NAME\s*\(\s*\)"), "CURRENT_USER()"),
        (_fn(r"\bORIGINAL_LOGIN\s*\(\s*\)"), "CURRENT_USER()"),
        (_fn(r"\bSYSTEM_USER\b(?!\s*\()"), "CURRENT_USER()"),
        (_fn(r"\bSESSION_USER\b(?!\s*\()"), "CURRENT_USER()"),
        (_fn(r"\bHOST_NAME\s*\(\s*\)"), "@@hostname"),
        (_fn(r"\bDB_NAME\s*\(\s*\)"), "DATABASE()"),
        (_fn(r"\bSCOPE_IDENTITY\s*\(\s*\)"), "LAST_INSERT_ID()"),
        (_fn(r"@@IDENTITY\b"), "LAST_INSERT_ID()"),
        (_fn(r"@@ROWCOUNT\b"), "ROW_COUNT()"),
        (_fn(r"@@VERSION\b"), "VERSION()"),
        (_fn(r"\bPI\s*\(\s*\)"), "PI()"),
    ],
    POSTGRES: [
        (_fn(r"\bGETUTCDATE\s*\(\s*\)"), "(NOW() AT TIME ZONE 'UTC')"),
        (_fn(r"\bSYSUTCDATETIME\s*\(\s*\)"), "(NOW() AT TIME ZONE 'UTC')"),
        (_fn(r"\bGETDATE\s*\(\s*\)"), "CURRENT_TIMESTAMP"),
        (_fn(r"\bSYSDATETIME\s*\(\s*\)"), "CURRENT_TIMESTAMP"),
        (_fn(r"\bSYSDATETIMEOFFSET\s*\(\s*\)"), "CURRENT_TIMESTAMP"),
        (_fn(r"\bNEWID\s*\(\s*\)"), "gen_random_uuid()"),
        (_fn(r"\bNEWSEQUENTIALID\s*\(\s*\)"), "gen_random_uuid()"),
        (_fn(r"\bSUSER_SNAME\s*\(\s*\)"), "CURRENT_USER"),
        (_fn(r"\bSUSER_NAME\s*\(\s*\)"), "CURRENT_USER"),
        (_fn(r"\bUSER_NAME\s*\(\s*\)"), "CURRENT_USER"),
        (_fn(r"\bORIGINAL_LOGIN\s*\(\s*\)"), "SESSION_USER"),
        (_fn(r"\bSYSTEM_USER\b(?!\s*\()"), "CURRENT_USER"),
        (_fn(r"\bSESSION_USER\b(?!\s*\()"), "SESSION_USER"),
        (_fn(r"\bHOST_NAME\s*\(\s*\)"), "inet_client_addr()"),
        (_fn(r"\bDB_NAME\s*\(\s*\)"), "CURRENT_DATABASE()"),
        (_fn(r"\bSCOPE_IDENTITY\s*\(\s*\)"), "LASTVAL()"),
        (_fn(r"@@IDENTITY\b"), "LASTVAL()"),
        (_fn(r"@@VERSION\b"), "VERSION()"),
    ],
}

# Simple name-for-name function renames (argument order unchanged).
_FUNCTION_RENAMES = {
    MYSQL: {
        "ISNULL": "IFNULL",
        "LEN": "CHAR_LENGTH",
        "DATALENGTH": "LENGTH",
        "CEILING": "CEILING",
        "SQUARE": "POW",          # handled below (needs a 2nd arg) -- see _rewrite_square
        "GETANSINULL": None,
    },
    POSTGRES: {
        "ISNULL": "COALESCE",
        "LEN": "LENGTH",
        "DATALENGTH": "OCTET_LENGTH",
        "CEILING": "CEIL",
        "CHARINDEX": None,        # argument order differs -- handled separately
    },
}

# Functions with no safe automatic equivalent. Presence of any of these
# leaves the object flagged rather than mistranslated.
_UNSUPPORTED_FUNCTIONS = {
    "OPENQUERY": "OPENQUERY (distributed query) has no target equivalent.",
    "OPENROWSET": "OPENROWSET (ad-hoc remote data) has no target equivalent.",
    "SP_EXECUTESQL": "sp_executesql dynamic SQL must be rewritten by hand.",
    "XP_CMDSHELL": "xp_cmdshell has no target equivalent and must be removed.",
    "CHECKSUM": "CHECKSUM() has no equivalent producing the same values.",
    "BINARY_CHECKSUM": "BINARY_CHECKSUM() has no equivalent producing the same values.",
    "PIVOT": "PIVOT must be rewritten as conditional aggregation.",
    "UNPIVOT": "UNPIVOT must be rewritten as a UNION ALL / LATERAL expansion.",
    "FOR XML": "FOR XML must be rewritten by hand.",
    "FOR JSON": "FOR JSON must be rewritten using the target's JSON functions.",
}

_DATE_PARTS = {
    "YEAR": "YEAR", "YY": "YEAR", "YYYY": "YEAR",
    "QUARTER": "QUARTER", "QQ": "QUARTER", "Q": "QUARTER",
    "MONTH": "MONTH", "MM": "MONTH", "M": "MONTH",
    "DAYOFYEAR": "DAYOFYEAR", "DY": "DAYOFYEAR", "Y": "DAYOFYEAR",
    "DAY": "DAY", "DD": "DAY", "D": "DAY",
    "WEEK": "WEEK", "WK": "WEEK", "WW": "WEEK",
    "WEEKDAY": "DAYOFWEEK", "DW": "DAYOFWEEK",
    "HOUR": "HOUR", "HH": "HOUR",
    "MINUTE": "MINUTE", "MI": "MINUTE", "N": "MINUTE",
    "SECOND": "SECOND", "SS": "SECOND", "S": "SECOND",
    "MILLISECOND": "MICROSECOND", "MS": "MICROSECOND",
    "MICROSECOND": "MICROSECOND", "MCS": "MICROSECOND",
}


def split_call_arguments(arg_text: str) -> List[str]:
    """Split a function-call argument list on top-level commas only --
    nested calls, parenthesised expressions and string literals containing
    commas all stay intact."""
    args: List[str] = []
    depth = 0
    current: List[str] = []
    i = 0
    n = len(arg_text)
    while i < n:
        ch = arg_text[i]
        if ch == "'":
            m = _STRING_LITERAL_RE.match(arg_text, max(0, i - 1) if arg_text[i - 1:i].upper() == "N" else i)
            m = _STRING_LITERAL_RE.match(arg_text, i)
            if m:
                current.append(m.group(0))
                i = m.end()
                continue
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        if ch == "," and depth == 0:
            args.append("".join(current).strip())
            current = []
            i += 1
            continue
        current.append(ch)
        i += 1
    tail = "".join(current).strip()
    if tail or args:
        args.append(tail)
    return args


def _find_call(text: str, name: str, start: int = 0) -> Optional[Tuple[int, int, str]]:
    """Locate the next `name(...)` call at or after `start`, outside string
    literals/comments. Returns (call_start, call_end, inner_argument_text)."""
    scan = scan_outside_strings(text)
    pattern = re.compile(r"\b" + re.escape(name) + r"\s*\(", re.IGNORECASE)
    m = pattern.search(scan, start)
    if not m:
        return None
    depth = 0
    i = m.end() - 1
    n = len(text)
    while i < n:
        ch = scan[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return m.start(), i + 1, text[m.end():i]
        i += 1
    return None


def _rewrite_calls(text: str, name: str, builder: Callable[[List[str]], Optional[str]]) -> str:
    """Replace every `name(args)` with `builder(args)`. `builder` returning
    None leaves that particular call untouched (used when the argument
    shape is one this module refuses to guess at)."""
    pos = 0
    out = text
    while True:
        found = _find_call(out, name, pos)
        if not found:
            return out
        start, end, inner = found
        args = split_call_arguments(inner)
        replacement = builder(args)
        if replacement is None:
            pos = end
            continue
        out = out[:start] + replacement + out[end:]
        pos = start + len(replacement)


def _rewrite_datefns(text: str, target: str, issues: List[ConversionIssue]) -> str:
    def part_of(arg: str) -> Optional[str]:
        return _DATE_PARTS.get(arg.strip().strip("[]").upper())

    def dateadd(args: List[str]) -> Optional[str]:
        if len(args) != 3:
            return None
        part = part_of(args[0])
        if part is None:
            issues.append(ConversionIssue(
                "error", f"DATEADD with unrecognised date part '{args[0].strip()}' was not converted."))
            return None
        if target == POSTGRES:
            unit = part.lower()
            if unit == "dayofweek" or unit == "dayofyear":
                unit = "day"
            return f"({args[2]} + ({args[1]}) * INTERVAL '1 {unit}')"
        return f"DATE_ADD({args[2]}, INTERVAL ({args[1]}) {part})"

    def datediff(args: List[str]) -> Optional[str]:
        if len(args) != 3:
            return None
        part = part_of(args[0])
        if part is None:
            issues.append(ConversionIssue(
                "error", f"DATEDIFF with unrecognised date part '{args[0].strip()}' was not converted."))
            return None
        if target == POSTGRES:
            issues.append(ConversionIssue(
                "warning",
                "DATEDIFF was converted using EXTRACT(EPOCH ...) arithmetic; PostgreSQL counts whole "
                "elapsed units where SQL Server counts boundary crossings, so results can differ by one "
                "for values either side of a boundary. Verify before relying on it.",
            ))
            secs = {"YEAR": 31536000, "QUARTER": 7776000, "MONTH": 2592000, "WEEK": 604800,
                    "DAY": 86400, "HOUR": 3600, "MINUTE": 60, "SECOND": 1}.get(part)
            if secs is None:
                return None
            return f"(EXTRACT(EPOCH FROM (({args[2]}) - ({args[1]}))) / {secs})::bigint"
        return f"TIMESTAMPDIFF({part}, {args[1]}, {args[2]})"

    def datepart(args: List[str]) -> Optional[str]:
        if len(args) != 2:
            return None
        part = part_of(args[0])
        if part is None:
            issues.append(ConversionIssue(
                "error", f"DATEPART with unrecognised date part '{args[0].strip()}' was not converted."))
            return None
        if target == POSTGRES:
            pg_part = {"DAYOFWEEK": "DOW", "DAYOFYEAR": "DOY", "MICROSECOND": "MICROSECONDS"}.get(part, part)
            return f"EXTRACT({pg_part} FROM {args[1]})"
        return f"EXTRACT({part} FROM {args[1]})"

    text = _rewrite_calls(text, "DATEADD", dateadd)
    text = _rewrite_calls(text, "DATEDIFF", datediff)
    text = _rewrite_calls(text, "DATEPART", datepart)
    return text


def _rewrite_convert_cast(text: str, target: str, issues: List[ConversionIssue]) -> str:
    def convert(args: List[str]) -> Optional[str]:
        if len(args) < 2:
            return None
        if len(args) > 2:
            issues.append(ConversionIssue(
                "warning",
                f"CONVERT(...) style code {args[2].strip()} was dropped -- the target has no "
                "equivalent style-numbered date formatting; verify the resulting format.",
            ))
        type_text, type_issues = convert_type_name(args[0], target)
        issues.extend(type_issues)
        return f"CAST({args[1]} AS {type_text})"

    def cast(args: List[str]) -> Optional[str]:
        # CAST's argument is "expr AS type" -- a single "argument" as far as
        # comma-splitting is concerned.
        if len(args) != 1:
            return None
        m = re.search(r"\bAS\b", args[0], re.IGNORECASE)
        if not m:
            return None
        expr = args[0][:m.start()].strip()
        type_text, type_issues = convert_type_name(args[0][m.end():], target)
        issues.extend(type_issues)
        return f"CAST({expr} AS {type_text})"

    text = _rewrite_calls(text, "CONVERT", convert)
    text = _rewrite_calls(text, "CAST", cast)
    return text


def _rewrite_misc_calls(text: str, target: str, issues: List[ConversionIssue]) -> str:
    def iif(args: List[str]) -> Optional[str]:
        if len(args) != 3:
            return None
        if target == POSTGRES:
            return f"(CASE WHEN {args[0]} THEN {args[1]} ELSE {args[2]} END)"
        return f"IF({args[0]}, {args[1]}, {args[2]})"

    def charindex(args: List[str]) -> Optional[str]:
        if target != POSTGRES:
            return None  # MySQL LOCATE has the same (needle, haystack) order
        if len(args) == 2:
            return f"STRPOS({args[1]}, {args[0]})"
        return None  # 3-arg form (start position) has no one-liner equivalent

    def square(args: List[str]) -> Optional[str]:
        if len(args) != 1:
            return None
        return f"POWER({args[0]}, 2)"

    def stuff(args: List[str]) -> Optional[str]:
        if len(args) != 4:
            return None
        s, start, length, repl = args
        if target == POSTGRES:
            return f"OVERLAY({s} PLACING {repl} FROM {start} FOR {length})"
        return f"INSERT({s}, {start}, {length}, {repl})"

    def choose(_args: List[str]) -> Optional[str]:
        issues.append(ConversionIssue(
            "error", "CHOOSE() has no direct target equivalent; rewrite as a CASE expression."))
        return None

    text = _rewrite_calls(text, "IIF", iif)
    text = _rewrite_calls(text, "CHARINDEX", charindex)
    text = _rewrite_calls(text, "SQUARE", square)
    text = _rewrite_calls(text, "STUFF", stuff)
    text = _rewrite_calls(text, "CHOOSE", choose)
    if target == MYSQL:
        text = _rewrite_calls(text, "CHARINDEX", lambda a: f"LOCATE({', '.join(a)})" if len(a) in (2, 3) else None)
    return text


_TOP_RE = re.compile(r"\bTOP\s*\(\s*(\d+)\s*\)|\bTOP\s+(\d+)\b", re.IGNORECASE)


def _rewrite_top(text: str, issues: List[ConversionIssue]) -> str:
    """`SELECT TOP 10 ...` -> `SELECT ... LIMIT 10`.

    Only the simple constant form is handled, and only when the statement
    has no TOP inside a subquery (which would need the LIMIT attached to
    that subquery rather than the outer one). Anything else is reported."""
    scan = scan_outside_strings(text)
    matches = list(_TOP_RE.finditer(scan))
    if not matches:
        return text
    if len(matches) > 1:
        issues.append(ConversionIssue(
            "error",
            "More than one TOP n clause was found; each needs its own LIMIT on the right subquery, "
            "so none were rewritten automatically.",
        ))
        return text
    m = matches[0]
    if re.search(r"\bPERCENT\b|\bWITH\s+TIES\b", scan[m.end():m.end() + 40], re.IGNORECASE):
        issues.append(ConversionIssue(
            "error", "TOP ... PERCENT / WITH TIES has no direct LIMIT equivalent and was left as-is."))
        return text
    n = m.group(1) or m.group(2)
    # Collapse the double space the removed TOP would otherwise leave
    # behind ("SELECT  col"), which is harmless to the server but makes
    # the generated DDL look sloppy in the review pane.
    body = (text[:m.start()].rstrip() + " " + text[m.end():].lstrip()).rstrip()
    trailing = ""
    while body.endswith(";"):
        body = body[:-1].rstrip()
        trailing = ";"
    return f"{body}\nLIMIT {n}{trailing}"


# Tokens that can never be part of a `+` expression, and therefore bound
# one. Used to find the maximal expression a `+` belongs to without
# writing a full T-SQL expression parser.
_CONCAT_STOP_WORDS = (
    "SELECT", "FROM", "WHERE", "AND", "OR", "NOT", "AS", "ON", "JOIN", "INNER", "LEFT",
    "RIGHT", "FULL", "OUTER", "CROSS", "APPLY", "WHEN", "THEN", "ELSE", "END", "CASE",
    "ORDER", "GROUP", "BY", "HAVING", "SET", "VALUES", "INTO", "LIMIT", "OFFSET", "UNION",
    "EXCEPT", "INTERSECT", "IS", "LIKE", "IN", "BETWEEN", "DESC", "ASC", "DISTINCT", "TOP",
    "EXISTS", "ALL", "ANY", "SOME", "OVER", "PARTITION", "RETURN", "IF", "WHILE", "BEGIN",
    "DECLARE", "INSERT", "UPDATE", "DELETE", "EXEC", "EXECUTE", "PRINT", "RAISERROR", "THROW",
)
_CONCAT_STOP_CHARS = ",()=<>!;"
_STOP_WORD_RE = re.compile(
    r"\b(?:" + "|".join(_CONCAT_STOP_WORDS) + r")\b", re.IGNORECASE)


def _concat_regions(text: str, scan: str):
    """Yield `(start, end)` spans of `text`, each a maximal expression
    containing at least one top-level `+`.

    T-SQL overloads `+` for both numeric addition and string
    concatenation, and MySQL has no infix concatenation operator at all
    (`||` there means logical OR unless PIPES_AS_CONCAT is set, so
    emitting it would produce SQL that parses and silently returns 0/1).
    Rewriting requires knowing the whole expression, not just the operator
    -- hence regions rather than a token-level substitution."""
    stops = set()
    depth_at = []
    depth = 0
    for i, ch in enumerate(scan):
        if ch == "(":
            depth += 1
        depth_at.append(depth)
        if ch == ")":
            depth -= 1
            depth_at[i] = depth
        if ch in _CONCAT_STOP_CHARS:
            stops.add(i)
    for m in _STOP_WORD_RE.finditer(scan):
        for i in range(m.start(), m.end()):
            stops.add(i)

    i = 0
    n = len(scan)
    while i < n:
        if scan[i] == "+" and i not in stops:
            base_depth = depth_at[i]
            left = i
            while left > 0:
                j = left - 1
                if j in stops and depth_at[j] <= base_depth:
                    break
                if depth_at[j] < base_depth:
                    break
                left = j
            right = i
            while right < n - 1:
                j = right + 1
                if j in stops and depth_at[j] <= base_depth:
                    break
                if depth_at[j] < base_depth:
                    break
                right = j
            yield left, right + 1
            i = right + 1
            continue
        i += 1


def _split_top_level_plus(region: str, scan_region: str):
    parts = []
    depth = 0
    start = 0
    for i, ch in enumerate(scan_region):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "+" and depth == 0:
            parts.append(region[start:i])
            start = i + 1
    parts.append(region[start:])
    return [p.strip() for p in parts]


def _rewrite_string_concat(text: str, target: str, issues: List[ConversionIssue]) -> str:
    """Rewrite T-SQL string concatenation.

    Only a `+` chain with at least one *string* operand is touched -- a
    string literal, or a call to a function that can only return text.
    That restriction is what makes the rewrite safe: `qty + 1` is left
    alone because nothing in it proves the `+` is not arithmetic, and
    getting that wrong would silently change results rather than fail
    loudly."""
    scan = scan_outside_strings(text)
    if "+" not in scan:
        return text

    string_fn = re.compile(
        r"^\s*(?:CONCAT|LEFT|RIGHT|SUBSTRING|SUBSTR|UPPER|LOWER|LTRIM|RTRIM|TRIM|REPLACE|"
        r"FORMAT|STR|CHAR|SPACE|REVERSE|STUFF|COALESCE|IFNULL|CAST|CONVERT)\s*\(",
        re.IGNORECASE)

    replacements = []
    for start, end in _concat_regions(text, scan):
        # A region is bounded by the *stop token*, so it carries the
        # whitespace on either side. Trim it back to the expression itself
        # or the rewrite swallows the space after "SELECT".
        while start < end and text[start].isspace():
            start += 1
        while end > start and text[end - 1].isspace():
            end -= 1
        region = text[start:end]
        parts = _split_top_level_plus(region, scan[start:end])
        if len(parts) < 2 or any(not p for p in parts):
            continue
        has_string = any(
            _STRING_LITERAL_RE.match(p) or string_fn.match(p) for p in parts)
        if not has_string:
            continue
        if target == POSTGRES:
            replacements.append((start, end, " || ".join(parts)))
        else:
            replacements.append((start, end, "CONCAT(" + ", ".join(parts) + ")"))

    if not replacements:
        return text
    out = []
    pos = 0
    for start, end, replacement in replacements:
        if start < pos:
            continue
        out.append(text[pos:start])
        out.append(replacement)
        pos = end
    out.append(text[pos:])
    return "".join(out)


def _flag_unsupported(text: str, issues: List[ConversionIssue]) -> None:
    scan = scan_outside_strings(text).upper()
    for token, message in _UNSUPPORTED_FUNCTIONS.items():
        if re.search(r"\b" + re.escape(token) + r"\b", scan):
            issues.append(ConversionIssue("error", message))


def translate_expression(
    text: str, target_engine: str, *, rewrite_top: bool = True, quote_identifiers: bool = True,
) -> Tuple[str, List[ConversionIssue]]:
    """Translate a T-SQL scalar expression, predicate or SELECT body into
    `target_engine`'s dialect.

    Returns `(translated_text, issues)`. Issues with severity "error" mean
    something in `text` could not be translated and the caller must not
    treat the result as ready to run."""
    target = normalize_target(target_engine)
    issues: List[ConversionIssue] = []
    if not text:
        return text, issues
    if target not in (MYSQL, POSTGRES):
        return text, issues

    out = text
    _flag_unsupported(out, issues)

    # Order matters: identifier/schema cleanup first (so later function
    # matching isn't confused by "[dbo].[LEN]"), then structural rewrites
    # that consume argument lists, then the flat token substitutions.
    out = strip_schema_prefix(out)
    if quote_identifiers:
        out = convert_bracket_identifiers(out, target)
    out = _rewrite_convert_cast(out, target, issues)
    out = _rewrite_datefns(out, target, issues)
    out = _rewrite_misc_calls(out, target, issues)

    renames = _FUNCTION_RENAMES.get(target, {})
    for src, dst in renames.items():
        if dst is None:
            continue
        pattern = re.compile(r"\b" + re.escape(src) + r"\s*\(", re.IGNORECASE)
        out = apply_outside_strings(out, lambda chunk, p=pattern, d=dst: p.sub(d + "(", chunk))

    for pattern, replacement in _SCALAR_CONSTANTS.get(target, []):
        out = apply_outside_strings(out, lambda chunk, p=pattern, r=replacement: p.sub(r, chunk))

    # N'...' unicode literals: both targets are UTF-8 natively.
    out = re.sub(r"\bN('(?:[^']|'')*')", r"\1", out)

    out = _rewrite_string_concat(out, target, issues)
    if rewrite_top:
        out = _rewrite_top(out, issues)

    # T-SQL's != is valid in both targets; square-bracket leftovers are not.
    if quote_identifiers and "[" in scan_outside_strings(out):
        issues.append(ConversionIssue(
            "warning", "Some [bracketed] identifiers could not be converted and were left as-is."))
    return out, issues


# ------------------------------------------------------- DEFAULT clauses

# Column DEFAULTs SQL Server hands back through sys.default_constraints are
# always wrapped in at least one layer of parentheses ("(getdate())",
# "((0))", "(N'unknown')"). Those layers are noise on every other engine
# and, for MySQL, actively harmful: MySQL only accepts a *parenthesised*
# default for a real expression, and rejects "DEFAULT ((0))" style
# double-wrapping on some types.
def _strip_redundant_parens(text: str) -> str:
    out = text.strip()
    while len(out) >= 2 and out[0] == "(" and out[-1] == ")":
        depth = 0
        balanced = True
        for idx, ch in enumerate(out):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0 and idx != len(out) - 1:
                    balanced = False
                    break
        if not balanced:
            break
        out = out[1:-1].strip()
    return out


_LITERAL_ONLY_RE = re.compile(
    r"^(?:N?'(?:[^']|'')*'|[-+]?\d+(?:\.\d+)?|NULL|TRUE|FALSE)$", re.IGNORECASE
)

_FSP_RE = re.compile(r"^\s*(?:DATETIME|TIMESTAMP)\s*\(\s*(\d+)\s*\)", re.IGNORECASE)


def _fsp_suffix(target_type: str) -> str:
    """`DATETIME(6)` -> `(6)`; `DATETIME` -> `''`.

    The fractional-seconds precision a MySQL CURRENT_TIMESTAMP default must
    carry to be accepted on that column -- see the call site for the rule
    and the error it prevents."""
    m = _FSP_RE.match(target_type or "")
    if not m:
        return ""
    return "" if m.group(1) == "0" else f"({m.group(1)})"


# Target types that cannot carry a literal DEFAULT in MySQL at all.
_MYSQL_NO_DEFAULT_TYPES = ("TEXT", "BLOB", "JSON", "GEOMETRY", "TINYTEXT", "MEDIUMTEXT",
                           "LONGTEXT", "TINYBLOB", "MEDIUMBLOB", "LONGBLOB")


_ZERO_DATE_RE = re.compile(
    r"^'?0000-00-00(\s+00:00:00(\.0+)?)?'?$")


#: MySQL/MariaDB spell several date-time functions in ways PostgreSQL does
#: not accept. MariaDB in particular reports a `DEFAULT CURRENT_TIMESTAMP`
#: back as the *call* `current_timestamp()`, and PostgreSQL has no
#: parenthesised form of it:
#:
#:     syntax error at or near ")"
#:     LINE 12: "created_at" TIMESTAMP NOT NULL DEFAULT current_timestamp(),
#:
#: which is one statement's difference between a schema applying and a
#: 1000-statement script stopping. Left untranslated these would also be
#: caught by the unknown-function check below and *dropped* -- silently
#: losing a default that has a perfectly good equivalent.
_MYSQL_TIME_DEFAULTS_POSTGRES = {
    "CURRENT_TIMESTAMP()": "CURRENT_TIMESTAMP",
    "LOCALTIMESTAMP()": "LOCALTIMESTAMP",
    "LOCALTIME()": "LOCALTIMESTAMP",
    "NOW()": "CURRENT_TIMESTAMP",
    "SYSDATE()": "CURRENT_TIMESTAMP",
    "CURDATE()": "CURRENT_DATE",
    "CURRENT_DATE()": "CURRENT_DATE",
    "CURTIME()": "CURRENT_TIME",
    "CURRENT_TIME()": "CURRENT_TIME",
    "UTC_TIMESTAMP()": "(now() AT TIME ZONE 'utc')",
    "UTC_DATE()": "(now() AT TIME ZONE 'utc')::date",
    "UTC_TIME()": "(now() AT TIME ZONE 'utc')::time",
}

_MYSQL_TIME_DEFAULTS_MYSQL = {
    "SYSDATE()": "CURRENT_TIMESTAMP",
    "LOCALTIME()": "CURRENT_TIMESTAMP",
    "LOCALTIMESTAMP()": "CURRENT_TIMESTAMP",
    "CURRENT_DATE()": "CURRENT_DATE",
    "CURRENT_TIME()": "CURRENT_TIME",
}


def _translate_mysql_time_default(expr: str, target: str) -> str:
    """Rewrite a MySQL/MariaDB date-time default into the target's spelling."""
    table = (_MYSQL_TIME_DEFAULTS_POSTGRES if target == POSTGRES
             else _MYSQL_TIME_DEFAULTS_MYSQL)
    squashed = re.sub(r"\s+", "", expr).upper()
    if squashed in table:
        return table[squashed]
    # CURRENT_TIMESTAMP(6) and friends keep their precision on both
    # targets; only the empty-parens form has to go.
    return expr


def translate_default(
    default_sql: Optional[str],
    source_engine: str,
    target_engine: str,
    column_name: str = "",
    target_type: str = "",
) -> Tuple[Optional[str], List[ConversionIssue]]:
    """Translate one column DEFAULT expression for `target_engine`.

    Returns `(default_clause_or_None, issues)`. A None result means the
    default must be omitted from the generated DDL -- either it was empty
    to begin with, or it uses something the target cannot express. In the
    latter case an "error"-severity issue explains what was dropped, so the
    table is flagged in the assessment report rather than silently losing
    a default nobody notices until an INSERT fails.

    This function is why a SQL Server -> MySQL/PostgreSQL run no longer
    dies on its very first CREATE TABLE: `(getdate())` reached the target
    verbatim before, and neither engine has a `getdate` function.
    """
    issues: List[ConversionIssue] = []
    if default_sql is None:
        return None, issues
    raw = str(default_sql).strip()
    if not raw:
        return None, issues

    target = normalize_target(target_engine)
    if target not in (MYSQL, POSTGRES):
        # Oracle / SQL Server / Db2 / Mongo targets keep the pre-existing
        # pass-through behaviour untouched.
        return raw, issues

    expr = _strip_redundant_parens(raw)

    src = (source_engine or "").lower()
    if src.startswith("oracle"):
        expr = re.sub(r"\bSYSTIMESTAMP\b", "CURRENT_TIMESTAMP", expr, flags=re.IGNORECASE)
        expr = re.sub(r"\bSYSDATE\b", "CURRENT_TIMESTAMP", expr, flags=re.IGNORECASE)
        expr = _strip_redundant_parens(expr)
    elif src.replace(" ", "").startswith("sqlserver"):
        expr, expr_issues = translate_expression(
            expr, target_engine, rewrite_top=False, quote_identifiers=False)
        expr = _strip_redundant_parens(expr)
        for issue in expr_issues:
            issues.append(ConversionIssue(
                issue.severity,
                f"Column {column_name} DEFAULT: {issue.message}" if column_name else issue.message,
            ))
    else:
        # MySQL/PostgreSQL source: `CURRENT_TIMESTAMP`, `now()`, literals and
        # `nextval(...)` are already spelled the same way on both targets.
        expr = re.sub(r"::[A-Za-z_][\w ]*(\(\d+(,\d+)?\))?", "", expr).strip()
        expr = _translate_mysql_time_default(expr, target)

    if not expr:
        return None, issues

    upper = expr.upper()

    # MySQL's "zero date". Only MySQL has ever accepted 0000-00-00 as a
    # date, and only with NO_ZERO_DATE out of sql_mode -- it is not a
    # representable date anywhere else, and not in MySQL 8's own default
    # mode either. Reaching the target unquoted it is a syntax error:
    #
    #     syntax error at or near "00"
    #     LINE 24: ..."last_modified" TIMESTAMP NOT NULL DEFAULT 0000-00-00 00:00:00
    #
    # and quoted it is "date/time field value out of range". Neither is
    # fixable by spelling; the default itself has no equivalent, so it is
    # dropped and reported -- including the part the user has to decide,
    # which is what should happen to the rows that hold that value.
    if _ZERO_DATE_RE.match(expr):
        issues.append(ConversionIssue(
            "warning",
            f"Column {column_name}: DEFAULT {raw} is MySQL's \"zero date\", which no other "
            f"engine can store -- {target_engine} rejects it outright. The default was "
            f"dropped so the table can be created. Rows holding that value arrive as NULL, "
            f"so if this column is NOT NULL either give it a real default on the target or "
            f"allow NULLs before migrating the data.",
        ))
        return None, issues

    # A SQL Server BIT column becomes BOOLEAN on PostgreSQL, but its
    # default arrives as the integer literal `((1))`. PostgreSQL refuses
    # the table outright -- 'column "isactive" is of type boolean but
    # default expression is of type integer' -- so the literal has to be
    # translated alongside the type. MySQL keeps TINYINT(1), where 0/1 is
    # already correct.
    base_target_type = (target_type or "").split("(")[0].strip().upper()
    if base_target_type in ("BOOLEAN", "BOOL"):
        if upper in ("1", "TRUE", "'1'", "B'1'"):
            return "TRUE", issues
        if upper in ("0", "FALSE", "'0'", "B'0'"):
            return "FALSE", issues
        if upper == "NULL":
            return "NULL", issues
        issues.append(ConversionIssue(
            "warning",
            f"Column {column_name}: DEFAULT {raw} was kept as-is on a boolean column; verify it "
            "evaluates to a boolean on the target.",
        ))

    # A leftover engine-specific function nobody mapped must not reach the
    # target -- both MySQL (error 3770) and PostgreSQL ("function ... does
    # not exist") fail the whole CREATE TABLE over it.
    leftover = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*\(", expr)
    known_ok = {
        # Deliberately conservative for MySQL. MySQL 8 rejects
        # session-dependent and non-deterministic functions in a DEFAULT
        # expression with the very error this whole module exists to
        # prevent -- 3770, "contains a disallowed function" -- and the
        # rejected set (UUID, CURRENT_USER, LAST_INSERT_ID, RAND,
        # DATABASE, VERSION, ROW_COUNT, SYSDATE, CONNECTION_ID) is exactly
        # what a T-SQL NEWID()/SUSER_SNAME() default translates into.
        # MariaDB happens to accept several of them, but a default that
        # only works on some servers is worse than one the user is told
        # about: these are dropped with an explicit message instead.
        MYSQL: {"CURRENT_TIMESTAMP", "NOW", "UTC_TIMESTAMP", "CURRENT_DATE",
                "CURRENT_TIME", "CURDATE", "CURTIME", "UTC_DATE", "UTC_TIME",
                "LOCALTIME", "LOCALTIMESTAMP", "PI", "CONCAT", "IFNULL", "COALESCE",
                "CHAR_LENGTH", "LENGTH", "ABS", "ROUND", "FLOOR", "CEILING", "CAST", "DATE_ADD",
                "TIMESTAMPDIFF", "EXTRACT", "IF", "LOCATE", "POWER", "INSERT"},
        POSTGRES: {"CURRENT_TIMESTAMP", "NOW", "GEN_RANDOM_UUID", "CURRENT_USER", "CURRENT_DATE",
                   "CURRENT_TIME", "SESSION_USER", "NEXTVAL", "COALESCE", "LENGTH", "OCTET_LENGTH",
                   "ABS", "ROUND", "FLOOR", "CEIL", "CAST", "EXTRACT", "STRPOS", "OVERLAY",
                   "POWER", "LASTVAL", "CURRENT_DATABASE", "VERSION", "INET_CLIENT_ADDR", "RANDOM"},
    }[target]
    if leftover and leftover.group(1).upper() not in known_ok:
        # "warning", not "error": the table itself converts and creates
        # perfectly well -- one column simply loses a default the target
        # will not accept. Reporting it as an error marked the whole table
        # "Requires manual conversion" in the object tree, which sent the
        # user hunting for a problem in a table that migrates fine.
        issues.append(ConversionIssue(
            "warning",
            f"Column {column_name}: DEFAULT {raw} uses '{leftover.group(1)}()', which {target_engine} "
            f"does not accept in a column default. The default was omitted so the table can still be "
            f"created -- supply the value from the application, or add a BEFORE INSERT trigger.",
        ))
        return None, issues

    # A system variable (`@@hostname`) is not a call, so the check above
    # cannot see it, and MySQL rejects one in a DEFAULT for the same reason.
    if target == MYSQL and "@@" in expr:
        issues.append(ConversionIssue(
            "warning",
            f"Column {column_name}: DEFAULT {raw} resolves to a MySQL system variable, which is not "
            "allowed in a column default. The default was omitted -- supply the value from the "
            "application, or add a BEFORE INSERT trigger.",
        ))
        return None, issues

    if target == MYSQL:
        base_type = (target_type or "").split("(")[0].strip().upper()
        if base_type in _MYSQL_NO_DEFAULT_TYPES:
            issues.append(ConversionIssue(
                "warning",
                f"Column {column_name}: MySQL does not allow a DEFAULT on {base_type} columns, so "
                f"DEFAULT {raw} was dropped.",
            ))
            return None, issues
        # MySQL 8.0.13+ accepts a function/expression default only when it is
        # parenthesised -- with the single exception of CURRENT_TIMESTAMP
        # (with or without precision), which must NOT be parenthesised for a
        # DATETIME/TIMESTAMP column.
        if (re.fullmatch(r"CURRENT_TIMESTAMP(\s*\(\s*\d*\s*\))?", upper)
                or re.fullmatch(r"NOW\s*\(\s*\)", upper)):
            # The fractional-seconds precision has to match the column's,
            # exactly. MySQL's rule: "If a TIMESTAMP or DATETIME column
            # definition includes an explicit fractional seconds precision
            # value, the same value must be used throughout the column
            # definition" -- so DATETIME(6) DEFAULT CURRENT_TIMESTAMP is
            # rejected with error 1067, "Invalid default value", and only
            # DATETIME(6) DEFAULT CURRENT_TIMESTAMP(6) is accepted.
            #
            # A SQL Server `datetime` maps to DATETIME(6) here, so this hits
            # every table carrying a `(getdate())` default -- which is most
            # of them. MariaDB accepts the unqualified form, which is
            # exactly why this needs to be driven by the rule rather than by
            # what one server happened to tolerate.
            return f"CURRENT_TIMESTAMP{_fsp_suffix(target_type)}", issues
        if _LITERAL_ONLY_RE.match(expr):
            return expr, issues
        return f"({expr})", issues

    # PostgreSQL takes any expression unparenthesised.
    if re.fullmatch(r"NOW\s*\(\s*\)", upper):
        return "CURRENT_TIMESTAMP", issues
    return expr, issues


def translate_check_condition(
    condition: Optional[str], source_engine: str, target_engine: str, constraint_name: str = "",
) -> Tuple[Optional[str], List[ConversionIssue]]:
    """Same idea as translate_default, for a CHECK constraint's condition."""
    issues: List[ConversionIssue] = []
    if not condition:
        return condition, issues
    if not (source_engine or "").replace(" ", "").lower().startswith("sqlserver"):
        return condition, issues
    target = normalize_target(target_engine)
    if target not in (MYSQL, POSTGRES):
        return condition, issues
    out, expr_issues = translate_expression(
        _strip_redundant_parens(condition), target_engine, rewrite_top=False)
    for issue in expr_issues:
        issues.append(ConversionIssue(
            issue.severity,
            f"CHECK {constraint_name}: {issue.message}" if constraint_name else issue.message,
        ))
    return out, issues
