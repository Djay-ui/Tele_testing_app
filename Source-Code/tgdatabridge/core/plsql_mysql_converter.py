"""Oracle PL/SQL -> MySQL / MariaDB stored-routine converter.

Until now an Oracle -> MySQL migration converted every table, every view
and not one routine. `sql_translator.translate_routine` -- the path
`plsql_converter.convert_routine` fell through to for a MySQL target --
applies four regex substitutions and hands back the PL/SQL essentially
unchanged, then labels it "Converted with warnings". That label is the
worst part: the text it produces is still PL/SQL, and MySQL cannot run a
line of it. `v := 0;`, `ELSIF`, `EXIT WHEN`, `FOR r IN (SELECT ...) LOOP`
and a trailing `EXCEPTION WHEN OTHERS THEN` are all syntax errors there.

So this module does for MySQL what plsql_converter.py does for
PostgreSQL: a real, rule-based translation of the constructs that make up
the overwhelming majority of real stored code, and an honest flag on
anything that has no safe mechanical equivalent.

WHAT MYSQL MAKES HARD, AND WHAT IS DONE ABOUT IT
------------------------------------------------

**Declarations must come first, in order.** MySQL requires every DECLARE
in a block to precede every statement, and within that, variables before
conditions before cursors before handlers. Oracle imposes no such order
and puts its exception handling at the *end*. So the emitted block is
reassembled rather than translated line for line.

**There is no exception section.** Oracle's trailing `EXCEPTION WHEN ...
THEN ...` becomes `DECLARE EXIT HANDLER FOR <condition> BEGIN ... END;`
hoisted to the top of the same block. The semantics line up well: an EXIT
handler ends the block it is declared in, which is exactly what an Oracle
exception handler does.

**There is no FOR loop and no implicit cursor loop.** `FOR i IN 1..10
LOOP` becomes a labelled WHILE with an explicit counter. `FOR r IN
(SELECT a, b FROM t) LOOP ... r.a ...` becomes a real cursor plus the
`NOT FOUND` handler and per-column fetch variables MySQL requires, with
every `r.a` reference rewritten to the variable that now holds it. That
last one is the single most common shape in Oracle code and the single
biggest reason "convert the routines" used to mean "rewrite them".

**`||` is not concatenation.** MySQL reads `||` as logical OR unless the
session happens to have PIPES_AS_CONCAT set, which silently turns
`'Hello ' || name` into a boolean. Emitting a session-mode change would
be worse -- it would ride along with everything else applied in that
connection -- so concatenation is rewritten into real `CONCAT(...)`
calls, with expression boundaries found properly rather than by a regex
that would swallow the rest of the statement.

**Packages do not exist.** A PACKAGE BODY is flattened into standalone
`<package>_<member>` routines, each with its own `DROP ... IF EXISTS`,
because MySQL has no `CREATE OR REPLACE PROCEDURE` either.

**A trigger fires for one event.** Oracle's `BEFORE INSERT OR UPDATE`
becomes two MySQL triggers over the same body. Where the body branches on
`INSERTING` / `UPDATING` / `DELETING`, those predicates are resolved to
TRUE/FALSE per emitted trigger rather than left to fail.

Everything genuinely out of reach -- DBMS_*/UTL_* packages other than
DBMS_OUTPUT, BULK COLLECT, FORALL, CONNECT BY, autonomous transactions,
local TYPE declarations, %ROWTYPE -- is left in place, commented, and
raised as an issue. Same contract as everywhere else in this tool:
convert what is safe, flag the rest, never guess.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from tgdatabridge.core import type_mapping
from tgdatabridge.core.nested_subprogram import extract_nested_subprograms, find_top_level_begin
from tgdatabridge.core.plsql_converter import (
    parse_param, parse_routine_header, split_top_level, transform_function_calls)
from tgdatabridge.core.schema_model import ConversionIssue, ConversionStatus, Routine
from tgdatabridge.utils.identifiers import quote_backtick

# --------------------------------------------------------------- masking

_STRING_RE = re.compile(r"'(?:[^']|'')*'")
_LINE_COMMENT_RE = re.compile(r"--[^\n]*")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)

_MASK_OPEN, _MASK_CLOSE = "\x01", "\x02"


#: Strings and comments in ONE alternation, scanned left to right, so
#: whichever starts first wins. Masking them in separate passes is subtly
#: wrong in both orders: comments-first eats the `--` inside a string
#: literal and swallows the rest of the line, while strings-first eats an
#: apostrophe inside a comment and then reads the code after it as string
#: content.
_MASK_RE = re.compile(r"'(?:[^']|'')*'|/\*.*?\*/|--[^\n]*", re.DOTALL)


class Masked:
    """PL/SQL with string literals and comments replaced by placeholders.

    Every keyword rewrite below matches on word boundaries, and a routine
    that prints `'... END LOOP ...'` or explains itself in a comment would
    otherwise have that text rewritten as if it were code. Masking first
    and restoring last removes the whole class of problem, and costs one
    pass either way.
    """

    def __init__(self, text: str):
        self.chunks: List[str] = []

        def keep(match: re.Match) -> str:
            found = match.group(0)
            if found.startswith("--"):
                # Stored as a block comment, not verbatim. The converted
                # body is re-flowed -- several Oracle lines become one
                # MySQL statement and vice versa -- so a `-- ...` comment
                # restored into the middle of a rebuilt line would comment
                # out the rest of that line. `/* ... */` cannot.
                body = found[2:].replace("*/", "* /").strip()
                found = f"/* {body} */" if body else ""
            self.chunks.append(found)
            return f"{_MASK_OPEN}{len(self.chunks) - 1}{_MASK_CLOSE}"

        self.text = _MASK_RE.sub(keep, text)

    def restore(self, text: str) -> str:
        def put(match: re.Match) -> str:
            return self.chunks[int(match.group(1))]

        return re.sub(f"{_MASK_OPEN}(\\d+){_MASK_CLOSE}", put, text)


def _is_placeholder(text: str) -> bool:
    return bool(re.fullmatch(f"{_MASK_OPEN}\\d+{_MASK_CLOSE}", text.strip()))


# ------------------------------------------------------------ conditions

#: Oracle predefined exceptions that have a real MySQL handler condition.
_HANDLER_CONDITIONS = {
    "NO_DATA_FOUND": "NOT FOUND",
    "TOO_MANY_ROWS": "SQLEXCEPTION",          # no exact code; narrowed by a note
    "DUP_VAL_ON_INDEX": "SQLSTATE '23000'",
    "ZERO_DIVIDE": "SQLSTATE '22012'",
    "INVALID_NUMBER": "SQLSTATE '22018'",
    "VALUE_ERROR": "SQLSTATE '22001'",
    "OTHERS": "SQLEXCEPTION",
}
#: The ones above whose mapping is close rather than exact.
_APPROXIMATE_CONDITIONS = {
    "TOO_MANY_ROWS": "MySQL has no TOO_MANY_ROWS condition; mapped to SQLEXCEPTION, which is "
                     "broader -- narrow it by hand if this handler must not catch other errors.",
    "VALUE_ERROR": "VALUE_ERROR was mapped to SQLSTATE '22001' (string data right-truncated), "
                   "which is the closest MySQL condition but not an exact match.",
    "INVALID_NUMBER": "INVALID_NUMBER was mapped to SQLSTATE '22018' (invalid character value for "
                      "cast), the closest MySQL condition.",
}

_MANUAL_MARKERS = [
    (re.compile(r"\bDBMS_(?!OUTPUT\.PUT_LINE)[A-Z_]+", re.IGNORECASE),
     "Uses an Oracle DBMS_* package with no MySQL equivalent."),
    (re.compile(r"\bUTL_[A-Z_]+", re.IGNORECASE),
     "Uses an Oracle UTL_* package with no MySQL equivalent."),
    (re.compile(r"\bCONNECT\s+BY\b", re.IGNORECASE),
     "Hierarchical query (CONNECT BY) must be rewritten as a recursive CTE "
     "(MySQL 8.0+ / MariaDB 10.2+ support WITH RECURSIVE)."),
    (re.compile(r"\bAUTONOMOUS_TRANSACTION\b", re.IGNORECASE),
     "Autonomous transactions have no MySQL equivalent; this needs a separate connection."),
    (re.compile(r"\bBULK\s+COLLECT\b", re.IGNORECASE),
     "BULK COLLECT has no MySQL equivalent; rewrite as a cursor loop or a set-based statement."),
    (re.compile(r"\bFORALL\b", re.IGNORECASE),
     "FORALL bulk-DML has no MySQL equivalent; rewrite as a single set-based statement."),
    (re.compile(r"%ROWTYPE", re.IGNORECASE),
     "%ROWTYPE anchored typing has no MySQL equivalent; declare each column's variable explicitly."),
    (re.compile(r"\bGOTO\b", re.IGNORECASE),
     "GOTO has no MySQL equivalent; restructure the control flow."),
]


# ------------------------------------------------------------ expressions

_CONCAT_STOP = re.compile(
    r"^(?:SELECT|FROM|WHERE|AND|OR|NOT|INTO|SET|VALUES|THEN|ELSE|WHEN|CASE|END|"
    r"GROUP|ORDER|HAVING|LIMIT|USING|RETURN|LEAVE|ITERATE|ON|AS|IS|BY|IF|LOOP|"
    r"WHILE|DO|BEGIN|DECLARE|OPEN|CLOSE|FETCH|SIGNAL|CALL|UPDATE|INSERT|DELETE)$",
    re.IGNORECASE)

_OPERATOR_CHARS = set("+-*/%<>=!,;()")


def _expression_bounds(text: str, pos: int) -> Tuple[int, int]:
    """The smallest expression around the `||` at `pos`.

    Walks left and right at the same parenthesis depth, stopping at a
    comma, a comparison or arithmetic operator, an opening/closing paren
    that is not part of this expression, or a SQL keyword. A regex cannot
    do this: `SET msg := 'a' || b || 'c';` and `WHERE x = a || b AND y =
    1` need different right-hand boundaries, and getting it wrong turns a
    working statement into one that concatenates the rest of the clause.
    """
    def token_before(index: int) -> Tuple[str, int]:
        end = index
        while end > 0 and text[end - 1].isspace():
            end -= 1
        start = end
        while start > 0 and (text[start - 1].isalnum() or text[start - 1] in "_$#.\x01\x02"):
            start -= 1
        return text[start:end], start

    left = pos
    depth = 0
    while left > 0:
        token, start = token_before(left)
        if token:
            if _CONCAT_STOP.match(token) and depth == 0:
                break
            left = start
            continue
        char = text[left - 1]
        if char == ")":
            depth += 1
            left -= 1
            continue
        if char == "(":
            if depth == 0:
                break
            depth -= 1
            left -= 1
            continue
        if char.isspace():
            left -= 1
            continue
        if char == "|" and left >= 2 and text[left - 2] == "|":
            left -= 2
            continue
        if char in _OPERATOR_CHARS and depth == 0:
            break
        left -= 1

    def token_after(index: int) -> Tuple[str, int]:
        start = index
        while start < len(text) and text[start].isspace():
            start += 1
        end = start
        while end < len(text) and (text[end].isalnum() or text[end] in "_$#.\x01\x02"):
            end += 1
        return text[start:end], end

    right = pos + 2
    depth = 0
    while right < len(text):
        token, end = token_after(right)
        if token:
            if token.upper() == "CASE":
                # A CASE expression is one operand, not a stopping point.
                # Skip to its matching END so the WHEN/THEN/ELSE inside it
                # never look like the end of this expression.
                nested, probe = 1, end
                while probe < len(text) and nested:
                    inner, probe_end = token_after(probe)
                    if not inner:
                        probe += 1
                        continue
                    if inner.upper() == "CASE":
                        nested += 1
                    elif inner.upper() == "END":
                        nested -= 1
                    probe = probe_end
                right = probe
                continue
            if _CONCAT_STOP.match(token) and depth == 0:
                break
            right = end
            # A function call: keep its argument list with it.
            probe = right
            while probe < len(text) and text[probe].isspace():
                probe += 1
            if probe < len(text) and text[probe] == "(":
                depth += 1
                right = probe + 1
            continue
        char = text[right]
        if char == "(":
            depth += 1
            right += 1
            continue
        if char == ")":
            if depth == 0:
                break
            depth -= 1
            right += 1
            continue
        if char.isspace():
            right += 1
            continue
        if char == "|" and text[right:right + 2] == "||":
            right += 2
            continue
        if char in _OPERATOR_CHARS and depth == 0:
            break
        right += 1

    return left, right


def convert_concatenation(text: str) -> str:
    """`a || b || c` -> `CONCAT(a, b, c)`.

    MySQL reads `||` as logical OR (unless PIPES_AS_CONCAT is set, which
    is not something a migration should quietly rely on the target
    session having), so leaving it alone would turn every string built in
    a routine into a 0 or 1 rather than failing loudly.
    """
    while True:
        match = re.search(r"\|\|", text)
        if not match:
            return text
        left, right = _expression_bounds(text, match.start())
        segment = text[left:right]
        parts = [p.strip() for p in re.split(r"\|\|", segment) if p.strip()]
        if len(parts) < 2:
            # Nothing sensible to build; neutralise it so the loop ends
            # rather than spinning on the same operator forever.
            return text[:left] + segment.replace("||", " ") + text[right:]
        # The boundary walk eats the whitespace before the first operand, so
        # `SET a = 'x' || y` would come back as `SET a =CONCAT('x', y)`.
        # Valid SQL, but the generated script is something people read.
        lead = "" if (left == 0 or text[left - 1] in " \t\n(") else " "
        text = text[:left] + lead + f"CONCAT({', '.join(parts)})" + text[right:]


# --------------------------------------------------------------- builtins


def correlation_names(text: str) -> str:
    """`:NEW.col` / `:OLD.col` -> `NEW.col` / `OLD.col`.

    Applied before anything reads the statement's shape, not only inside
    expressions: `:NEW.created_at := SYSDATE;` has to be recognised as an
    assignment, and the leading colon stops the assignment pattern from
    matching at all -- which is how a converted trigger ended up emitting
    the PL/SQL `:=` verbatim and failing with error 1064.
    """
    text = re.sub(r":\s*NEW\s*\.", "NEW.", text, flags=re.IGNORECASE)
    return re.sub(r":\s*OLD\s*\.", "OLD.", text, flags=re.IGNORECASE)


def convert_expressions(text: str, issues: List[ConversionIssue],
                        masked: Optional["Masked"] = None) -> str:
    """Oracle's built-in functions, rewritten to MySQL's.

    `masked`, when given, lets the date-format translator read a string
    literal that has been replaced by a placeholder -- without it, a
    format mask arrives as an opaque token and `TO_CHAR(d,'DD/MM/YYYY')`
    silently keeps Oracle's mask, which MySQL reads as the literal text
    "DD/MM/YYYY".
    """
    # Concatenation first, while every `||` still sits between two plain
    # operands. Running it last instead meant DECODE had already become
    # `CASE ... WHEN ... THEN ... END`, and the expression-boundary walk
    # stops at those very keywords -- so it sliced the CASE in half and
    # produced SQL the server rejected. A function call is atomic to the
    # walk; a CASE expression is not.
    text = correlation_names(text)
    text = convert_concatenation(text)
    text = re.sub(r"\bSYSTIMESTAMP\b", "NOW(6)", text, flags=re.IGNORECASE)
    text = re.sub(r"\bSYSDATE\b", "NOW()", text, flags=re.IGNORECASE)
    text = re.sub(r"\bNVL\s*\(", "IFNULL(", text, flags=re.IGNORECASE)

    def _nvl2(args: List[str]) -> str:
        if len(args) == 3:
            return f"CASE WHEN ({args[0]}) IS NOT NULL THEN {args[1]} ELSE {args[2]} END"
        issues.append(ConversionIssue(
            "error", f"NVL2 with {len(args)} arguments could not be converted."))
        return f"NVL2({', '.join(args)})"

    text, _ = transform_function_calls(text, "NVL2", _nvl2)

    def _decode(args: List[str]) -> str:
        if len(args) < 3:
            issues.append(ConversionIssue(
                "error", f"DECODE with {len(args)} arguments could not be converted."))
            return f"DECODE({', '.join(args)})"
        expr, rest = args[0], args[1:]
        out = [f"CASE {expr}"]
        for k in range(len(rest) // 2):
            out.append(f" WHEN {rest[2 * k]} THEN {rest[2 * k + 1]}")
        if len(rest) % 2 == 1:
            out.append(f" ELSE {rest[-1]}")
        out.append(" END")
        issues.append(ConversionIssue(
            "warning",
            "DECODE was converted to CASE. DECODE treats NULL = NULL as a match and CASE does "
            "not -- review any branch that relied on that."))
        return "".join(out)

    text, _ = transform_function_calls(text, "DECODE", _decode)

    def _instr(args: List[str]) -> str:
        # Oracle INSTR(string, substring[, position[, occurrence]]);
        # MySQL INSTR(str, substr) has the same argument order, and
        # LOCATE(substr, str, pos) covers the 3-argument form.
        if len(args) == 2:
            return f"INSTR({args[0]}, {args[1]})"
        if len(args) == 3:
            return f"LOCATE({args[1]}, {args[0]}, {args[2]})"
        issues.append(ConversionIssue(
            "warning",
            "INSTR with an occurrence count has no MySQL equivalent; left as-is for review."))
        return f"INSTR({', '.join(args)})"

    text, _ = transform_function_calls(text, "INSTR", _instr)

    def _to_char(args: List[str]) -> str:
        if len(args) == 1:
            return f"CAST({args[0]} AS CHAR)"
        if len(args) == 2 and args[1].strip().startswith(("'", _MASK_OPEN)):
            return f"DATE_FORMAT({args[0]}, {_date_format(args[1], issues, masked)})"
        issues.append(ConversionIssue(
            "warning", "TO_CHAR with an NLS parameter was left for review."))
        return f"TO_CHAR({', '.join(args)})"

    text, _ = transform_function_calls(text, "TO_CHAR", _to_char)

    def _to_date(args: List[str]) -> str:
        if len(args) == 1:
            return f"CAST({args[0]} AS DATETIME)"
        if len(args) == 2:
            return f"STR_TO_DATE({args[0]}, {_date_format(args[1], issues, masked)})"
        issues.append(ConversionIssue(
            "warning", "TO_DATE with an NLS parameter was left for review."))
        return f"TO_DATE({', '.join(args)})"

    text, _ = transform_function_calls(text, "TO_DATE", _to_date)
    text, _ = transform_function_calls(
        text, "TO_NUMBER",
        lambda args: f"CAST({args[0]} AS DECIMAL(38,10))" if len(args) == 1
        else f"TO_NUMBER({', '.join(args)})")

    def _trunc(args: List[str]) -> str:
        if len(args) == 2:
            return f"TRUNCATE({args[0]}, {args[1]})"
        if len(args) == 1:
            issues.append(ConversionIssue(
                "warning",
                f"TRUNC({args[0]}) was read as a date truncation and converted to "
                f"DATE({args[0]}). If {args[0]} is a number, this needs TRUNCATE({args[0]}, 0) "
                f"instead."))
            return f"DATE({args[0]})"
        return f"TRUNC({', '.join(args)})"

    text, _ = transform_function_calls(text, "TRUNC", _trunc)

    def _put_line(args: List[str]) -> str:
        issues.append(ConversionIssue(
            "warning",
            "DBMS_OUTPUT.PUT_LINE has no MySQL equivalent (there is no server-side console). "
            "It was converted to SELECT ... AS message, which returns a result set to the "
            "caller -- remove it if that is not wanted."))
        return f"SELECT {args[0]} AS message" if len(args) == 1 else \
            f"SELECT CONCAT({', '.join(args)}) AS message"

    text, _ = transform_function_calls(text, r"DBMS_OUTPUT\.PUT_LINE", _put_line)

    def _raise_app(args: List[str]) -> str:
        if len(args) >= 2:
            # By this point args[1] is masked text (see the Masked class
            # above), so a bare string literal like 'boom' arrives here as
            # a single placeholder token, not literal quote characters --
            # _is_placeholder(args[1]) is this file's own existing way to
            # tell "one literal/comment token" from "an expression built
            # out of more than that" (see its other two call sites).
            if not _is_placeholder(args[1].strip()):
                # MySQL's SIGNAL only accepts a literal or a variable for
                # MESSAGE_TEXT, never an inline expression -- a message
                # built with Oracle's 'text' || variable concatenation
                # would need a local variable declared and assigned first,
                # which is real surgery this mechanical substitution can't
                # safely do in general. Left unconverted and flagged rather
                # than emitting SIGNAL...SET MESSAGE_TEXT = 'text'||variable,
                # which MySQL rejects outright.
                issues.append(ConversionIssue(
                    "error",
                    "RAISE_APPLICATION_ERROR's message is a computed expression (not a plain string "
                    "literal), which MySQL's SIGNAL statement cannot take inline -- assign it to a "
                    "local variable first, then SIGNAL ... SET MESSAGE_TEXT = that variable.",
                ))
                return f"RAISE_APPLICATION_ERROR({', '.join(args)})"
            return (f"SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = {args[1]}, "
                    f"MYSQL_ERRNO = 1644")
        issues.append(ConversionIssue("error", "RAISE_APPLICATION_ERROR could not be parsed."))
        return f"RAISE_APPLICATION_ERROR({', '.join(args)})"

    text, _ = transform_function_calls(text, "RAISE_APPLICATION_ERROR", _raise_app)

    # Sequences: MySQL has none, so the DDL generator emits a helper table
    # plus <name>_NEXTVAL()/<name>_CURRVAL() functions -- see
    # ddl_generator.generate_sequence_ddl_mysql. Point the call at those.
    text = re.sub(r"\b([A-Za-z_][\w$#]*)\.NEXTVAL\b",
                  lambda m: f"{quote_backtick(m.group(1) + '_NEXTVAL')}()",
                  text, flags=re.IGNORECASE)
    text = re.sub(r"\b([A-Za-z_][\w$#]*)\.CURRVAL\b",
                  lambda m: f"{quote_backtick(m.group(1) + '_CURRVAL')}()",
                  text, flags=re.IGNORECASE)

    return text


_ORACLE_TO_MYSQL_FORMAT = [
    ("YYYY", "%Y"), ("RRRR", "%Y"), ("YY", "%y"),
    ("MONTH", "%M"), ("MON", "%b"), ("MM", "%m"),
    ("DAY", "%W"), ("DY", "%a"), ("DD", "%d"),
    ("HH24", "%H"), ("HH12", "%h"), ("HH", "%h"),
    ("MI", "%i"), ("SS", "%s"), ("AM", "%p"), ("PM", "%p"),
]


def _date_format(literal: str, issues: List[ConversionIssue],
                 masked: Optional["Masked"] = None) -> str:
    """Translate an Oracle date mask to MySQL's `%`-codes."""
    literal = literal.strip()
    if masked is not None and _is_placeholder(literal):
        literal = masked.restore(literal)
    if not literal.startswith("'"):
        issues.append(ConversionIssue(
            "warning", "A date format that is not a literal was left unconverted for review."))
        return literal
    inner = literal[1:-1]
    out, i = [], 0
    while i < len(inner):
        for oracle, mysql in _ORACLE_TO_MYSQL_FORMAT:
            if inner[i:i + len(oracle)].upper() == oracle:
                out.append(mysql)
                i += len(oracle)
                break
        else:
            out.append(inner[i])
            i += 1
    return "'" + "".join(out) + "'"


