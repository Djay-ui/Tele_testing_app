"""
Oracle PL/SQL -> PostgreSQL PL/pgSQL source-code converter.

This does real, rule-based syntax translation (not just flagging) for the
constructs that come up in the overwhelming majority of stored procedures,
functions, and triggers: signatures, variable declarations, common builtin
functions (NVL, NVL2, DECODE, INSTR, DBMS_OUTPUT.PUT_LINE, sequences,
RAISE_APPLICATION_ERROR, EXECUTE IMMEDIATE, cursor syntax, FROM DUAL,
:NEW/:OLD and INSERTING/UPDATING/DELETING in triggers), and reassembles
the result into a Postgres-ready CREATE FUNCTION / CREATE PROCEDURE /
CREATE TRIGGER statement.

Constructs that genuinely have no safe mechanical translation (DBMS_*/UTL_*
packages other than DBMS_OUTPUT, BULK COLLECT/FORALL, CONNECT BY, autonomous
transactions, Oracle-only named exceptions, deeply nested subprograms) are
left in place and flagged as issues rather than guessed at — the same
"convert what's safe, flag the rest" approach used for tables and views.

convert_routine()'s Oracle-target branch is the one exception to all of
the above: since routine.source is already real Oracle PL/SQL by the time
that branch runs (guaranteed by the non-Oracle-source guard earlier in
the same function), no rule-based syntax rewriting is needed -- it only
reconstructs the "CREATE [OR REPLACE] ..." header ALL_SOURCE/
ALL_TRIGGERS.TRIGGER_BODY never included in the first place, and is
always AUTOMATIC, unlike every dispatch below it.
"""
from __future__ import annotations

import re
from typing import Callable, Dict, List, Optional, Tuple

from tgdatabridge.core import type_mapping
from tgdatabridge.core.nested_subprogram import extract_nested_subprograms, find_top_level_begin
from tgdatabridge.core.schema_model import ConversionIssue, ConversionStatus, Routine
from tgdatabridge.utils.identifiers import quote_double

# ---------------------------------------------------------------- utilities


def split_top_level(text: str, sep: str = ",") -> List[str]:
    """Split text on `sep` at paren-depth 0, respecting simple '...' string
    literals so commas inside NUMBER(10,2) or 'a,b' don't split."""
    depth = 0
    in_string = False
    current: List[str] = []
    parts: List[str] = []
    for ch in text:
        if ch == "'":
            in_string = not in_string
            current.append(ch)
        elif in_string:
            current.append(ch)
        elif ch == "(":
            depth += 1
            current.append(ch)
        elif ch == ")":
            depth -= 1
            current.append(ch)
        elif ch == sep and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    if current:
        parts.append("".join(current))
    return [p.strip() for p in parts if p.strip() != ""]


def transform_function_calls(
    text: str, func_name_pattern: str, builder: Callable[[List[str]], str]
) -> Tuple[str, int]:
    """Find every call to `func_name_pattern(...)` (regex, case-insensitive),
    balanced-paren match its argument list, and replace the whole call with
    builder(args). Returns (new_text, number_of_replacements)."""
    pattern = re.compile(r"\b" + func_name_pattern + r"\s*\(", re.IGNORECASE)
    out: List[str] = []
    count = 0
    pos = 0
    while True:
        m = pattern.search(text, pos)
        if not m:
            out.append(text[pos:])
            break
        out.append(text[pos : m.start()])
        depth = 1
        i = m.end()
        in_string = False
        while i < len(text) and depth > 0:
            ch = text[i]
            if ch == "'":
                in_string = not in_string
            elif not in_string:
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
            i += 1
        args_text = text[m.end() : i - 1]
        args = split_top_level(args_text, ",")
        out.append(builder(args))
        count += 1
        pos = i
    return "".join(out), count


#: A bare Oracle string literal, and nothing else -- 'text', with Oracle's
#: own ''-escaping for an embedded quote, anchored start-to-end so
#: `'a' || 'b'` (two literals joined by concatenation) does not match.
_SIMPLE_STRING_LITERAL_RE = re.compile(r"^'(?:[^']|'')*'$")


def is_simple_string_literal(expr: str) -> bool:
    """True only for a message argument like `RAISE_APPLICATION_ERROR(-20001,
    'Something went wrong')` -- NOT for one built by concatenation, like
    `RAISE_APPLICATION_ERROR(-20001, 'No row found for id=' || p_id)`.

    Shared by every RAISE_APPLICATION_ERROR translation across all four
    target converters (this module's own, plus tsql_converter.py,
    plsql_mysql_converter.py, db2_converter.py) because each target's
    error-raising statement has a different rule for what a *computed*
    message needs, and getting that rule wrong produces DDL that parses
    fine here and then fails the moment "Apply DDL to Target" actually
    runs it:
      - PostgreSQL's `RAISE EXCEPTION 'format', args...` requires a literal
        format string in that position -- `'text' || x` there is a syntax
        error, not a runtime one. Fixed unconditionally by using
        `RAISE EXCEPTION USING MESSAGE = <expr>` instead, which accepts any
        expression (literal or computed) with no special-casing needed --
        see plsql_converter.py's own _raise_app_error.
      - MySQL's `SIGNAL ... SET MESSAGE_TEXT = value` and SQL Server's
        `THROW error, message, state` both accept only a literal or a
        variable there, never an inline expression -- concatenating in
        place is a hard syntax restriction on both, not something a
        different keyword works around the way Postgres's USING clause
        does. Correctly handling a computed message there means declaring
        a local variable first and assigning the concatenated value to it
        before the SIGNAL/THROW -- real, non-mechanical surgery this tool
        does not attempt (same reasoning as DECODE/ROWNUM in views: a
        genuine rewrite needing context this function doesn't have, not a
        safe token substitution) -- so both leave a *literal* message
        translated exactly as before, and flag a computed one as needing
        manual conversion rather than emitting DDL guaranteed to fail.
      - Db2's `SIGNAL SQLSTATE '...' SET MESSAGE_TEXT = message-text`
        accepts a general expression already, and Db2's own `||` operator
        is identical to Oracle's -- nothing to translate or flag either
        way, so db2_converter.py doesn't call this at all.
    """
    return bool(_SIMPLE_STRING_LITERAL_RE.match(expr.strip()))


#: USERENV parameters with a genuinely equivalent PostgreSQL session
#: keyword/function -- see replace_sys_context()'s docstring for why the
#: list stops here rather than guessing at the rest.
_SYS_CONTEXT_USERENV_MAP = {
    "SESSION_USER": "session_user",
    "CURRENT_USER": "current_user",
    "CURRENT_SCHEMA": "current_schema",
    "DB_NAME": "current_database()",
}


def replace_sys_context(text: str, issues: List[ConversionIssue]) -> str:
    """SYS_CONTEXT('USERENV', 'SESSION_USER') -> session_user, and similarly
    for the small set of USERENV parameters in _SYS_CONTEXT_USERENV_MAP.

    Anything else -- a different USERENV parameter (HOST, IP_ADDRESS,
    OS_USER, CLIENT_IDENTIFIER, SESSIONID, ...), a non-literal argument, a
    wrong argument count, or a namespace other than USERENV entirely (a
    caller's own `CREATE CONTEXT` package, which is never an Oracle
    built-in) -- is left exactly as written and flagged as needing manual
    conversion, never guessed at. A wrong guess here compiles fine and then
    returns a plausible-looking but incorrect value forever, which is worse
    than the loud, obvious failure this replaces.
    """

    def _sys_context(args: List[str]) -> str:
        if len(args) == 2 and is_simple_string_literal(args[0]) and is_simple_string_literal(args[1]):
            namespace = args[0].strip()[1:-1].upper()
            param = args[1].strip()[1:-1].upper()
            if namespace == "USERENV" and param in _SYS_CONTEXT_USERENV_MAP:
                return _SYS_CONTEXT_USERENV_MAP[param]
            issues.append(ConversionIssue(
                "warning",
                f"SYS_CONTEXT('{namespace}', '{param}') has no equivalent PostgreSQL session "
                "function and was left unconverted -- it will fail at runtime "
                "(`sys_context(...) does not exist`) until rewritten manually, e.g. with "
                "current_setting(), inet_client_addr(), or a session variable of your own.",
            ))
            return f"SYS_CONTEXT({', '.join(args)})"
        issues.append(ConversionIssue(
            "warning",
            f"SYS_CONTEXT with {len(args)} argument(s), or a non-literal argument, could not be "
            "converted automatically -- it will fail at runtime until rewritten manually.",
        ))
        return f"SYS_CONTEXT({', '.join(args)})"

    text, n = transform_function_calls(text, "SYS_CONTEXT", _sys_context)
    if n:
        issues.append(ConversionIssue("info", f"Converted {n} SYS_CONTEXT(...) call(s)."))
    return text


# ------------------------------------------- Oracle SQL-expression gaps
#
# Four PostgreSQL-target gaps, shared between convert_body (a routine's
# embedded SELECTs) and ddl_generator.generate_view_ddl (a view's own
# SELECT) since both reach the target through exactly the same SQL text.
# Each function below is exported (no leading underscore) for that reason.
# Every one leaves the input untouched and flags it, rather than guessing,
# the moment the shape gets past what is safely, mechanically convertible
# -- the same "don't guess" rule this file already applies to DECODE,
# INSTR's 3/4-arg forms, and RAISE_APPLICATION_ERROR's computed message.


def _scan_to_matching_paren(text: str, open_pos: int) -> int:
    """`text[open_pos]` must be '('. Returns the index of the matching ')',
    respecting '...'-quoted string literals so a paren inside a literal is
    never mistaken for real nesting."""
    depth = 1
    i = open_pos + 1
    in_string = False
    while i < len(text) and depth > 0:
        ch = text[i]
        if ch == "'":
            in_string = not in_string
        elif not in_string:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
        i += 1
    return i - 1


# Anchored so the ROWNUM comparison must be the very last thing before a
# statement/subquery boundary (';', a closing ')', or end of text) -- the
# overwhelmingly common "top-N" idiom, including the classic
# `SELECT * FROM (SELECT ... ORDER BY ...) WHERE ROWNUM <= N` pattern for
# an ordered top-N, where the ROWNUM predicate is on the *outer* query.
# Any other placement (ROWNUM as the first of several AND-ed conditions,
# ROWNUM inside a CASE expression, `ROWNUM BETWEEN a AND b` for a page of
# results) is a materially different rewrite this doesn't attempt --
# it's left untouched, and the caller's own oracle_markers/manual-marker
# scan still flags the leftover "ROWNUM" text for manual review.
_ROWNUM_TOPN_RE = re.compile(
    r"(?P<connector>\bWHERE\b|\bAND\b)\s+ROWNUM\s*(?P<op><=|<|=)\s*(?P<bound>[^\s;)]+)\s*(?=;|\)|$)",
    re.IGNORECASE,
)


def rewrite_rownum_topn(text: str, issues: List[ConversionIssue]) -> str:
    """`WHERE ROWNUM <= N` (optionally preceded by other AND-ed conditions)
    -> a trailing `LIMIT N`, `LIMIT (N - 1)` for `<`, or `LIMIT 1` for
    `ROWNUM = 1`. Postgres has no ROWNUM pseudo-column at all; left
    untouched this used to reach the target as literal, invalid syntax."""
    def _repl(m: "re.Match") -> str:
        connector = m.group("connector")
        op = m.group("op")
        bound = m.group("bound").strip()
        if op == "<=":
            limit_expr = bound
        elif op == "<":
            limit_expr = f"({bound} - 1)"
        else:  # "="
            limit_expr = "1"
        issues.append(ConversionIssue(
            "info",
            f"Converted ROWNUM {op} {bound} to LIMIT {limit_expr} -- Postgres has no ROWNUM "
            "pseudo-column; this only covers the top-N idiom (ROWNUM as the last WHERE condition).",
        ))
        # connector == "WHERE" means ROWNUM was the *only* predicate, so the
        # whole "WHERE ROWNUM ..." span (matched from "WHERE" onward) is
        # replaced outright; connector == "AND" means other conditions
        # precede it and must survive, so only the "AND ROWNUM ..." tail is
        # replaced, leaving "WHERE <other conditions>" in place before it.
        return f"\nLIMIT {limit_expr}"

    return _ROWNUM_TOPN_RE.sub(_repl, text)


_LISTAGG_START_RE = re.compile(r"\bLISTAGG\s*\(", re.IGNORECASE)
_WITHIN_GROUP_RE = re.compile(r"\s*WITHIN\s+GROUP\s*\(", re.IGNORECASE)
_ORDER_BY_RE = re.compile(r"\s*ORDER\s+BY\s+", re.IGNORECASE)


