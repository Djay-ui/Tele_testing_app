"""Recognising "that object is already there" across every target engine.

Applying a converted schema is not a one-shot operation in practice. A
migration is tuned: apply, find one routine that needs a tweak, convert
again, apply again. Most of the generated script survives that loop
untouched -- `CREATE TABLE IF NOT EXISTS` no-ops, `CREATE OR REPLACE
VIEW` replaces -- but a foreign key cannot be written that way in
standard SQL:

    ALTER TABLE `EmployeeAttendance`
      ADD CONSTRAINT `FK_Attendance_Employees`
      FOREIGN KEY (`EmployeeID`) REFERENCES `Employees` (`EmployeeID`)

There is no portable `ADD CONSTRAINT IF NOT EXISTS`, so the second run
stops dead on

    1826 (HY000): Duplicate foreign key constraint name
    'FK_Attendance_Employees'

with the schema left half-applied -- even though the constraint the
statement asks for is already present and correct, i.e. the end state the
user wanted is the state they are already in.

`is_already_exists_error` answers the one question that turns that into a
skip instead of an abort: did this statement fail *because what it
creates already exists*? Callers decide what to do with the answer; the
apply path in the GUI records it as a skipped statement, names it in the
summary and logs it at warning level, so nothing is hidden -- it is
simply not treated as a reason to abandon the rest of the script.

Deliberately narrow. Anything else -- a shape mismatch, a type error, a
missing referenced column -- must still fail loudly, because statement
N+1 usually depends on statement N having worked.
"""
from __future__ import annotations

import re
from typing import Optional

# MySQL / MariaDB server error numbers whose sole meaning is "the object
# named in this statement is already there".
_MYSQL_CODES = {
    1050,  # Table '%s' already exists
    1060,  # Duplicate column name
    1061,  # Duplicate key name  (CREATE INDEX re-run)
    1068,  # Multiple primary key defined -- a PK is already on the table
    1304,  # PROCEDURE %s already exists
    1359,  # Trigger already exists
    1826,  # Duplicate foreign key constraint name
    1022,  # Can't write; duplicate key in table
}

# PostgreSQL SQLSTATEs in class 42 that mean "duplicate object".
_PG_SQLSTATES = {
    "42710",  # duplicate_object   (constraint, trigger, ...)
    "42P07",  # duplicate_table    (table, index, view, sequence)
    "42701",  # duplicate_column
    "42723",  # duplicate_function
}

# SQL Server message numbers for the same idea.
_SQLSERVER_CODES = {
    2714,  # There is already an object named '%s' in the database
    1779,  # Table already has a primary key defined on it
    2726,  # Duplicate index/constraint name
    1913,  # An index with name '%s' already exists
}

# Oracle ORA- numbers.
_ORACLE_CODES = {
    955,   # name is already used by an existing object
    1430,  # column being added already exists in table
    2260,  # table can have only one primary key
    2261,  # such unique or primary key already exists in the table
    2264,  # name already used by an existing constraint
    2275,  # such a referential constraint already exists in the table
    1408,  # such column list already indexed
}

# Db2 reports through SQLSTATE, and ibm_db surfaces it only in the text.
_DB2_SQLSTATES = {"42710", "42711", "42891", "01543"}

# MySQL 8 answers a repeated ADD CONSTRAINT with 1826 ("Duplicate foreign
# key constraint name"), but MariaDB reports the same situation through
# the generic 1005 with an InnoDB sub-code: errno 121, "Duplicate key on
# write or update". 1005 is *not* an already-exists code in general --
# errno 150 on the same 1005 is a genuinely malformed foreign key -- so
# it is decided on the sub-code, never on 1005 alone.
_MYSQL_1005_ALREADY_EXISTS = re.compile(r"errno:\s*121\b", re.IGNORECASE)

_TEXT_PATTERNS = (
    re.compile(r"duplicate\s+(foreign\s+key\s+)?constraint", re.IGNORECASE),
    re.compile(r"duplicate\s+key\s+on\s+write\s+or\s+update", re.IGNORECASE),
    re.compile(r"duplicate\s+key\s+name", re.IGNORECASE),
    re.compile(r"already\s+exists", re.IGNORECASE),
    re.compile(r"already\s+used\s+by\s+an\s+existing\s+object", re.IGNORECASE),
    re.compile(r"already\s+an\s+object\s+named", re.IGNORECASE),
    re.compile(r"already\s+has\s+a\s+primary\s+key", re.IGNORECASE),
    re.compile(r"multiple\s+primary\s+key\s+defined", re.IGNORECASE),
    re.compile(r"relation\s+\".+\"\s+already\s+exists", re.IGNORECASE),
)

# Must never be swallowed even though the text above might match a
# fragment of them: these say the statement's *dependencies* are wrong,
# not that its object is already built.
_NEVER = (
    re.compile(r"does\s+not\s+exist", re.IGNORECASE),
    re.compile(r"missing\s+column", re.IGNORECASE),
    re.compile(r"failed\s+to\s+add\s+the\s+foreign\s+key\s+constraint", re.IGNORECASE),
    re.compile(r"incorrectly\s+formed", re.IGNORECASE),
    re.compile(r"errno:\s*150", re.IGNORECASE),
)


