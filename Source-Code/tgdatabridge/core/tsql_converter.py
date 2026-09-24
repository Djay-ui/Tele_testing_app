"""
Oracle PL/SQL -> Microsoft SQL Server T-SQL source-code converter.

This mirrors plsql_converter.py's "convert what's safe, flag the rest"
approach, but T-SQL differs from PL/pgSQL in several structural ways that
plain function-call rewriting can't paper over, so this module does more
work than the Postgres converter:

  * T-SQL has no THEN/END IF -- `IF ... THEN ... END IF;` becomes
    `IF ... BEGIN ... END`. There's no CASE *statement* either (only a CASE
    *expression*), so a genuine Oracle CASE statement (one that ends in
    `END CASE;`) has no safe rewrite and is flagged rather than guessed at.
  * T-SQL has no FOR loop at all -- both the numeric range form
    (`FOR i IN 1..10 LOOP`) and the implicit-cursor/row form
    (`FOR rec IN (SELECT ...) LOOP`) are rewritten: the numeric form
    becomes a counter + WHILE loop, and the row form becomes an explicit
    CURSOR/FETCH/WHILE @@FETCH_STATUS loop (only when a simple, explicit
    column list can be determined -- SELECT * or expression columns without
    an alias are flagged for manual conversion instead of guessed at).
  * Local variables are referenced bare in Oracle (`v_total := 0;`) but
    must be `@`-prefixed in T-SQL (`SET @v_total = 0;`); this converter
    tracks every declared variable/parameter/loop-counter name and prefixes
    its references throughout the body.
  * Postgres has native `EXCEPTION WHEN ... THEN` handlers; T-SQL only has
    TRY/CATCH, so an Oracle exception section is restructured into
    `BEGIN TRY ... END TRY BEGIN CATCH IF ERROR_NUMBER() = ... ... END CATCH`.
  * SQL Server triggers are AFTER/INSTEAD OF only (no BEFORE), and operate
    on statement-level `inserted`/`deleted` pseudo-tables rather than
    Oracle's row-level `:NEW`/`:OLD` -- a BEFORE trigger is flagged for
    manual rewrite rather than silently substituted with INSTEAD OF, and
    :NEW/:OLD references are rewritten to single-row `inserted`/`deleted`
    lookups with an explicit warning that this assumes single-row DML.
"""
from __future__ import annotations

import re
from typing import Callable, List, Optional, Tuple

from tgdatabridge.core import type_mapping
from tgdatabridge.core.nested_subprogram import extract_nested_subprograms, find_top_level_begin
from tgdatabridge.core.plsql_converter import (
    dbms_lob_getlength_replacer, dbms_lob_substr_replacer, is_simple_string_literal,
    parse_param, parse_routine_header, replace_dbms_random_value, split_top_level,
)
from tgdatabridge.core.schema_model import ConversionIssue, ConversionStatus, Routine
from tgdatabridge.utils.identifiers import quote_bracket

# ---------------------------------------------------------------- utilities


def _quote_sqlserver(identifier: str, schema: Optional[str] = None) -> str:
    """T-SQL's default collation is case-insensitive, so (unlike the
    Postgres converter, which lowercases and quotes) this case-preserves the
    identifier and brackets it, matching ddl_generator's SQL Server quoting
    convention."""
    ident = quote_bracket(identifier)
    return f"{quote_bracket(schema)}.{ident}" if schema else ident


_STRING_LITERAL_RE = re.compile(r"'(?:[^']|'')*'")