def rewrite_listagg(text: str, issues: List[ConversionIssue]) -> str:
    """`LISTAGG(expr[, delim]) WITHIN GROUP (ORDER BY sort...)` ->
    `STRING_AGG(expr, delim ORDER BY sort...)` -- a real, mechanical
    rewrite (STRING_AGG's ORDER BY clause is the same idea, just spelled
    inside the call instead of via a trailing WITHIN GROUP), except that
    Postgres's STRING_AGG has no default delimiter the way Oracle's
    LISTAGG does, so an omitted delimiter is filled in as ''. Anything
    Oracle 19c's `ON OVERFLOW ...` clause, or a WITHIN GROUP-less call
    (both rare) is left untouched and flagged rather than guessed at."""
    out: List[str] = []
    pos = 0
    while True:
        m = _LISTAGG_START_RE.search(text, pos)
        if not m:
            out.append(text[pos:])
            break
        out.append(text[pos:m.start()])
        open_paren = m.end() - 1
        close_paren = _scan_to_matching_paren(text, open_paren)
        args = split_top_level(text[open_paren + 1:close_paren], ",")
        after = text[close_paren + 1:]
        wg_m = _WITHIN_GROUP_RE.match(after)
        if len(args) not in (1, 2) or not wg_m:
            issues.append(ConversionIssue(
                "error",
                "LISTAGG could not be converted automatically -- expected "
                "LISTAGG(expr[, delimiter]) WITHIN GROUP (ORDER BY ...); rewrite using "
                "STRING_AGG(expr, delimiter ORDER BY ...) by hand (this also covers Oracle 19c's "
                "ON OVERFLOW clause, which has no Postgres equivalent).",
            ))
            out.append(text[m.start():close_paren + 1])
            pos = close_paren + 1
            continue
        wg_open_abs = close_paren + 1 + wg_m.end() - 1
        wg_close_abs = _scan_to_matching_paren(text, wg_open_abs)
        inner = text[wg_open_abs + 1:wg_close_abs]
        ob_m = _ORDER_BY_RE.match(inner)
        if not ob_m:
            issues.append(ConversionIssue(
                "error",
                "LISTAGG's WITHIN GROUP clause did not start with ORDER BY as expected; rewrite "
                "using STRING_AGG(...) by hand.",
            ))
            out.append(text[m.start():wg_close_abs + 1])
            pos = wg_close_abs + 1
            continue
        expr = args[0].strip()
        delimiter = args[1].strip() if len(args) == 2 else "''"
        order_by_clause = inner[ob_m.end():].strip()
        out.append(f"STRING_AGG({expr}, {delimiter} ORDER BY {order_by_clause})")
        pos = wg_close_abs + 1
    return "".join(out)


def rewrite_regexp_functions(text: str, issues: List[ConversionIssue]) -> str:
    """Oracle's REGEXP_LIKE/REGEXP_REPLACE/REGEXP_SUBSTR/REGEXP_COUNT/
    REGEXP_INSTR, mapped only where a safe, version-independent Postgres
    equivalent exists:

      - REGEXP_LIKE(str, pattern) -> `(str) ~ (pattern)`; with a bare 'i'
        match_param -> `(str) ~* (pattern)` (case-insensitive match). Any
        other match_param (multiline 'm', extended 'x', combinations) is
        flagged rather than guessed.
      - REGEXP_REPLACE(str, pattern, replace) -- exactly 3 arguments --
        needs no change at all: Postgres's own REGEXP_REPLACE has an
        identical 3-argument form (replace the first match only). A 4th
        argument is flagged instead of passed through, because Postgres's
        4th positional argument is a *flags* string, not Oracle's
        position/occurrence/match_param -- silently identical-looking
        text there would run with the wrong semantics, not fail loudly.
      - REGEXP_SUBSTR(str, pattern) -- exactly 2 arguments -- ->
        `substring(str from pattern)`, standard SQL syntax equivalent to
        Oracle's default (position 1, first occurrence). 3+ arguments
        (position/occurrence/subexpression) are flagged.
      - REGEXP_COUNT and REGEXP_INSTR have no safe one-line Postgres
        equivalent this tool is confident is correct in every case (both
        need real reconstruction with regexp_matches()/array_length() or
        a position-tracking scan) -- always flagged, never guessed.
    """
    def _regexp_like(args: List[str]) -> str:
        if len(args) == 2:
            return f"({args[0]}) ~ ({args[1]})"
        if len(args) == 3 and args[2].strip().strip("'").upper() == "I":
            return f"({args[0]}) ~* ({args[1]})"
        issues.append(ConversionIssue(
            "error",
            f"REGEXP_LIKE with {len(args)} argument(s) and this match_param could not be converted "
            "automatically; rewrite using Postgres's ~ / ~* operators by hand.",
        ))
        return f"REGEXP_LIKE({', '.join(args)})"

    text, _n = transform_function_calls(text, "REGEXP_LIKE", _regexp_like)

    def _regexp_replace(args: List[str]) -> str:
        if len(args) == 3:
            return f"REGEXP_REPLACE({', '.join(args)})"
        issues.append(ConversionIssue(
            "error",
            f"REGEXP_REPLACE with {len(args)} arguments (position/occurrence/match_param) has no "
            "direct Postgres equivalent -- Postgres's 4th argument is a flags string, not an "
            "occurrence count; rewrite by hand.",
        ))
        return f"REGEXP_REPLACE({', '.join(args)})"

    text, _n = transform_function_calls(text, "REGEXP_REPLACE", _regexp_replace)

    def _regexp_substr(args: List[str]) -> str:
        if len(args) == 2:
            return f"substring({args[0]} from {args[1]})"
        issues.append(ConversionIssue(
            "error",
            f"REGEXP_SUBSTR with {len(args)} arguments (position/occurrence/subexpression) has no "
            "direct Postgres equivalent; rewrite using regexp_matches()/substring() by hand.",
        ))
        return f"REGEXP_SUBSTR({', '.join(args)})"

    text, _n = transform_function_calls(text, "REGEXP_SUBSTR", _regexp_substr)

    for fn_name in ("REGEXP_COUNT", "REGEXP_INSTR"):
        if re.search(rf"\b{fn_name}\s*\(", text, re.IGNORECASE):
            issues.append(ConversionIssue(
                "error",
                f"{fn_name} has no direct Postgres equivalent and was left unconverted; rewrite by "
                "hand (e.g. regexp_matches() with array_length() for a count, or a manual scan for a "
                "position).",
            ))

    return text


#: Oracle date-truncation format models this tool is confident map onto a
#: single Postgres date_trunc() field with identical behavior. Week-based
#: models (WW/IW/W), the week-start-day models (DAY/DY/D), century (CC/SCC)
#: and the Roman-numeral month model (RM) are deliberately absent -- each
#: needs a real computed expression, not a single field name, to match
#: Oracle's behavior, so a call using one of those is flagged instead.
_TRUNC_DATE_FORMATS = {
    "YYYY": "year", "YEAR": "year", "SYYYY": "year", "YYY": "year", "YY": "year", "Y": "year",
    "Q": "quarter",
    "MM": "month", "MONTH": "month", "MON": "month",
    "DD": "day", "DDD": "day", "J": "day",
    "HH": "hour", "HH12": "hour", "HH24": "hour",
    "MI": "minute",
}


def rewrite_oracle_date_functions(text: str, issues: List[ConversionIssue]) -> str:
    """ADD_MONTHS(date, n) -> date + interval arithmetic; LAST_DAY(date) ->
    a date_trunc()-based end-of-month expression; TRUNC(date, 'fmt') ->
    date_trunc('field', date) for the unambiguous format models in
    _TRUNC_DATE_FORMATS above. Numeric TRUNC(number[, decimals]) is left
    completely alone -- distinguished from the date form by checking
    whether the 2nd argument is a quoted string literal (a format model)
    rather than a numeric literal/expression, which is always true for one
    and never true for the other."""
    def _add_months(args: List[str]) -> str:
        if len(args) == 2:
            issues.append(ConversionIssue(
                "warning",
                "ADD_MONTHS was converted to date + interval arithmetic; note Oracle clamps the "
                "result to the last day of the target month when the original date is itself "
                "month-end (e.g. Jan 31 + 1 month = Feb 28/29), while Postgres's interval arithmetic "
                "does not (Jan 31 + 1 month = Mar 3) -- review any call where the source date can "
                "be a month-end date.",
            ))
            return f"(({args[0]}) + (({args[1]}) * INTERVAL '1 month'))"
        issues.append(ConversionIssue("error", f"ADD_MONTHS with {len(args)} arguments could not be converted automatically."))
        return f"ADD_MONTHS({', '.join(args)})"

    text, _n = transform_function_calls(text, "ADD_MONTHS", _add_months)

    def _last_day(args: List[str]) -> str:
        if len(args) == 1:
            return f"((date_trunc('month', ({args[0]})) + INTERVAL '1 month' - INTERVAL '1 day')::date)"
        issues.append(ConversionIssue("error", f"LAST_DAY with {len(args)} arguments could not be converted automatically."))
        return f"LAST_DAY({', '.join(args)})"

    text, _n = transform_function_calls(text, "LAST_DAY", _last_day)

    def _trunc(args: List[str]) -> str:
        if len(args) == 2 and is_simple_string_literal(args[1]):
            fmt_key = args[1].strip().strip("'").strip().upper()
            pg_field = _TRUNC_DATE_FORMATS.get(fmt_key)
            if pg_field:
                return f"date_trunc('{pg_field}', ({args[0]}))"
            issues.append(ConversionIssue(
                "error",
                f"TRUNC(date, {args[1].strip()}) has no direct date_trunc() equivalent for this "
                "format model (week-based and Oracle-specific formats like WW/IW/W/DAY/DY/D/CC/RM "
                "need a hand-written expression); rewrite manually.",
            ))
            return f"TRUNC({', '.join(args)})"
        # 1-arg TRUNC(number) or TRUNC(date), and 2-arg numeric
        # TRUNC(number, decimals) already work unchanged on Postgres.
        return f"TRUNC({', '.join(args)})"

    text, _n = transform_function_calls(text, "TRUNC", _trunc)

    return text


# --------------------------------------------- DBMS_LOB / DBMS_RANDOM mappings
#
# Shared across all three converters (plsql_converter.py imports these
# directly; tsql_converter.py / db2_converter.py import them the same way
# they already import transform_function_calls) since the rewrite shape is
# identical for all three targets -- only the target function/caveat text
# differs, which is why each is parameterized by `target_fn`/`caveat`
# rather than being three separate copies.


def dbms_lob_getlength_replacer(
    target_fn: str, issues: List[ConversionIssue], caveat: Optional[str] = None
) -> Callable[[List[str]], str]:
    """DBMS_LOB.GETLENGTH(lob) -> <target_fn>(lob) -- a real, safe mapping:
    every target's plain length function accepts a LOB/large-object value
    the same way it accepts any other string/binary value. `caveat`, if
    given, is emitted once per call as an info-level issue noting a target-
    specific behavioral difference worth double-checking (see the SQL
    Server LEN() vs DATALENGTH() caveat at its call site)."""
    def _fn(args: List[str]) -> str:
        if len(args) == 1:
            if caveat:
                issues.append(ConversionIssue("info", caveat))
            return f"{target_fn}({args[0]})"
        issues.append(ConversionIssue(
            "warning", f"DBMS_LOB.GETLENGTH with {len(args)} arguments (expected 1) left unconverted."))
        return f"DBMS_LOB.GETLENGTH({', '.join(args)})"
    return _fn


def dbms_lob_substr_replacer(target_fn: str, issues: List[ConversionIssue]) -> Callable[[List[str]], str]:
    """DBMS_LOB.SUBSTR(lob[, amount[, offset]]) -> <target_fn>(lob, offset,
    amount) -- note the argument-order swap: Oracle's DBMS_LOB.SUBSTR takes
    amount *before* offset (amount defaulting to 32767, offset to 1 if
    omitted), unlike every target's plain substring function, which all
    take (value, start, length) -- offset before amount, matching Oracle's
    own plain (non-LOB) SUBSTR, just not this LOB-specific variant."""
    def _fn(args: List[str]) -> str:
        if len(args) == 1:
            return f"{target_fn}({args[0]}, 1, 32767)"
        if len(args) == 2:
            return f"{target_fn}({args[0]}, 1, {args[1]})"
        if len(args) == 3:
            return f"{target_fn}({args[0]}, {args[2]}, {args[1]})"
        issues.append(ConversionIssue(
            "warning", f"DBMS_LOB.SUBSTR with {len(args)} arguments (expected 1-3) left unconverted."))
        return f"DBMS_LOB.SUBSTR({', '.join(args)})"
    return _fn


_DBMS_RANDOM_VALUE_RE = re.compile(r"\bDBMS_RANDOM\.VALUE\b(?!\s*\()", re.IGNORECASE)


def replace_dbms_random_value(text: str, issues: List[ConversionIssue], replacement: str) -> str:
    """DBMS_RANDOM.VALUE, referenced bare with no parens (Oracle allows a
    parameterless package function to be referenced without an empty
    argument list) -> `replacement` (e.g. 'random()'). Both sides return a
    value in [0, 1). The 2-argument ranged form, DBMS_RANDOM.VALUE(low,
    high), is deliberately left alone (the negative lookahead skips it) --
    out of scope for this mapping, so it still falls through to the generic
    DBMS_* manual marker."""
    n = len(_DBMS_RANDOM_VALUE_RE.findall(text))
    if n:
        text = _DBMS_RANDOM_VALUE_RE.sub(replacement, text)
        issues.append(ConversionIssue(
            "info", f"Converted {n} DBMS_RANDOM.VALUE reference(s) to {replacement}."))
    return text


def _quote_pg(identifier: str) -> str:
    # Lowercased for the same reason as ddl_generator._quote_pg: Postgres
    # folds unquoted identifiers (including the ones inside the largely
    # verbatim SQL bodies this converter passes through) to lowercase, so
    # generated object names must be lowercase to match those references.
    return quote_double(identifier.lower())


# ------------------------------------------------------------- body rewrites

_EXCEPTION_NAME_MAP = {
    "DUP_VAL_ON_INDEX": "unique_violation",
    "ZERO_DIVIDE": "division_by_zero",
}
_UNMAPPABLE_EXCEPTIONS = {
    "VALUE_ERROR", "INVALID_CURSOR", "LOGIN_DENIED", "NOT_LOGGED_ON",
    "PROGRAM_ERROR", "STORAGE_ERROR", "TIMEOUT_ON_RESOURCE",
    "ROWTYPE_MISMATCH", "SUBSCRIPT_BEYOND_COUNT", "SUBSCRIPT_OUTSIDE_LIMIT",
    "COLLECTION_IS_NULL", "CURSOR_ALREADY_OPEN", "INVALID_NUMBER",
}

