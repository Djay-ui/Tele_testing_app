"""Routines and triggers whose source is MySQL SQL/PSM or PL/pgSQL.

Before this, `plsql_converter.convert_routine` converted routines from
exactly two sources: Oracle and SQL Server. Everything else hit a guard
that flagged it MANUAL on sight -- so a MySQL -> PostgreSQL migration, a
PostgreSQL -> MySQL one, and even a **MySQL -> MySQL** one converted every
table and view and not one procedure, function or trigger. "Unable to
migrate the triggers of any database" was an accurate description.

Two jobs here.

SAME ENGINE
-----------
When the source and the target are the same engine there is nothing to
translate; the only reason a routine did not migrate is that nobody
rebuilt the `CREATE` statement its catalog never stored. MySQL's
`information_schema.routines.routine_definition` and PostgreSQL's
`pg_proc.prosrc` are the *body alone*, and MySQL's
`information_schema.triggers.action_statement` likewise -- so the header
has to be reassembled from the metadata the introspector now carries
alongside it (`Routine.parameters`, `.return_type`, `.table_name`,
`.timing`, `.events`, `.row_level`). Nothing is rewritten, so this is
always a clean conversion.

MySQL <-> PostgreSQL
--------------------
The two procedural languages differ in a small, enumerable set of ways,
and this translates them in both directions:

    SET v = x;              <->  v := x;
    ELSEIF                  <->  ELSIF
    WHILE c DO … END WHILE  <->  WHILE c LOOP … END LOOP
    REPEAT … UNTIL c        ->   LOOP … EXIT WHEN c; END LOOP
    LEAVE lbl / ITERATE lbl <->  EXIT lbl / CONTINUE lbl
    lbl: LOOP               <->  <<lbl>> LOOP
    SIGNAL SQLSTATE '45000' <->  RAISE EXCEPTION
    DECLARE v INT DEFAULT 0 <->  v INT := 0   (in the DECLARE section)
    IFNULL / IF(a,b,c)      <->  COALESCE / CASE WHEN a THEN b ELSE c END
    DATE_FORMAT             <->  to_char, with the format mask translated

and the structural difference that matters most: **PostgreSQL keeps a
trigger's body in a separate function.** A MySQL trigger becomes a
`CREATE FUNCTION … RETURNS TRIGGER` plus a `CREATE TRIGGER … EXECUTE
FUNCTION`, with a `RETURN NEW;` added because a BEFORE row trigger that
returns nothing silently discards the row. Going the other way, that
function is inlined back into the trigger body.

What is not safely translatable is flagged, not guessed: MySQL's
`DECLARE … HANDLER` (PostgreSQL has no handler declarations, only an
EXCEPTION section), cursors declared with parameters, dynamic SQL,
`GET DIAGNOSTICS`, and anything referencing engine-specific builtins with
no counterpart.
"""
from __future__ import annotations

import re
from typing import List, Optional, Tuple

from tgdatabridge.core.plsql_converter import split_top_level, transform_function_calls
from tgdatabridge.core.schema_model import ConversionIssue, ConversionStatus, Routine
from tgdatabridge.utils.identifiers import quote_backtick, quote_double

# ------------------------------------------------------------- utilities

_STRING_RE = re.compile(r"'(?:[^']|'')*'")
_LINE_COMMENT_RE = re.compile(r"(--|#)[^\n]*")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_MASK_OPEN, _MASK_CLOSE = "\x01", "\x02"


#: Strings and comments in ONE alternation, scanned left to right, so
#: whichever starts first wins. Masking them in separate passes is subtly
#: wrong in both orders: comments-first eats the `#` in `CONCAT(v, ' #',
#: id)` and swallows the rest of the line (which is exactly how a
#: perfectly good function came out as `... ' /* ', p_id); */`), while
#: strings-first eats an apostrophe inside a comment and then reads the
#: code after it as string content.
def _mask_pattern(hash_comments: bool) -> re.Pattern:
    line = r"(?:--|#)[^\n]*" if hash_comments else r"--[^\n]*"
    return re.compile(r"'(?:[^']|'')*'|/\*.*?\*/|" + line, re.DOTALL)