# ------------------------------------------------------------ declarations


def _mysql_type(type_text: str, issues: List[ConversionIssue],
                where: str = "") -> str:
    """Map one Oracle declared type to MySQL.

    `%TYPE` cannot be resolved without the table it anchors to, which this
    converter does not have, so it is flagged rather than guessed -- a
    silently wrong width is far worse here than an explicit note, because
    it truncates data at runtime instead of failing at conversion time.
    """
    text = (type_text or "").strip()
    if not text:
        return "TEXT"
    if "%ROWTYPE" in text.upper():
        issues.append(ConversionIssue(
            "error",
            f"{where}%ROWTYPE has no MySQL equivalent; declare one variable per column."))
        return "TEXT"
    anchored = re.match(r"^([\w$#]+)\.([\w$#]+)%TYPE$", text, re.IGNORECASE)
    if anchored:
        issues.append(ConversionIssue(
            "warning",
            f"{where}{text} was declared as TEXT: MySQL has no %TYPE anchoring, and this "
            f"converter cannot see {anchored.group(1)}'s definition. Replace TEXT with the "
            f"column's real type if it matters."))
        return "TEXT"
    if text.upper().endswith("%TYPE"):
        issues.append(ConversionIssue(
            "warning", f"{where}{text} was declared as TEXT (no %TYPE in MySQL)."))
        return "TEXT"

    # PL/SQL-only scalar types, which never appear on a column and so are
    # not in the column type map.
    plsql_only = {
        "PLS_INTEGER": "BIGINT", "BINARY_INTEGER": "BIGINT",
        "SIMPLE_INTEGER": "BIGINT", "NATURAL": "BIGINT", "POSITIVE": "BIGINT",
        "BINARY_FLOAT": "FLOAT", "BINARY_DOUBLE": "DOUBLE",
    }
    if text.upper() in plsql_only:
        return plsql_only[text.upper()]
    if text.upper() == "BOOLEAN":
        issues.append(ConversionIssue(
            "warning",
            f"{where}BOOLEAN became TINYINT(1): MySQL has no boolean in a routine, so TRUE/FALSE "
            f"are 1/0 and any `IF v THEN` still works, but `v IS TRUE` does not."))
        return "TINYINT(1)"

    # Unconstrained NUMBER is the one mapping that must differ between a
    # column and a routine variable. As a column, DECIMAL(65,30) is the
    # right call -- it is exact, and a stored value must not lose
    # precision. As a variable it is the wrong call for a reason that only
    # shows up at runtime: MySQL renders a DECIMAL with its full scale, so
    # `'Order ' || p_order_id` -- which Oracle turns into "Order 10" --
    # comes out as "Order 10.000000000000000000000000000000". Oracle's
    # unconstrained NUMBER has no fixed scale, and MySQL's type with no
    # fixed scale is DOUBLE.
    if re.fullmatch(r"(NUMBER|NUMERIC|DECIMAL|DEC)\s*(\(\s*\*\s*\))?", text, re.IGNORECASE):
        issues.append(ConversionIssue(
            "warning",
            f"{where}NUMBER with no precision became DOUBLE, which matches Oracle's "
            f"scale-free behaviour when the value is printed or concatenated. If this "
            f"variable holds money, give it DECIMAL(p,s) instead -- DOUBLE is binary "
            f"floating point and does not round like Oracle's NUMBER."))
        return "DOUBLE"

    mapped, type_issues = type_mapping.to_mysql(text)
    for issue in type_issues:
        if issue.severity in ("warning", "error"):
            issues.append(issue)
    return mapped