# high-risk constructs this converter deliberately does not rewrite
# (BULK COLLECT / FORALL are handled by dedicated functions below instead of
# a plain marker -- see _rewrite_bulk_collect_select / _flag_forall)
_MANUAL_MARKERS = [
    (re.compile(r"\bDBMS_(?!OUTPUT\.PUT_LINE)[A-Z_]+", re.IGNORECASE), 3,
     "Uses an Oracle DBMS_* package with no direct Postgres equivalent."),
    (re.compile(r"\bUTL_[A-Z_]+", re.IGNORECASE), 3,
     "Uses an Oracle UTL_* package with no direct Postgres equivalent."),
    (re.compile(r"\bCONNECT BY\b", re.IGNORECASE), 3,
     "Hierarchical query (CONNECT BY) must be rewritten as a recursive CTE."),
    (re.compile(r"\bAUTONOMOUS_TRANSACTION\b", re.IGNORECASE), 3,
     "Autonomous transactions have no equivalent; requires dblink or a separate connection."),
    # Checked *after* rewrite_rownum_topn has already run (see convert_body)
    # -- this only fires for a ROWNUM usage that rewrite didn't recognize
    # as the top-N idiom (e.g. ROWNUM as the first of several AND-ed
    # conditions, or inside a CASE expression), since a recognized one is
    # gone from the text by the time this scan runs.
    (re.compile(r"\bROWNUM\b", re.IGNORECASE), 3,
     "ROWNUM must be rewritten using LIMIT/OFFSET or ROW_NUMBER() OVER (...); the automatic "
     "top-N rewrite only covers ROWNUM as the last condition in a WHERE clause."),
    # Catches Oracle collection/array element ASSIGNMENT syntax
    # (`v_values(1) := 100;`) independently of how the variable was
    # declared. convert_declare_block's unsupported_type_vars check (see
    # its own docstring) only catches this when the collection type was
    # declared with a local `TYPE ... IS TABLE OF/RECORD/REF CURSOR` in the
    # SAME declare section -- it can't see a type declared in the PACKAGE
    # SPECIFICATION (this converter only ever receives the PACKAGE BODY)
    # or an Oracle built-in collection type used directly. Either way,
    # `identifier(args) := value` is unambiguously invalid PL/pgSQL syntax
    # regardless of how (or whether) this converter could resolve the
    # variable's declared type: a valid PL/pgSQL assignment target is only
    # a plain identifier, a `record.field`, or `array[index]` with square
    # brackets -- never `name(...)`. Before this marker existed, a variable
    # like this fell through to a generic (wrong) scalar type mapping with
    # no issue raised at all, and the unmodified `v_values(1):=100;` reached
    # "Apply DDL to Target" as live, broken SQL instead of being flagged
    # MANUAL up front.
    (re.compile(r"\b[A-Za-z_][\w$#]*(?:\.[A-Za-z_][\w$#]*)?\s*\([^()]*\)\s*:="), 3,
     "Assigns to 'name(...)' -- Oracle collection/array element-assignment syntax. PL/pgSQL has no "
     "indexed assignment on a plain variable (valid assignment targets are only a plain identifier, "
     "record.field, or array[index] with square brackets); redesign the variable as a Postgres array "
     "or a mapping table and rewrite every element assignment and reference by hand."),
    # Oracle's pipelined-table-function idiom (`PIPE ROW(record_constructor(...));`
    # inside a `FUNCTION ... RETURN some_tab_type PIPELINED`) streams rows to
    # its caller one at a time as it computes them, so the caller can
    # consume them like a table (`SELECT * FROM TABLE(my_pipelined_func(...))`)
    # before the function has even finished running. PL/pgSQL has no
    # equivalent statement -- a Postgres set-returning function is built
    # from a completely different shape (`RETURNS SETOF sometype` with
    # `RETURN NEXT`/`RETURN QUERY`, or `RETURNS TABLE(...)`), which this
    # converter cannot safely retrofit onto an arbitrary existing routine
    # body: unlike a mechanical rename (SYSDATE, NVL, ...), this requires
    # redesigning the whole function's control flow and return type by
    # hand, so it's flagged here rather than guessed at. Left unconverted,
    # `PIPE ROW(...)` reached "Apply DDL to Target" verbatim and failed
    # with `syntax error at or near "PIPE"` -- Postgres has no PIPE
    # keyword at all.
    (re.compile(r"\bPIPE\s+ROW\b", re.IGNORECASE), 3,
     "PIPE ROW has no PostgreSQL equivalent; a pipelined table function must be redesigned by hand as "
     "a set-returning function (RETURNS SETOF/TABLE with RETURN NEXT / RETURN QUERY) rather than "
     "mechanically converted."),
]

# `SELECT cols BULK COLLECT INTO vars FROM rest;` -- the one BULK COLLECT
# shape this converter actually rewrites (see _rewrite_bulk_collect_select).
# `[^;]+?` rather than `.*?` deliberately keeps each captured piece from
# crossing a statement boundary -- none of a column list, a target-variable
# list, or a FROM-clause (table/WHERE/GROUP BY/ORDER BY) legitimately
# contains a top-level ';' of its own, so this is a safe, simple bound
# without needing full paren/BEGIN-END-depth tracking.
_BULK_COLLECT_SELECT_RE = re.compile(
    r"\bSELECT\s+(?P<cols>[^;]+?)\s+BULK\s+COLLECT\s+INTO\s+(?P<vars>[^;]+?)\s+FROM\s+(?P<rest>[^;]+?);",
    re.IGNORECASE | re.DOTALL,
)

_FETCH_BULK_COLLECT_RE = re.compile(
    r"\bFETCH\s+[A-Za-z_][\w$#]*\s+BULK\s+COLLECT\s+INTO\b", re.IGNORECASE,
)

_FORALL_DML_RE = re.compile(
    r"\bFORALL\b.*?\b(INSERT|UPDATE|DELETE|MERGE)\b", re.IGNORECASE | re.DOTALL,
)


def _rewrite_bulk_collect_select(text: str, issues: List[ConversionIssue]) -> Tuple[str, bool]:
    """Rewrites the common `SELECT c1, c2 BULK COLLECT INTO a1, a2 FROM ...;`
    shape (one Oracle collection per selected column) into Postgres's
    `SELECT array_agg(c1), array_agg(c2) INTO a1, a2 FROM ...;` -- a real,
    safe, mechanical translation for this specific shape, not a guess: both
    sides collect every matching row's values into an array/collection, one
    per column, in a single statement. `SELECT * BULK COLLECT INTO
    single_var` (one collection of whole records) and any column/variable
    count mismatch are left unrewritten and flagged with a specific reason
    instead, since those need a real record/composite array type this
    converter can't safely infer."""
    def _repl(m: "re.Match") -> str:
        cols = [c.strip() for c in split_top_level(m.group("cols"), ",") if c.strip()]
        vars_ = [v.strip() for v in split_top_level(m.group("vars"), ",") if v.strip()]
        rest = m.group("rest").strip()
        if any(c == "*" for c in cols):
            issues.append(ConversionIssue(
                "error",
                "BULK COLLECT INTO with 'SELECT *' collects whole records into a single collection; "
                "Postgres has no direct array-of-record equivalent here -- rewrite using a loop into an "
                "array of a composite type, or list the columns explicitly to collect them as separate arrays.",
            ))
            return m.group(0)
        if not cols or not vars_ or len(cols) != len(vars_):
            issues.append(ConversionIssue(
                "error",
                f"BULK COLLECT INTO has {len(vars_)} target variable(s) for {len(cols)} selected column(s) -- "
                "no automatic array_agg() rewrite could be generated for this mismatched shape; rewrite by "
                "hand as a set-returning query or a fetch loop appending to arrays.",
            ))
            return m.group(0)
        agg_cols = ", ".join(f"array_agg({c})" for c in cols)
        issues.append(ConversionIssue(
            "info",
            f"Rewrote 'BULK COLLECT INTO {', '.join(vars_)}' as array_agg() per column. Note: unlike "
            f"Oracle's BULK COLLECT, which always leaves the target collection(s) empty (never NULL) for a "
            f"zero-row result, Postgres's SELECT ... INTO on zero rows sets the target(s) to NULL -- if the "
            f"caller checks '{vars_[0]} IS NULL' to mean not-found, switch it to a cardinality/array_length "
            f"check, or COALESCE the result to '{{}}'.",
        ))
        return f"SELECT {agg_cols} INTO {', '.join(vars_)} FROM {rest};"

    matched = _BULK_COLLECT_SELECT_RE.search(text) is not None
    return _BULK_COLLECT_SELECT_RE.sub(_repl, text), matched


def _flag_remaining_bulk_collect_and_forall(
    text: str, issues: List[ConversionIssue], select_shape_already_flagged: bool = False
) -> None:
    """Flags whatever BULK COLLECT / FORALL usage is still left in `text`
    after _rewrite_bulk_collect_select has already handled (or specifically
    flagged) the one shape it rewrites -- with a diagnostic tailored to the
    remaining shape instead of one generic marker for both constructs.

    `select_shape_already_flagged` is True whenever
    _rewrite_bulk_collect_select's own regex matched at all (whether it
    rewrote the match or bailed with its own specific issue for a
    mismatched/`SELECT *` shape) -- either way an issue for that exact
    occurrence has already been added, so the generic fallback below must
    not add a second, redundant one for the same leftover 'BULK COLLECT'
    text."""
    if _FETCH_BULK_COLLECT_RE.search(text):
        issues.append(ConversionIssue(
            "error",
            "Cursor 'FETCH ... BULK COLLECT INTO' (optionally with a LIMIT clause, for batched fetching) "
            "has no direct Postgres equivalent -- rewrite as a loop that FETCHes one row at a time and "
            "appends to an array with array_append(), or replace the cursor with a single "
            "'SELECT array_agg(...) INTO ... FROM ...' if the whole result set is small enough to collect "
            "in one shot.",
        ))
    elif not select_shape_already_flagged and re.search(r"\bBULK COLLECT\b", text, re.IGNORECASE):
        # Some other BULK COLLECT shape this converter doesn't specifically
        # recognize (e.g. EXECUTE IMMEDIATE ... BULK COLLECT INTO, or a
        # multi-statement SELECT this scan's semicolon-bounded regex
        # couldn't safely isolate).
        issues.append(ConversionIssue(
            "error",
            "BULK COLLECT has no direct Postgres equivalent for this shape; rewrite using array_agg() in a "
            "single SELECT (for the common per-column-array case) or a loop appending to arrays.",
        ))

    dml_m = _FORALL_DML_RE.search(text)
    if dml_m:
        dml = dml_m.group(1).upper()
        issues.append(ConversionIssue(
            "error",
            f"FORALL ... {dml} has no direct Postgres equivalent -- rewrite as a set-based {dml} driven "
            f"directly off the source array/table (e.g. '{dml} ... FROM unnest($1) AS t(...)' or a join "
            f"against a temp table), or as a plain loop over the array issuing one {dml} per element if a "
            f"set-based rewrite isn't practical.",
        ))
    elif re.search(r"\bFORALL\b", text, re.IGNORECASE):
        issues.append(ConversionIssue(
            "error",
            "FORALL has no direct Postgres equivalent; rewrite as a set-based DML statement driven off the "
            "source array/table, or as a plain loop issuing one statement per element.",
        ))


def _convert_declared_type(type_text: str) -> str:
    """Map a declared Oracle type unless it's an anchored %TYPE/%ROWTYPE
    reference, which Postgres supports natively as-is."""
    if re.search(r"%TYPE|%ROWTYPE", type_text, re.IGNORECASE):
        return type_text
    mapped, _issues = type_mapping.to_postgres(type_text)
    return mapped


# Oracle's implicit-cursor FOR loop (`FOR rec IN cursor_name LOOP` or
# `FOR rec IN (SELECT ...) LOOP` / `FOR rec IN SELECT ... LOOP`) never
# declares its loop variable at all — Oracle infers it automatically.
# PostgreSQL's PL/pgSQL is *documented* to do the same for an undeclared
# target, but real-world testing against a live target ran into
# "loop variable of loop over rows must be a record variable or list of
# scalar variables" with the variable left undeclared. An explicit
# `<name> RECORD;` declaration (the form used in PostgreSQL's own
# documentation examples) is the reliable, safe-either-way choice, so this
# converter emits one for every such loop variable — see
# _ensure_row_loop_vars_declared. Numeric range loops (`FOR i IN 1..10
# LOOP`) are intentionally excluded below since they behave differently
# (Postgres shadows any existing declaration rather than requiring one).
_ROW_OR_CURSOR_FOR_LOOP_RE = re.compile(
    r"\bFOR\s+([A-Za-z_][\w$#]*)\s+IN\s+"
    r"(?:\(|SELECT\b|[A-Za-z_][\w$#]*\s*(?:\([^)]*\))?\s+LOOP\b)",
    re.IGNORECASE,
)


def _find_row_loop_variables(body_text: str) -> set:
    """Names used as the loop variable of a row/cursor FOR loop anywhere in
    the routine body — these must end up declared as RECORD (or a valid
    row-compatible type) in Postgres; see _ensure_row_loop_vars_declared."""
    return {m.group(1) for m in _ROW_OR_CURSOR_FOR_LOOP_RE.finditer(body_text)}