class Masked:
    """Body text with literals and comments replaced by placeholders, so a
    keyword rewrite can never touch a string that merely contains the
    keyword.

    `hash_comments` is for MySQL, where `#` starts a comment. PL/pgSQL has
    no such thing and does use `#` as an operator, so it stays off there.
    """

    def __init__(self, text: str, hash_comments: bool = True):
        self.chunks: List[str] = []

        def keep(match: re.Match) -> str:
            found = match.group(0)
            if found.startswith(("--", "#")):
                # Stored as a block comment: the converted body is
                # re-flowed, and a `--` comment restored into the middle of
                # a rebuilt line would comment out the rest of that line.
                body = re.sub(r"^(--|#)", "", found).replace("*/", "* /").strip()
                found = f"/* {body} */" if body else ""
            self.chunks.append(found)
            return f"{_MASK_OPEN}{len(self.chunks) - 1}{_MASK_CLOSE}"

        self.text = _mask_pattern(hash_comments).sub(keep, text)

    def restore(self, text: str) -> str:
        return re.sub(f"{_MASK_OPEN}(\\d+){_MASK_CLOSE}",
                      lambda m: self.chunks[int(m.group(1))], text)


#: What can precede a statement. Used to tell an assignment's `SET` from
#: the `SET` clause of an UPDATE, and a statement-leading `:=` from one
#: inside a declaration default.
_STATEMENT_START = (r"(^|;|\bTHEN\b|\bELSE\b|\bBEGIN\b|\bLOOP\b|\bDO\b|"
                    r"\bREPEAT\b|\*/|\x02)(\s*)")


def _engine_key(engine: str) -> str:
    key = (engine or "").lower().replace(" ", "")
    for name in ("postgres", "mysql", "mariadb", "sqlserver", "oracle", "db2", "mongo"):
        if key.startswith(name):
            return "mysql" if name == "mariadb" else name
    return key


def same_engine(source_engine: str, target_engine: str) -> bool:
    return _engine_key(source_engine) == _engine_key(target_engine)


# ------------------------------------------------------- signature text


def _mysql_signature(routine: Routine) -> str:
    parts = []
    for parameter in routine.parameters:
        mode = "" if routine.kind == "FUNCTION" else f"{parameter.mode} "
        parts.append(f"{mode}{quote_backtick(parameter.name)} {parameter.data_type}")
    return ", ".join(parts)


def _postgres_signature(routine: Routine, issues: List[ConversionIssue]) -> str:
    parts = []
    for parameter in routine.parameters:
        mode = "" if parameter.mode.upper() == "IN" else f"{parameter.mode.upper()} "
        piece = f"{mode}{quote_double(parameter.name)} {parameter.data_type}"
        if parameter.default:
            piece += f" DEFAULT {parameter.default}"
        parts.append(piece)
    return ", ".join(parts)


def _characteristics(body: str) -> str:
    """MySQL refuses to create a routine with no data-access
    characteristic when binary logging is on (error 1418)."""
    if re.search(r"\b(INSERT|UPDATE|DELETE|REPLACE|CREATE|DROP|ALTER|CALL)\b",
                 body, re.IGNORECASE):
        return "MODIFIES SQL DATA"
    if re.search(r"\bSELECT\b", body, re.IGNORECASE):
        return "READS SQL DATA"
    return "DETERMINISTIC NO SQL"


# ------------------------------------------------ same-engine rebuilding


def rebuild_mysql(routine: Routine) -> Tuple[str, List[ConversionIssue]]:
    """A MySQL routine, re-emitted as MySQL. Nothing is translated."""
    issues: List[ConversionIssue] = []
    name = quote_backtick(routine.name)
    body = routine.source.strip()
    if not body:
        issues.append(ConversionIssue(
            "error",
            f"{routine.kind} {routine.name} has an empty body in the source catalog -- the "
            f"account this tool connected with may not have SELECT on mysql.proc / the "
            f"routine's definer rights. Nothing was generated for it."))
        return f"-- MANUAL CONVERSION REQUIRED for {routine.kind} {routine.name}", issues

    if routine.kind == "TRIGGER":
        if not routine.table_name:
            issues.append(ConversionIssue(
                "error", "The trigger's table was not captured; the DDL names UNKNOWN_TABLE."))
        table = quote_backtick(routine.table_name or "UNKNOWN_TABLE")
        blocks = []
        events = routine.events or ["INSERT"]
        if len(events) > 1:
            issues.append(ConversionIssue(
                "warning",
                f"MySQL allows one event per trigger, so {len(events)} triggers were emitted "
                f"over the same body."))
        for event in events:
            trigger_name = routine.name if len(events) == 1 else f"{routine.name}_{event}"
            blocks.append(
                f"DROP TRIGGER IF EXISTS {quote_backtick(trigger_name)};\n"
                f"CREATE TRIGGER {quote_backtick(trigger_name)}\n"
                f"{(routine.timing or 'BEFORE').upper()} {event} ON {table}\n"
                f"FOR EACH ROW\n{body}"
                + ("" if body.rstrip().endswith(";") else ";"))
        return "\n\n".join(blocks), issues

    keyword = "FUNCTION" if routine.kind == "FUNCTION" else "PROCEDURE"
    header = f"DROP {keyword} IF EXISTS {name};\nCREATE {keyword} {name}({_mysql_signature(routine)})"
    if routine.kind == "FUNCTION":
        if not routine.return_type:
            issues.append(ConversionIssue(
                "warning", "The function's return type was not captured; defaulted to TEXT."))
        header += f"\nRETURNS {routine.return_type or 'TEXT'}"
    header += f"\n{_characteristics(body)}"
    return f"{header}\n{body}" + ("" if body.rstrip().endswith(";") else ";"), issues