def _apply_outside_strings(text: str, func: Callable[[str], str]) -> str:
    """Apply `func` (a full-text transform) only to the portions of `text`
    outside single-quoted string literals, so rewrites never mangle literal
    string contents."""
    out: List[str] = []
    pos = 0
    for m in _STRING_LITERAL_RE.finditer(text):
        out.append(func(text[pos:m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(func(text[pos:]))
    return "".join(out)


def _indent(text: str, spaces: int = 2) -> str:
    pad = " " * spaces
    return "\n".join((pad + line if line.strip() else line) for line in text.splitlines())


def _convert_exit_continue(text: str) -> str:
    """EXIT / EXIT WHEN / CONTINUE / CONTINUE WHEN are valid inside any
    T-SQL WHILE loop (every Oracle loop form is rewritten to a WHILE loop),
    so BREAK/CONTINUE are direct equivalents. This must only run on text
    that has already been carved out as a specific loop's body by the
    block-structure scanners -- see the note in _builtin_rewrites."""
    def _rewrite(chunk: str) -> str:
        chunk = re.sub(r"\bEXIT\s+WHEN\s+(.+?);", r"IF \1 BREAK;", chunk, flags=re.IGNORECASE | re.DOTALL)
        chunk = re.sub(r"\bEXIT\s*;", "BREAK;", chunk, flags=re.IGNORECASE)
        chunk = re.sub(r"\bCONTINUE\s+WHEN\s+(.+?);", r"IF \1 CONTINUE;", chunk, flags=re.IGNORECASE | re.DOTALL)
        chunk = re.sub(r"\bCONTINUE\s*;", "CONTINUE;", chunk, flags=re.IGNORECASE)
        return chunk
    return _apply_outside_strings(text, _rewrite)


# ------------------------------------------------------------- body rewrites

_EXCEPTION_ERROR_NUMBER_MAP = {
    "DUP_VAL_ON_INDEX": "2627",
    "ZERO_DIVIDE": "8134",
}
_UNMAPPABLE_EXCEPTIONS = {
    "VALUE_ERROR", "INVALID_CURSOR", "LOGIN_DENIED", "NOT_LOGGED_ON",
    "PROGRAM_ERROR", "STORAGE_ERROR", "TIMEOUT_ON_RESOURCE",
    "ROWTYPE_MISMATCH", "SUBSCRIPT_BEYOND_COUNT", "SUBSCRIPT_OUTSIDE_LIMIT",
    "COLLECTION_IS_NULL", "CURSOR_ALREADY_OPEN", "INVALID_NUMBER",
}

_MANUAL_MARKERS = [
    (re.compile(r"\bDBMS_(?!OUTPUT\.PUT_LINE)[A-Z_]+", re.IGNORECASE), "Uses an Oracle DBMS_* package with no direct SQL Server equivalent."),
    (re.compile(r"\bUTL_[A-Z_]+", re.IGNORECASE), "Uses an Oracle UTL_* package with no direct SQL Server equivalent."),
    (re.compile(r"\bBULK COLLECT\b", re.IGNORECASE), "BULK COLLECT has no direct equivalent; rewrite using a table variable or a loop."),
    (re.compile(r"\bFORALL\b", re.IGNORECASE), "FORALL has no direct equivalent; rewrite as a loop or a set-based statement."),
    (re.compile(r"\bCONNECT BY\b", re.IGNORECASE), "Hierarchical query (CONNECT BY) must be rewritten as a recursive CTE (WITH ... AS)."),
    (re.compile(r"\bAUTONOMOUS_TRANSACTION\b", re.IGNORECASE), "Autonomous transactions have no equivalent; requires a separate connection/linked server."),
    (re.compile(r"\bEND\s+CASE\b", re.IGNORECASE), "CASE *statement* (ending in END CASE) has no T-SQL statement equivalent; rewrite as IF / ELSE IF."),
]


def _convert_declared_type(type_text: str) -> Tuple[str, List[ConversionIssue]]:
    """Map a declared Oracle type to its T-SQL equivalent. Anchored
    %TYPE/%ROWTYPE references have no T-SQL equivalent (T-SQL has no way to
    anchor a variable's type to a column's or row's type) so these are
    flagged and defaulted to SQL_VARIANT rather than guessed at."""
    if re.search(r"%ROWTYPE", type_text, re.IGNORECASE):
        return "SQL_VARIANT", [ConversionIssue(
            "error",
            f"'{type_text}' anchors to a row type (%ROWTYPE), which T-SQL has no equivalent for; "
            "declare explicit columns/variables instead of a row-shaped variable.",
        )]
    if re.search(r"%TYPE", type_text, re.IGNORECASE):
        return "SQL_VARIANT", [ConversionIssue(
            "warning",
            f"'{type_text}' anchors to a column's type (%TYPE), which T-SQL has no equivalent for; "
            "defaulted to SQL_VARIANT -- replace with the column's actual data type for best results.",
        )]
    return type_mapping.to_sqlserver(type_text)


# Oracle's implicit-cursor `SELECT col[, col...] INTO var[, var...] FROM ...`
# has no T-SQL equivalent syntax -- plain `SELECT ... INTO x FROM ...` means
# something entirely different in T-SQL (create table x from the result set).
# The T-SQL way to assign a query result into variables is
# `SELECT @var = col[, @var2 = col2...] FROM ...`, so this rewrites the
# clause shape (variable @-prefixing happens later, in the same pass that
# prefixes every other declared-variable reference).
# The [^;]+? captures (rather than a DOTALL .+?) are deliberate and load-
# bearing: an unbounded .+? can walk straight past this statement's own
# terminating ';' and latch onto an unrelated INTO/FROM much later in the
# text (e.g. a `FETCH NEXT FROM cursor INTO @var;` emitted by the row/cursor
# FOR-loop converter, which runs before this and can leave exactly that
# text sitting right after a real SELECT...INTO...FROM statement) -- Oracle
# statements are always ';'-terminated, so cols/vars can never legitimately
# contain one, making this a safe, tighter scope rather than a semantic
# restriction. [^;] still matches newlines, so multi-line SELECT...INTO
# statements are unaffected.
_SELECT_INTO_RE = re.compile(
    r"\bSELECT\b(?P<cols>[^;]+?)\bINTO\b(?P<vars>[^;]+?)\bFROM\b", re.IGNORECASE
)


def _rewrite_select_into(text: str, issues: List[ConversionIssue]) -> str:
    def _do(m: re.Match) -> str:
        cols_text, vars_text = m.group("cols").strip(), m.group("vars").strip()
        if re.search(r"\bBULK\s+COLLECT\b", cols_text + vars_text, re.IGNORECASE):
            return m.group(0)  # left for the BULK COLLECT manual-conversion marker instead
        cols = split_top_level(cols_text, ",")
        vars_ = split_top_level(vars_text, ",")
        if not cols or len(cols) != len(vars_):
            issues.append(ConversionIssue(
                "error",
                f"MANUAL CONVERSION REQUIRED: 'SELECT {cols_text} INTO {vars_text} FROM' has a mismatched "
                "column/variable count and could not be rewritten to T-SQL's `SELECT @var = col FROM ...` form.",
            ))
            return m.group(0)
        assignments = ", ".join(f"{v.strip()} = {c.strip()}" for c, v in zip(cols, vars_))
        return f"SELECT {assignments} FROM"

    return _SELECT_INTO_RE.sub(_do, text)


def _builtin_rewrites(text: str) -> Tuple[str, List[ConversionIssue]]:
    """Rewrite Oracle builtin functions/keywords with no structural impact
    (i.e. everything except control flow, loops, and exception handling,
    which are handled separately since they change the statement shape)."""
    issues: List[ConversionIssue] = []

    def _rewrite(chunk: str) -> str:
        chunk = re.sub(r"\bSYSDATE\b", "GETDATE()", chunk, flags=re.IGNORECASE)
        chunk = re.sub(r"\bSYSTIMESTAMP\b", "SYSDATETIME()", chunk, flags=re.IGNORECASE)
        chunk = re.sub(r"\bNVL\s*\(", "ISNULL(", chunk, flags=re.IGNORECASE)
        chunk = re.sub(r"\bEXECUTE\s+IMMEDIATE\b", "EXEC", chunk, flags=re.IGNORECASE)
        chunk = re.sub(r"\bFROM\s+DUAL\b", "", chunk, flags=re.IGNORECASE)
        # SAVEPOINT / ROLLBACK TO -- a real, mechanical keyword rename, unlike
        # the PostgreSQL target (see plsql_converter.py's own handling for why
        # Postgres has no equivalent statement at all): T-SQL supports the
        # same mid-transaction checkpoint idiom natively, just spelled
        # differently -- SAVE TRANSACTION name; / ROLLBACK TRANSACTION name;
        # -- and, unlike Postgres, this works the same whether it appears in
        # the main body or inside a TRY/CATCH handler, so no handler-position
        # heuristic is needed here. ROLLBACK TO must be rewritten *before*
        # SAVEPOINT: Oracle's optional-keyword form `ROLLBACK TO SAVEPOINT
        # name;` contains a "SAVEPOINT name;" tail that the SAVEPOINT rule
        # would otherwise match and rewrite on its own first, leaving behind
        # "ROLLBACK TO SAVE TRANSACTION name;" -- never matching the
        # ROLLBACK TO rule at all.
        chunk = re.sub(r"\bROLLBACK\s+TO\s+(?:SAVEPOINT\s+)?([A-Za-z_][\w$#]*)\s*;",
                        r"ROLLBACK TRANSACTION \1;", chunk, flags=re.IGNORECASE)
        chunk = re.sub(r"\bSAVEPOINT\s+([A-Za-z_][\w$#]*)\s*;",
                        r"SAVE TRANSACTION \1;", chunk, flags=re.IGNORECASE)
        chunk = _rewrite_select_into(chunk, issues)
        # Oracle's `:=` assignment operator isn't valid T-SQL at all -- a
        # bare `x = y;` statement (without SET) is a syntax error in T-SQL,
        # so every assignment statement needs a `SET` keyword inserted.
        chunk = re.sub(
            r"\b([A-Za-z_][\w$#]*(?:\.[A-Za-z_][\w$#]*)?)\s*:=\s*",
            r"SET \1 = ", chunk, flags=re.IGNORECASE,
        )
        return chunk

    text = _apply_outside_strings(text, _rewrite)

    from tgdatabridge.core.plsql_converter import transform_function_calls

    def _nvl2(args: List[str]) -> str:
        if len(args) == 3:
            return f"CASE WHEN ({args[0]}) IS NOT NULL THEN {args[1]} ELSE {args[2]} END"
        issues.append(ConversionIssue("error", f"NVL2 with {len(args)} arguments could not be converted automatically."))
        return f"NVL2({', '.join(args)})"

    text, _n = transform_function_calls(text, "NVL2", _nvl2)

    def _decode(args: List[str]) -> str:
        if len(args) < 3:
            issues.append(ConversionIssue("error", f"DECODE with {len(args)} arguments could not be converted automatically."))
            return f"DECODE({', '.join(args)})"
        expr, rest = args[0], args[1:]
        lines = [f"CASE {expr}"]
        pair_count = len(rest) // 2
        for k in range(pair_count):
            lines.append(f"  WHEN {rest[2*k]} THEN {rest[2*k+1]}")
        if len(rest) % 2 == 1:
            lines.append(f"  ELSE {rest[-1]}")
        lines.append("END")
        issues.append(ConversionIssue(
            "warning",
            "DECODE was converted to CASE; note DECODE treats NULL = NULL as a match while CASE does not "
            "-- review any branch that relies on that Oracle-specific NULL-matching behavior.",
        ))
        return "\n".join(lines)

    text, _n = transform_function_calls(text, "DECODE", _decode)

    def _instr(args: List[str]) -> str:
        # Oracle INSTR(str, substr[, start[, occurrence]]) -- CHARINDEX takes
        # (substr, str[, start]) with no occurrence argument, so the first
        # two arguments swap order and a 4-argument call is flagged.
        if len(args) == 2:
            return f"CHARINDEX({args[1]}, {args[0]})"
        if len(args) == 3:
            return f"CHARINDEX({args[1]}, {args[0]}, {args[2]})"
        issues.append(ConversionIssue(
            "warning",
            f"INSTR with {len(args)} arguments (occurrence) has no direct CHARINDEX() equivalent; "
            "left as INSTR() -- review and rewrite manually.",
        ))
        return f"INSTR({', '.join(args)})"

    text, _n = transform_function_calls(text, "INSTR", _instr)

    def _put_line(args: List[str]) -> str:
        if len(args) == 1:
            return f"PRINT ({args[0]})"
        issues.append(ConversionIssue("warning", "DBMS_OUTPUT.PUT_LINE with unexpected argument count left unconverted."))
        return f"DBMS_OUTPUT.PUT_LINE({', '.join(args)})"

    text, n = transform_function_calls(text, r"DBMS_OUTPUT\.PUT_LINE", _put_line)
    if n:
        issues.append(ConversionIssue("info", f"Converted {n} DBMS_OUTPUT.PUT_LINE call(s) to PRINT."))

    # DBMS_LOB.GETLENGTH(lob) -> LEN(lob); DBMS_LOB.SUBSTR(lob, amount, offset) -> SUBSTRING(lob, offset, amount)
    text, _n = transform_function_calls(
        text, r"DBMS_LOB\.GETLENGTH",
        dbms_lob_getlength_replacer(
            "LEN", issues,
            caveat="Converted DBMS_LOB.GETLENGTH(...) to LEN(...); note T-SQL's LEN() trims trailing "
                   "spaces, unlike Oracle's DBMS_LOB.GETLENGTH -- use DATALENGTH(...) instead if trailing "
                   "whitespace must be counted.",
        ),
    )
    text, _n = transform_function_calls(text, r"DBMS_LOB\.SUBSTR", dbms_lob_substr_replacer("SUBSTRING", issues))

    # DBMS_RANDOM.VALUE (no-arg form) -> RAND()
    text = replace_dbms_random_value(text, issues, "RAND()")

    def _raise_app_error(args: List[str]) -> str:
        if len(args) >= 2:
            if not is_simple_string_literal(args[1]):
                # T-SQL's THROW only accepts a literal or a variable for its
                # message argument, never an inline expression -- a message
                # built with Oracle's 'text' || variable concatenation would
                # need a local variable declared and assigned first, which is
                # real surgery this mechanical substitution can't safely do
                # in general (see is_simple_string_literal's docstring).
                # Left unconverted and flagged rather than emitting
                # THROW 50000, 'text'||variable, 1, which T-SQL rejects
                # outright (and `+` wouldn't fix it either -- it's the
                # inline expression itself THROW disallows, not the operator).
                # The error-code-dropped warning below doesn't apply here --
                # nothing was actually converted, so nothing was dropped.
                issues.append(ConversionIssue(
                    "error",
                    "RAISE_APPLICATION_ERROR's message is a computed expression (not a plain string "
                    "literal), which T-SQL's THROW statement cannot take inline -- assign it to a "
                    "local variable first, then THROW 50000, @that_variable, 1.",
                ))
                return f"RAISE_APPLICATION_ERROR({', '.join(args)})"
            issues.append(ConversionIssue(
                "warning",
                f"RAISE_APPLICATION_ERROR error code {args[0]} was dropped; T-SQL THROW requires a user "
                "error number >= 50000 -- add one (and register it with sys.sp_addmessage if a specific "
                "number must be preserved) if the caller depends on it.",
            ))
            return f"THROW 50000, {args[1]}, 1"
        issues.append(ConversionIssue("error", "RAISE_APPLICATION_ERROR could not be parsed."))
        return f"RAISE_APPLICATION_ERROR({', '.join(args)})"

    text, _n = transform_function_calls(text, "RAISE_APPLICATION_ERROR", _raise_app_error)

    def _seq_ref(m: re.Match, kind: str) -> str:
        seq_name = m.group(1)
        if kind == "NEXTVAL":
            return f"NEXT VALUE FOR {_quote_sqlserver(seq_name)}"
        issues.append(ConversionIssue(
            "error",
            f"'{seq_name}.CURRVAL' has no T-SQL equivalent -- native SEQUENCE objects have no session-level "
            "\"last value\" function; capture the value from NEXT VALUE FOR into a variable instead.",
        ))
        return f"{seq_name}.CURRVAL"

    text = re.sub(r"\b([A-Za-z_][\w$#]*)\.NEXTVAL\b",
                  lambda m: _seq_ref(m, "NEXTVAL"), text, flags=re.IGNORECASE)
    text = re.sub(r"\b([A-Za-z_][\w$#]*)\.CURRVAL\b",
                  lambda m: _seq_ref(m, "CURRVAL"), text, flags=re.IGNORECASE)

    # NOTE: EXIT/EXIT WHEN/CONTINUE/CONTINUE WHEN are deliberately *not*
    # rewritten here (see _convert_exit_continue) -- doing it this early
    # would inject a bare "IF ... BREAK;" with no matching "END IF" into the
    # text before the BEGIN/EXCEPTION/END and IF/END-IF block-structure
    # scanners run, corrupting their depth tracking. It's applied later,
    # once a loop's body has already been carved out by the block scanners.

    for pattern, message in _MANUAL_MARKERS:
        if pattern.search(text):
            issues.append(ConversionIssue("error", message))

    return text, issues


# --------------------------------------------------------- implicit cursors

_ROW_FOR_HEADER_RE = re.compile(
    r"\bFOR\s+([A-Za-z_][\w$#]*)\s+IN\s+", re.IGNORECASE
)
_NUMERIC_RANGE_RE = re.compile(
    r"^(REVERSE\s+)?(.+?)\.\.(.+)$", re.IGNORECASE | re.DOTALL
)


def _find_matching_end_loop(text: str, start: int) -> Optional[Tuple[int, int]]:
    """text[start:] begins right after the LOOP keyword that opens the loop
    being matched. Returns the (start, end) span of the matching END LOOP,
    tracking nested LOOP/END LOOP pairs so an inner loop's own END LOOP
    doesn't prematurely close the outer one."""
    token_re = re.compile(r"\bLOOP\b|\bEND\s+LOOP\b", re.IGNORECASE)
    depth = 0

    def _scan(s: str, pos: int):
        nonlocal depth
        for m in token_re.finditer(s, pos):
            word = re.sub(r"\s+", " ", m.group(0).upper())
            if word == "LOOP":
                depth += 1
            else:
                if depth == 0:
                    return m.start(), m.end()
                depth -= 1
        return None

    return _apply_outside_strings_scan(text, start, _scan)


def _apply_outside_strings_scan(text: str, start: int, scan_func):
    """Like _apply_outside_strings, but for a positional scan (rather than a
    full-text rewrite): runs scan_func only over the code portions after
    `start`, translating any returned span back to original-text offsets."""
    # Build a version of text with string-literal contents blanked out
    # (same length, so offsets stay valid) so keyword scans never trigger on
    # text that happens to appear inside a string literal.
    blanked = list(text)
    for m in _STRING_LITERAL_RE.finditer(text):
        for i in range(m.start(), m.end()):
            if blanked[i] not in ("\n",):
                blanked[i] = "x"
    blanked_text = "".join(blanked)
    return scan_func(blanked_text, start)


_CTE_HEADER_RE = re.compile(
    r"^\s*WITH\s+(?:RECURSIVE\s+)?[A-Za-z_][\w$#]*\s*\(([^)]*)\)\s+AS\s*\(",
    re.IGNORECASE,
)


def _extract_select_columns(select_text: str) -> Optional[List[str]]:
    """Return the simple, explicit output column/alias names of a SELECT
    statement's column list, or None if the list isn't simple enough to
    safely auto-convert (SELECT *, or an unaliased expression column).

    A `select_text` that starts with a `WITH <cte> (col1, col2, ...) AS (`
    header -- i.e. connect_by_rewriter.py's own recursive-CTE rewrite of a
    CONNECT BY query -- is a special case: its final SELECT is always just
    `SELECT <same column list> FROM <cte>`, so the already-known-safe
    column list is read directly out of the CTE's own declared column
    list rather than re-parsing the trailing SELECT (which the general
    logic below can't do, since it doesn't start with the literal keyword
    SELECT)."""
    cte_m = _CTE_HEADER_RE.match(select_text)
    if cte_m:
        cols = [c.strip() for c in cte_m.group(1).split(",")]
        return cols if all(cols) else None

    m = re.match(r"^\s*SELECT\b", select_text, re.IGNORECASE)
    if not m:
        return None
    # find the top-level FROM (paren-depth 0) that ends the column list
    depth = 0
    in_string = False
    from_pos = None
    i = m.end()
    while i < len(select_text):
        ch = select_text[i]
        if ch == "'":
            in_string = not in_string
        elif not in_string:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif depth == 0 and select_text[i:i+4].upper() == "FROM" and (
                i == 0 or not select_text[i-1].isalnum()
            ) and (i + 4 >= len(select_text) or not select_text[i+4].isalnum()):
                from_pos = i
                break
        i += 1
    if from_pos is None:
        return None
    col_list_text = select_text[m.end():from_pos].strip()
    if col_list_text == "*":
        return None
    cols = split_top_level(col_list_text, ",")
    names: List[str] = []
    for col in cols:
        col = col.strip()
        if col == "*" or col.endswith(".*"):
            return None
        alias_m = re.search(r"\bAS\s+([A-Za-z_][\w$#]*)\s*$", col, re.IGNORECASE)
        if alias_m:
            names.append(alias_m.group(1))
            continue
        bare_alias_m = re.match(r"^[A-Za-z_][\w$#.]*\s+([A-Za-z_][\w$#]*)\s*$", col)
        if bare_alias_m and " " in col.strip():
            names.append(bare_alias_m.group(1))
            continue
        # a bare column or table.column reference with no function call/operator
        if re.match(r"^[A-Za-z_][\w$#]*(\.[A-Za-z_][\w$#]*)?$", col):
            names.append(col.split(".")[-1])
            continue
        return None  # expression column with no alias -- can't name the output
    if not names:
        return None
    return names


def _convert_row_for_loops(
    body_text: str, declare_text: str, issues: List[ConversionIssue]
) -> str:
    """Rewrite every Oracle implicit-cursor/row FOR loop
    (`FOR rec IN (SELECT ...) LOOP`, `FOR rec IN SELECT ... LOOP`, or
    `FOR rec IN cursor_name LOOP`) into an explicit T-SQL
    CURSOR/OPEN/FETCH/WHILE @@FETCH_STATUS/CLOSE/DEALLOCATE loop, only when
    the SELECT's column list is simple and explicit enough to name each
    fetched value; anything else (SELECT *, expression columns with no
    alias) is left in place with a MANUAL CONVERSION REQUIRED marker rather
    than guessed at."""
    out: List[str] = []
    pos = 0
    while True:
        m = _ROW_FOR_HEADER_RE.search(body_text, pos)
        if not m:
            out.append(body_text[pos:])
            break

        loop_var = m.group(1)
        after_in = m.end()
        rest = body_text[after_in:]

        # is this a numeric range loop ("FOR i IN 1..10 LOOP" / "FOR i IN
        # REVERSE 1..10 LOOP")? Those are handled by the control-flow
        # converter, not here -- detect and skip past this match entirely.
        loop_kw_m = re.search(r"\bLOOP\b", rest, re.IGNORECASE)
        if loop_kw_m and _NUMERIC_RANGE_RE.match(rest[: loop_kw_m.start()].strip()):
            out.append(body_text[pos:after_in])
            pos = after_in
            continue

        select_text = None
        cursor_name_ref = None
        end_of_header = None

        stripped = rest.lstrip()
        leading_ws = len(rest) - len(stripped)
        if stripped.startswith("("):
            depth = 1
            i = leading_ws + 1
            in_string = False
            while i < len(rest) and depth > 0:
                ch = rest[i]
                if ch == "'":
                    in_string = not in_string
                elif not in_string:
                    if ch == "(":
                        depth += 1
                    elif ch == ")":
                        depth -= 1
                i += 1
            select_text = rest[leading_ws + 1 : i - 1].strip()
            end_of_header = i
        elif re.match(r"^SELECT\b", stripped, re.IGNORECASE):
            if not loop_kw_m:
                out.append(body_text[pos:after_in])
                pos = after_in
                continue
            select_text = stripped[: loop_kw_m.start() - leading_ws].strip()
            end_of_header = leading_ws + (loop_kw_m.start() - leading_ws)
        else:
            name_m = re.match(r"^([A-Za-z_][\w$#]*)", stripped)
            if not name_m:
                out.append(body_text[pos:after_in])
                pos = after_in
                continue
            cursor_name_ref = name_m.group(1)
            end_of_header = leading_ws + name_m.end()

        # advance to (and past) the LOOP keyword that starts the loop body
        after_header = rest[end_of_header:]
        loop_m = re.match(r"^\s*LOOP\b", after_header, re.IGNORECASE)
        if not loop_m:
            out.append(body_text[pos:after_in])
            pos = after_in
            continue
        loop_body_start_in_rest = end_of_header + loop_m.end()

        end_span = _find_matching_end_loop(body_text, after_in + loop_body_start_in_rest)
        if end_span is None:
            out.append(body_text[pos:after_in])
            pos = after_in
            continue
        end_loop_start, end_loop_end = end_span
        raw_loop_body = body_text[after_in + loop_body_start_in_rest : end_loop_start]
        # trailing ';' after END LOOP
        stmt_end = end_loop_end
        semi_m = re.match(r"\s*;", body_text[end_loop_end:])
        if semi_m:
            stmt_end = end_loop_end + semi_m.end()

        if cursor_name_ref is not None:
            cursor_m = re.search(
                rf"\bCURSOR\s+{re.escape(cursor_name_ref)}\s*(\([^)]*\))?\s+IS\s+(.*?);",
                declare_text, re.IGNORECASE | re.DOTALL,
            )
            if cursor_m:
                select_text = cursor_m.group(2).strip()

        columns = _extract_select_columns(select_text) if select_text else None

        if columns is None:
            issues.append(ConversionIssue(
                "error",
                f"MANUAL CONVERSION REQUIRED: implicit-cursor FOR loop '{loop_var}' uses SELECT * or an "
                "unaliased expression column, so its fetched columns can't be named automatically; give "
                "every selected column an explicit alias and re-run, or rewrite this loop by hand as an "
                "explicit CURSOR/FETCH/WHILE @@FETCH_STATUS loop.",
            ))
            out.append(body_text[pos:m.start()])
            out.append(
                f"\n  /* MANUAL CONVERSION REQUIRED -- original Oracle loop follows:\n"
                f"{body_text[m.start(): stmt_end]}\n  */\n"
            )
            pos = stmt_end
            continue

        col_vars = [f"@{c}" for c in columns]
        cursor_ident = _quote_sqlserver(f"{loop_var}_cursor") if cursor_name_ref is None else cursor_name_ref

        converted_inner, inner_issues = convert_body(raw_loop_body, declare_text=declare_text)
        issues.extend(inner_issues)

        # replace <loop_var>.<col> references with @<col>
        converted_inner = re.sub(
            rf"\b{re.escape(loop_var)}\.([A-Za-z_][\w$#]*)\b",
            lambda mm: (f"@{mm.group(1)}" if mm.group(1).lower() in [c.lower() for c in columns] else mm.group(0)),
            converted_inner, flags=re.IGNORECASE,
        )

        decl_vars = ", ".join(f"{v} SQL_VARIANT" for v in col_vars)
        fetch_list = ", ".join(col_vars)

        pieces = [f"DECLARE {decl_vars};"]
        if cursor_name_ref is None:
            pieces.append(f"DECLARE {cursor_ident} CURSOR LOCAL FAST_FORWARD FOR\n  {select_text};")
        pieces.append(f"OPEN {cursor_ident};")
        pieces.append(f"FETCH NEXT FROM {cursor_ident} INTO {fetch_list};")
        pieces.append("WHILE @@FETCH_STATUS = 0")
        pieces.append("BEGIN")
        pieces.append(_indent(converted_inner))
        pieces.append(_indent(f"FETCH NEXT FROM {cursor_ident} INTO {fetch_list};"))
        pieces.append("END")
        pieces.append(f"CLOSE {cursor_ident};")
        pieces.append(f"DEALLOCATE {cursor_ident};")

        issues.append(ConversionIssue(
            "info",
            f"Converted implicit-cursor FOR loop '{loop_var}' into an explicit CURSOR/FETCH/WHILE loop; "
            f"fetched column(s) {', '.join(columns)} were declared as SQL_VARIANT -- tighten these to the "
            "actual column types for best performance and type safety.",
        ))

        out.append(body_text[pos:m.start()])
        out.append("\n" + "\n".join(pieces) + "\n")
        pos = stmt_end

    return "".join(out)


# ------------------------------------------------------------- control flow

_CTRL_OPENER_RE = re.compile(r"\bIF\b|\bWHILE\b|\bFOR\b|\bLOOP\b|\bBEGIN\b", re.IGNORECASE)


def _find_matching_end_if(text: str, start: int) -> Optional[dict]:
    """text[start:] begins right after the top-level IF keyword's condition
    start. Returns a dict describing the THEN/ELSIF/ELSE marker positions
    (all at this IF's own nesting depth) and the matching END IF span, or
    None if malformed / not found."""
    token_re = re.compile(r"\bIF\b|\bEND\s+IF\b|\bTHEN\b|\bELSIF\b|\bELSE\b", re.IGNORECASE)
    depth = 0
    markers: List[Tuple[str, int, int]] = []
    end_if_span = None

    def _scan(s: str, pos: int):
        nonlocal depth, markers, end_if_span
        for m in token_re.finditer(s, pos):
            word = re.sub(r"\s+", " ", m.group(0).upper())
            if word == "IF":
                depth += 1
                continue
            if word == "END IF":
                if depth == 0:
                    end_if_span = (m.start(), m.end())
                    return
                depth -= 1
                continue
            if depth == 0 and word in ("THEN", "ELSIF", "ELSE"):
                markers.append((word, m.start(), m.end()))

    _apply_outside_strings_scan(text, start, _scan)
    if end_if_span is None or not markers or markers[0][0] != "THEN":
        return None
    return {"markers": markers, "end_if_start": end_if_span[0], "end_if_end": end_if_span[1]}


def _convert_if_statement(text: str, cond_start: int) -> Optional[Tuple[str, int, List[ConversionIssue]]]:
    """text[cond_start:] begins right after the top-level 'IF' keyword.
    Returns (converted_tsql, position_just_after_the_statement's_trailing_';',
    issues), or None if the IF couldn't be parsed (caller falls back to a
    MANUAL marker)."""
    parsed = _find_matching_end_if(text, cond_start)
    if parsed is None:
        return None
    markers = parsed["markers"]
    issues: List[ConversionIssue] = []

    branches: List[Tuple[Optional[str], str]] = []
    current_cond = text[cond_start: markers[0][1]].strip()
    body_start = markers[0][2]
    i = 1
    while i < len(markers):
        kind, mstart, mend = markers[i]
        if kind == "ELSIF":
            branches.append((current_cond, text[body_start:mstart]))
            if i + 1 >= len(markers) or markers[i + 1][0] != "THEN":
                return None
            current_cond = text[mend: markers[i + 1][1]].strip()
            body_start = markers[i + 1][2]
            i += 2
            continue
        if kind == "ELSE":
            branches.append((current_cond, text[body_start:mstart]))
            current_cond = None
            body_start = mend
            i += 1
            continue
        i += 1
    branches.append((current_cond, text[body_start: parsed["end_if_start"]]))

    parts: List[str] = []
    for idx, (cond, body) in enumerate(branches):
        conv_body, sub_issues = _convert_control_flow(body)
        issues.extend(sub_issues)
        if cond is not None:
            keyword = "IF" if idx == 0 else "ELSE IF"
            parts.append(f"{keyword} {cond}\nBEGIN\n{_indent(conv_body)}\nEND")
        else:
            parts.append(f"ELSE\nBEGIN\n{_indent(conv_body)}\nEND")

    end_pos = parsed["end_if_end"]
    semi_m = re.match(r"\s*;", text[end_pos:])
    if semi_m:
        end_pos += semi_m.end()

    return "\n".join(parts), end_pos, issues


def _convert_loop_construct(text: str, kw_start: int, kw_word: str) -> Optional[Tuple[str, int, List[ConversionIssue]]]:
    """text[kw_start:] begins at a WHILE/FOR/LOOP keyword that opens a loop.
    Returns (converted_tsql, position_just_after_the_loop's_trailing_';',
    issues), or None if malformed."""
    issues: List[ConversionIssue] = []

    if kw_word == "LOOP":
        loop_kw_end = kw_start + len("LOOP")
        header_line = None
    else:
        loop_m = re.search(r"\bLOOP\b", text[kw_start:], re.IGNORECASE)
        if not loop_m:
            return None
        header_line = text[kw_start: kw_start + loop_m.start()].strip()
        loop_kw_end = kw_start + loop_m.end()

    end_span = _find_matching_end_loop(text, loop_kw_end)
    if end_span is None:
        return None
    end_start, end_end = end_span
    raw_body = text[loop_kw_end:end_start]
    end_pos = end_end
    semi_m = re.match(r"\s*;", text[end_pos:])
    if semi_m:
        end_pos += semi_m.end()

    conv_body, sub_issues = _convert_control_flow(raw_body)
    issues.extend(sub_issues)
    # EXIT/CONTINUE (WHEN or bare) are only rewritten now, after this loop's
    # body has already been carved out and recursively converted -- see the
    # note in _builtin_rewrites for why doing this any earlier corrupts the
    # block-structure scanners.
    conv_body = _convert_exit_continue(conv_body)

    if kw_word == "LOOP":
        return f"WHILE 1 = 1\nBEGIN\n{_indent(conv_body)}\nEND", end_pos, issues

    if kw_word == "WHILE":
        cond = header_line[len("WHILE"):].strip() if header_line.upper().startswith("WHILE") else header_line
        return f"WHILE {cond}\nBEGIN\n{_indent(conv_body)}\nEND", end_pos, issues

    # FOR <var> IN [REVERSE] low..high  (numeric range loop; row/cursor FOR
    # loops are already removed by _convert_row_for_loops before this runs)
    for_m = re.match(
        r"^FOR\s+([A-Za-z_][\w$#]*)\s+IN\s+(REVERSE\s+)?(.+?)\.\.(.+)$",
        header_line, re.IGNORECASE | re.DOTALL,
    )
    if not for_m:
        issues.append(ConversionIssue(
            "error",
            f"MANUAL CONVERSION REQUIRED: could not parse FOR-loop header '{header_line}'.",
        ))
        return f"/* MANUAL CONVERSION REQUIRED: {header_line} */\n{conv_body}", end_pos, issues

    var, reverse, low, high = for_m.groups()
    reverse = bool(reverse)
    var_ref = f"@{var}"
    cmp_op = ">=" if reverse else "<="
    step = "- 1" if reverse else "+ 1"
    return (
        f"DECLARE {var_ref} INT = {low.strip()};\n"
        f"WHILE {var_ref} {cmp_op} {high.strip()}\n"
        f"BEGIN\n{_indent(conv_body)}\n{_indent(f'SET {var_ref} = {var_ref} {step};')}\nEND"
    ), end_pos, issues


def _split_begin_exception_end(text: str, kw_start: int) -> Optional[Tuple[str, int, List[ConversionIssue]]]:
    """text[kw_start:] begins at a 'BEGIN' keyword. Finds its matching
    top-level EXCEPTION (if any) and END, converts the main/handler text via
    _assemble_try_catch, and returns (inner_content, position_just_after_the
    _block's_trailing_';', issues) -- WITHOUT re-wrapping in BEGIN/END, so
    callers that need to combine the result with their own DECLARE
    statements (routine bodies) can do so, while callers that just need a
    self-contained nested block (_convert_begin_block) add the wrap
    themselves."""
    body_start = kw_start + len("BEGIN")
    # BEGIN/CASE/IF/LOOP are all tracked as depth-incrementing "openers" here
    # (not just nested BEGIN blocks) so that a bare `END` closing a CASE
    # *expression*, or an `END IF`/`END LOOP` closing an IF/LOOP construct
    # nested directly in this block, is correctly skipped over rather than
    # being mistaken for this block's own closing END -- see the identical
    # technique (and rationale) in _split_exception_clauses.
    token_re = re.compile(
        r"\bBEGIN\b|\bCASE\b|\bIF\b|\bLOOP\b|\bEND\s+IF\b|\bEND\s+CASE\b|"
        r"\bEND\s+LOOP\b|\bEND\b|\bEXCEPTION\b",
        re.IGNORECASE,
    )
    depth = 0
    exc_start = None
    end_span = None

    def _scan(s: str, pos: int):
        nonlocal depth, exc_start, end_span
        for m in token_re.finditer(s, pos):
            word = re.sub(r"\s+", " ", m.group(0).upper())
            if word in ("BEGIN", "CASE", "IF", "LOOP"):
                depth += 1
                continue
            if word == "EXCEPTION" and depth == 0:
                exc_start = m.start()
                continue
            if word in ("END IF", "END CASE", "END LOOP"):
                depth -= 1
                continue
            if word == "END":
                if depth == 0:
                    end_span = (m.start(), m.end())
                    return
                depth -= 1

    _apply_outside_strings_scan(text, body_start, _scan)
    if end_span is None:
        return None
    end_start, end_end = end_span
    main_end = exc_start if exc_start is not None else end_start
    main_text = text[body_start:main_end]
    exc_text = text[exc_start + len("EXCEPTION"): end_start] if exc_start is not None else None

    inner, issues = _assemble_try_catch(main_text, exc_text)
    end_pos = end_end
    semi_m = re.match(r"\s*;", text[end_pos:])
    if semi_m:
        end_pos += semi_m.end()
    return inner, end_pos, issues


def _convert_begin_block(text: str, kw_start: int) -> Optional[Tuple[str, int, List[ConversionIssue]]]:
    """text[kw_start:] begins at a nested 'BEGIN' keyword (an anonymous
    sub-block, not the routine's own outer BEGIN). Returns
    (converted_tsql, position_just_after_the_block's_trailing_';', issues)."""
    result = _split_begin_exception_end(text, kw_start)
    if result is None:
        return None
    inner, end_pos, issues = result
    return f"BEGIN\n{_indent(inner)}\nEND", end_pos, issues


def _convert_control_flow(text: str) -> Tuple[str, List[ConversionIssue]]:
    """Recursively rewrite Oracle IF/THEN/ELSIF/END IF, LOOP/END LOOP,
    WHILE...LOOP, FOR...LOOP (numeric range), and nested BEGIN...END blocks
    into T-SQL equivalents; everything else (assignments, SQL statements,
    RETURN, etc.) passes through untouched at this stage."""
    issues: List[ConversionIssue] = []
    out: List[str] = []
    pos = 0

    blanked = list(text)
    for m in _STRING_LITERAL_RE.finditer(text):
        for i in range(m.start(), m.end()):
            if blanked[i] != "\n":
                blanked[i] = "x"
    blanked_text = "".join(blanked)

    while True:
        m = _CTRL_OPENER_RE.search(blanked_text, pos)
        if not m:
            out.append(text[pos:])
            break
        word = m.group(0).upper()

        # T-SQL text already emitted by earlier passes (the row/cursor
        # FOR-loop converter's `CURSOR ... FOR <select>` declaration and
        # `WHILE @@FETCH_STATUS = 0 BEGIN` wrapper) can contain bare FOR/
        # WHILE keywords that are *not* Oracle FOR/WHILE...LOOP constructs.
        # Only treat them as openers when the Oracle-specific grammar that
        # must follow is actually present; otherwise this token is just
        # ordinary text and scanning continues right past it.
        if word == "FOR" and not re.match(r"\s*[A-Za-z_][\w$#]*\s+IN\b", text[m.end():], re.IGNORECASE):
            out.append(text[pos:m.end()])
            pos = m.end()
            continue
        if word == "WHILE":
            next_kw_m = re.search(r"\bLOOP\b|\bBEGIN\b|;", text[m.end():], re.IGNORECASE)
            if not (next_kw_m and next_kw_m.group(0).upper() == "LOOP"):
                out.append(text[pos:m.end()])
                pos = m.end()
                continue

        out.append(text[pos:m.start()])

        result = None
        if word == "IF":
            result = _convert_if_statement(text, m.end())
        elif word == "BEGIN":
            result = _convert_begin_block(text, m.start())
        elif word in ("WHILE", "FOR", "LOOP"):
            result = _convert_loop_construct(text, m.start(), word)

        if result is None:
            issues.append(ConversionIssue(
                "error",
                f"MANUAL CONVERSION REQUIRED: could not parse the {word} construct starting near "
                f"'{text[m.start(): m.start()+60].strip()}...'.",
            ))
            out.append(text[m.start(): m.start() + 1])
            pos = m.start() + 1
            continue

        converted, end_pos, sub_issues = result
        issues.extend(sub_issues)
        out.append(converted)
        pos = end_pos
        # keep blanked_text in sync with the positions we've already
        # consumed (blanked_text is only used for locating tokens, and its
        # original-offset positions past `pos` remain valid since we never
        # rewrite text before `pos`)

    return "".join(out), issues


# ---------------------------------------------------------- exception -> TRY/CATCH


def _split_exception_clauses(exc_text: str) -> List[Tuple[str, str]]:
    """Split an Oracle EXCEPTION section into (condition_text, statements)
    pairs for each top-level 'WHEN ... THEN ...' clause, ignoring WHEN/THEN
    that belong to a nested CASE/IF/LOOP/BEGIN inside a handler's own
    statements."""
    token_re = re.compile(
        r"\bBEGIN\b|\bCASE\b|\bIF\b|\bLOOP\b|\bEND\s+IF\b|\bEND\s+CASE\b|"
        r"\bEND\s+LOOP\b|\bEND\b|\bWHEN\b|\bTHEN\b",
        re.IGNORECASE,
    )
    depth = 0
    when_marks: List[Tuple[int, int]] = []   # (when_start, condition_start)
    then_marks: List[Tuple[int, int]] = []   # (then_start, then_end) paired 1:1 with when_marks
    awaiting_then = False

    def _scan(s: str, pos: int):
        nonlocal depth, awaiting_then
        for m in token_re.finditer(s, pos):
            word = re.sub(r"\s+", " ", m.group(0).upper())
            if word in ("BEGIN", "CASE", "IF", "LOOP"):
                depth += 1
            elif word in ("END IF", "END CASE", "END LOOP"):
                depth -= 1
            elif word == "END":
                depth -= 1
            elif word == "WHEN" and depth == 0 and not awaiting_then:
                when_marks.append((m.start(), m.end()))
                awaiting_then = True
            elif word == "THEN" and depth == 0 and awaiting_then:
                then_marks.append((m.start(), m.end()))
                awaiting_then = False

    _apply_outside_strings_scan(exc_text, 0, _scan)

    clauses: List[Tuple[str, str]] = []
    for idx in range(min(len(when_marks), len(then_marks))):
        when_start, cond_start = when_marks[idx]
        then_start, then_end = then_marks[idx]
        condition_text = exc_text[cond_start:then_start].strip()
        stmts_end = when_marks[idx + 1][0] if idx + 1 < len(when_marks) else len(exc_text)
        stmts_text = exc_text[then_end:stmts_end]
        clauses.append((condition_text, stmts_text))
    return clauses


def _exception_condition_to_check(condition_text: str, issues: List[ConversionIssue]) -> Optional[str]:
    """Convert an Oracle exception-name condition (possibly OR-combined)
    into a T-SQL CATCH-block boolean check on ERROR_NUMBER(), or None if
    this is the OTHERS handler (which becomes the final ELSE branch)."""
    names = [n.strip() for n in re.split(r"\bOR\b", condition_text, flags=re.IGNORECASE) if n.strip()]
    if len(names) == 1 and names[0].upper() == "OTHERS":
        return None
    numbers: List[str] = []
    for name in names:
        upper = name.upper()
        if upper == "OTHERS":
            continue  # OTHERS combined with other names is unusual; drop it, OTHERS is handled as the final ELSE
        if upper in _EXCEPTION_ERROR_NUMBER_MAP:
            numbers.append(_EXCEPTION_ERROR_NUMBER_MAP[upper])
        else:
            issues.append(ConversionIssue(
                "error" if upper in _UNMAPPABLE_EXCEPTIONS else "warning",
                f"Exception handler 'WHEN {name} THEN' has no known T-SQL ERROR_NUMBER() mapping "
                f"{'(built-in Oracle exception with no SQL Server equivalent condition)' if upper in _UNMAPPABLE_EXCEPTIONS else '(user-defined exception)'}; "
                "review and adjust the generated CATCH block's condition by hand.",
            ))
            numbers.append("/* unmappable: " + name + " */ -1")
    if not numbers:
        return "1 = 0"
    return f"ERROR_NUMBER() IN ({', '.join(numbers)})"


def _convert_raise_statements(text: str) -> str:
    """RAISE; (bare re-raise) -> THROW; ; RAISE exc_name; (a declared,
    user-defined exception with no T-SQL equivalent condition) -> a THROW
    with the exception's name as the message, since there's nothing else to
    map it to."""
    text = re.sub(r"\bRAISE\s*;", "THROW;", text, flags=re.IGNORECASE)
    text = re.sub(
        r"\bRAISE\s+([A-Za-z_][\w$#]*)\s*;",
        lambda m: f"THROW 50000, '{m.group(1)}', 1;",
        text, flags=re.IGNORECASE,
    )
    return text


def _assemble_try_catch(main_text: str, exc_text: Optional[str]) -> Tuple[str, List[ConversionIssue]]:
    """Convert a 'BEGIN main_stmts [EXCEPTION WHEN ... THEN handler_stmts]*
    END' body into T-SQL, wrapping in BEGIN TRY/BEGIN CATCH only when an
    EXCEPTION section is actually present (a plain body has no need for
    TRY/CATCH at all)."""
    issues: List[ConversionIssue] = []
    # A RAISE of a locally-declared exception (or a bare re-raise) can
    # legitimately appear in the main try-body too, not just inside a
    # WHEN...THEN handler below -- e.g. "IF x < 0 THEN RAISE my_exc; END
    # IF;", later caught by this same block's own EXCEPTION section. Left
    # unconverted, that RAISE is not valid T-SQL syntax on its own (T-SQL's
    # RAISE keyword only exists as the bare re-raise form, THROW;) --
    # regression test for this in test_golden_plsql.py's
    # function_with_exceptions fixture, found while building that suite.
    main_converted, main_issues = _convert_control_flow(_convert_raise_statements(main_text))
    issues.extend(main_issues)

    if exc_text is None:
        return main_converted, issues

    clauses = _split_exception_clauses(exc_text)
    if not clauses:
        issues.append(ConversionIssue(
            "error", "EXCEPTION section present but no WHEN...THEN clauses could be parsed from it; "
                     "manual conversion of the CATCH block is required."))
        return (
            f"BEGIN TRY\n{_indent(main_converted)}\nEND TRY\n"
            f"BEGIN CATCH\n  -- MANUAL CONVERSION REQUIRED: {exc_text.strip()}\n  THROW;\nEND CATCH"
        ), issues

    branch_parts: List[str] = []
    else_body: Optional[str] = None
    for idx, (condition_text, stmts_text) in enumerate(clauses):
        check = _exception_condition_to_check(condition_text, issues)
        stmts_conv, stmts_issues = _convert_control_flow(_convert_raise_statements(stmts_text))
        issues.extend(stmts_issues)
        if check is None:
            else_body = stmts_conv
            continue
        keyword = "IF" if not branch_parts else "ELSE IF"
        branch_parts.append(f"{keyword} {check}\nBEGIN\n{_indent(stmts_conv)}\nEND")

    if else_body is not None:
        branch_parts.append(f"ELSE\nBEGIN\n{_indent(else_body)}\nEND")
    else:
        # No OTHERS handler -- unmatched errors must still be re-raised
        # rather than silently swallowed.
        branch_parts.append("ELSE\nBEGIN\n  THROW;\nEND")

    catch_body = "\n".join(branch_parts) if branch_parts else "THROW;"
    return (
        f"BEGIN TRY\n{_indent(main_converted)}\nEND TRY\n"
        f"BEGIN CATCH\n{_indent(catch_body)}\nEND CATCH"
    ), issues


# ------------------------------------------------------------ declare block


def convert_declare_block(
    text: str, skip_names: Optional[set] = None
) -> Tuple[str, List[ConversionIssue], set]:
    """Convert the DECLARE section (everything between IS/AS and BEGIN) into
    a sequence of T-SQL `DECLARE @name TYPE [= default];` / named-cursor
    statements. Returns (declare_sql, issues, variable_names) where
    variable_names is the set of plain-variable names (not cursors) that
    must be `@`-prefixed everywhere they're referenced in the body.

    `skip_names` (case-insensitive) are variable names used as the loop
    variable of an Oracle implicit-cursor FOR loop elsewhere in the routine
    -- those are declared as SQL_VARIANT column captures by the row-loop
    converter instead, so any pre-declaration for that name here is dropped."""
    issues: List[ConversionIssue] = []
    text, nested = extract_nested_subprograms(text)
    out_lines: List[str] = []
    for ns in nested:
        out_lines.append(
            f"-- MANUAL CONVERSION REQUIRED: nested {ns.kind} {ns.name}, declared inside this "
            f"routine's own DECLARE section -- T-SQL has no nested named-subprogram declaration.\n"
            f"-- Extract it as a standalone procedure/function, or inline its logic by hand. "
            f"Original source:\n"
            f"/*\n{ns.source}\n*/"
        )
        issues.append(ConversionIssue(
            "error",
            f"Nested {ns.kind} '{ns.name}' is declared inside this routine's own DECLARE section; "
            f"T-SQL has no nested named-subprogram declaration -- extract it as a standalone "
            f"procedure/function, or inline its logic by hand.",
        ))
    statements = split_top_level(text, ";")
    var_names: set = set()
    skip_upper = {n.upper() for n in (skip_names or set())}

    for stmt in statements:
        stmt = stmt.strip()
        if not stmt:
            continue

        if re.match(r"^TYPE\s+", stmt, re.IGNORECASE):
            out_lines.append(f"-- MANUAL CONVERSION REQUIRED: {stmt};")
            issues.append(ConversionIssue(
                "error",
                f"Local type declaration '{stmt};' (TABLE OF / RECORD / REF CURSOR) has no direct "
                f"T-SQL equivalent -- rewrite using a table variable, a user-defined table type, or a "
                f"cursor variable as appropriate, and update every reference to it in this routine's body.",
            ))
            continue

        if re.match(r"^PRAGMA\b", stmt, re.IGNORECASE):
            out_lines.append(f"-- (removed) {stmt};  -- T-SQL has no PRAGMA; review if this affected behavior")
            issues.append(ConversionIssue("warning", f"Dropped PRAGMA directive: {stmt.strip()};"))
            continue

        cursor_m = re.match(
            r"^CURSOR\s+([A-Za-z_][\w$#]*)\s*(\([^)]*\))?\s+IS\s+(.*)$",
            stmt, re.IGNORECASE | re.DOTALL,
        )
        if cursor_m:
            cur_name, cur_params, cur_query = cursor_m.groups()
            if cur_params:
                issues.append(ConversionIssue(
                    "warning",
                    f"Cursor '{cur_name}' takes parameters, which T-SQL cursors don't support directly; "
                    "the parameter list was dropped -- reference the values via variables instead.",
                ))
            out_lines.append(f"DECLARE {cur_name} CURSOR LOCAL FAST_FORWARD FOR\n  {cur_query.strip()};")
            # _MANUAL_MARKERS only scans the executable body text, so a
            # CONNECT BY that's still present here -- i.e.
            # connect_by_rewriter.py already had its shot at it (from
            # convert_routine, before this function ever runs) and it
            # didn't match the supported simple shape -- would otherwise
            # never get flagged at all.
            if re.search(r"\bCONNECT\s+BY\b", cur_query, re.IGNORECASE):
                issues.append(ConversionIssue(
                    "error",
                    f"Cursor '{cur_name}': hierarchical query (CONNECT BY) must be rewritten as a recursive CTE (WITH ... AS).",
                ))
            continue

        exc_m = re.match(r"^([A-Za-z_][\w$#]*)\s+EXCEPTION\s*$", stmt, re.IGNORECASE)
        if exc_m:
            out_lines.append(f"-- (kept for reference only) {stmt};  -- T-SQL has no user-defined EXCEPTION type")
            issues.append(ConversionIssue(
                "error",
                f"Exception '{exc_m.group(1)}' is declared but T-SQL has no equivalent to an Oracle "
                f"user-defined EXCEPTION; RAISE statements referencing it are converted to THROW 50000 with "
                f"the exception's name as the message -- review and assign a real error number/message.",
            ))
            continue

        var_m = re.match(r"^([A-Za-z_][\w$#]*)\s+(.+)$", stmt, re.DOTALL)
        if not var_m:
            out_lines.append(f"-- MANUAL REVIEW: could not parse declaration: {stmt};")
            issues.append(ConversionIssue("error", f"Could not parse declaration: {stmt};"))
            continue

        name, rest = var_m.group(1), var_m.group(2).strip()

        if name.upper() in skip_upper:
            out_lines.append(
                f"-- (removed) {stmt};  -- '{name}' is used as an implicit FOR-loop record variable; "
                f"its fetched columns are declared individually as SQL_VARIANT instead."
            )
            issues.append(ConversionIssue(
                "info",
                f"Dropped declaration of '{name}': it is used as a row/cursor FOR-loop variable, whose "
                f"fetched columns are declared individually instead.",
            ))
            continue

        is_constant = False
        if re.match(r"^CONSTANT\b", rest, re.IGNORECASE):
            is_constant = True
            rest = re.sub(r"^CONSTANT\s+", "", rest, flags=re.IGNORECASE)

        default = None
        parts = re.split(r":=", rest, maxsplit=1)
        type_text = parts[0].strip()
        if len(parts) == 2:
            default = parts[1].strip()

        not_null = False
        if re.search(r"\bNOT\s+NULL\s*$", type_text, re.IGNORECASE):
            not_null = True
            type_text = re.sub(r"\bNOT\s+NULL\s*$", "", type_text, flags=re.IGNORECASE).strip()

        mapped_type, type_issues = _convert_declared_type(type_text)
        issues.extend(type_issues)

        if is_constant:
            issues.append(ConversionIssue(
                "info",
                f"'{name}' was declared CONSTANT in Oracle; T-SQL local variables have no immutability "
                "enforcement, so it is declared as a plain variable -- avoid reassigning it.",
            ))
        if not_null:
            issues.append(ConversionIssue(
                "warning",
                f"'{name} NOT NULL' has no T-SQL local-variable equivalent; the NOT NULL constraint is not "
                "enforced -- add an explicit check if this mattered.",
            ))

        var_names.add(name)
        line = f"DECLARE @{name} {mapped_type}"
        if default:
            line += f" = {default}"
        line += ";"
        out_lines.append(line)

    return "\n".join(out_lines), issues, var_names


# ------------------------------------------------------- variable prefixing


def _prefix_variable_references(text: str, names: set) -> str:
    """Prefix every whole-word reference to a declared local variable or
    parameter name with '@' (T-SQL's required sigil for local variables),
    skipping string literals, already-prefixed references, and dotted
    member-access references (`table.column`) where the name follows a
    '.' -- those are column references, not variable references."""
    if not names:
        return text
    sorted_names = sorted(names, key=len, reverse=True)
    alt = "|".join(re.escape(n) for n in sorted_names)
    pattern = re.compile(rf"(?<![@.\w])(?:{alt})\b", re.IGNORECASE)

    def _rewrite(chunk: str) -> str:
        return pattern.sub(lambda m: f"@{m.group(0)}", chunk)

    return _apply_outside_strings(text, _rewrite)


# ------------------------------------------------------------ top-level body


def convert_body(text: str, declare_text: str = "") -> Tuple[str, List[ConversionIssue]]:
    """Convert a flat (not BEGIN/EXCEPTION/END-wrapped) statement sequence
    -- used both directly (a loop body) and internally by the top-level
    routine-body converter after it has split out the outer EXCEPTION
    section. Runs implicit row/cursor FOR-loop conversion, builtin-function
    rewrites, and recursive IF/LOOP/WHILE/FOR control-flow conversion."""
    issues: List[ConversionIssue] = []
    text = _convert_row_for_loops(text, declare_text, issues)
    text, builtin_issues = _builtin_rewrites(text)
    issues.extend(builtin_issues)
    text, ctrl_issues = _convert_control_flow(text)
    issues.extend(ctrl_issues)
    return text, issues


def convert_routine_body(full_body_text: str, declare_text: str = "") -> Tuple[str, List[ConversionIssue]]:
    """Convert the full `BEGIN ... [EXCEPTION WHEN ... THEN ...]* END;` (or
    `END routine_name;`) body of a procedure/function/trigger into T-SQL
    *statement content only* (no outer BEGIN/END -- the caller combines this
    with its own DECLARE statements inside a single BEGIN/END), including
    the BEGIN TRY/CATCH restructuring T-SQL requires in place of Oracle's
    native EXCEPTION WHEN handlers."""
    issues: List[ConversionIssue] = []
    text = _convert_row_for_loops(full_body_text, declare_text, issues)
    text, builtin_issues = _builtin_rewrites(text)
    issues.extend(builtin_issues)

    # normalize a trailing `END routine_name;` to plain `END;` so the block
    # matcher's bare-END recognition and trailing-';' handling apply cleanly
    text = text.rstrip()
    end_m = re.search(r"END\s+([A-Za-z_][\w$#]*)\s*;\s*$", text, re.IGNORECASE)
    if end_m and end_m.group(1).upper() not in ("IF", "LOOP", "CASE"):
        text = text[: end_m.start()] + "END;"

    begin_kw_m = re.search(r"\bBEGIN\b", text, re.IGNORECASE)
    if not begin_kw_m:
        issues.append(ConversionIssue("error", "Could not locate the routine's BEGIN keyword; manual conversion required."))
        return f"-- MANUAL CONVERSION REQUIRED\n/*\n{text}\n*/", issues

    result = _split_begin_exception_end(text, begin_kw_m.start())
    if result is None:
        issues.append(ConversionIssue("error", "Could not parse the routine body's BEGIN/END structure; manual conversion required."))
        return f"-- MANUAL CONVERSION REQUIRED\n/*\n{text}\n*/", issues

    body_sql, _end_pos, block_issues = result
    issues.extend(block_issues)
    return body_sql, issues


# ------------------------------------------------------- implicit-var names


def _find_row_loop_variable_names(body_text: str) -> set:
    """Names used as the loop variable of an implicit-cursor/row FOR loop
    (`FOR rec IN ...`) anywhere in the routine -- these have no valid
    pre-declaration in T-SQL (there's no row/RECORD variable type), so any
    existing DECLARE for the name is dropped rather than translated; see
    convert_declare_block's skip_names parameter."""
    names = set()
    for m in _ROW_FOR_HEADER_RE.finditer(body_text):
        rest = body_text[m.end():]
        loop_kw_m = re.search(r"\bLOOP\b", rest, re.IGNORECASE)
        header = rest[: loop_kw_m.start()].strip() if loop_kw_m else ""
        if not _NUMERIC_RANGE_RE.match(header):
            names.add(m.group(1))
    return names


def _find_numeric_range_loop_var_names(body_text: str) -> set:
    """Names used as the counter of a numeric-range FOR loop
    (`FOR i IN 1..10 LOOP`) -- Oracle declares these implicitly too, and
    the T-SQL rewrite emits an explicit `DECLARE @i INT = ...;` for each
    inline at the loop site, so every bare reference to the name elsewhere
    in the body must also be `@`-prefixed."""
    names = set()
    for m in _ROW_FOR_HEADER_RE.finditer(body_text):
        rest = body_text[m.end():]
        loop_kw_m = re.search(r"\bLOOP\b", rest, re.IGNORECASE)
        if not loop_kw_m:
            continue
        header = rest[: loop_kw_m.start()].strip()
        if _NUMERIC_RANGE_RE.match(header):
            names.add(m.group(1))
    return names


# ------------------------------------------------------------ top-level API


def _extract_declared_exceptions(declare_text: str) -> set:
    names = set()
    for stmt in split_top_level(declare_text, ";"):
        m = re.match(r"^([A-Za-z_][\w$#]*)\s+EXCEPTION\s*$", stmt.strip(), re.IGNORECASE)
        if m:
            names.add(m.group(1))
    return names


def convert_procedure_or_function(routine: Routine) -> Tuple[str, List[ConversionIssue]]:
    issues: List[ConversionIssue] = []
    parsed = parse_routine_header(routine.source)
    if parsed is None:
        issues.append(ConversionIssue(
            "error", "Could not parse the routine header; manual conversion required."))
        return f"-- MANUAL CONVERSION REQUIRED for {routine.kind} {routine.name}\n/*\n{routine.source}\n*/", issues

    params = split_top_level(parsed["params_text"], ",")
    tsql_params: List[str] = []
    param_names: set = set()
    has_output_param = False
    for raw_param in params:
        if not raw_param:
            continue
        name, mode, type_text, default = parse_param(raw_param)
        mapped_type, type_issues = _convert_declared_type(type_text)
        issues.extend(type_issues)
        piece = f"@{name} {mapped_type}"
        if default:
            piece += f" = {default}"
        if mode in ("OUT", "INOUT"):
            piece += " OUTPUT"
            has_output_param = True
        tsql_params.append(piece)
        param_names.add(name)
    params_clause = ",\n  ".join(tsql_params)

    row_loop_vars = _find_row_loop_variable_names(parsed["body_text"])
    numeric_loop_vars = _find_numeric_range_loop_var_names(parsed["body_text"])

    declare_sql, declare_issues, var_names = convert_declare_block(parsed["declare_text"], row_loop_vars)
    issues.extend(declare_issues)

    body_sql, body_issues = convert_routine_body(parsed["body_text"], parsed["declare_text"])
    issues.extend(body_issues)

    all_names = var_names | param_names | numeric_loop_vars
    declare_sql = _prefix_variable_references(declare_sql, all_names)
    body_sql = _prefix_variable_references(body_sql, all_names)

    inner_parts = []
    inner_parts.append("SET NOCOUNT ON;")
    if declare_sql.strip():
        inner_parts.append(declare_sql)
    inner_parts.append(body_sql)
    inner = "\n".join(inner_parts)

    if parsed["kind"] == "FUNCTION":
        if has_output_param:
            issues.append(ConversionIssue(
                "error",
                "T-SQL scalar functions do not support OUTPUT parameters; convert this routine to a "
                "PROCEDURE, or restructure it to return a table-valued/composite result instead.",
            ))
        if parsed["return_type"]:
            return_type, return_issues = _convert_declared_type(parsed["return_type"])
            issues.extend(return_issues)
        else:
            return_type = "NVARCHAR(MAX)"
            issues.append(ConversionIssue("warning", "FUNCTION had no parsable RETURN type; defaulted to NVARCHAR(MAX)."))
        if re.search(r"\b(PRINT|THROW|RAISERROR|INSERT|UPDATE|DELETE|EXEC)\b", body_sql, re.IGNORECASE):
            issues.append(ConversionIssue(
                "warning",
                "T-SQL scalar functions cannot contain PRINT, THROW/RAISERROR, DML statements, or EXEC "
                "calls; review the generated body and relocate any such logic (e.g. into a calling "
                "procedure) -- SQL Server will reject the CREATE FUNCTION otherwise.",
            ))
        params_block = f"(\n  {params_clause}\n)" if params_clause else "()"
        ddl = (
            f"CREATE OR ALTER FUNCTION {_quote_sqlserver(routine.name)}{params_block}\n"
            f"RETURNS {return_type}\n"
            f"AS\n"
            f"BEGIN\n"
            f"{_indent(inner)}\n"
            f"END;"
        )
    else:
        params_block = f"\n  {params_clause}\n" if params_clause else "\n"
        ddl = (
            f"CREATE OR ALTER PROCEDURE {_quote_sqlserver(routine.name)}{params_block}"
            f"AS\n"
            f"BEGIN\n"
            f"{_indent(inner)}\n"
            f"END;"
        )

    return ddl, issues


def convert_trigger(routine: Routine) -> Tuple[str, List[ConversionIssue]]:
    issues: List[ConversionIssue] = []

    timing = (routine.timing or "BEFORE").upper()
    if timing == "BEFORE":
        issues.append(ConversionIssue(
            "error",
            f"Trigger '{routine.name}' is a BEFORE trigger; SQL Server has no BEFORE trigger type (only "
            "AFTER and INSTEAD OF). INSTEAD OF is not a safe silent substitute -- it replaces the "
            "triggering statement entirely, so the original INSERT/UPDATE/DELETE must be re-issued "
            "explicitly inside the trigger body. This requires manual review and rewrite.",
        ))
        return (
            f"-- MANUAL CONVERSION REQUIRED for BEFORE TRIGGER {routine.name}: SQL Server has no BEFORE "
            f"trigger; rewrite as INSTEAD OF (re-issuing the DML yourself) or move this logic into the "
            f"calling application/procedure.\n/*\n{routine.source}\n*/"
        ), issues

    body = routine.source.strip()
    declare_text, body_text = "", body
    # find_top_level_begin (not a bare `re.search(r"\bBEGIN\b", ...)`) is
    # needed here -- a trigger's DECLARE section can just as easily hide a
    # nested subprogram before the trigger's own BEGIN; see that
    # function's own docstring and plsql_converter.parse_routine_header's
    # matching comment.
    begin_pos = find_top_level_begin(body)
    if body[:8].upper().startswith("DECLARE") and begin_pos is not None:
        declare_text = body[len("DECLARE"): begin_pos]
        body_text = body[begin_pos:]

    row_loop_vars = _find_row_loop_variable_names(body_text)
    numeric_loop_vars = _find_numeric_range_loop_var_names(body_text)
    declare_sql, declare_issues, var_names = (
        convert_declare_block(declare_text, row_loop_vars) if declare_text.strip() else ("", [], set())
    )
    issues.extend(declare_issues)

    if routine.row_level:
        issues.append(ConversionIssue(
            "warning",
            f"Trigger '{routine.name}' is FOR EACH ROW in Oracle; SQL Server triggers are always "
            "statement-level and operate on the `inserted`/`deleted` pseudo-tables, which may contain "
            "zero, one, or many rows per statement. :NEW/:OLD references below were rewritten as "
            "single-row lookups against inserted/deleted, which is only correct for single-row DML -- "
            "rewrite this trigger to be fully set-based if multi-row DML against this table is expected.",
        ))

    def _rewrite_new_old(chunk: str) -> str:
        chunk = re.sub(r":NEW\.(\w+)", r"(SELECT \1 FROM inserted)", chunk, flags=re.IGNORECASE)
        chunk = re.sub(r":OLD\.(\w+)", r"(SELECT \1 FROM deleted)", chunk, flags=re.IGNORECASE)
        return chunk

    body_text = _apply_outside_strings(body_text, _rewrite_new_old)

    body_sql, body_issues = convert_routine_body(body_text, declare_text)
    issues.extend(body_issues)

    all_names = var_names | numeric_loop_vars
    declare_sql = _prefix_variable_references(declare_sql, all_names)
    body_sql = _prefix_variable_references(body_sql, all_names)

    inner_parts = ["SET NOCOUNT ON;"]
    if declare_sql.strip():
        inner_parts.append(declare_sql)
    inner_parts.append(body_sql)
    inner = "\n".join(inner_parts)

    events = ", ".join(routine.events or ["INSERT"])
    table_name = routine.table_name or "UNKNOWN_TABLE"
    if routine.table_name is None:
        issues.append(ConversionIssue("error", "Trigger's target table was not captured; DDL references UNKNOWN_TABLE."))

    trigger_timing = "INSTEAD OF" if timing == "INSTEAD OF" else "AFTER"
    ddl = (
        f"CREATE OR ALTER TRIGGER {_quote_sqlserver(routine.name)}\n"
        f"ON {_quote_sqlserver(table_name)}\n"
        f"{trigger_timing} {events}\n"
        f"AS\n"
        f"BEGIN\n"
        f"{_indent(inner)}\n"
        f"END;"
    )

    return ddl, issues


def convert_package_body(routine: Routine) -> Tuple[str, List[ConversionIssue]]:
    """Best-effort flattening of a (non-nested) PACKAGE BODY into standalone
    procedures/functions named '<package>_<member>', mirroring
    plsql_converter.convert_package_body's Oracle-source chunking."""
    issues: List[ConversionIssue] = []
    source = routine.source

    header_positions = [
        m for m in re.finditer(r"\b(PROCEDURE|FUNCTION)\s+([A-Za-z_][\w$#]*)", source, re.IGNORECASE)
    ]
    if not header_positions:
        issues.append(ConversionIssue(
            "error", "No PROCEDURE/FUNCTION members found in package body; manual conversion required."))
        return f"-- MANUAL CONVERSION REQUIRED for PACKAGE BODY {routine.name}\n/*\n{source}\n*/", issues

    chunks: List[str] = []
    for idx, m in enumerate(header_positions):
        start = m.start()
        end = header_positions[idx + 1].start() if idx + 1 < len(header_positions) else len(source)
        chunks.append(source[start:end])

    # strip the package body's own trailing "END <package_name>;" (or bare
    # "END;") from the last member's chunk -- see the matching comment in
    # plsql_converter.convert_package_body for why this is necessary.
    if chunks:
        last = chunks[-1]
        close_m = re.search(
            rf"END\s+{re.escape(routine.name)}\s*;\s*$|END\s*;\s*$",
            last, re.IGNORECASE,
        )
        if close_m:
            remainder = last[: close_m.start()]
            if re.search(r"\bEND\b", remainder, re.IGNORECASE):
                chunks[-1] = remainder

    ddl_parts: List[str] = [
        f"-- Flattened from PACKAGE BODY {routine.name} ({len(chunks)} member(s)). "
        f"Package-level state (if any) is not carried over -- see notes below."
    ]
    for chunk in chunks:
        member_m = re.match(r"^\s*(PROCEDURE|FUNCTION)\s+([A-Za-z_][\w$#]*)", chunk, re.IGNORECASE)
        member_name = member_m.group(2) if member_m else "unknown_member"
        renamed_chunk = re.sub(
            rf"^(\s*(?:PROCEDURE|FUNCTION)\s+){re.escape(member_name)}\b",
            rf"\g<1>{routine.name}_{member_name}",
            chunk, count=1, flags=re.IGNORECASE,
        )
        pseudo_routine = Routine(
            name=f"{routine.name}_{member_name}", schema=routine.schema,
            kind="FUNCTION" if member_m and member_m.group(1).upper() == "FUNCTION" else "PROCEDURE",
            source=renamed_chunk,
        )
        member_ddl, member_issues = convert_procedure_or_function(pseudo_routine)
        ddl_parts.append(member_ddl)
        issues.extend(member_issues)

    issues.append(ConversionIssue(
        "warning",
        "Package-level variables/constants (if the package declared any) have no direct T-SQL equivalent; "
        "consider a settings table or SESSION_CONTEXT if state needs to be shared across the flattened "
        "procedures/functions.",
    ))

    return "\n\n".join(ddl_parts), issues


def convert_routine(routine: Routine, target_engine: str) -> Routine:
    """Populate routine.converted_source / .status / .issues in place and
    return it."""
    # A CONNECT BY hierarchical query anywhere in the routine's source gets
    # one shot at an automatic recursive-CTE rewrite before any of the rest
    # of this converter looks at routine.source -- see
    # connect_by_rewriter.py's module docstring for exactly which shapes
    # qualify. A successful rewrite removes the literal "CONNECT BY" text,
    # so the _MANUAL_MARKERS scan below no longer flags it.
    from tgdatabridge.core.connect_by_rewriter import rewrite_routine_source
    connect_by_issues = rewrite_routine_source(routine, cte_keyword="WITH")

    if routine.kind == "TRIGGER":
        ddl, issues = convert_trigger(routine)
    elif routine.kind == "PACKAGE BODY":
        ddl, issues = convert_package_body(routine)
    elif routine.kind == "PACKAGE":
        ddl = (
            f"-- PACKAGE {routine.name} has no T-SQL equivalent (no packaging construct). "
            f"Its public members are converted individually from the matching PACKAGE BODY. "
            f"Package-level constants/types declared only in the spec must be moved by hand."
        )
        issues = [ConversionIssue(
            "warning", "Package specs have no T-SQL equivalent; only the package body's members were converted.")]
    else:
        ddl, issues = convert_procedure_or_function(routine)

    issues = connect_by_issues + issues
    routine.converted_source = ddl
    routine.issues = issues
    if any(i.severity == "error" for i in issues):
        routine.status = ConversionStatus.MANUAL
    elif any(i.severity == "warning" for i in issues):
        routine.status = ConversionStatus.AUTOMATIC_WITH_WARNINGS
    else:
        routine.status = ConversionStatus.AUTOMATIC

    from tgdatabridge.core.sql_translator import score_complexity
    score, _marker_issues = score_complexity(routine.source)
    routine.complexity_score = score

    return routine


def convert_all_routines(routines: List[Routine], target_engine: str) -> List[Routine]:
    return [convert_routine(r, target_engine) for r in routines]