def _names_with_valid_record_declaration(declare_text: str) -> set:
    """Names already declared in the DECLARE section with a row-compatible
    type (table/view/cursor %ROWTYPE, or RECORD) — these already satisfy a
    row/cursor FOR loop's requirements and shouldn't get a second, redundant
    RECORD declaration appended alongside them."""
    names = set()
    for stmt in split_top_level(declare_text, ";"):
        stmt = stmt.strip()
        var_m = re.match(r"^([A-Za-z_][\w$#]*)\s+(.+)$", stmt, re.DOTALL)
        if not var_m:
            continue
        name, rest = var_m.group(1), var_m.group(2)
        if re.search(r"%ROWTYPE\b", rest, re.IGNORECASE) or re.match(r"^\s*RECORD\b", rest, re.IGNORECASE):
            names.add(name)
    return names


def _ensure_row_loop_vars_declared(
    declare_sql: str, issues: List[ConversionIssue], row_loop_vars: set, declare_text: str
) -> Tuple[str, List[ConversionIssue]]:
    """Append an explicit `<name> RECORD;` declaration for every row/cursor
    FOR-loop target that doesn't already have a valid row-compatible
    declaration. See the comment above _ROW_OR_CURSOR_FOR_LOOP_RE for why
    this converter declares these explicitly rather than relying on
    Postgres's undeclared-target auto-creation."""
    if not row_loop_vars:
        return declare_sql, issues
    already_valid = {n.upper() for n in _names_with_valid_record_declaration(declare_text)}
    needs_decl = sorted(n for n in row_loop_vars if n.upper() not in already_valid)
    if not needs_decl:
        return declare_sql, issues
    record_lines = "\n".join(f"  {name} RECORD;" for name in needs_decl)
    declare_sql = f"{declare_sql}\n{record_lines}" if declare_sql.strip() else record_lines
    issues.append(ConversionIssue(
        "info",
        f"Explicitly declared {', '.join(needs_decl)} as RECORD for use as an implicit-cursor "
        f"FOR-loop target; Oracle never requires this declaration but it is emitted here for "
        f"reliable PostgreSQL compilation.",
    ))
    return declare_sql, issues