def rebuild_postgres(routine: Routine) -> Tuple[str, List[ConversionIssue]]:
    """A PostgreSQL routine, re-emitted as PostgreSQL."""
    issues: List[ConversionIssue] = []
    body = routine.source.strip()
    if not body:
        issues.append(ConversionIssue(
            "error",
            f"{routine.kind} {routine.name} has an empty body in pg_proc. Nothing was "
            f"generated for it."))
        return f"-- MANUAL CONVERSION REQUIRED for {routine.kind} {routine.name}", issues

    if routine.kind == "TRIGGER":
        function_name = routine.trigger_function or f"{routine.name}_fn"
        table = quote_double(routine.table_name or "UNKNOWN_TABLE")
        if not routine.table_name:
            issues.append(ConversionIssue(
                "error", "The trigger's table was not captured; the DDL names UNKNOWN_TABLE."))
        level = "ROW" if routine.row_level else "STATEMENT"
        events = " OR ".join(routine.events or ["INSERT"])
        return (
            f"CREATE OR REPLACE FUNCTION {quote_double(function_name)}()\n"
            f"RETURNS TRIGGER AS $$\n{body}\n$$ LANGUAGE plpgsql;\n\n"
            f"DROP TRIGGER IF EXISTS {quote_double(routine.name)} ON {table};\n"
            f"CREATE TRIGGER {quote_double(routine.name)}\n"
            f"{(routine.timing or 'BEFORE').upper()} {events} ON {table}\n"
            f"FOR EACH {level}\n"
            f"EXECUTE FUNCTION {quote_double(function_name)}();"), issues

    signature = _postgres_signature(routine, issues)
    if routine.kind == "PROCEDURE":
        return (f"CREATE OR REPLACE PROCEDURE {quote_double(routine.name)}({signature})\n"
                f"AS $$\n{body}\n$$ LANGUAGE plpgsql;"), issues
    returns = routine.return_type or "void"
    return (f"CREATE OR REPLACE FUNCTION {quote_double(routine.name)}({signature})\n"
            f"RETURNS {returns} AS $$\n{body}\n$$ LANGUAGE plpgsql;"), issues


def rebuild_verbatim(routine: Routine) -> Tuple[str, List[ConversionIssue]]:
    """SQL Server and Db2 keep the whole `CREATE ...` statement in their
    catalogs, so a migration back to the same engine needs no
    reassembly at all -- only the `CREATE OR ALTER` / drop-first handling
    that makes re-applying the script safe."""
    issues: List[ConversionIssue] = []
    text = (routine.source or "").strip()
    if not text:
        issues.append(ConversionIssue(
            "error", f"{routine.kind} {routine.name} has an empty definition in the catalog."))
        return f"-- MANUAL CONVERSION REQUIRED for {routine.kind} {routine.name}", issues
    if _engine_key(routine.source_engine) == "sqlserver":
        # CREATE OR ALTER makes the script re-runnable; ddl_generator's
        # _replace_prefix recognises it and adds no DROP of its own.
        text = re.sub(r"^\s*CREATE\s+(?:OR\s+ALTER\s+)?", "CREATE OR ALTER ", text,
                      count=1, flags=re.IGNORECASE)
    return text + ("" if text.endswith(";") else ";"), issues


# ------------------------------------------------- MySQL -> PostgreSQL

_MYSQL_UNSUPPORTED = [
    (re.compile(r"\bDECLARE\b[^;]*\bHANDLER\b", re.IGNORECASE),
     "A DECLARE ... HANDLER has no PostgreSQL equivalent; PostgreSQL catches errors in an "
     "EXCEPTION section at the end of a block instead. Rewrite the handler by hand."),
    (re.compile(r"\bGET\s+DIAGNOSTICS\b", re.IGNORECASE),
     "GET DIAGNOSTICS has no direct PostgreSQL equivalent in this shape; use GET STACKED "
     "DIAGNOSTICS inside an EXCEPTION block, or ROW_COUNT."),
    (re.compile(r"\bPREPARE\b.*\bFROM\b", re.IGNORECASE | re.DOTALL),
     "Dynamic SQL (PREPARE/EXECUTE) must be rewritten as PostgreSQL's EXECUTE ... USING."),
    (re.compile(r"\bGROUP_CONCAT\b", re.IGNORECASE),
     "GROUP_CONCAT has no PostgreSQL equivalent; use string_agg(expr, ',')."),
]