class Declarations:
    """The four MySQL declaration groups, kept apart because MySQL insists
    on them in this order and rejects the block outright otherwise."""

    def __init__(self) -> None:
        self.variables: List[str] = []
        self.conditions: List[str] = []
        self.cursors: List[str] = []
        self.handlers: List[str] = []
        self.names: set = set()
        self._not_found_flags: List[str] = []
        self._not_found_index: Optional[int] = None

    def variable(self, line: str, name: Optional[str] = None) -> None:
        if name:
            if name.upper() in self.names:
                return
            self.names.add(name.upper())
        self.variables.append(line)

    def handler_for_not_found(self, flag: str) -> None:
        """The `NOT FOUND` handler every MySQL cursor loop needs.

        MySQL allows exactly one handler per condition per block, so two
        cursor loops in the same block must share one -- hence a single
        handler that sets every loop's own done-flag rather than one
        handler each, which fails to create with error 1338.
        """
        self._not_found_flags.append(flag)
        sets = ", ".join(f"{quote_backtick(f)} = 1" for f in self._not_found_flags)
        line = f"DECLARE CONTINUE HANDLER FOR NOT FOUND SET {sets};"
        if self._not_found_index is None:
            self._not_found_index = len(self.handlers)
            self.handlers.append(line)
        else:
            self.handlers[self._not_found_index] = line

    def render(self, indent: str = "  ") -> str:
        lines = self.variables + self.conditions + self.cursors + self.handlers
        return "\n".join(indent + line for line in lines)

    def __bool__(self) -> bool:
        return bool(self.variables or self.conditions or self.cursors or self.handlers)