def convert_declare_block(
    text: str, skip_names: Optional[set] = None
) -> Tuple[str, List[ConversionIssue], set]:
    """Convert the DECLARE section (everything between IS/AS and BEGIN):
    variable/constant declarations, cursor declarations, PRAGMA and
    EXCEPTION declarations.

    `skip_names` (case-insensitive) are variable names that are used as the
    loop variable of an Oracle implicit-cursor FOR loop elsewhere in the
    routine; Postgres requires such a loop variable to be an unshadowed
    RECORD it creates itself, so any pre-declaration for that name here is
    dropped rather than translated (see _find_row_loop_variables).

    Returns (declare_sql, issues, unsupported_type_vars). A local
    `TYPE t IS TABLE OF / RECORD / REF CURSOR ...;` declaration is already
    flagged and commented out on its own line below, but a *variable*
    later declared with that local type name (e.g. `v_ids t_id_list;`)
    used to fall straight through to _convert_declared_type, which has no
    idea `t_id_list` means anything and silently mapped it to some
    unrelated scalar default (observed in the field as `v_ids TEXT;`) --
    while the body's Oracle-only element syntax for it (`v_ids(1) := 10;`
    or a `.COUNT`/`.FIRST`/`.LAST`/`.EXISTS`/`.DELETE` method call) was left
    completely untouched, since collection-element manipulation has no
    safe mechanical translation (see this module's own docstring). That
    combination reached "Apply DDL to Target" as syntactically-plausible
    but broken PL/pgSQL and failed there instead of being flagged up
    front. `unsupported_type_vars` names every variable declared with a
    locally-unsupported type, so the caller (convert_procedure_or_function
    / convert_trigger) can bail the *whole* routine out to a manual
    placeholder instead of emitting DDL doomed to fail at apply time."""
    issues: List[ConversionIssue] = []
    text, nested = extract_nested_subprograms(text)
    out_lines: List[str] = []
    local_unsupported_types: set = set()
    unsupported_type_vars: set = set()
    for ns in nested:
        out_lines.append(
            f"  -- MANUAL CONVERSION REQUIRED: nested {ns.kind} {ns.name}, declared inside this "
            f"routine's own DECLARE section -- PL/pgSQL has no nested named-subprogram declaration.\n"
            f"  -- Extract it as a standalone function/procedure, or inline its logic by hand. "
            f"Original source:\n"
            f"  /*\n{ns.source}\n  */"
        )
        issues.append(ConversionIssue(
            "error",
            f"Nested {ns.kind} '{ns.name}' is declared inside this routine's own DECLARE section; "
            f"PL/pgSQL has no nested named-subprogram declaration -- extract it as a standalone "
            f"function/procedure, or inline its logic by hand.",
        ))
    statements = split_top_level(text, ";")
    skip_upper = {n.upper() for n in (skip_names or set())}

    for stmt in statements:
        stmt = stmt.strip()
        if not stmt:
            continue

        if re.match(r"^TYPE\s+", stmt, re.IGNORECASE):
            type_name_m = re.match(r"^TYPE\s+([A-Za-z_][\w$#]*)", stmt, re.IGNORECASE)
            if type_name_m:
                local_unsupported_types.add(type_name_m.group(1).upper())
            out_lines.append(f"  -- MANUAL CONVERSION REQUIRED: {stmt};")
            issues.append(ConversionIssue(
                "error",
                f"Local type declaration '{stmt};' (TABLE OF / RECORD / REF CURSOR) has no direct "
                f"Postgres equivalent -- rewrite using an array, a composite/row type, or a refcursor "
                f"variable as appropriate, and update every reference to it in this routine's body.",
            ))
            continue

        if re.match(r"^PRAGMA\b", stmt, re.IGNORECASE):
            out_lines.append(f"  -- (removed) {stmt};  -- Postgres has no PRAGMA; review if this affected behavior")
            issues.append(ConversionIssue("warning", f"Dropped PRAGMA directive: {stmt.strip()};"))
            continue

        cursor_m = re.match(
            r"^CURSOR\s+([A-Za-z_][\w$#]*)\s*(\([^)]*\))?\s+IS\s+(.*)$",
            stmt, re.IGNORECASE | re.DOTALL,
        )
        if cursor_m:
            cur_name, cur_params, cur_query = cursor_m.groups()
            # A parameterized cursor's parameter list is a second, separate
            # place a declared TYPE reaches the target -- distinct from a
            # routine's own parameter list (see convert_procedure_or_function)
            # but needing exactly the same _convert_declared_type mapping.
            # Before this fix, cur_params was passed through completely
            # unmapped, so `CURSOR cur_x(cp_category VARCHAR2) IS ...`
            # reached "Apply DDL to Target" with literal Oracle "VARCHAR2"
            # still in it -- Postgres has no such type name and rejected the
            # whole CREATE FUNCTION/PROCEDURE with 'type "varchar2" does not
            # exist'. Oracle cursor parameters never carry an IN/OUT mode
            # keyword (they're always effectively IN), so parse_param's
            # default mode is simply discarded here rather than emitted.
            params_clause = ""
            if cur_params:
                inner = cur_params.strip()[1:-1]
                mapped_params = []
                for raw_param in split_top_level(inner, ","):
                    raw_param = raw_param.strip()
                    if not raw_param:
                        continue
                    p_name, _p_mode, p_type_text, p_default = parse_param(raw_param)
                    piece = f"{p_name} {_convert_declared_type(p_type_text)}"
                    if p_default:
                        piece += f" DEFAULT {p_default}"
                    mapped_params.append(piece)
                params_clause = f"({', '.join(mapped_params)})"
            cur_query = cur_query.strip()
            # Same four gaps convert_body applies to a body statement's own
            # SELECTs (see their docstrings): a CURSOR's query is just as
            # much a SELECT reaching the target verbatim, and this is a
            # separate call site from convert_body's because a cursor
            # declaration lives in the DECLARE section, not the body.
            cur_query = rewrite_rownum_topn(cur_query, issues)
            cur_query = rewrite_listagg(cur_query, issues)
            cur_query = rewrite_regexp_functions(cur_query, issues)
            cur_query = rewrite_oracle_date_functions(cur_query, issues)
            out_lines.append(f"  {cur_name} CURSOR{params_clause} FOR {cur_query};")
            # _MANUAL_MARKERS only scans the executable body text (see
            # convert_body), so a CONNECT BY that's still present here --
            # i.e. connect_by_rewriter.py already had its shot at it (from
            # convert_routine, before this function ever runs) and it
            # didn't match the supported simple shape -- would otherwise
            # never get flagged at all.
            if re.search(r"\bCONNECT\s+BY\b", cur_query, re.IGNORECASE):
                issues.append(ConversionIssue(
                    "error",
                    f"Cursor '{cur_name}': hierarchical query (CONNECT BY) must be rewritten as a recursive CTE.",
                ))
            continue

        exc_m = re.match(r"^([A-Za-z_][\w$#]*)\s+EXCEPTION\s*$", stmt, re.IGNORECASE)
        if exc_m:
            out_lines.append(f"  -- (kept for reference only) {stmt};  -- Postgres has no user-defined EXCEPTION type")
            issues.append(ConversionIssue(
                "warning",
                f"Exception '{exc_m.group(1)}' is declared but Postgres has no user-defined EXCEPTION "
                f"type; every RAISE and WHEN referencing it in this routine's body was rewritten to use "
                f"a synthetic SQLSTATE code instead (see convert_body) -- review only if code outside "
                f"this routine needs to catch it by a *specific*, stable SQLSTATE of its own choosing.",
            ))
            continue

        var_m = re.match(r"^([A-Za-z_][\w$#]*)\s+(.+)$", stmt, re.DOTALL)
        if not var_m:
            out_lines.append(f"  -- MANUAL REVIEW: could not parse declaration: {stmt};")
            issues.append(ConversionIssue("error", f"Could not parse declaration: {stmt};"))
            continue

        name, rest = var_m.group(1), var_m.group(2).strip()

        if name.upper() in skip_upper:
            out_lines.append(
                f"  -- (removed) {stmt};  -- '{name}' is used as an implicit FOR-loop record "
                f"variable; Postgres creates it automatically and rejects a pre-declaration here."
            )
            issues.append(ConversionIssue(
                "info",
                f"Dropped declaration of '{name}': it is used as a row/cursor FOR-loop variable, which "
                f"Postgres requires to be auto-created as a RECORD rather than pre-declared.",
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

        referenced_local_types = {
            t for t in local_unsupported_types
            if re.search(rf"\b{re.escape(t)}\b", type_text.upper())
        }
        if referenced_local_types:
            unsupported_type_vars.add(name)
            out_lines.append(f"  -- MANUAL CONVERSION REQUIRED: {stmt};")
            issues.append(ConversionIssue(
                "error",
                f"'{name}' is declared as {type_text}, a locally-declared Oracle collection/record/REF "
                f"CURSOR type with no direct Postgres equivalent -- every reference to '{name}' in this "
                f"routine's body (element access such as '{name}(i) := ...', or a collection method like "
                f".COUNT/.FIRST/.LAST/.EXISTS/.DELETE) must be rewritten by hand using a Postgres array, "
                f"composite type, or refcursor variable as appropriate.",
            ))
            continue

        mapped_type = _convert_declared_type(type_text)
        pieces = [name]
        if is_constant:
            pieces.append("CONSTANT")
        pieces.append(mapped_type)
        if not_null:
            pieces.append("NOT NULL")
        line = "  " + " ".join(pieces)
        if default:
            line += f" := {default}"
        line += ";"
        out_lines.append(line)

    return "\n".join(out_lines), issues, unsupported_type_vars


_SAVEPOINT_RE = re.compile(r"\bSAVEPOINT\s+([A-Za-z_][\w$#]*)\s*;", re.IGNORECASE)
_ROLLBACK_TO_RE = re.compile(r"\bROLLBACK\s+TO\s+(?:SAVEPOINT\s+)?([A-Za-z_][\w$#]*)\s*;", re.IGNORECASE)
_BEGIN_OR_EXCEPTION_RE = re.compile(r"\b(BEGIN|EXCEPTION)\b", re.IGNORECASE)


def _rewrite_savepoint_and_rollback_to(text: str, issues: List[ConversionIssue]) -> str:
    """Oracle's `SAVEPOINT name;` and the matching `ROLLBACK TO name;` mark
    and undo to a mid-procedure checkpoint. PL/pgSQL has no equivalent
    executable SAVEPOINT/ROLLBACK TO statement inside a function or
    procedure body at all -- entering a `BEGIN ... EXCEPTION ... END`
    block already establishes an implicit savepoint the instant it is
    entered, and rolls back to it automatically the instant an exception
    is caught, *before* the handler even runs. That makes Oracle's very
    common

        SAVEPOINT before_update;
        ...
        EXCEPTION WHEN OTHERS THEN
          ROLLBACK TO before_update;
          RAISE_APPLICATION_ERROR(-20001, ...);

    idiom entirely redundant on Postgres: by the time WHEN OTHERS runs,
    the rollback has already happened. Left untouched, `ROLLBACK TO
    before_update;` reaches Postgres verbatim, which parses it as the
    top-level transaction-control `ROLLBACK TO SAVEPOINT` statement --
    not legal inside a plpgsql function/procedure body -- and rejects it
    with a syntax error at "TO". A real migration's
    FEATURE_PL_ADMIN_PKG hit exactly this, on the statement right after
    the Round 32 RAISE_APPLICATION_ERROR fix let "Apply DDL to Target"
    get that far.

    SAVEPOINT is always safe to drop: it is never itself the cause of
    any behavior a Postgres function needs, so it is commented out
    unconditionally (kept for reference, like the exception-declaration
    handling above).

    ROLLBACK TO needs different treatment depending on *where* it
    appears, found with a simple "nearest preceding BEGIN-or-EXCEPTION
    keyword" scan over this routine's own body text (which always starts
    with a literal BEGIN -- see parse_routine_header/convert_trigger --
    so there is always at least one marker to find):

      - If the nearest preceding marker is EXCEPTION, this ROLLBACK TO
        is inside an exception handler -- safe to drop, since it is
        redundant with the implicit rollback-on-catch described above.
      - If the nearest preceding marker is BEGIN (i.e. still in the
        main executable body, before any EXCEPTION section), this is a
        deliberate mid-flow rollback decision with no safe automatic
        translation -- dropping it could silently change behavior, so
        it is left untouched and flagged as an error for manual
        rewrite (as a nested BEGIN...EXCEPTION...END block that catches
        the condition this was meant to roll back from) instead.

    String literals and comments are masked out first (reusing the same
    approach as _resolve_trigger_event_pseudocolumns above), so neither
    a logging message like `RAISE NOTICE 'rollback to the last save
    point'` nor a comment mentioning either keyword is mistaken for the
    real statement.
    """
    chunks: List[str] = []

    def _mask(match: "re.Match") -> str:
        chunks.append(match.group(0))
        return f"\x00{len(chunks) - 1}\x00"

    masked = _STRING_OR_COMMENT_RE.sub(_mask, text)

    # ROLLBACK TO must be substituted *before* SAVEPOINT, for two reasons:
    #   1. `\bSAVEPOINT\s+name\s*;` would otherwise also match the
    #      "SAVEPOINT name;" tail of `ROLLBACK TO SAVEPOINT name;` (Oracle's
    #      optional-keyword form), truncating off the "ROLLBACK TO " prefix
    #      before this function ever gets a chance to recognize it.
    #   2. The nearest-preceding-marker scan below must see the *source's*
    #      real BEGIN/EXCEPTION keywords only -- if SAVEPOINT's own
    #      replacement comment text (see _savepoint_repl) ran first and
    #      happened to mention either word, a later ROLLBACK TO's scan
    #      could mistake that comment for a real marker.
    def _rollback_to_repl(m: "re.Match") -> str:
        name = m.group(1)
        preceding = masked[:m.start()]
        markers = list(_BEGIN_OR_EXCEPTION_RE.finditer(preceding))
        in_handler = bool(markers) and markers[-1].group(1).upper() == "EXCEPTION"
        if in_handler:
            issues.append(ConversionIssue(
                "info",
                f"Dropped ROLLBACK TO {name} inside an exception handler -- Postgres's "
                "BEGIN...EXCEPTION block already rolled back any partial work from this block the "
                "instant the exception was caught, before the handler runs, so this statement was "
                "redundant.",
            ))
            return (f"-- (kept for reference only) ROLLBACK TO {name};  "
                    f"-- redundant here: already rolled back automatically")
        issues.append(ConversionIssue(
            "error",
            f"ROLLBACK TO {name} appears outside an exception handler -- Postgres has no equivalent "
            "explicit SAVEPOINT/ROLLBACK TO statement inside a function/procedure body; rewrite this "
            "as a nested BEGIN ... EXCEPTION ... END block that catches the condition this was meant "
            "to roll back from.",
        ))
        return m.group(0)

    masked = _ROLLBACK_TO_RE.sub(_rollback_to_repl, masked)

    # SAVEPOINT itself is always safe to drop -- run only now that every
    # ROLLBACK TO has already been matched and replaced, so a `ROLLBACK TO
    # SAVEPOINT name;` (Oracle's optional-keyword form) is never seen here
    # as a bare, truncatable "SAVEPOINT name;" tail. The replacement
    # comment deliberately avoids the words BEGIN/EXCEPTION together, so it
    # can never be mistaken for a real marker by another ROLLBACK TO
    # elsewhere in the same body (moot now given the ordering above, but a
    # cheap second guard against the same class of bug).
    def _savepoint_repl(m: "re.Match") -> str:
        name = m.group(1)
        issues.append(ConversionIssue(
            "info",
            f"Dropped SAVEPOINT {name} -- PL/pgSQL has no equivalent executable SAVEPOINT statement "
            "inside a function/procedure body; entering a BEGIN...EXCEPTION...END block already "
            "establishes an implicit savepoint of its own.",
        ))
        return (f"-- (kept for reference only) SAVEPOINT {name};  "
                f"-- redundant here: already covered by Postgres's implicit per-block savepoint")

    masked = _SAVEPOINT_RE.sub(_savepoint_repl, masked)

    def _unmask(match: "re.Match") -> str:
        return chunks[int(match.group(1))]

    return re.sub(r"\x00(\d+)\x00", _unmask, masked)


# A statement-level "name(args)" or "pkg.name(args)" immediately preceded
# by a statement boundary -- ';', or one of the keywords that open a new
# statement sequence with no ';' of its own (BEGIN, THEN, ELSE, LOOP) --
# and, per _rewrite_bare_procedure_calls's balanced-paren check, followed
# by nothing but whitespace before the next ';'.
_STATEMENT_BOUNDARY_CALL_RE = re.compile(
    r"(;|\bBEGIN\b|\bTHEN\b|\bELSE\b|\bLOOP\b)(\s*)([A-Za-z_][\w$#]*(?:\.[A-Za-z_][\w$#]*)?)\s*\(",
    re.IGNORECASE,
)

# Statement-leading keywords that can be immediately followed by '(' without
# being a procedure call -- e.g. `IF (a = 1) THEN`, `RETURN(expr);`,
# `OPEN cur(param);` (the parameterized-cursor OPEN form; note the space
# before '(' there means it never actually reaches this identifier slot --
# listed anyway as a second, explicit guard), or a package/DBMS_* call
# already handled by an earlier, more specific rewrite (RAISE_APPLICATION_
# ERROR, DBMS_OUTPUT.PUT_LINE, DBMS_LOB.*, EXECUTE IMMEDIATE) by the time
# this function runs.
_BARE_CALL_EXCLUDED_IDENTIFIERS = {
    "IF", "CASE", "WHILE", "FOR", "LOOP", "BEGIN", "DECLARE", "END",
    "EXCEPTION", "RETURN", "EXIT", "CONTINUE", "NULL", "RAISE",
    "EXECUTE", "OPEN", "CLOSE", "FETCH", "COMMIT", "ROLLBACK",
    "SAVEPOINT", "PERFORM", "CALL", "SELECT", "INSERT", "UPDATE",
    "DELETE", "MERGE", "WITH", "GOTO", "PIPE", "RAISE_APPLICATION_ERROR",
}


def _rewrite_bare_procedure_calls(text: str, issues: List[ConversionIssue]) -> str:
    """Oracle allows a procedure (never a function -- PL/SQL has no way to
    call a function and discard its result other than assigning it, so any
    surviving bare `name(args);` statement, by Oracle's own grammar, can
    only be a procedure call) to be invoked as a standalone statement, with
    no leading keyword: `feature_pl_create_row(p_id, p_name);`. PL/pgSQL
    has no such bare-call statement form at all -- every statement must
    start with a keyword its parser recognizes, and a plain identifier is
    not one, so this reaches Postgres as
    `syntax error at or near "feature_pl_create_row"` the moment "Apply DDL
    to Target" tries to run it, 90-something statements into a real
    migration, instead of ever being flagged during conversion. The fix:
    Postgres's own `CALL name(args);` statement is the direct, always-
    correct equivalent for exactly this shape, so this is a real rewrite
    (not a MANUAL flag) -- unlike PIPE ROW below, which has no Postgres
    equivalent to rewrite to at all.

    Detection is declaration-independent, the same design already used for
    the collection-element-assignment MANUAL marker above: scan the body
    text itself for "name(" immediately after a statement boundary --
    ';', or a keyword that opens a new statement sequence with no ';' of
    its own (BEGIN/THEN/ELSE/LOOP) -- with a balanced-paren scan (reusing
    transform_function_calls's own approach, since call arguments routinely
    contain nested calls of their own, e.g. `do_x(NVL(v_id, 0), v_name);`)
    to find the matching close paren, and only rewriting when nothing but
    whitespace separates that close paren from the next ';' -- i.e. the
    call really is the *entire* statement, not a call embedded inside a
    larger expression like `v_total := do_x(v_a) + v_b;` or the condition
    of `IF do_x(v_a) THEN`, which need no CALL keyword and must not be
    prefixed with one.
    """
    out: List[str] = []
    pos = 0
    while True:
        m = _STATEMENT_BOUNDARY_CALL_RE.search(text, pos)
        if not m:
            out.append(text[pos:])
            break
        ident = m.group(3)
        if ident.split(".")[0].upper() in _BARE_CALL_EXCLUDED_IDENTIFIERS:
            out.append(text[pos : m.end()])
            pos = m.end()
            continue

        depth = 1
        i = m.end()
        in_string = False
        while i < len(text) and depth > 0:
            ch = text[i]
            if ch == "'":
                in_string = not in_string
            elif not in_string:
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
            i += 1
        if depth != 0:
            # Unbalanced parens (shouldn't happen in valid source) -- leave
            # untouched rather than guess.
            out.append(text[pos : m.end()])
            pos = m.end()
            continue

        if not text[i:].lstrip().startswith(";"):
            # The call is part of a larger expression/condition, not a
            # standalone statement -- leave it exactly as-is.
            out.append(text[pos : m.end()])
            pos = m.end()
            continue

        out.append(text[pos : m.start(3)])
        out.append("CALL ")
        out.append(text[m.start(3) : m.end()])
        pos = m.end()
        issues.append(ConversionIssue(
            "info",
            f"Added CALL to the bare procedure-call statement '{ident}(...)' -- Oracle allows an "
            "unqualified procedure invocation as a standalone statement, but PL/pgSQL requires the "
            "CALL keyword; left as-is this fails with a syntax error at the procedure's own name.",
        ))
    return "".join(out)


def convert_body(text: str, declared_exceptions: Optional[List[str]] = None,
                  exception_codes: Optional[Dict[str, str]] = None) -> Tuple[str, List[ConversionIssue]]:
    """Convert the executable BEGIN...END body: builtin function rewrites,
    sequence references, DUAL removal, EXECUTE IMMEDIATE, RAISE_APPLICATION_ERROR,
    exception-name remapping, and :NEW/:OLD for triggers.

    `exception_codes`, if given, is a {name: SQLSTATE code} mapping for
    exceptions whose code was already decided *before* this call -- used
    by convert_package_body for a PACKAGE-level exception referenced by
    more than one flattened member (see that function's own docstring).
    `declared_exceptions` (routine-local: declared inside *this* routine's
    own DECLARE section) is still numbered here, independently per call,
    exactly as before; the two are disjoint in the normal case, since a
    package-level exception is never in any one member's own declare
    text."""
    issues: List[ConversionIssue] = []
    declared_exceptions = declared_exceptions or []
    exception_codes = exception_codes or {}

    # BULK COLLECT (the single-SELECT-into-per-column-arrays shape) --
    # run first, before any other rewrite below has a chance to touch the
    # SELECT/FROM text this scans for.
    text, select_bulk_collect_seen = _rewrite_bulk_collect_select(text, issues)

    # SYSDATE / SYSTIMESTAMP
    text = re.sub(r"\bSYSDATE\b", "CURRENT_TIMESTAMP", text, flags=re.IGNORECASE)
    text = re.sub(r"\bSYSTIMESTAMP\b", "CURRENT_TIMESTAMP", text, flags=re.IGNORECASE)

    # NVL -> COALESCE (identical arg semantics, just a rename)
    text = re.sub(r"\bNVL\s*\(", "COALESCE(", text, flags=re.IGNORECASE)

    # SYS_CONTEXT('USERENV', 'parameter') -- Oracle's generic session/
    # environment lookup, used in triggers/procedures far more often than
    # its obscurity suggests (e.g. an audit trigger stamping SESSION_USER
    # onto a row). Left unconverted before this, it reached the target
    # verbatim: `sys_context(unknown, unknown) does not exist` blows up the
    # very first time the routine actually runs (a real production
    # migration hit exactly this in an audit trigger, failing every insert
    # into the table it was attached to). Only the handful of USERENV
    # parameters with a genuinely equivalent PostgreSQL session keyword/
    # function are rewritten; everything else (client host, IP address, OS
    # user, session id, a caller's own custom CONTEXT namespace, ...) has
    # no faithful Postgres equivalent and is left untouched but flagged,
    # rather than mapped to something that merely compiles but returns the
    # wrong value at runtime -- the same "don't guess" rule INSTR's 3/4-arg
    # forms and DECODE's shape mismatches already follow in this file.
    text = replace_sys_context(text, issues)

    # ROWNUM top-N / LISTAGG / REGEXP_* / date-arithmetic gaps -- see each
    # function's own docstring above for exactly what is and isn't
    # rewritten. These apply to a routine's own embedded SELECTs the same
    # way generate_view_ddl applies them to a view's SELECT text.
    text = rewrite_rownum_topn(text, issues)
    text = rewrite_listagg(text, issues)
    text = rewrite_regexp_functions(text, issues)
    text = rewrite_oracle_date_functions(text, issues)

    # NVL2(a, b, c) -> CASE WHEN (a) IS NOT NULL THEN b ELSE c END
    def _nvl2(args: List[str]) -> str:
        if len(args) == 3:
            return f"CASE WHEN ({args[0]}) IS NOT NULL THEN {args[1]} ELSE {args[2]} END"
        issues.append(ConversionIssue("error", f"NVL2 with {len(args)} arguments could not be converted automatically."))
        return f"NVL2({', '.join(args)})"

    text, n = transform_function_calls(text, "NVL2", _nvl2)

    # DECODE(expr, s1, r1, s2, r2, ..., default) -> CASE expr WHEN s1 THEN r1 ... [ELSE default] END
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
            "— review any branch that relies on that Oracle-specific NULL-matching behavior.",
        ))
        return "\n".join(lines)

    text, n = transform_function_calls(text, "DECODE", _decode)

    # INSTR(str, substr) -> POSITION(substr IN str); 3/4-arg INSTR flagged, not guessed
    def _instr(args: List[str]) -> str:
        if len(args) == 2:
            return f"POSITION({args[1]} IN {args[0]})"
        issues.append(ConversionIssue(
            "warning",
            f"INSTR with {len(args)} arguments (start position / occurrence) has no direct POSITION() "
            "equivalent; left as INSTR() — review and rewrite manually.",
        ))
        return f"INSTR({', '.join(args)})"

    text, n = transform_function_calls(text, "INSTR", _instr)

    # DBMS_OUTPUT.PUT_LINE(x) -> RAISE NOTICE '%', x
    def _put_line(args: List[str]) -> str:
        if len(args) == 1:
            return f"RAISE NOTICE '%', {args[0]}"
        issues.append(ConversionIssue("warning", "DBMS_OUTPUT.PUT_LINE with unexpected argument count left unconverted."))
        return f"DBMS_OUTPUT.PUT_LINE({', '.join(args)})"

    text, n = transform_function_calls(text, r"DBMS_OUTPUT\.PUT_LINE", _put_line)
    if n:
        issues.append(ConversionIssue("info", f"Converted {n} DBMS_OUTPUT.PUT_LINE call(s) to RAISE NOTICE."))

    # DBMS_LOB.GETLENGTH(lob) -> LENGTH(lob); DBMS_LOB.SUBSTR(lob, amount, offset) -> SUBSTR(lob, offset, amount)
    text, _n = transform_function_calls(text, r"DBMS_LOB\.GETLENGTH", dbms_lob_getlength_replacer("LENGTH", issues))
    text, _n = transform_function_calls(text, r"DBMS_LOB\.SUBSTR", dbms_lob_substr_replacer("SUBSTR", issues))

    # DBMS_RANDOM.VALUE (no-arg form) -> random()
    text = replace_dbms_random_value(text, issues, "random()")

    # RAISE_APPLICATION_ERROR(-20001, 'message') -> RAISE EXCEPTION USING MESSAGE = 'message'
    def _raise_app_error(args: List[str]) -> str:
        if len(args) >= 2:
            issues.append(ConversionIssue(
                "warning",
                f"RAISE_APPLICATION_ERROR error code {args[0]} was dropped; Postgres uses SQLSTATE codes "
                "instead — add `USING ERRCODE = '...'` if the caller depends on the specific code.",
            ))
            # `RAISE EXCEPTION 'format', args...` requires a *literal* format
            # string in that position -- a message built with Oracle's 'text'
            # || variable concatenation used to be emitted there verbatim
            # (`RAISE EXCEPTION 'text'||variable`), which is a syntax error,
            # not a runtime one: it fails "Apply DDL to Target" outright, on
            # every function/procedure/trigger with a computed error message.
            # PL/pgSQL's argument-free `RAISE ... USING option = expression`
            # form takes any expression in that position -- literal or
            # computed alike -- so this needs no special-casing for which
            # kind args[1] is (see is_simple_string_literal's own docstring
            # for why MySQL/SQL Server can't use this same fix).
            return f"RAISE EXCEPTION USING MESSAGE = {args[1]}"
        issues.append(ConversionIssue("error", "RAISE_APPLICATION_ERROR could not be parsed."))
        return f"RAISE_APPLICATION_ERROR({', '.join(args)})"

    text, n = transform_function_calls(text, "RAISE_APPLICATION_ERROR", _raise_app_error)

    # sequence NEXTVAL / CURRVAL
    def _seq_ref(m: re.Match, fn: str) -> str:
        return f"{fn}('{_quote_pg(m.group(1))}')"

    text = re.sub(r"\b([A-Za-z_][\w$#]*)\.NEXTVAL\b",
                  lambda m: _seq_ref(m, "nextval"), text, flags=re.IGNORECASE)
    text = re.sub(r"\b([A-Za-z_][\w$#]*)\.CURRVAL\b",
                  lambda m: _seq_ref(m, "currval"), text, flags=re.IGNORECASE)

    # EXECUTE IMMEDIATE -> EXECUTE
    text = re.sub(r"\bEXECUTE\s+IMMEDIATE\b", "EXECUTE", text, flags=re.IGNORECASE)

    # FROM DUAL -> (removed)
    text = re.sub(r"\bFROM\s+DUAL\b", "", text, flags=re.IGNORECASE)

    # SAVEPOINT / ROLLBACK TO -- see _rewrite_savepoint_and_rollback_to's
    # own docstring for why these need different treatment inside vs.
    # outside an exception handler.
    text = _rewrite_savepoint_and_rollback_to(text, issues)

    # Bare procedure-call statements -- see _rewrite_bare_procedure_calls's
    # own docstring. Runs after the RAISE_APPLICATION_ERROR / DBMS_OUTPUT.
    # PUT_LINE / EXECUTE IMMEDIATE rewrites above, so none of those already-
    # handled shapes are still sitting around looking like a plain
    # "name(args);" statement by the time this scans for one.
    text = _rewrite_bare_procedure_calls(text, issues)

    # trigger correlation names
    text = re.sub(r":NEW\.", "NEW.", text, flags=re.IGNORECASE)
    text = re.sub(r":OLD\.", "OLD.", text, flags=re.IGNORECASE)

    # named-exception remapping in WHEN clauses
    for oracle_name, pg_name in _EXCEPTION_NAME_MAP.items():
        text = re.sub(rf"\bWHEN\s+{oracle_name}\s+THEN", f"WHEN {pg_name} THEN", text, flags=re.IGNORECASE)

    for oracle_name in _UNMAPPABLE_EXCEPTIONS:
        if re.search(rf"\bWHEN\s+{oracle_name}\s+THEN", text, re.IGNORECASE):
            issues.append(ConversionIssue(
                "error",
                f"Exception handler WHEN {oracle_name} THEN has no direct Postgres equivalent condition name; "
                "rewrite using a SQLSTATE check or a custom RAISE.",
            ))

    # User-defined exceptions: Postgres has no user-defined exception
    # *type* -- only SQLSTATE codes -- so a RAISE of one and a WHEN
    # clause naming it need a code that stands in for its identity, and
    # both sides need the *same* code or the handler won't actually catch
    # what was raised. A stable code is minted here per exception name (in
    # declaration order, so the same routine always gets the same codes
    # across repeated conversions), avoiding '00000' (reserved, means "no
    # error") and never ending in three zeroes (those are Postgres
    # *category* codes, trappable only by trapping the whole category) --
    # see https://www.postgresql.org/docs/current/plpgsql-errors-and-messages.html.
    # Before this fix, `RAISE exc_name;` was rewritten but a `WHEN
    # exc_name THEN` naming the same exception was left completely
    # untouched -- Postgres then tried to parse "exc_name" itself as a
    # *condition name* (like division_by_zero) and rejected the whole
    # CREATE FUNCTION/PROCEDURE with "unrecognized exception condition".
    #
    # exception_codes first: a PACKAGE-level exception (declared once,
    # ahead of every member, and potentially raised in one flattened
    # member and caught in another -- see convert_package_body) needs the
    # *same* code wherever it's used, which only the caller can guarantee
    # by deciding it once up front; enumerate()-ing it independently in
    # each member below would hand out a different code per member and
    # the WHEN clause would simply never match what a different member's
    # RAISE actually raises. This was the real, still-open half of the
    # FEATURE_PL_ADMIN_PKG/e_invalid_status bug: routine-local exceptions
    # (below) were fixed first, but e_invalid_status was declared at
    # PACKAGE level, not inside the one member that referenced it, so it
    # was never even in that member's own `declared_exceptions` to begin
    # with -- the fix below never ran on it at all.
    for exc_name, code in exception_codes.items():
        text = re.sub(
            rf"\bRAISE\s+{re.escape(exc_name)}\s*;",
            f"RAISE EXCEPTION '{exc_name}' USING ERRCODE = '{code}';",
            text, flags=re.IGNORECASE,
        )
        text = re.sub(
            rf"\bWHEN\s+{re.escape(exc_name)}\s+THEN",
            f"WHEN SQLSTATE '{code}' THEN",
            text, flags=re.IGNORECASE,
        )

    for idx, exc_name in enumerate(declared_exceptions):
        code = f"U{idx:03d}1"
        text = re.sub(
            rf"\bRAISE\s+{re.escape(exc_name)}\s*;",
            f"RAISE EXCEPTION '{exc_name}' USING ERRCODE = '{code}';",
            text, flags=re.IGNORECASE,
        )
        text = re.sub(
            rf"\bWHEN\s+{re.escape(exc_name)}\s+THEN",
            f"WHEN SQLSTATE '{code}' THEN",
            text, flags=re.IGNORECASE,
        )

    # flag remaining high-risk constructs without rewriting them
    for pattern, _weight, message in _MANUAL_MARKERS:
        if pattern.search(text):
            issues.append(ConversionIssue("error", message))
    _flag_remaining_bulk_collect_and_forall(text, issues, select_bulk_collect_seen)

    # normalize a trailing `END routine_name;` to plain `END;` (Postgres doesn't
    # accept the routine-name suffix); leave `END IF/LOOP/CASE;` untouched
    text = text.rstrip()
    end_m = re.search(r"END\s+([A-Za-z_][\w$#]*)\s*;\s*$", text, re.IGNORECASE)
    if end_m and end_m.group(1).upper() not in ("IF", "LOOP", "CASE"):
        text = text[: end_m.start()] + "END;"

    return text, issues