_MYSQL_TO_PG_FORMAT = [
    ("%Y", "YYYY"), ("%y", "YY"), ("%M", "Month"), ("%b", "Mon"), ("%m", "MM"),
    ("%W", "Day"), ("%a", "Dy"), ("%d", "DD"), ("%e", "FMDD"),
    ("%H", "HH24"), ("%h", "HH12"), ("%i", "MI"), ("%s", "SS"), ("%S", "SS"),
    ("%p", "AM"), ("%%", "%"),
]
_PG_TO_MYSQL_FORMAT = [
    ("YYYY", "%Y"), ("Month", "%M"), ("Mon", "%b"), ("MM", "%m"), ("YY", "%y"),
    ("Day", "%W"), ("Dy", "%a"), ("FMDD", "%e"), ("DD", "%d"),
    ("HH24", "%H"), ("HH12", "%h"), ("MI", "%i"), ("SS", "%s"), ("AM", "%p"),
]


def _translate_format(literal: str, table, masked: Optional[Masked]) -> str:
    text = literal.strip()
    if masked is not None and re.fullmatch(f"{_MASK_OPEN}\\d+{_MASK_CLOSE}", text):
        text = masked.restore(text)
    if not text.startswith("'"):
        return literal
    inner, out, i = text[1:-1], [], 0
    while i < len(inner):
        for source, target in table:
            if inner[i:i + len(source)] == source:
                out.append(target)
                i += len(source)
                break
        else:
            out.append(inner[i])
            i += 1
    return "'" + "".join(out) + "'"


def mysql_body_to_plpgsql(text: str, issues: List[ConversionIssue],
                          masked: Optional[Masked] = None) -> str:
    """The statement-level translation. Operates on masked text."""
    for pattern, message in _MYSQL_UNSUPPORTED:
        if pattern.search(text):
            issues.append(ConversionIssue("error", message))

    # Backtick identifiers -> double quotes. The migrated tables carry the
    # MySQL names verbatim, and PostgreSQL folds anything unquoted to lower
    # case, so quoting is what keeps `userId` resolving.
    text = re.sub(r"`([^`]+)`", lambda m: quote_double(m.group(1)), text)

    # Declarations: MySQL puts DECLARE on each line inside BEGIN;
    # PL/pgSQL has one DECLARE section and no DECLARE keyword per variable.
    text = re.sub(r"\bDECLARE\s+([A-Za-z_\"][\w$\" ]*?)\s+([^;]+?)\s+DEFAULT\s+([^;]+);",
                  r"\1 \2 := \3;", text, flags=re.IGNORECASE)
    text = re.sub(r"\bDECLARE\s+([A-Za-z_\"][\w$\" ]*?)\s+([^;]+);",
                  r"\1 \2;", text, flags=re.IGNORECASE)

    def _signal(match: re.Match) -> str:
        message = (match.group("message") or "").strip() or "'application error'"
        return f"RAISE EXCEPTION {message}"

    text = re.sub(
        # The SQLSTATE literal arrives MASKED (it is a string), so this has
        # to accept a placeholder as well as a quoted literal.
        r"\bSIGNAL\s+SQLSTATE\s+(?:'[^']*'|\x01\d+\x02)\s*"
        r"(?:SET\s+MESSAGE_TEXT\s*=\s*(?P<message>[^;,]+))?"
        r"(?:\s*,\s*MYSQL_ERRNO\s*=\s*\d+)?",
        _signal, text, flags=re.IGNORECASE)
    text = re.sub(r"\bRESIGNAL\b", "RAISE", text, flags=re.IGNORECASE)

    # Only a SET that *starts a statement* is an assignment. `UPDATE people
    # SET tag = p_tag` has a SET too, and rewriting that one turned a
    # working UPDATE into `UPDATE people tag := p_tag`, which PostgreSQL
    # rejects at the `:=`. The preceding delimiter is what tells them
    # apart.
    text = re.sub(_STATEMENT_START + r"SET\s+(?!TRANSACTION\b|SESSION\b|GLOBAL\b|@)"
                  r"([A-Za-z_\"][\w$.\"]*)\s*=\s*", r"\1\2\3 := ", text,
                  flags=re.IGNORECASE)
    text = re.sub(r"\bELSEIF\b", "ELSIF", text, flags=re.IGNORECASE)
    text = re.sub(r"\bWHILE\b(.+?)\bDO\b", r"WHILE\1LOOP", text,
                  flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"\bEND\s+WHILE\b", "END LOOP", text, flags=re.IGNORECASE)
    text = re.sub(r"\bEND\s+REPEAT\b", "END LOOP", text, flags=re.IGNORECASE)
    text = re.sub(r"\bREPEAT\b", "LOOP", text, flags=re.IGNORECASE)
    text = re.sub(r"\bUNTIL\b\s*(.+?)\s*(?=\bEND\s+LOOP\b|;)", r"EXIT WHEN \1;", text,
                  flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"\bLEAVE\s+([A-Za-z_][\w$]*)", r"EXIT \1", text, flags=re.IGNORECASE)
    text = re.sub(r"\bITERATE\s+([A-Za-z_][\w$]*)", r"CONTINUE \1", text, flags=re.IGNORECASE)
    # `lbl: LOOP` -> `<<lbl>> LOOP`, and drop the label after END LOOP.
    text = re.sub(r"^(\s*)([A-Za-z_][\w$]*)\s*:\s*(LOOP|WHILE)\b",
                  r"\1<<\2>> \3", text, flags=re.IGNORECASE | re.MULTILINE)
    text = re.sub(r"\bEND\s+LOOP\s+[A-Za-z_][\w$]*\s*;", "END LOOP;", text, flags=re.IGNORECASE)

    text = re.sub(r"\bIFNULL\s*\(", "COALESCE(", text, flags=re.IGNORECASE)
    text = re.sub(r"\bNOW\s*\(\s*\d*\s*\)", "now()", text, flags=re.IGNORECASE)
    text = re.sub(r"\bCURDATE\s*\(\s*\)", "current_date", text, flags=re.IGNORECASE)
    text = re.sub(r"\bRAND\s*\(\s*\)", "random()", text, flags=re.IGNORECASE)
    text = re.sub(r"\bAUTO_INCREMENT\b", "", text, flags=re.IGNORECASE)

    def _if_function(args: List[str]) -> str:
        if len(args) == 3:
            return f"CASE WHEN {args[0]} THEN {args[1]} ELSE {args[2]} END"
        issues.append(ConversionIssue(
            "error", f"IF() with {len(args)} arguments could not be converted."))
        return f"IF({', '.join(args)})"

    text, _ = transform_function_calls(text, r"(?<![\w.])IF", _if_function)
    text, _ = transform_function_calls(
        text, "CONCAT_WS",
        lambda args: "concat_ws(" + ", ".join(args) + ")")
    text, _ = transform_function_calls(
        text, "DATE_FORMAT",
        lambda args: (f"to_char({args[0]}, "
                      f"{_translate_format(args[1], _MYSQL_TO_PG_FORMAT, masked)})")
        if len(args) == 2 else f"DATE_FORMAT({', '.join(args)})")
    text, _ = transform_function_calls(
        text, "STR_TO_DATE",
        lambda args: (f"to_timestamp({args[0]}, "
                      f"{_translate_format(args[1], _MYSQL_TO_PG_FORMAT, masked)})")
        if len(args) == 2 else f"STR_TO_DATE({', '.join(args)})")
    return text