def _errno(exc) -> Optional[int]:
    """The engine's own numeric code, wherever that driver puts it."""
    for attr in ("errno", "number", "code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    # oracledb raises DatabaseError whose args[0] is an _Error with .code
    args = getattr(exc, "args", ()) or ()
    for arg in args:
        code = getattr(arg, "code", None)
        if isinstance(code, int):
            return code
    return None


def _sqlstate(exc) -> Optional[str]:
    for attr in ("sqlstate", "pgcode"):
        value = getattr(exc, attr, None)
        if isinstance(value, str) and value:
            return value.upper()
    # psycopg3 keeps it on the diagnostic record
    diag = getattr(exc, "diag", None)
    value = getattr(diag, "sqlstate", None)
    if isinstance(value, str) and value:
        return value.upper()
    # pyodbc: args == ('42S01', "[42S01] ... ")
    args = getattr(exc, "args", ()) or ()
    if args and isinstance(args[0], str) and re.fullmatch(r"[0-9A-Za-z]{5}", args[0]):
        return args[0].upper()
    return None


_CREATES_RE = re.compile(
    r"\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:UNIQUE\s+)?"
    r"(TABLE|VIEW|INDEX|SEQUENCE)\s+(?:IF\s+NOT\s+EXISTS\s+)?[`\"\[]?([\w$#]+)",
    re.IGNORECASE)
_NAMED_IN_ERROR_RE = re.compile(r"[\"'`]([\w$#]+)[\"'`]")


def _blames_a_different_object(text: str, statement: Optional[str]) -> bool:
    """True when the statement creates X but the error is about some
    other object Y.

    The case this exists for: a MySQL source names every primary key
    `PRIMARY`, so `CREATE TABLE "drop_down_lang" (... CONSTRAINT
    "primary" PRIMARY KEY ...)` fails on PostgreSQL with

        relation "primary" already exists

    -- which is an "already exists" message about the *index*, not about
    drop_down_lang. Treating it as "the table is already there" skipped
    the statement and left the table uncreated, and the consequence
    surfaced hundreds of statements later as a foreign key pointing at a
    table with no primary key. The name collision itself is fixed in
    ddl_generator.uniquify_object_names; this makes sure that if
    anything like it ever happens again it fails loudly instead of
    silently.
    """
    if not statement:
        return False
    creates = _CREATES_RE.search(" ".join(statement.split()))
    if not creates:
        return False
    created = creates.group(2).lower()
    named = {n.lower() for n in _NAMED_IN_ERROR_RE.findall(text)}
    if not named:
        return False
    return created not in named


def is_already_exists_error(exc: BaseException, statement: Optional[str] = None) -> bool:
    """True when `exc` says the object the statement creates is already
    present on the target, and nothing worse than that.

    `statement`, when given, is checked against the object the error
    actually names -- see _blames_a_different_object.
    """
    text = str(exc)
    if _blames_a_different_object(text, statement):
        return False
    code = _errno(exc)
    if code == 1005 or "1005" in text[:12]:
        # Decided entirely on the InnoDB sub-code -- see
        # _MYSQL_1005_ALREADY_EXISTS.
        return bool(_MYSQL_1005_ALREADY_EXISTS.search(text))
    if any(p.search(text) for p in _NEVER):
        return False

    if code is not None:
        if code in _MYSQL_CODES or code in _SQLSERVER_CODES or code in _ORACLE_CODES:
            return True
        if abs(code) in _ORACLE_CODES:
            return True

    state = _sqlstate(exc)
    if state and (state in _PG_SQLSTATES or state in _DB2_SQLSTATES
                  or state in {"42S01", "42S11", "42S21"}):
        return True

    # Db2 through ibm_db only ever shows it in the message text.
    m = re.search(r"SQLSTATE\s*=\s*([0-9A-Z]{5})", text, re.IGNORECASE)
    if m and m.group(1).upper() in _DB2_SQLSTATES:
        return True

    # ORA-00955 and friends, when the driver hides the numeric code.
    m = re.search(r"ORA-0*(\d+)", text)
    if m and int(m.group(1)) in _ORACLE_CODES:
        return True

    return any(p.search(text) for p in _TEXT_PATTERNS)


def describe_skip(statement: str) -> str:
    """A short human phrase for what a skipped statement was going to
    create, for the summary the user reads."""
    flat = " ".join((statement or "").split())
    patterns = (
        (r"ADD\s+CONSTRAINT\s+[`\"\[]?([\w$#]+)", "foreign key / constraint"),
        (r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+[`\"\[]?([\w$#]+)", "index"),
        (r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[`\"\[]?([\w$#]+)", "table"),
        (r"CREATE\s+(?:OR\s+REPLACE\s+)?VIEW\s+[`\"\[]?([\w$#]+)", "view"),
        (r"CREATE\s+(?:DEFINER=\S+\s+)?PROCEDURE\s+[`\"\[]?([\w$#]+)", "procedure"),
        (r"CREATE\s+(?:DEFINER=\S+\s+)?FUNCTION\s+[`\"\[]?([\w$#]+)", "function"),
        (r"CREATE\s+(?:DEFINER=\S+\s+)?TRIGGER\s+[`\"\[]?([\w$#]+)", "trigger"),
        (r"CREATE\s+SEQUENCE\s+[`\"\[]?([\w$#]+)", "sequence"),
    )
    for pattern, kind in patterns:
        m = re.search(pattern, flat, re.IGNORECASE)
        if m:
            return f"{kind} {m.group(1)}"
    return flat[:60]