# --------------------------------------------------------- header / params


def _extract_declared_exceptions(declare_text: str) -> List[str]:
    """In declaration order, deduplicated -- order matters here (unlike a
    plain set) because convert_body mints each exception a stable SQLSTATE
    code by position; the same routine must get the same codes every time
    it's converted."""
    names: List[str] = []
    seen = set()
    for stmt in split_top_level(declare_text, ";"):
        m = re.match(r"^([A-Za-z_][\w$#]*)\s+EXCEPTION\s*$", stmt.strip(), re.IGNORECASE)
        if m and m.group(1).upper() not in seen:
            seen.add(m.group(1).upper())
            names.append(m.group(1))
    return names


def parse_param(raw: str) -> Tuple[str, str, str, Optional[str]]:
    """Parse one Oracle parameter declaration into (name, mode, type_text, default)."""
    raw = raw.strip()
    m = re.match(r"^([A-Za-z_][\w$#]*)\s+(.*)$", raw, re.DOTALL)
    if not m:
        return raw, "IN", "TEXT", None
    name, rest = m.group(1), m.group(2).strip()

    mode = "IN"
    mode_m = re.match(r"^(IN\s+OUT|IN|OUT)\b\s*(.*)$", rest, re.IGNORECASE | re.DOTALL)
    if mode_m:
        raw_mode = re.sub(r"\s+", " ", mode_m.group(1).upper())
        mode = "INOUT" if raw_mode == "IN OUT" else raw_mode
        rest = mode_m.group(2).strip()

    rest = re.sub(r"^NOCOPY\s+", "", rest, flags=re.IGNORECASE)

    default = None
    parts = re.split(r":=|\bDEFAULT\b", rest, maxsplit=1, flags=re.IGNORECASE)
    type_text = parts[0].strip()
    if len(parts) == 2:
        default = parts[1].strip()

    return name, mode, type_text, default


def parse_routine_header(source: str) -> Optional[dict]:
    """Parse `PROCEDURE name (params) IS/AS ...` or
    `FUNCTION name (params) RETURN type IS/AS ...` (the format Oracle's
    ALL_SOURCE stores, i.e. without the CREATE OR REPLACE prefix)."""
    header_m = re.match(r"^\s*(PROCEDURE|FUNCTION)\s+([A-Za-z_][\w$#]*)\s*", source, re.IGNORECASE)
    if not header_m:
        return None
    kind = header_m.group(1).upper()
    name = header_m.group(2)
    pos = header_m.end()

    params_text = ""
    while pos < len(source) and source[pos] in " \t\r\n":
        pos += 1
    if pos < len(source) and source[pos] == "(":
        depth = 1
        start = pos + 1
        i = pos + 1
        in_string = False
        while i < len(source) and depth > 0:
            ch = source[i]
            if ch == "'":
                in_string = not in_string
            elif not in_string:
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
            i += 1
        params_text = source[start : i - 1]
        pos = i

    return_type = None
    remainder = source[pos:]
    return_m = re.match(r"^\s*RETURN\s+([A-Za-z_][\w$#.]*(?:%TYPE|%ROWTYPE)?)", remainder, re.IGNORECASE)
    if return_m:
        return_type = return_m.group(1)
        pos += return_m.end()
        remainder = source[pos:]

    is_as_m = re.search(r"\b(IS|AS)\b", remainder, re.IGNORECASE)
    if not is_as_m:
        return None
    rest = remainder[is_as_m.end():]

    # find_top_level_begin (not a bare `re.search(r"\bBEGIN\b", ...)`) is
    # required here: a naive first-BEGIN-anywhere search finds a nested
    # subprogram's own BEGIN instead whenever the DECLARE section declares
    # one before this routine's real body, misidentifying the declare/body
    # split for the *outer* routine itself -- see find_top_level_begin's
    # own docstring, and extract_nested_subprograms (called from
    # convert_declare_block) for the matching fix on the declare-section
    # side once that boundary is found correctly.
    begin_pos = find_top_level_begin(rest)
    if begin_pos is None:
        return None
    declare_text = rest[:begin_pos]
    body_text = rest[begin_pos:]

    return {
        "kind": kind,
        "name": name,
        "params_text": params_text,
        "return_type": return_type,
        "declare_text": declare_text,
        "body_text": body_text,
    }