def convert_declare_block(text: str, issues: List[ConversionIssue],
                          decls: Declarations,
                          masked: Optional["Masked"] = None) -> None:
    """Translate an Oracle DECLARE section into `decls`."""
    text, nested = extract_nested_subprograms(text)
    for ns in nested:
        issues.append(ConversionIssue(
            "error",
            f"Nested {ns.kind} '{ns.name}' is declared inside this routine; MySQL has no nested "
            f"named subprograms. Extract it as its own procedure/function, or inline it."))

    for raw in split_top_level(text, ";"):
        stmt = raw.strip()
        if not stmt or _is_placeholder(stmt):
            continue

        if re.match(r"^TYPE\s+", stmt, re.IGNORECASE):
            issues.append(ConversionIssue(
                "error",
                f"Local type declaration '{stmt};' (TABLE OF / RECORD / REF CURSOR) has no MySQL "
                f"equivalent; rewrite it using a temporary table or individual variables."))
            continue

        if re.match(r"^PRAGMA\b", stmt, re.IGNORECASE):
            issues.append(ConversionIssue(
                "warning", f"Dropped PRAGMA directive: {stmt};  MySQL has no equivalent."))
            continue

        cursor_m = re.match(
            r"^CURSOR\s+([A-Za-z_][\w$#]*)\s*(\([^)]*\))?\s+IS\s+(.*)$",
            stmt, re.IGNORECASE | re.DOTALL)
        if cursor_m:
            name, params, query = cursor_m.groups()
            if params:
                issues.append(ConversionIssue(
                    "error",
                    f"Cursor '{name}' takes parameters, which MySQL cursors cannot. Move the "
                    f"parameter into a variable the cursor's query reads instead."))
            decls.cursors.append(
                f"DECLARE {quote_backtick(name)} CURSOR FOR "
                f"{convert_expressions(query.strip(), issues, masked)};")
            continue

        exc_m = re.match(r"^([A-Za-z_][\w$#]*)\s+EXCEPTION$", stmt, re.IGNORECASE)
        if exc_m:
            # MySQL's CONDITION declaration is a genuinely good match for
            # a user-defined Oracle exception -- better than PostgreSQL,
            # which has no equivalent at all.
            decls.conditions.append(
                f"DECLARE {quote_backtick(exc_m.group(1))} CONDITION FOR SQLSTATE '45000';")
            continue

        var_m = re.match(r"^([A-Za-z_][\w$#]*)\s+(.+)$", stmt, re.DOTALL)
        if not var_m:
            issues.append(ConversionIssue("error", f"Could not parse declaration: {stmt};"))
            continue

        name, rest = var_m.group(1), var_m.group(2).strip()
        if re.match(r"^CONSTANT\b", rest, re.IGNORECASE):
            rest = re.sub(r"^CONSTANT\s+", "", rest, flags=re.IGNORECASE)
            issues.append(ConversionIssue(
                "info",
                f"'{name}' was declared CONSTANT; MySQL has no constant local, so it became an "
                f"ordinary variable. Nothing assigns to it unless the body does."))
        parts = re.split(r":=|\bDEFAULT\b", rest, maxsplit=1, flags=re.IGNORECASE)
        type_text = parts[0].strip()
        default = parts[1].strip() if len(parts) == 2 else None
        type_text = re.sub(r"\bNOT\s+NULL\s*$", "", type_text, flags=re.IGNORECASE).strip()

        line = f"DECLARE {quote_backtick(name)} {_mysql_type(type_text, issues, f'{name}: ')}"
        if default:
            line += f" DEFAULT {convert_expressions(default, issues, masked)}"
        decls.variable(line + ";", name)


# ------------------------------------------------------------------ body
#
# A tokeniser and a block parser, rather than a line-by-line rewrite.
#
# The first attempt reflowed the text so every control keyword began a
# line and then walked the lines. It cannot be made to work: `IF x THEN y
# := 1; END IF;` is ordinary Oracle and puts four constructs on one line,
# while `FOR r IN (SELECT ... ) LOOP` must NOT be split at its LOOP even
# though a bare `LOOP` on its own must be recognised. Deciding that from a
# regex over lines means guessing. Deciding it from a scanner that knows
# where it is in the grammar does not.

_IDENT_RE = re.compile(r"[A-Za-z_][\w$#]*")


def _skip_space(text: str, pos: int) -> int:
    while pos < len(text) and text[pos].isspace():
        pos += 1
    return pos


def _word_at(text: str, pos: int) -> str:
    m = _IDENT_RE.match(text, pos)
    return m.group(0).upper() if m else ""


def _find_keyword(text: str, pos: int, keyword: str) -> int:
    """Position of the next `keyword` at parenthesis depth 0, skipping any
    that belongs to a CASE expression opened after `pos`.

    The CASE tracking is what stops `IF x = CASE WHEN a THEN 1 ELSE 2 END
    THEN` from ending the IF's condition at the CASE's own THEN.
    """
    depth = 0
    case_depth = 0
    index = pos
    while index < len(text):
        char = text[index]
        if char == "(":
            depth += 1
            index += 1
            continue
        if char == ")":
            depth -= 1
            index += 1
            continue
        m = _IDENT_RE.match(text, index)
        if not m:
            index += 1
            continue
        word = m.group(0).upper()
        if word == "CASE":
            case_depth += 1
        elif word == "END" and case_depth:
            case_depth -= 1
        elif word == keyword and depth == 0 and case_depth == 0:
            return index
        index = m.end()
    return -1