def convert_mysql_to_postgres(routine: Routine) -> Tuple[str, List[ConversionIssue]]:
    issues: List[ConversionIssue] = []
    if not routine.source.strip():
        issues.append(ConversionIssue(
            "error", f"{routine.kind} {routine.name} has an empty body in the source catalog."))
        return f"-- MANUAL CONVERSION REQUIRED for {routine.kind} {routine.name}", issues

    masked = Masked(routine.source.strip(), hash_comments=True)
    converted = mysql_body_to_plpgsql(masked.text, issues, masked)
    body = _reshape_declarations(converted)
    body = masked.restore(body)

    if routine.kind == "TRIGGER":
        # A BEFORE row trigger that returns nothing throws the row away, so
        # the RETURN is not optional -- PostgreSQL has no default.
        if not re.search(r"\bRETURN\b", body, re.IGNORECASE):
            returned = "NULL" if (routine.timing or "").upper() == "AFTER" else "NEW"
            if routine.events == ["DELETE"]:
                returned = "OLD"
            body = _insert_before_final_end(body, f"  RETURN {returned};")
            issues.append(ConversionIssue(
                "info",
                f"Added `RETURN {returned};` -- a PostgreSQL trigger function must return a "
                f"row, and a BEFORE trigger returning nothing discards the row entirely."))
        function_name = f"{routine.name}_fn"
        table = quote_double(routine.table_name or "UNKNOWN_TABLE")
        if not routine.table_name:
            issues.append(ConversionIssue(
                "error", "The trigger's table was not captured; the DDL names UNKNOWN_TABLE."))
        level = "ROW" if routine.row_level else "STATEMENT"
        events = " OR ".join(routine.events or ["INSERT"])
        return (
            f"CREATE OR REPLACE FUNCTION {quote_double(function_name)}()\n"
            f"RETURNS TRIGGER AS $$\n{body}\n$$ LANGUAGE plpgsql;\n\n"
            f"DROP TRIGGER IF EXISTS {quote_double(routine.name)} ON {table};\n"
            f"CREATE TRIGGER {quote_double(routine.name)}\n"
            f"{(routine.timing or 'BEFORE').upper()} {events} ON {table}\n"
            f"FOR EACH {level}\n"
            f"EXECUTE FUNCTION {quote_double(function_name)}();"), issues

    from tgdatabridge.core import type_mapping
    parameters = []
    for parameter in routine.parameters:
        mapped, type_issues = type_mapping.from_mysql(parameter.data_type)
        pg_type, more = type_mapping.to_postgres(mapped)
        issues.extend(i for i in type_issues + more if i.severity == "error")
        mode = "" if parameter.mode.upper() == "IN" else f"{parameter.mode.upper()} "
        parameters.append(f"{mode}{quote_double(parameter.name)} {pg_type}")
    signature = ", ".join(parameters)

    if routine.kind == "PROCEDURE":
        return (f"CREATE OR REPLACE PROCEDURE {quote_double(routine.name)}({signature})\n"
                f"AS $$\n{body}\n$$ LANGUAGE plpgsql;"), issues
    returns = "TEXT"
    if routine.return_type:
        mapped, _ = type_mapping.from_mysql(routine.return_type)
        returns, _ = type_mapping.to_postgres(mapped)
    else:
        issues.append(ConversionIssue(
            "warning", "The function's return type was not captured; defaulted to TEXT."))
    return (f"CREATE OR REPLACE FUNCTION {quote_double(routine.name)}({signature})\n"
            f"RETURNS {returns} AS $$\n{body}\n$$ LANGUAGE plpgsql;"), issues