# ------------------------------------------------------------ top-level API


def convert_procedure_or_function(
    routine: Routine, exception_codes: Optional[Dict[str, str]] = None,
) -> Tuple[str, List[ConversionIssue]]:
    """`exception_codes`, if given, is forwarded to convert_body as-is --
    see that function's own docstring. Used by convert_package_body to
    give a PACKAGE-level exception the same SQLSTATE code in every
    flattened member that raises or catches it; every other caller leaves
    this unset, exactly as before this parameter existed."""
    issues: List[ConversionIssue] = []
    parsed = parse_routine_header(routine.source)
    if parsed is None:
        issues.append(ConversionIssue(
            "error", "Could not parse the routine header; manual conversion required."))
        return f"-- MANUAL CONVERSION REQUIRED for {routine.kind} {routine.name}\n/*\n{routine.source}\n*/", issues

    params = split_top_level(parsed["params_text"], ",")
    pg_params = []
    for raw_param in params:
        if not raw_param:
            continue
        name, mode, type_text, default = parse_param(raw_param)
        mapped_type = _convert_declared_type(type_text)
        piece = f"{mode} {name} {mapped_type}" if mode != "IN" else f"{name} {mapped_type}"
        if default:
            piece += f" DEFAULT {default}"
        pg_params.append(piece)
    params_clause = ", ".join(pg_params)

    declared_exceptions = _extract_declared_exceptions(parsed["declare_text"])
    row_loop_vars = _find_row_loop_variables(parsed["body_text"])
    declare_sql, declare_issues, unsupported_type_vars = convert_declare_block(
        parsed["declare_text"], row_loop_vars)
    if unsupported_type_vars:
        # See convert_declare_block's own docstring for the failure this
        # avoids: emitting a live CREATE OR REPLACE whose body still uses
        # Oracle-only collection/record element syntax Postgres cannot
        # parse (e.g. "v_values(1) := 100;"), which used to reach "Apply
        # DDL to Target" and fail there instead of being flagged here.
        names = ", ".join(sorted(unsupported_type_vars))
        sample = sorted(unsupported_type_vars)[0]
        issues.append(ConversionIssue(
            "error",
            f"{routine.kind} {routine.name}: variable(s) {names} use a locally-declared Oracle "
            f"collection/record/REF CURSOR type with no direct Postgres equivalent; this routine's "
            f"body almost certainly relies on Oracle-only element access (e.g. '{sample}(i) := ...') "
            f"or collection methods (.COUNT/.FIRST/.LAST/.EXISTS/.DELETE) that cannot be mechanically "
            f"rewritten -- redesign using a Postgres array, composite type, or refcursor variable and "
            f"rewrite the whole routine by hand.",
        ))
        return (
            f"-- MANUAL CONVERSION REQUIRED for {routine.kind} {routine.name}\n"
            f"-- Uses a locally-declared Oracle collection/record/REF CURSOR type ({names}) with no "
            f"direct Postgres equivalent -- see the assessment report for details.\n"
            f"/*\n{routine.source}\n*/"
        ), issues
    declare_sql, declare_issues = _ensure_row_loop_vars_declared(
        declare_sql, declare_issues, row_loop_vars, parsed["declare_text"])
    body_sql, body_issues = convert_body(parsed["body_text"], declared_exceptions, exception_codes)
    issues.extend(declare_issues)
    issues.extend(body_issues)

    declare_clause = f"DECLARE\n{declare_sql}\n" if declare_sql.strip() else ""

    if parsed["kind"] == "FUNCTION":
        return_type = _convert_declared_type(parsed["return_type"]) if parsed["return_type"] else "TEXT"
        if not parsed["return_type"]:
            issues.append(ConversionIssue("warning", "FUNCTION had no parsable RETURN type; defaulted to TEXT."))
        ddl = (
            f"CREATE OR REPLACE FUNCTION {_quote_pg(routine.name)}({params_clause})\n"
            f"RETURNS {return_type} AS $$\n"
            f"{declare_clause}"
            f"{body_sql}\n"
            f"$$ LANGUAGE plpgsql;"
        )
    else:
        ddl = (
            f"CREATE OR REPLACE PROCEDURE {_quote_pg(routine.name)}({params_clause})\n"
            f"AS $$\n"
            f"{declare_clause}"
            f"{body_sql}\n"
            f"$$ LANGUAGE plpgsql;"
        )

    return ddl, issues


_TRIGGER_EVENT_PSEUDOCOLUMNS = (
    ("INSERTING", "INSERT"),
    ("UPDATING", "UPDATE"),
    ("DELETING", "DELETE"),
)

# Matches a '...'-quoted string literal (with '' as the escaped-quote form),
# a `-- ...` line comment, or a `/* ... */` block comment -- the three
# places a bare occurrence of the word "inserting" and friends is data, not
# a pseudo-column reference, and must not be rewritten.
_STRING_OR_COMMENT_RE = re.compile(r"'(?:[^']|'')*'|--[^\n]*|/\*.*?\*/", re.DOTALL)


def _resolve_trigger_event_pseudocolumns(body_sql: str) -> str:
    """Oracle trigger bodies branch on which DML statement fired them using
    the boolean pseudo-columns INSERTING/UPDATING/DELETING, e.g.:

        IF INSERTING THEN ... ELSIF UPDATING THEN ... END IF;

    Postgres has no such pseudo-columns -- a plpgsql trigger function
    finds this out from the special `TG_OP` variable the trigger machinery
    always populates ('INSERT'/'UPDATE'/'DELETE'). Left untouched,
    `INSERTING` reaches Postgres as nothing more than a bare, undeclared
    identifier, and plpgsql resolves an unqualified name inside a
    row-level trigger function as a column reference on NEW/OLD first --
    which fails with `column "inserting" does not exist` the moment the
    trigger actually fires. That is not hypothetical: a real migration's
    FEATURE_TEST_DATA trigger hit exactly this error, because nothing in
    this converter (unlike plsql_mysql_converter's equivalent handling for
    a MySQL target) ever rewrote these three keywords.

    Unlike the MySQL converter -- which has to split one Oracle trigger
    into several MySQL ones, because MySQL allows only one event per
    trigger and has nothing like TG_OP to distinguish at runtime -- a
    single Postgres trigger can fire `BEFORE INSERT OR UPDATE OR DELETE`
    and tell them apart itself, so this is a plain, context-free text
    substitution rather than a per-event trigger split.

    String literals and comments are masked out first, so a message like
    `RAISE NOTICE 'inserting a new order'` keeps its wording -- only a
    bare, word-bounded keyword occurrence is a pseudo-column reference.
    """
    chunks: List[str] = []

    def mask(match: "re.Match") -> str:
        chunks.append(match.group(0))
        return f"\x00{len(chunks) - 1}\x00"

    masked = _STRING_OR_COMMENT_RE.sub(mask, body_sql)
    for pseudocolumn, op in _TRIGGER_EVENT_PSEUDOCOLUMNS:
        masked = re.sub(rf"\b{pseudocolumn}\b", f"(TG_OP = '{op}')", masked, flags=re.IGNORECASE)

    def unmask(match: "re.Match") -> str:
        return chunks[int(match.group(1))]

    return re.sub(r"\x00(\d+)\x00", unmask, masked)


def convert_trigger(routine: Routine) -> Tuple[str, List[ConversionIssue]]:
    issues: List[ConversionIssue] = []
    body = routine.source.strip()

    # trigger_body from ALL_TRIGGERS starts at DECLARE or BEGIN (no header to parse).
    # find_top_level_begin (see parse_routine_header's own comment on why)
    # is needed here too -- a trigger's DECLARE section can just as easily
    # hide a nested subprogram before the trigger's own BEGIN.
    declare_text, body_text = "", body
    begin_pos = find_top_level_begin(body)
    if body[:8].upper().startswith("DECLARE") and begin_pos is not None:
        declare_text = body[len("DECLARE"): begin_pos]
        body_text = body[begin_pos:]

    declared_exceptions = _extract_declared_exceptions(declare_text)
    row_loop_vars = _find_row_loop_variables(body_text)
    declare_sql, declare_issues, unsupported_type_vars = (
        convert_declare_block(declare_text, row_loop_vars) if declare_text.strip() else ("", [], set())
    )
    if unsupported_type_vars:
        # See convert_declare_block's own docstring for the failure this
        # avoids: emitting a live trigger function whose body still uses
        # Oracle-only collection/record element syntax Postgres cannot
        # parse, which used to reach "Apply DDL to Target" and fail there
        # instead of being flagged here.
        names = ", ".join(sorted(unsupported_type_vars))
        sample = sorted(unsupported_type_vars)[0]
        issues.append(ConversionIssue(
            "error",
            f"TRIGGER {routine.name}: variable(s) {names} use a locally-declared Oracle "
            f"collection/record/REF CURSOR type with no direct Postgres equivalent; this trigger's "
            f"body almost certainly relies on Oracle-only element access (e.g. '{sample}(i) := ...') "
            f"or collection methods (.COUNT/.FIRST/.LAST/.EXISTS/.DELETE) that cannot be mechanically "
            f"rewritten -- redesign using a Postgres array, composite type, or refcursor variable and "
            f"rewrite the whole trigger by hand.",
        ))
        return (
            f"-- MANUAL CONVERSION REQUIRED for TRIGGER {routine.name}\n"
            f"-- Uses a locally-declared Oracle collection/record/REF CURSOR type ({names}) with no "
            f"direct Postgres equivalent -- see the assessment report for details.\n"
            f"/*\n{routine.source}\n*/"
        ), issues
    declare_sql, declare_issues = _ensure_row_loop_vars_declared(
        declare_sql, declare_issues, row_loop_vars, declare_text)
    body_sql, body_issues = convert_body(body_text, declared_exceptions)
    issues.extend(declare_issues)
    issues.extend(body_issues)

    # See _resolve_trigger_event_pseudocolumns' own docstring: without
    # this, IF INSERTING/UPDATING/DELETING reaches Postgres as a bare,
    # undeclared identifier and fails at trigger-fire time with
    # `column "inserting" does not exist` -- the exact error a real
    # migration hit on FEATURE_TEST_DATA.
    body_sql = _resolve_trigger_event_pseudocolumns(body_sql)

    # ensure the function returns something — trigger functions must RETURN NEW/OLD/NULL
    if not re.search(r"\bRETURN\b", body_sql, re.IGNORECASE):
        default_return = "NULL" if (routine.timing or "").upper() == "AFTER" else (
            "OLD" if "DELETE" in (routine.events or []) and len(routine.events) == 1 else "NEW"
        )
        body_sql = body_sql[:-1] if body_sql.endswith(";") else body_sql  # drop trailing END; temporarily
        # body_sql currently ends with "END;" after normalization — insert RETURN before it
        if body_sql.rstrip().endswith("END"):
            body_sql = body_sql.rstrip()[: -len("END")].rstrip() + f"\n  RETURN {default_return};\nEND;"
        else:
            body_sql += f"\n  RETURN {default_return};"
        issues.append(ConversionIssue(
            "warning", f"No RETURN found in the trigger body; added `RETURN {default_return};` before the "
                       "end — verify this matches the intended trigger semantics."))

    fn_name = f"{routine.name}_fn"
    declare_clause = f"DECLARE\n{declare_sql}\n" if declare_sql.strip() else ""
    function_ddl = (
        f"CREATE OR REPLACE FUNCTION {_quote_pg(fn_name)}()\n"
        f"RETURNS TRIGGER AS $$\n"
        f"{declare_clause}"
        f"{body_sql}\n"
        f"$$ LANGUAGE plpgsql;"
    )

    timing = routine.timing or "BEFORE"
    events = " OR ".join(routine.events or ["INSERT"])
    level = "ROW" if routine.row_level else "STATEMENT"
    table_name = routine.table_name or "UNKNOWN_TABLE"
    if routine.table_name is None:
        issues.append(ConversionIssue("error", "Trigger's target table was not captured; DDL references UNKNOWN_TABLE."))

    # DROP ... IF EXISTS first: plain CREATE TRIGGER has no IF NOT EXISTS
    # clause (CREATE OR REPLACE TRIGGER only exists on Postgres 14+), so
    # re-applying the same DDL against a target that already has this
    # trigger from a previous run would otherwise fail with "trigger ...
    # already exists". This makes the statement safe to re-run regardless
    # of target Postgres version.
    trigger_ddl = (
        f"DROP TRIGGER IF EXISTS {_quote_pg(routine.name)} ON {_quote_pg(table_name)};\n"
        f"CREATE TRIGGER {_quote_pg(routine.name)}\n"
        f"{timing} {events} ON {_quote_pg(table_name)}\n"
        f"FOR EACH {level}\n"
        f"EXECUTE FUNCTION {_quote_pg(fn_name)}();"
    )

    return f"{function_ddl}\n\n{trigger_ddl}", issues