def _find_statement_end(text: str, pos: int) -> int:
    """End of a plain statement: the next `;` at paren depth 0, with any
    CASE expression inside it skipped over."""
    depth = 0
    case_depth = 0
    index = pos
    while index < len(text):
        char = text[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == ";" and depth == 0 and case_depth == 0:
            return index
        else:
            m = _IDENT_RE.match(text, index)
            if m:
                word = m.group(0).upper()
                if word == "CASE":
                    case_depth += 1
                elif word == "END" and case_depth:
                    case_depth -= 1
                index = m.end()
                continue
        index += 1
    return len(text)


# ------------------------------------------------------------------ nodes


class Node:
    pass


class Stmt(Node):
    def __init__(self, text: str):
        self.text = text


class Comment(Node):
    def __init__(self, text: str):
        self.text = text


class If(Node):
    def __init__(self):
        self.branches: List[Tuple[str, List[Node]]] = []
        self.otherwise: Optional[List[Node]] = None


class CaseStmt(Node):
    def __init__(self, selector: str):
        self.selector = selector
        self.branches: List[Tuple[str, List[Node]]] = []
        self.otherwise: Optional[List[Node]] = None


class Loop(Node):
    def __init__(self, header: str, body: List[Node]):
        self.header = header      # "" for a bare LOOP
        self.body = body


class Block(Node):
    """A BEGIN ... [EXCEPTION ...] END, nested inside a routine's body."""

    def __init__(self, body: List[Node],
                 handlers: List[Tuple[str, List[Node]]]):
        self.body = body
        self.handlers = handlers


class ParseError(Exception):
    pass


# --------------------------------------------------------------- parsing

_END_OF_BLOCK = ("END", "ELSE", "ELSIF", "ELSEIF", "EXCEPTION", "WHEN")


class Parser:
    """PL/SQL statement list -> Node tree. Masked text only."""

    def __init__(self, text: str):
        self.text = text
        self.pos = 0

    def _at_end(self) -> bool:
        return _skip_space(self.text, self.pos) >= len(self.text)

    def _peek(self) -> str:
        return _word_at(self.text, _skip_space(self.text, self.pos))

    def _eat_word(self, word: str) -> bool:
        pos = _skip_space(self.text, self.pos)
        m = _IDENT_RE.match(self.text, pos)
        if m and m.group(0).upper() == word:
            self.pos = m.end()
            return True
        return False

    def _eat_semicolons(self) -> None:
        while True:
            pos = _skip_space(self.text, self.pos)
            if pos < len(self.text) and self.text[pos] == ";":
                self.pos = pos + 1
                continue
            return

    def parse_statements(self) -> List[Node]:
        nodes: List[Node] = []
        while True:
            self._eat_semicolons()
            if self._at_end():
                return nodes
            word = self._peek()
            if word in _END_OF_BLOCK:
                return nodes
            nodes.append(self._statement())

    def _statement(self) -> Node:
        pos = _skip_space(self.text, self.pos)
        self.pos = pos
        if self.text[pos] == _MASK_OPEN:
            close = self.text.index(_MASK_CLOSE, pos)
            self.pos = close + 1
            return Comment(self.text[pos:close + 1])

        word = _word_at(self.text, pos)
        if word == "IF":
            return self._if()
        if word == "BEGIN":
            return self._block()
        if word == "LOOP":
            self.pos = pos + 4
            return self._loop("")
        if word in ("FOR", "WHILE"):
            loop_at = _find_keyword(self.text, pos, "LOOP")
            if loop_at < 0:
                raise ParseError(f"loop header with no LOOP: {self.text[pos:pos + 60]}")
            header = self.text[pos:loop_at].strip()
            self.pos = loop_at + 4
            return self._loop(header)
        if word == "CASE":
            return self._case()
        if word == "DECLARE":
            # An anonymous nested block with its own declarations. MySQL
            # allows the same shape, so it is parsed as a Block and the
            # declarations are converted with the block's own.
            raise ParseError("nested DECLARE block")

        end = _find_statement_end(self.text, pos)
        text = self.text[pos:end].strip()
        self.pos = end + 1 if end < len(self.text) else end
        if not text:
            return Comment("")
        return Stmt(text)

    def _condition_then(self) -> str:
        then_at = _find_keyword(self.text, self.pos, "THEN")
        if then_at < 0:
            raise ParseError(f"no THEN after {self.text[self.pos:self.pos + 60]}")
        condition = self.text[self.pos:then_at].strip()
        self.pos = then_at + 4
        return condition

    def _if(self) -> If:
        self._eat_word("IF")
        node = If()
        node.branches.append((self._condition_then(), self.parse_statements()))
        while True:
            word = self._peek()
            if word in ("ELSIF", "ELSEIF"):
                self._eat_word(word)
                node.branches.append((self._condition_then(), self.parse_statements()))
                continue
            if word == "ELSE":
                self._eat_word("ELSE")
                node.otherwise = self.parse_statements()
                continue
            break
        if not self._eat_word("END"):
            raise ParseError("unterminated IF")
        self._eat_word("IF")
        self._eat_semicolons()
        return node

    def _case(self) -> CaseStmt:
        self._eat_word("CASE")
        when_at = _find_keyword(self.text, self.pos, "WHEN")
        if when_at < 0:
            raise ParseError("CASE with no WHEN")
        node = CaseStmt(self.text[self.pos:when_at].strip())
        self.pos = when_at
        while self._peek() == "WHEN":
            self._eat_word("WHEN")
            node.branches.append((self._condition_then(), self.parse_statements()))
        if self._peek() == "ELSE":
            self._eat_word("ELSE")
            node.otherwise = self.parse_statements()
        if not self._eat_word("END"):
            raise ParseError("unterminated CASE")
        self._eat_word("CASE")
        self._eat_semicolons()
        return node

    def _loop(self, header: str) -> Loop:
        body = self.parse_statements()
        if not self._eat_word("END"):
            raise ParseError("unterminated LOOP")
        self._eat_word("LOOP")
        # `END LOOP label;`
        pos = _skip_space(self.text, self.pos)
        m = _IDENT_RE.match(self.text, pos)
        if m:
            self.pos = m.end()
        self._eat_semicolons()
        return Loop(header, body)

    def _block(self) -> Block:
        self._eat_word("BEGIN")
        body = self.parse_statements()
        handlers: List[Tuple[str, List[Node]]] = []
        if self._peek() == "EXCEPTION":
            self._eat_word("EXCEPTION")
            while self._peek() == "WHEN":
                self._eat_word("WHEN")
                handlers.append((self._condition_then(), self.parse_statements()))
        if not self._eat_word("END"):
            raise ParseError("unterminated BEGIN")
        pos = _skip_space(self.text, self.pos)
        m = _IDENT_RE.match(self.text, pos)
        if m and m.group(0).upper() not in _END_OF_BLOCK:
            self.pos = m.end()
        self._eat_semicolons()
        return Block(body, handlers)


def parse_body(text: str) -> Tuple[List[Node], List[Tuple[str, List[Node]]]]:
    """Parse a routine body that starts at its outermost BEGIN.

    Returns the statement list and the outermost EXCEPTION section's
    handlers, which become the routine's own.
    """
    parser = Parser(text)
    parser._eat_semicolons()
    if parser._peek() != "BEGIN":
        return Parser(text).parse_statements(), []
    block = parser._block()
    return block.body, block.handlers


# --------------------------------------------------------------- emitting

_FOR_RANGE_RE = re.compile(
    r"^FOR\s+([A-Za-z_][\w$#]*)\s+IN\s+(REVERSE\s+)?(.+?)\s*\.\.\s*(.+)$",
    re.IGNORECASE | re.DOTALL)
_FOR_CURSOR_RE = re.compile(
    r"^FOR\s+([A-Za-z_][\w$#]*)\s+IN\s+(.+)$", re.IGNORECASE | re.DOTALL)
_WHILE_RE = re.compile(r"^WHILE\s+(.+)$", re.IGNORECASE | re.DOTALL)
_EXIT_WHEN_RE = re.compile(r"^EXIT\s+WHEN\s+(.+)$", re.IGNORECASE | re.DOTALL)
_CONTINUE_WHEN_RE = re.compile(r"^CONTINUE\s+WHEN\s+(.+)$", re.IGNORECASE | re.DOTALL)
_ASSIGN_RE = re.compile(r"^([A-Za-z_][\w$#]*(?:\.[A-Za-z_][\w$#]*)?)\s*:=\s*(.+)$", re.DOTALL)
_SELECT_LIST_RE = re.compile(r"^SELECT\s+(.*?)\s+FROM\b", re.IGNORECASE | re.DOTALL)


def _select_columns(query: str) -> Optional[List[str]]:
    """Column names a cursor FOR loop's fetch variables should take.

    None when the list cannot be read with confidence -- `SELECT *`, or an
    expression with no alias. Guessing here would produce a routine that
    compiles and then binds the wrong column, which is worse than saying
    so.
    """
    m = _SELECT_LIST_RE.match(" ".join(query.split()))
    if not m:
        return None
    names = []
    for item in split_top_level(m.group(1), ","):
        item = item.strip()
        if item == "*" or item.endswith(".*"):
            return None
        alias = re.search(r"\bAS\s+([A-Za-z_][\w$#]*)$", item, re.IGNORECASE)
        if alias:
            names.append(alias.group(1))
            continue
        bare = re.fullmatch(r"[A-Za-z_][\w$#]*(?:\.([A-Za-z_][\w$#]*))?", item)
        if bare:
            names.append(bare.group(1) or item)
            continue
        trailing = re.search(r"[)\w]\s+([A-Za-z_][\w$#]*)$", item)
        if trailing and trailing.group(1).upper() not in ("ASC", "DESC"):
            names.append(trailing.group(1))
            continue
        return None
    return names or None


class Emitter:
    """Node tree -> MySQL, carrying the declarations each block needs.

    Loop labels are allocated here rather than at parse time because EXIT
    has to resolve to the innermost enclosing loop, which is a property of
    where a statement sits, not of how it was written.
    """

    def __init__(self, issues: List[ConversionIssue], kind: str,
                 cursor_queries: Optional[Dict[str, str]] = None,
                 outer_label: str = "routine_body",
                 masked: Optional["Masked"] = None):
        self.masked = masked
        self.issues = issues
        self.kind = kind
        self.cursor_queries = {k.upper(): v for k, v in (cursor_queries or {}).items()}
        self.outer_label = outer_label
        self.used_outer_label = False
        self.counter = 0
        self.loop_labels: List[str] = []
        self.decl_stack: List[Declarations] = []
        self.record_rewrites: List[Tuple[re.Pattern, str]] = []

    # -- helpers -------------------------------------------------------

    @property
    def decls(self) -> Declarations:
        return self.decl_stack[-1]

    def _next(self, stem: str) -> str:
        self.counter += 1
        return f"{stem}_{self.counter}"

    def _expr(self, text: str) -> str:
        return convert_expressions(text, self.issues, self.masked)

    # -- the walk ------------------------------------------------------

    def emit(self, nodes: List[Node], indent: int = 0) -> List[str]:
        out: List[str] = []
        for node in nodes:
            out.extend(self._node(node, indent))
        return out

    def _node(self, node: Node, indent: int) -> List[str]:
        pad = "  " * indent
        if isinstance(node, Comment):
            return [pad + node.text] if node.text else []
        if isinstance(node, Stmt):
            return [pad + line for line in self._statement(node.text)]
        if isinstance(node, If):
            return self._if(node, indent)
        if isinstance(node, CaseStmt):
            return self._case(node, indent)
        if isinstance(node, Loop):
            return self._loop(node, indent)
        if isinstance(node, Block):
            return self._block(node, indent)
        return []

    def _if(self, node: If, indent: int) -> List[str]:
        pad = "  " * indent
        out: List[str] = []
        for index, (condition, body) in enumerate(node.branches):
            keyword = "IF" if index == 0 else "ELSEIF"
            out.append(f"{pad}{keyword} {self._expr(condition)} THEN")
            out.extend(self.emit(body, indent + 1))
        if node.otherwise is not None:
            out.append(f"{pad}ELSE")
            out.extend(self.emit(node.otherwise, indent + 1))
        out.append(f"{pad}END IF;")
        return out

    def _case(self, node: CaseStmt, indent: int) -> List[str]:
        pad = "  " * indent
        selector = self._expr(node.selector) if node.selector.strip() else ""
        out = [f"{pad}CASE {selector}".rstrip()]
        for condition, body in node.branches:
            out.append(f"{pad}  WHEN {self._expr(condition)} THEN")
            out.extend(self.emit(body, indent + 2))
        if node.otherwise is not None:
            out.append(f"{pad}  ELSE")
            out.extend(self.emit(node.otherwise, indent + 2))
        out.append(f"{pad}END CASE;")
        return out

    def _block(self, node: Block, indent: int) -> List[str]:
        pad = "  " * indent
        self.decl_stack.append(Declarations())
        body = self.emit(node.body, indent + 1)
        for condition, statements in node.handlers:
            self._handler(condition, statements)
        declarations = self.decls.render(pad + "  ")
        self.decl_stack.pop()
        out = [f"{pad}BEGIN"]
        if declarations:
            out.append(declarations)
        out.extend(body)
        out.append(f"{pad}END;")
        return out

    def _handler(self, condition: str, statements: List[Node]) -> None:
        """One `WHEN x THEN ...` -> one MySQL EXIT handler on this block."""
        name = condition.strip().upper()
        if name in self.declared_exceptions:
            mysql_condition = quote_backtick(condition.strip())
        elif name in _HANDLER_CONDITIONS:
            mysql_condition = _HANDLER_CONDITIONS[name]
            if name in _APPROXIMATE_CONDITIONS:
                self.issues.append(ConversionIssue("warning", _APPROXIMATE_CONDITIONS[name]))
        else:
            self.issues.append(ConversionIssue(
                "error",
                f"Exception handler 'WHEN {condition.strip()} THEN' has no MySQL condition; it "
                f"was mapped to SQLEXCEPTION, which catches everything. Narrow it by hand."))
            mysql_condition = "SQLEXCEPTION"

        self.decl_stack.append(Declarations())
        body = self.emit(statements, 2)
        inner = self.decls.render("    ")
        self.decl_stack.pop()
        pieces = [line for line in ([inner] if inner else []) + body if line.strip()]
        if not pieces:
            pieces = ["    BEGIN END;"]
        self.decls.handlers.append(
            f"DECLARE EXIT HANDLER FOR {mysql_condition}\n  BEGIN\n"
            + "\n".join(pieces) + "\n  END;")

    declared_exceptions: set = set()

    # -- loops ---------------------------------------------------------

    def _loop(self, node: Loop, indent: int) -> List[str]:
        header = node.header.strip()
        if not header:
            return self._plain_loop(node, indent)
        m = _WHILE_RE.match(header)
        if m:
            return self._while_loop(m.group(1), node, indent)
        if _FOR_RANGE_RE.match(header):
            return self._range_loop(_FOR_RANGE_RE.match(header), node, indent)
        m = _FOR_CURSOR_RE.match(header)
        if m:
            return self._cursor_loop(m.group(1), m.group(2).strip(), node, indent)
        self.issues.append(ConversionIssue(
            "error", f"Could not parse loop header: {header}"))
        return ["  " * indent + f"-- MANUAL: {header} LOOP ... END LOOP;"]

    def _plain_loop(self, node: Loop, indent: int) -> List[str]:
        pad = "  " * indent
        label = self._next("loop")
        self.loop_labels.append(label)
        body = self.emit(node.body, indent + 1)
        self.loop_labels.pop()
        return [f"{pad}{label}: LOOP", *body, f"{pad}END LOOP {label};"]

    def _while_loop(self, condition: str, node: Loop, indent: int) -> List[str]:
        pad = "  " * indent
        label = self._next("while")
        self.loop_labels.append(label)
        body = self.emit(node.body, indent + 1)
        self.loop_labels.pop()
        return [f"{pad}{label}: WHILE {self._expr(condition)} DO", *body,
                f"{pad}END WHILE {label};"]

    def _range_loop(self, m: re.Match, node: Loop, indent: int) -> List[str]:
        pad = "  " * indent
        var, reverse, low, high = m.groups()
        label = self._next("for")
        bound = f"{var}_limit_{self.counter}"
        self.decls.variable(f"DECLARE {quote_backtick(var)} BIGINT;", var)
        self.decls.variable(f"DECLARE {quote_backtick(bound)} BIGINT;", bound)
        low_sql, high_sql = self._expr(low), self._expr(high)
        if reverse:
            first, last = high_sql, low_sql
            test = f"{quote_backtick(var)} >= {quote_backtick(bound)}"
            step = f"SET {quote_backtick(var)} = {quote_backtick(var)} - 1;"
        else:
            first, last = low_sql, high_sql
            test = f"{quote_backtick(var)} <= {quote_backtick(bound)}"
            step = f"SET {quote_backtick(var)} = {quote_backtick(var)} + 1;"
        self.loop_labels.append(label)
        body = self.emit(node.body, indent + 1)
        self.loop_labels.pop()
        return [
            f"{pad}SET {quote_backtick(var)} = {first};",
            f"{pad}SET {quote_backtick(bound)} = {last};",
            f"{pad}{label}: WHILE {test} DO",
            *body,
            f"{pad}  {step}",
            f"{pad}END WHILE {label};",
        ]

    def _cursor_loop(self, record: str, source: str, node: Loop,
                     indent: int) -> List[str]:
        """`FOR r IN (SELECT ...) LOOP` / `FOR r IN some_cursor LOOP`.

        MySQL has no implicit cursor loop, so the whole apparatus is
        built: the cursor, the NOT FOUND handler that is the only way to
        detect exhaustion, one fetch variable per selected column, and a
        rewrite of every `r.col` in the body to the variable now holding
        it.
        """
        pad = "  " * indent
        query = source.strip()
        if query.startswith("(") and query.endswith(")"):
            query = query[1:-1].strip()
        elif re.fullmatch(r"[A-Za-z_][\w$#]*", query):
            named = self.cursor_queries.get(query.upper())
            if not named:
                self.issues.append(ConversionIssue(
                    "error",
                    f"Loop over cursor '{query}' could not be converted: its declaration was not "
                    f"found, so the columns it fetches are unknown."))
                return [f"{pad}-- MANUAL: FOR {record} IN {query} LOOP ... END LOOP;"]
            query = named

        columns = _select_columns(query)
        if columns is None:
            self.issues.append(ConversionIssue(
                "error",
                f"Loop over '{record}' could not be converted: its query selects * or an "
                f"unaliased expression, so MySQL's FETCH ... INTO has no column list to bind. "
                f"Name each column (or give it an alias) and convert again."))
            return [f"{pad}-- MANUAL: FOR {record} IN ({query}) LOOP ... END LOOP;"]

        label = self._next("cur")
        suffix = self.counter
        cursor = f"{record}_cur_{suffix}"
        done = f"{record}_done_{suffix}"
        fetch = [f"{record}_{column}" for column in columns]

        # The loop gets its own BEGIN ... END rather than declaring into
        # the enclosing block. MySQL permits one handler per condition per
        # block (error 1338 otherwise), so a routine with two cursor loops
        # -- or one cursor loop and a `WHEN NO_DATA_FOUND` handler, which
        # is also NOT FOUND -- would fail to create. Scoping each loop
        # makes those cases independent, and matches Oracle, where the
        # record variable only exists inside the loop anyway.
        self.decl_stack.append(Declarations())
        for name in fetch:
            self.decls.variable(f"DECLARE {quote_backtick(name)} TEXT;", name)
        self.decls.variable(f"DECLARE {quote_backtick(done)} INT DEFAULT 0;", done)
        self.decls.cursors.append(
            f"DECLARE {quote_backtick(cursor)} CURSOR FOR {self._expr(query)};")
        self.decls.handler_for_not_found(done)
        self.issues.append(ConversionIssue(
            "info",
            f"The cursor loop over '{record}' became an explicit MySQL cursor with a NOT FOUND "
            f"handler. Its fetch variables were declared TEXT -- give them the columns' real "
            f"types if a comparison inside the loop depends on them."))

        for column in columns:
            self.record_rewrites.append((
                re.compile(rf"\b{re.escape(record)}\.{re.escape(column)}\b", re.IGNORECASE),
                quote_backtick(f"{record}_{column}")))

        self.loop_labels.append(label)
        body = self.emit(node.body, indent + 2)
        self.loop_labels.pop()
        declarations = self.decls.render(pad + "  ")
        self.decl_stack.pop()
        return [
            f"{pad}BEGIN",
            declarations,
            f"{pad}  SET {quote_backtick(done)} = 0;",
            f"{pad}  OPEN {quote_backtick(cursor)};",
            f"{pad}  {label}: LOOP",
            f"{pad}    FETCH {quote_backtick(cursor)} INTO "
            + ", ".join(quote_backtick(name) for name in fetch) + ";",
            f"{pad}    IF {quote_backtick(done)} = 1 THEN LEAVE {label}; END IF;",
            *body,
            f"{pad}  END LOOP {label};",
            f"{pad}  CLOSE {quote_backtick(cursor)};",
            f"{pad}END;",
        ]

    # -- statements ----------------------------------------------------

    def _current_label(self) -> str:
        if self.loop_labels:
            return self.loop_labels[-1]
        self.used_outer_label = True
        return self.outer_label

    def _statement(self, raw: str) -> List[str]:
        text = correlation_names(raw.strip().rstrip(";").strip())
        if not text:
            return []

        if re.fullmatch(r"NULL", text, re.IGNORECASE):
            # MySQL has no NULL statement; an empty compound block stands
            # in for it anywhere a statement is allowed.
            return ["BEGIN END;"]

        m = _EXIT_WHEN_RE.match(text)
        if m:
            return [f"IF {self._expr(m.group(1))} THEN LEAVE {self._current_label()}; END IF;"]
        m = _CONTINUE_WHEN_RE.match(text)
        if m:
            return [f"IF {self._expr(m.group(1))} THEN ITERATE {self._current_label()}; END IF;"]
        if re.fullmatch(r"EXIT", text, re.IGNORECASE):
            return [f"LEAVE {self._current_label()};"]
        if re.fullmatch(r"CONTINUE", text, re.IGNORECASE):
            return [f"ITERATE {self._current_label()};"]

        if re.fullmatch(r"RETURN", text, re.IGNORECASE):
            if self.kind == "FUNCTION":
                self.issues.append(ConversionIssue(
                    "error", "A bare RETURN in a function has no value to return."))
                return ["RETURN NULL;"]
            self.used_outer_label = True
            return [f"LEAVE {self.outer_label};"]

        m = re.match(r"^EXECUTE\s+IMMEDIATE\s+(.+)$", text, re.IGNORECASE | re.DOTALL)
        if m:
            return self._execute_immediate(m.group(1))

        m = re.match(r"^RAISE\s+([A-Za-z_][\w$#]*)$", text, re.IGNORECASE)
        if m:
            name = m.group(1)
            if name.upper() in self.declared_exceptions:
                return [f"SIGNAL {quote_backtick(name)} SET MESSAGE_TEXT = '{name}';"]
            self.issues.append(ConversionIssue(
                "warning",
                f"RAISE {name} refers to an exception this routine does not declare; it became a "
                f"SIGNAL with SQLSTATE '45000'."))
            return [f"SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{name}';"]
        if re.fullmatch(r"RAISE", text, re.IGNORECASE):
            return ["RESIGNAL;"]

        m = _ASSIGN_RE.match(text)
        if m:
            target, value = m.group(1), self._expr(m.group(2))
            if "." in target:
                return [f"SET {target} = {value};"]     # NEW.col := x, in a trigger
            return [f"SET {quote_backtick(target)} = {value};"]

        return [self._expr(text) + ";"]

    def _execute_immediate(self, argument: str) -> List[str]:
        argument = argument.strip()
        if re.search(r"\b(USING|INTO)\b", argument, re.IGNORECASE):
            self.issues.append(ConversionIssue(
                "error",
                "EXECUTE IMMEDIATE with a USING or INTO clause was left unconverted; MySQL's "
                "PREPARE binds parameters from user variables (SET @p = ...; ... USING @p)."))
            return [f"/* MANUAL: EXECUTE IMMEDIATE {argument}; */"]
        self.issues.append(ConversionIssue(
            "info", "EXECUTE IMMEDIATE became PREPARE / EXECUTE / DEALLOCATE PREPARE."))
        return [
            f"SET @_tg_dyn = {self._expr(argument)};",
            "PREPARE _tg_ps FROM @_tg_dyn;",
            "EXECUTE _tg_ps;",
            "DEALLOCATE PREPARE _tg_ps;",
        ]


def _apply_record_rewrites(lines: List[str], emitter: Emitter) -> List[str]:
    for pattern, replacement in emitter.record_rewrites:
        lines = [pattern.sub(replacement, line) for line in lines]
    return lines


def convert_statements(body_text: str, issues: List[ConversionIssue],
                       decls: Declarations, kind: str,
                       cursor_queries: Optional[Dict[str, str]] = None,
                       declared_exceptions: Optional[set] = None,
                       outer_label: str = "routine_body",
                       masked: Optional["Masked"] = None) -> Tuple[List[str], bool]:
    """Convert one routine body. Returns its MySQL lines and whether the
    outer block needs a label (because a bare RETURN became a LEAVE)."""
    emitter = Emitter(issues, kind, cursor_queries, outer_label, masked)
    emitter.declared_exceptions = {n.upper() for n in (declared_exceptions or set())}
    emitter.decl_stack.append(decls)
    try:
        nodes, handlers = parse_body(body_text)
    except (ParseError, ValueError) as exc:
        issues.append(ConversionIssue(
            "error",
            f"The routine body could not be parsed ({exc}); it needs converting by hand."))
        return [], False
    lines = emitter.emit(nodes, 0)
    for condition, statements in handlers:
        emitter._handler(condition, statements)
    emitter.decl_stack.pop()
    return _apply_record_rewrites(lines, emitter), emitter.used_outer_label


# ------------------------------------------------------- whole routines


def _characteristics(body: str) -> str:
    """MySQL refuses to create a routine with no data-access
    characteristic when binary logging is on (error 1418), so one is
    always declared, chosen from what the body actually does."""
    if re.search(r"\b(INSERT|UPDATE|DELETE|REPLACE|CREATE|DROP|ALTER|CALL)\b",
                 body, re.IGNORECASE):
        return "MODIFIES SQL DATA"
    if re.search(r"\bSELECT\b", body, re.IGNORECASE):
        return "READS SQL DATA"
    return "DETERMINISTIC NO SQL"


def _cursor_queries(declare_text: str) -> Dict[str, str]:
    found: Dict[str, str] = {}
    for stmt in split_top_level(declare_text or "", ";"):
        m = re.match(r"^\s*CURSOR\s+([A-Za-z_][\w$#]*)\s*(?:\([^)]*\))?\s+IS\s+(.*)$",
                     stmt, re.IGNORECASE | re.DOTALL)
        if m:
            found[m.group(1).upper()] = m.group(2).strip()
    return found


def _declared_exception_names(declare_text: str) -> set:
    names = set()
    for stmt in split_top_level(declare_text or "", ";"):
        m = re.match(r"^([A-Za-z_][\w$#]*)\s+EXCEPTION$", stmt.strip(), re.IGNORECASE)
        if m:
            names.add(m.group(1).upper())
    return names


def _flag_markers(text: str, issues: List[ConversionIssue]) -> None:
    for pattern, message in _MANUAL_MARKERS:
        if pattern.search(text):
            issues.append(ConversionIssue("error", message))


def convert_procedure_or_function(routine: Routine) -> Tuple[str, List[ConversionIssue]]:
    issues: List[ConversionIssue] = []
    masked = Masked(routine.source)
    parsed = parse_routine_header(masked.text)
    if parsed is None:
        issues.append(ConversionIssue(
            "error", "Could not parse the routine header; manual conversion required."))
        return (f"-- MANUAL CONVERSION REQUIRED for {routine.kind} {routine.name}\n"
                f"/*\n{routine.source}\n*/"), issues

    _flag_markers(masked.restore(masked.text), issues)

    is_function = parsed["kind"] == "FUNCTION"
    params = []
    for raw in split_top_level(parsed["params_text"], ","):
        if not raw.strip():
            continue
        name, mode, type_text, default = parse_param(raw)
        mapped = _mysql_type(type_text, issues, f"parameter {name}: ")
        if is_function:
            if mode != "IN":
                issues.append(ConversionIssue(
                    "error",
                    f"Parameter '{name}' is {mode}; a MySQL function's parameters are all IN. "
                    f"Convert this routine to a procedure, or return the value instead."))
            params.append(f"{quote_backtick(name)} {mapped}")
        else:
            params.append(f"{mode} {quote_backtick(name)} {mapped}")
        if default is not None:
            issues.append(ConversionIssue(
                "warning",
                f"Parameter '{name}' had a default ({default}); MySQL has no parameter defaults, "
                f"so every caller must now pass it."))

    declare_text = parsed["declare_text"]
    body_text = parsed["body_text"]
    declared = _declared_exception_names(declare_text)
    cursors = _cursor_queries(declare_text)

    decls = Declarations()
    convert_declare_block(declare_text, issues, decls, masked)

    label = "routine_body"
    lines, labelled = convert_statements(
        body_text, issues, decls, parsed["kind"], cursors, declared, label, masked)

    body = "\n".join("  " + line for line in lines if line.strip())
    header_decls = decls.render("  ")
    opener = f"{label}: BEGIN" if labelled else "BEGIN"
    closer = f"END {label};" if labelled else "END;"

    name = quote_backtick(routine.name)
    params_clause = ", ".join(params)
    characteristics = _characteristics(masked.restore(body_text))

    if is_function:
        return_type = _mysql_type(parsed["return_type"] or "", issues, "return type: ") \
            if parsed["return_type"] else "TEXT"
        if not parsed["return_type"]:
            issues.append(ConversionIssue(
                "warning", "FUNCTION had no parsable RETURN type; defaulted to TEXT."))
        head = (f"CREATE FUNCTION {name}({params_clause})\n"
                f"RETURNS {return_type}\n{characteristics}\n{opener}")
    else:
        head = f"CREATE PROCEDURE {name}({params_clause})\n{characteristics}\n{opener}"

    pieces = [head]
    if header_decls:
        pieces.append(header_decls)
    if body.strip():
        pieces.append(body)
    pieces.append(closer)
    return masked.restore("\n".join(pieces)), issues


def convert_trigger(routine: Routine) -> Tuple[str, List[ConversionIssue]]:
    """One Oracle trigger becomes one MySQL trigger per event.

    MySQL allows exactly one event per trigger, so `BEFORE INSERT OR
    UPDATE` has to become two objects. Where the body asks which event
    fired -- `IF INSERTING THEN` -- the predicate is resolved per emitted
    trigger, since neither INSERTING nor its siblings exist in MySQL.
    """
    issues: List[ConversionIssue] = []
    masked = Masked(routine.source.strip())
    source = masked.text
    _flag_markers(routine.source, issues)

    declare_text, body_text = "", source
    begin_pos = find_top_level_begin(source)
    if source[:8].upper().startswith("DECLARE") and begin_pos is not None:
        declare_text = source[len("DECLARE"):begin_pos]
        body_text = source[begin_pos:]

    if not routine.row_level:
        issues.append(ConversionIssue(
            "error",
            f"Trigger {routine.name} is a statement-level trigger; MySQL only supports FOR EACH "
            f"ROW. It was emitted as a row-level trigger, which fires once per affected row -- "
            f"verify that is acceptable, or move the logic into the calling code."))

    table = routine.table_name
    if not table:
        issues.append(ConversionIssue(
            "error", "The trigger's target table was not captured; the DDL names UNKNOWN_TABLE."))
        table = "UNKNOWN_TABLE"

    events = [e.upper() for e in (routine.events or ["INSERT"])]
    timing = (routine.timing or "BEFORE").upper()
    if timing not in ("BEFORE", "AFTER"):
        issues.append(ConversionIssue(
            "error", f"Trigger timing '{timing}' has no MySQL equivalent; emitted as BEFORE."))
        timing = "BEFORE"
    if len(events) > 1:
        issues.append(ConversionIssue(
            "warning",
            f"Oracle's '{' OR '.join(events)}' fires one trigger for several events; MySQL allows "
            f"one event each, so {len(events)} triggers were emitted over the same body."))

    declared = _declared_exception_names(declare_text)
    cursors = _cursor_queries(declare_text)
    blocks = []
    for index, event in enumerate(events):
        decls = Declarations()
        # Issues are collected from the first event's pass only: the body
        # is the same each time round, so letting every pass append would
        # report each problem once per emitted trigger.
        event_issues = issues if index == 0 else []
        convert_declare_block(declare_text, event_issues, decls, masked)
        lines, labelled = convert_statements(
            _resolve_event_predicates(body_text, event), event_issues, decls,
            "TRIGGER", cursors, declared, "trigger_body", masked)
        body = "\n".join("  " + line for line in lines if line.strip())
        header_decls = decls.render("  ")

        name = routine.name if len(events) == 1 else f"{routine.name}_{event}"
        opener = "trigger_body: BEGIN" if labelled else "BEGIN"
        closer = "END trigger_body;" if labelled else "END;"
        pieces = [f"DROP TRIGGER IF EXISTS {quote_backtick(name)};",
                  f"CREATE TRIGGER {quote_backtick(name)}\n"
                  f"{timing} {event} ON {quote_backtick(table)}\n"
                  f"FOR EACH ROW\n{opener}"]
        if header_decls:
            pieces.append(header_decls)
        if body.strip():
            pieces.append(body)
        pieces.append(closer)
        blocks.append("\n".join(pieces))

    return masked.restore("\n\n".join(blocks)), issues


_EVENT_PREDICATES = ("INSERTING", "UPDATING", "DELETING")


def _resolve_event_predicates(text: str, event: str) -> str:
    """`IF INSERTING THEN` in a trigger emitted for the INSERT event
    becomes `IF TRUE THEN`, and `IF DELETING` becomes `IF FALSE`."""
    for predicate in _EVENT_PREDICATES:
        truth = "TRUE" if predicate[:-3].upper().startswith(event[:4].upper()) else "FALSE"
        text = re.sub(rf"\b{predicate}\b", truth, text, flags=re.IGNORECASE)
    return text


def convert_package_body(routine: Routine) -> Tuple[str, List[ConversionIssue]]:
    """Flatten a PACKAGE BODY into `<package>_<member>` routines.

    Each gets its own `DROP ... IF EXISTS` because MySQL has no `CREATE OR
    REPLACE PROCEDURE`, and the package's own DROP (emitted by
    ddl_generator._replace_prefix) names an object that never exists on a
    MySQL target.
    """
    issues: List[ConversionIssue] = []
    masked = Masked(routine.source)
    source = masked.text

    headers = list(re.finditer(r"\b(PROCEDURE|FUNCTION)\s+([A-Za-z_][\w$#]*)",
                               source, re.IGNORECASE))
    if not headers:
        issues.append(ConversionIssue(
            "error", "No PROCEDURE/FUNCTION members found in the package body."))
        return (f"-- MANUAL CONVERSION REQUIRED for PACKAGE BODY {routine.name}\n"
                f"/*\n{routine.source}\n*/"), issues

    chunks: List[str] = []
    for index, match in enumerate(headers):
        end = headers[index + 1].start() if index + 1 < len(headers) else len(source)
        chunks.append(source[match.start():end])

    # The final chunk carries the package body's own closing END, which
    # would otherwise be parsed as part of the last member and emitted as
    # a stray unmatched END.
    if chunks:
        last = chunks[-1]
        close = re.search(rf"END\s+{re.escape(routine.name)}\s*;\s*$|END\s*;\s*$",
                          last, re.IGNORECASE)
        if close:
            remainder = last[:close.start()]
            if re.search(r"\bEND\b", remainder, re.IGNORECASE):
                chunks[-1] = remainder

    parts = [f"-- Flattened from PACKAGE BODY {routine.name} into {len(chunks)} standalone "
             f"routine(s); MySQL has no package construct."]
    for chunk in chunks:
        member = re.match(r"^\s*(PROCEDURE|FUNCTION)\s+([A-Za-z_][\w$#]*)",
                          chunk, re.IGNORECASE)
        member_kind = member.group(1).upper() if member else "PROCEDURE"
        member_name = member.group(2) if member else "unknown_member"
        flat_name = f"{routine.name}_{member_name}"
        renamed = re.sub(rf"^(\s*(?:PROCEDURE|FUNCTION)\s+){re.escape(member_name)}\b",
                         rf"\g<1>{flat_name}", chunk, count=1, flags=re.IGNORECASE)
        pseudo = Routine(name=flat_name, schema=routine.schema, kind=member_kind,
                         source=masked.restore(renamed))
        ddl, member_issues = convert_procedure_or_function(pseudo)
        keyword = "FUNCTION" if member_kind == "FUNCTION" else "PROCEDURE"
        parts.append(f"DROP {keyword} IF EXISTS {quote_backtick(flat_name)};\n{ddl}")
        issues.extend(member_issues)

    issues.append(ConversionIssue(
        "warning",
        "Package-level variables and constants (if this package declared any) have no MySQL "
        "equivalent -- the flattened routines do not share state. Use a settings table or "
        "session variables if they need to."))
    return "\n\n".join(parts), issues


def convert_routine(routine: Routine, target_engine: str) -> Routine:
    """Convert one Oracle routine for a MySQL/MariaDB target, in place."""
    issues: List[ConversionIssue] = []
    if routine.kind == "TRIGGER":
        ddl, issues = convert_trigger(routine)
    elif routine.kind == "PACKAGE BODY":
        ddl, issues = convert_package_body(routine)
    elif routine.kind == "PACKAGE":
        ddl = (f"-- PACKAGE {routine.name} has no MySQL equivalent. Its members are converted "
               f"individually from the matching PACKAGE BODY, as {routine.name}_<member>. "
               f"Constants and types declared only in the spec must be moved by hand.")
        issues = [ConversionIssue(
            "warning",
            f"Package spec {routine.name}: MySQL has no package construct; only the package "
            f"body's members were converted.")]
    else:
        ddl, issues = convert_procedure_or_function(routine)

    routine.converted_source = ddl
    routine.issues = issues
    if any(i.severity == "error" for i in issues):
        routine.status = ConversionStatus.MANUAL
    elif any(i.severity == "warning" for i in issues):
        routine.status = ConversionStatus.AUTOMATIC_WITH_WARNINGS
    else:
        routine.status = ConversionStatus.AUTOMATIC

    from tgdatabridge.core.sql_translator import score_complexity
    routine.complexity_score = score_complexity(routine.source)[0]
    return routine