_DECL_RE = re.compile(
    r"^\s*([A-Za-z_\"][\w$\"]*)\s+((?:[A-Za-z][\w ]*)(?:\([^)]*\))?)"
    r"(\s*:=\s*[^;]+)?;\s*$")


def _reshape_declarations(body: str) -> str:
    """Move the variable declarations of the outermost block into a
    PL/pgSQL DECLARE section.

    MySQL writes them as statements inside BEGIN; PL/pgSQL wants them
    before it. `mysql_body_to_plpgsql` has already stripped the DECLARE
    keyword, so what is left is a run of `name type [:= default];` lines at
    the top of the block -- exactly the shape PL/pgSQL's DECLARE section
    takes.
    """
    text = body.strip()
    match = re.match(r"^BEGIN\b", text, re.IGNORECASE)
    if not match:
        return text
    rest = text[match.end():]
    lines = rest.split("\n")
    declarations, index = [], 0
    while index < len(lines):
        line = lines[index].strip()
        if not line:
            index += 1
            continue
        if _DECL_RE.match(line) and not re.match(
                r"^(SET|SELECT|INSERT|UPDATE|DELETE|IF|WHILE|LOOP|RETURN|CALL|OPEN|CLOSE|"
                r"FETCH|RAISE|EXIT|CONTINUE|PERFORM|BEGIN|END|CASE|EXECUTE)\b",
                line, re.IGNORECASE):
            declarations.append(line)
            index += 1
            continue
        break
    if not declarations:
        return text
    remainder = "\n".join(lines[index:])
    return ("DECLARE\n" + "\n".join("  " + d for d in declarations)
            + "\nBEGIN" + remainder)


def _insert_before_final_end(body: str, statement: str) -> str:
    match = None
    for match in re.finditer(r"\bEND\s*;?\s*$", body, re.IGNORECASE):
        pass
    if not match:
        return body + "\n" + statement
    return body[:match.start()] + statement + "\n" + body[match.start():]


# ------------------------------------------------- PostgreSQL -> MySQL


_PG_UNSUPPORTED = [
    (re.compile(r"\bRETURNS\s+SETOF\b|\bRETURN\s+QUERY\b", re.IGNORECASE),
     "A set-returning function has no MySQL equivalent; MySQL functions return one scalar. "
     "Rewrite it as a view or a procedure that SELECTs."),
    (re.compile(r"\bPERFORM\b", re.IGNORECASE),
     "PERFORM has no MySQL equivalent; use SELECT ... INTO a throwaway variable, or CALL."),
    (re.compile(r"::\s*[A-Za-z]", re.IGNORECASE),
     "PostgreSQL's `::` cast has no MySQL equivalent; use CAST(x AS type)."),
    (re.compile(r"\bARRAY\s*\[", re.IGNORECASE),
     "PostgreSQL arrays have no MySQL equivalent."),
]