def convert_package_body(routine: Routine) -> Tuple[str, List[ConversionIssue]]:
    """Best-effort flattening of a (non-nested) PACKAGE BODY into standalone
    functions/procedures named '<package>_<member>'. Deeply nested local
    subprograms inside a member are not split further — flagged instead."""
    issues: List[ConversionIssue] = []
    source = routine.source

    header_positions = [
        m for m in re.finditer(r"\b(PROCEDURE|FUNCTION)\s+([A-Za-z_][\w$#]*)", source, re.IGNORECASE)
    ]
    if not header_positions:
        issues.append(ConversionIssue(
            "error", "No PROCEDURE/FUNCTION members found in package body; manual conversion required."))
        return f"-- MANUAL CONVERSION REQUIRED for PACKAGE BODY {routine.name}\n/*\n{source}\n*/", issues

    # Anything before the first member header -- typically package-level
    # variable/constant/exception declarations, shared by every member --
    # is not part of any chunk below and would otherwise be silently
    # dropped. Most of that (state) genuinely has no mechanical Postgres
    # equivalent once the package is flattened into independent functions
    # (see the warning issued below), but a package-level EXCEPTION is
    # different: converting it needs no shared state at all, only a
    # SQLSTATE code, so it can be preserved exactly -- see
    # package_exception_codes below for why that code has to be minted
    # once here rather than per member.
    preamble = source[: header_positions[0].start()]
    # _extract_declared_exceptions expects a *declare section* -- text
    # after "IS"/"AS", the way parsed["declare_text"] already arrives for
    # a standalone routine (parse_routine_header strips the "PROCEDURE
    # name(...) IS" header itself). The preamble still has its own
    # "PACKAGE BODY name IS" header attached, which isn't a declaration
    # and must be stripped the same way, or a statement like
    # "PACKAGE BODY FEATURE_PL_ADMIN_PKG IS\n  e_invalid_status EXCEPTION"
    # (everything up to the first ';', since there's nothing else to split
    # on before it) never matches _extract_declared_exceptions' own
    # "name EXCEPTION" pattern and the declaration is silently missed --
    # exactly as if this whole fix didn't exist.
    package_header_m = re.match(
        r"^\s*PACKAGE\s+BODY\s+[A-Za-z_][\w$#]*\s+(?:IS|AS)\b", preamble, re.IGNORECASE)
    package_declare_text = preamble[package_header_m.end():] if package_header_m else preamble
    package_level_exceptions = _extract_declared_exceptions(package_declare_text)
    # "P" (package), not "U" (used for a routine-local exception -- see
    # convert_body), so the two numbering spaces never look like the same
    # scheme even though a collision would be harmless (each flattened
    # member's own RAISE/WHEN pair only matters within that one function).
    package_exception_codes = {
        name: f"P{idx:03d}1" for idx, name in enumerate(package_level_exceptions)
    }

    chunks: List[str] = []
    for idx, m in enumerate(header_positions):
        start = m.start()
        end = header_positions[idx + 1].start() if idx + 1 < len(header_positions) else len(source)
        chunks.append(source[start:end])

    # The last chunk runs through the end of the source, which also
    # includes the package body's own closing "END <package_name>;" (or a
    # bare "END;") after the last member's own END. Left in place, that
    # trailing END is parsed as part of the member's body and survives
    # convert_body's END-normalization untouched (normalization only
    # rewrites the *last* END in the text), leaving a stray, unmatched
    # second END statement — e.g. "END get_total_customers;\n\nEND;" —
    # which Postgres rejects. Strip it before the chunk is parsed.
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
        f"Package-level state (if any) is not carried over — see notes below."
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
        member_ddl, member_issues = convert_procedure_or_function(pseudo_routine, package_exception_codes)
        ddl_parts.append(member_ddl)
        issues.extend(member_issues)

    if package_level_exceptions:
        names = ", ".join(package_level_exceptions)
        issues.append(ConversionIssue(
            "info",
            f"Package-level exception(s) {names} were preserved: each got its own synthetic SQLSTATE "
            f"code, the same one in every flattened member that raises or catches it, so a RAISE in one "
            f"member is still caught by a WHEN naming it in another -- see convert_body's own docstring "
            f"on why that code has to match everywhere the exception is used.",
        ))

    issues.append(ConversionIssue(
        "warning",
        "Package-level variables/constants (if the package declared any) have no direct Postgres "
        "equivalent; consider a settings table or session variables (SET/current_setting) if state "
        "needs to be shared across the flattened functions.",
    ))

    return "\n\n".join(ddl_parts), issues


def convert_routine(routine: Routine, target_engine: str) -> Routine:
    """Populate routine.converted_source / .status / .issues in place and
    return it. PostgreSQL and SQL Server both get a real syntax conversion;
    MySQL stored procedure syntax is different enough that it is out of
    scope here and the routine is left flagged for manual conversion."""
    if routine.source_engine == "SQL Server":
        # SQL Server sources used to fall into the generic non-Oracle guard
        # below and be flagged manual unconditionally, which meant a
        # SQL Server -> MySQL/PostgreSQL migration converted every table and
        # not one procedure, function or trigger. tsql_routine_converter is
        # a real T-SQL front end for exactly those two targets: it converts
        # the shapes it fully understands and still flags anything it does
        # not (dynamic SQL, table variables, MERGE, INSTEAD OF triggers, ...)
        # rather than guessing -- see its module docstring for the exact
        # supported surface. Every other target keeps the honest
        # "needs a human" answer via the guard below.
        engine_key_sqlserver = target_engine.lower().replace(" ", "")
        if engine_key_sqlserver.startswith(("mysql", "mariadb", "postgres")):
            from tgdatabridge.core.tsql_routine_converter import (
                convert_routine as convert_routine_from_tsql,
            )
            return convert_routine_from_tsql(routine, target_engine)

    # A MySQL or PostgreSQL source, or any source migrating to its own
    # engine. Until Round 20 all of these fell into the guard below and
    # were flagged MANUAL on sight -- which meant a MySQL -> PostgreSQL
    # migration, a PostgreSQL -> MySQL one, and even a MySQL -> MySQL one
    # converted every table and view and not one procedure, function or
    # trigger. See tgdatabridge.core.native_routine_converter.
    from tgdatabridge.core.native_routine_converter import (
        can_convert as can_convert_native,
        convert_routine as convert_routine_native,
        same_engine,
    )
    if routine.source_engine != "Oracle" and (
            can_convert_native(routine.source_engine, target_engine)
            or same_engine(routine.source_engine, target_engine)):
        return convert_routine_native(routine, target_engine)

    if routine.source_engine != "Oracle":
        # Every converter this module dispatches to below (this file's own
        # PL/pgSQL conversion, tsql_converter.py, db2_converter.py,
        # sql_translator.py's legacy flag-only path) is written against
        # Oracle PL/SQL syntax specifically -- running any of their regexes
        # against a MySQL/PostgreSQL/SQL Server routine body risks a
        # partial, silently-wrong "success" (some of those patterns are
        # generic enough to accidentally match non-Oracle syntax) rather
        # than the honest "this needs a human" a routine from a source this
        # tool doesn't understand actually deserves. See
        # tgdatabridge.core.mysql_introspector's module docstring for where
        # `source_engine` gets set to something other than "Oracle".
        routine.converted_source = (
            f"-- MANUAL CONVERSION REQUIRED for {routine.kind} {routine.name}\n"
            f"-- Source engine is {routine.source_engine}, not Oracle -- this tool's stored-routine "
            f"converters are Oracle PL/SQL-specific and were not run against it.\n"
            f"/*\n{routine.source}\n*/"
        )
        routine.issues = [ConversionIssue(
            "error",
            f"{routine.kind} {routine.name}: automatic conversion is only implemented for an Oracle "
            f"source; this {routine.source_engine}-sourced routine must be rewritten by hand for "
            f"{target_engine}.",
        )]
        routine.status = ConversionStatus.MANUAL
        routine.complexity_score = 5  # deliberately above the "> 4" threshold assessment.py treats as high-effort
        return routine

    engine_key = target_engine.lower().replace(" ", "")
    if engine_key.startswith("oracle"):
        # By this point routine.source_engine == "Oracle" is guaranteed
        # (the guard above already returned for anything else), so
        # routine.source is genuine Oracle PL/SQL -- but it is not a
        # complete, directly-executable statement as-is, so this still
        # isn't a pure byte-for-byte passthrough: ALL_SOURCE (what
        # introspector.py reads PROCEDURE/FUNCTION/PACKAGE/PACKAGE BODY
        # source from) omits the leading "CREATE [OR REPLACE]" keyword,
        # and ALL_TRIGGERS.TRIGGER_BODY omits the *entire*
        # "CREATE TRIGGER ... {BEFORE|AFTER} {event} ON table [FOR EACH
        # ROW]" header, starting straight at DECLARE/BEGIN (see
        # convert_trigger's own comment above for the identical
        # observation) -- both are reconstructed here from what's already
        # on the Routine, no rule-based syntax rewriting needed for
        # either. Still the one target where nothing about the PL/SQL
        # *body itself* is touched, unlike every other branch below.
        if routine.kind == "TRIGGER":
            timing = routine.timing or "BEFORE"
            events = " OR ".join(routine.events or ["INSERT"])
            table_ref = quote_double((routine.table_name or "UNKNOWN_TABLE").upper())
            header = f'CREATE OR REPLACE TRIGGER "{routine.name.upper()}"\n{timing} {events}\nON {table_ref}'
            if routine.row_level:
                header += "\nFOR EACH ROW"
            routine.converted_source = f"{header}\n{routine.source.strip()}"
        else:
            routine.converted_source = f"CREATE OR REPLACE {routine.source.strip()}"
        routine.issues = []
        routine.status = ConversionStatus.AUTOMATIC
        from tgdatabridge.core.sql_translator import score_complexity
        score, _marker_issues = score_complexity(routine.source)
        routine.complexity_score = score
        return routine
    if engine_key.startswith(("mysql", "mariadb")):
        # Until Round 19 this fell through to sql_translator.translate_routine
        # at the bottom of this function -- four regex substitutions over the
        # PL/SQL, then a "Converted with warnings" label on text MySQL cannot
        # run a line of (`v := 0;`, `ELSIF`, `EXIT WHEN`, a trailing
        # `EXCEPTION WHEN OTHERS THEN`). plsql_mysql_converter is a real
        # MySQL back end: see its module docstring for what it translates and
        # what it still refuses to guess at.
        from tgdatabridge.core.plsql_mysql_converter import (
            convert_routine as convert_routine_mysql,
        )
        return convert_routine_mysql(routine, target_engine)
    if engine_key.startswith("sqlserver"):
        from tgdatabridge.core.tsql_converter import convert_routine as convert_routine_sqlserver
        return convert_routine_sqlserver(routine, target_engine)
    if engine_key.startswith("db2"):
        from tgdatabridge.core.db2_converter import convert_routine as convert_routine_db2
        return convert_routine_db2(routine, target_engine)
    if engine_key.startswith("mongo"):
        # Unlike MySQL's target treatment (sql_translator.translate_routine
        # below -- a generic best-effort draft that can still land on
        # AUTOMATIC_WITH_WARNINGS), MongoDB gets no attempted translation
        # at all: there's no server-side stored-procedure/trigger concept
        # in MongoDB for a translated body to even target, so every
        # routine is unconditionally flagged manual, mirroring the
        # non-Oracle-source guard's own placeholder shape above rather
        # than sql_translator's draft-with-substitutions approach.
        routine.converted_source = (
            f"-- MANUAL CONVERSION REQUIRED for {routine.kind} {routine.name}\n"
            f"-- MongoDB has no server-side stored-procedure/trigger equivalent -- this must be "
            f"reimplemented as application-layer logic (or, for a trigger, an Atlas Trigger/change "
            f"stream listener if using MongoDB Atlas).\n"
            f"/*\n{routine.source}\n*/"
        )
        routine.issues = [ConversionIssue(
            "error",
            f"{routine.kind} {routine.name}: MongoDB has no stored-procedure/trigger equivalent; "
            "this must be reimplemented by hand as application-layer logic.",
        )]
        routine.status = ConversionStatus.MANUAL
        routine.complexity_score = 5  # same "deliberately above the > 4 high-effort threshold" as the guard above
        return routine
    if not target_engine.lower().startswith("postgres"):
        from tgdatabridge.core.sql_translator import translate_routine  # legacy flag-only path
        return translate_routine(routine, target_engine)

    # A CONNECT BY hierarchical query anywhere in the routine's source (a
    # cursor declaration, a row-FOR-loop header, a SELECT...INTO, ...) gets
    # one shot at an automatic recursive-CTE rewrite before any of the rest
    # of this converter looks at routine.source -- see
    # connect_by_rewriter.py's module docstring for exactly which shapes
    # qualify. A successful rewrite removes the literal "CONNECT BY" text,
    # so the _MANUAL_MARKERS scan inside convert_body no longer flags it.
    from tgdatabridge.core.connect_by_rewriter import rewrite_routine_source
    connect_by_issues = rewrite_routine_source(routine, cte_keyword="WITH RECURSIVE")

    if routine.kind == "TRIGGER":
        ddl, issues = convert_trigger(routine)
    elif routine.kind == "PACKAGE BODY":
        ddl, issues = convert_package_body(routine)
    elif routine.kind == "PACKAGE":
        ddl = (
            f"-- PACKAGE {routine.name} has no Postgres equivalent (no packaging construct). "
            f"Its public members are converted individually from the matching PACKAGE BODY. "
            f"Package-level constants/types declared only in the spec must be moved by hand."
        )
        issues = [ConversionIssue(
            "warning", "Package specs have no Postgres equivalent; only the package body's members were converted.")]
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