def plpgsql_body_to_mysql(text: str, issues: List[ConversionIssue],
                          masked: Optional[Masked] = None) -> str:
    for pattern, message in _PG_UNSUPPORTED:
        if pattern.search(text):
            issues.append(ConversionIssue("error", message))

    text = re.sub(r'"([^"]+)"', lambda m: quote_backtick(m.group(1)), text)
    text = re.sub(r"\bELSIF\b", "ELSEIF", text, flags=re.IGNORECASE)
    text = re.sub(r"<<\s*([A-Za-z_][\w$]*)\s*>>\s*", r"\1: ", text)
    text = re.sub(r"\bEXIT\s+WHEN\b(.+?);", r"IF\1 THEN LEAVE __loop; END IF;", text,
                  flags=re.IGNORECASE)
    text = re.sub(r"\bEXIT\s+([A-Za-z_][\w$]*)", r"LEAVE \1", text, flags=re.IGNORECASE)
    text = re.sub(r"\bCONTINUE\s+([A-Za-z_][\w$]*)", r"ITERATE \1", text, flags=re.IGNORECASE)
    text = re.sub(r"\bRAISE\s+(?:EXCEPTION|WARNING)\s+([^;]+);",
                  r"SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = \1;", text, flags=re.IGNORECASE)
    text = re.sub(r"\bCOALESCE\s*\(", "COALESCE(", text, flags=re.IGNORECASE)
    text = re.sub(r"\bnow\s*\(\s*\)", "NOW()", text, flags=re.IGNORECASE)
    text = re.sub(r"\bcurrent_date\b", "CURDATE()", text, flags=re.IGNORECASE)
    text = re.sub(r"\brandom\s*\(\s*\)", "RAND()", text, flags=re.IGNORECASE)
    text, _ = transform_function_calls(
        text, "to_char",
        lambda args: (f"DATE_FORMAT({args[0]}, "
                      f"{_translate_format(args[1], _PG_TO_MYSQL_FORMAT, masked)})")
        if len(args) == 2 else f"to_char({', '.join(args)})")
    text, _ = transform_function_calls(
        text, "string_agg",
        lambda args: f"GROUP_CONCAT({args[0]} SEPARATOR {args[1]})"
        if len(args) == 2 else f"string_agg({', '.join(args)})")

    # `v := x;` -> `SET v = x;`, but not `:=` inside a DECLARE default,
    # which _fold_declare_section has already turned into DEFAULT.
    text = re.sub(_STATEMENT_START + r"([A-Za-z_`][\w$.`]*)\s*:=\s*",
                  r"\1\2SET \3 = ", text, flags=re.IGNORECASE)

    # `a || b` is concatenation in PostgreSQL and logical OR in MySQL, so
    # leaving it turns every string a routine builds into 0 or 1. Reuses
    # the expression-boundary walk written for the Oracle -> MySQL path
    # rather than a second, differently-buggy regex.
    from tgdatabridge.core.plsql_mysql_converter import convert_concatenation
    return convert_concatenation(text)


def convert_postgres_to_mysql(routine: Routine) -> Tuple[str, List[ConversionIssue]]:
    issues: List[ConversionIssue] = []
    if not routine.source.strip():
        issues.append(ConversionIssue(
            "error", f"{routine.kind} {routine.name} has an empty body in pg_proc."))
        return f"-- MANUAL CONVERSION REQUIRED for {routine.kind} {routine.name}", issues

    masked = Masked(routine.source.strip(), hash_comments=False)
    body = _fold_declare_section(masked.text, issues)
    body = plpgsql_body_to_mysql(body, issues, masked)
    body = masked.restore(body)

    if routine.kind == "TRIGGER":
        # PostgreSQL's `RETURN NEW;` is how a trigger function passes the
        # row on; MySQL has no such thing and rejects RETURN in a trigger.
        body = re.sub(r"(?m)^[ \t]*RETURN\s+(NEW|OLD|NULL)\s*;[ \t]*\n?", "", body,
                      flags=re.IGNORECASE)
        body = re.sub(r"\bRETURN\s+(NEW|OLD|NULL)\s*;", "", body, flags=re.IGNORECASE)
        if not routine.row_level:
            issues.append(ConversionIssue(
                "error",
                f"Trigger {routine.name} is statement-level; MySQL only has FOR EACH ROW. "
                f"It was emitted row-level, which fires once per affected row."))
        table = quote_backtick(routine.table_name or "UNKNOWN_TABLE")
        if not routine.table_name:
            issues.append(ConversionIssue(
                "error", "The trigger's table was not captured; the DDL names UNKNOWN_TABLE."))
        events = routine.events or ["INSERT"]
        timing = (routine.timing or "BEFORE").upper()
        if timing == "INSTEAD OF":
            issues.append(ConversionIssue(
                "error", "MySQL has no INSTEAD OF trigger; emitted as BEFORE."))
            timing = "BEFORE"
        if len(events) > 1:
            issues.append(ConversionIssue(
                "warning",
                f"MySQL allows one event per trigger, so {len(events)} triggers were emitted "
                f"over the same body."))
        blocks = []
        for event in events:
            trigger_name = routine.name if len(events) == 1 else f"{routine.name}_{event}"
            blocks.append(
                f"DROP TRIGGER IF EXISTS {quote_backtick(trigger_name)};\n"
                f"CREATE TRIGGER {quote_backtick(trigger_name)}\n"
                f"{timing} {event} ON {table}\nFOR EACH ROW\n{body.strip()}"
                + ("" if body.strip().endswith(";") else ";"))
        return "\n\n".join(blocks), issues

    from tgdatabridge.core import type_mapping
    parameters = []
    for parameter in routine.parameters:
        mapped, _ = type_mapping.from_postgres(parameter.data_type)
        my_type, _ = type_mapping.to_mysql(mapped)
        mode = "" if routine.kind == "FUNCTION" else f"{parameter.mode.upper()} "
        parameters.append(f"{mode}{quote_backtick(parameter.name)} {my_type}")
    signature = ", ".join(parameters)
    name = quote_backtick(routine.name)
    keyword = "FUNCTION" if routine.kind == "FUNCTION" else "PROCEDURE"
    header = f"DROP {keyword} IF EXISTS {name};\nCREATE {keyword} {name}({signature})"
    if routine.kind == "FUNCTION":
        returns = "TEXT"
        if routine.return_type:
            mapped, _ = type_mapping.from_postgres(routine.return_type)
            returns, _ = type_mapping.to_mysql(mapped)
        header += f"\nRETURNS {returns}"
    header += f"\n{_characteristics(body)}"
    return f"{header}\n{body.strip()}" + ("" if body.strip().endswith(";") else ";"), issues


def _fold_declare_section(body: str, issues: List[ConversionIssue]) -> str:
    """PL/pgSQL's `DECLARE ... BEGIN` -> MySQL's `BEGIN DECLARE ...`."""
    match = re.match(r"^\s*DECLARE\b(.*?)\bBEGIN\b", body, re.IGNORECASE | re.DOTALL)
    if not match:
        return body
    declarations = []
    for statement in split_top_level(match.group(1), ";"):
        piece = statement.strip()
        if not piece:
            continue
        parts = re.split(r":=|\bDEFAULT\b", piece, maxsplit=1, flags=re.IGNORECASE)
        if len(parts) == 2:
            declarations.append(f"  DECLARE {parts[0].strip()} DEFAULT {parts[1].strip()};")
        else:
            declarations.append(f"  DECLARE {piece};")
    return "BEGIN\n" + "\n".join(declarations) + "\n" + body[match.end():]


# ------------------------------------------------------------- dispatch


def can_convert(source_engine: str, target_engine: str) -> bool:
    """Whether this module has a real conversion for the pair."""
    source, target = _engine_key(source_engine), _engine_key(target_engine)
    if source not in ("mysql", "postgres"):
        return False
    return target in ("mysql", "postgres")


def convert_routine(routine: Routine, target_engine: str) -> Routine:
    """Convert one MySQL- or PostgreSQL-sourced routine, in place."""
    source = _engine_key(routine.source_engine)
    target = _engine_key(target_engine)
    issues: List[ConversionIssue] = []

    if source == target == "mysql":
        ddl, issues = rebuild_mysql(routine)
    elif source == target == "postgres":
        ddl, issues = rebuild_postgres(routine)
    elif source == target in ("sqlserver", "db2"):
        ddl, issues = rebuild_verbatim(routine)
    elif source == "mysql" and target == "postgres":
        ddl, issues = convert_mysql_to_postgres(routine)
    elif source == "postgres" and target == "mysql":
        ddl, issues = convert_postgres_to_mysql(routine)
    else:
        ddl = (f"-- MANUAL CONVERSION REQUIRED for {routine.kind} {routine.name}\n"
               f"-- No automatic {routine.source_engine} -> {target_engine} routine converter.\n"
               f"/*\n{routine.source}\n*/")
        issues = [ConversionIssue(
            "error",
            f"{routine.kind} {routine.name}: converting a {routine.source_engine} routine to "
            f"{target_engine} is not automated; it must be rewritten by hand.")]

    routine.converted_source = ddl
    routine.issues = issues
    if any(i.severity == "error" for i in issues):
        routine.status = ConversionStatus.MANUAL
    elif any(i.severity == "warning" for i in issues):
        routine.status = ConversionStatus.AUTOMATIC_WITH_WARNINGS
    else:
        routine.status = ConversionStatus.AUTOMATIC
    return routine
