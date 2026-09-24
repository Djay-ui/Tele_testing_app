"""
Oracle PL/SQL -> IBM Db2 (LUW) SQL PL source-code converter.

Db2's SQL PL is, structurally, much closer to Oracle PL/SQL than T-SQL is:
IF/THEN/ELSIF/END IF, bare LOOP/END LOOP, and native SELECT ... INTO are all
directly supported (nearly verbatim), and local variables are referenced
bare rather than needing a sigil like T-SQL's '@'. That said, Db2 has its
own structural rules with no Oracle equivalent that this module handles:

  * Every DECLARE (variables, cursors, condition handlers) in a Db2 compound
    statement (`BEGIN ... END`) must appear *before* any executable
    statement in that same block. Oracle's numeric-range FOR loop
    (`FOR i IN 1..10 LOOP`) has no Db2 equivalent and is rewritten to a
    WHILE loop with an explicit counter -- but unlike the T-SQL converter
    (which can `DECLARE @i INT = 1;` inline, wherever the loop happens to
    sit), Db2 requires that counter's DECLARE to be hoisted to the top of
    the routine, alongside its other declared variables.
  * Db2 has no exception-handling block scoped to WHEN clauses the way
    Oracle's `EXCEPTION WHEN ... THEN ...` is. Db2 uses `DECLARE [EXIT|
    CONTINUE] HANDLER FOR <condition-list> <handler-body>;` statements,
    which -- like every other DECLARE -- must sit in the declare section,
    before the block's executable statements. An Oracle EXCEPTION section
    is therefore restructured into one or more DECLARE HANDLER statements
    hoisted to the top of whichever BEGIN...END block it was attached to
    (recursively, for a nested anonymous block with its own local EXCEPTION
    section), rather than wrapped around the body the way T-SQL's TRY/CATCH
    is.
  * Db2 has no unlabeled EXIT/CONTINUE -- LEAVE and ITERATE always require a
    label, and Db2 (unlike Oracle) requires every loop that uses them to be
    explicitly labeled. This converter synthesizes a label for every loop
    it converts and rewrites EXIT/EXIT WHEN/CONTINUE/CONTINUE WHEN to
    LEAVE/ITERATE against that label -- applied only after a loop's own
    body has already been recursively converted, so a bare EXIT/CONTINUE
    left over after that always belongs to *this* loop, not an inner one
    that already consumed its own.
  * Db2's WHILE loop is `WHILE cond DO ... END WHILE;` (DO/END WHILE, not
    LOOP/END LOOP), and its implicit-cursor/row FOR loop is a *native*
    `FOR var [AS cursor-name CURSOR FOR] select-statement DO ... END FOR;`
    construct -- Db2, like Oracle, keeps row-variable dot access
    (`var.column`) working automatically, so (unlike the T-SQL converter)
    this needs no explicit CURSOR/FETCH/WHILE rewrite or column-list
    extraction at all; only the surrounding LOOP/END LOOP keywords change
    to DO/END FOR.
"""
from __future__ import annotations

import re
from typing import Callable, List, Optional, Tuple

from tgdatabridge.core import type_mapping
from tgdatabridge.core.nested_subprogram import extract_nested_subprograms, find_top_level_begin
from tgdatabridge.core.plsql_converter import (
    dbms_lob_getlength_replacer, dbms_lob_substr_replacer, parse_param,
    parse_routine_header, replace_dbms_random_value, split_top_level,
)
from tgdatabridge.core.schema_model import ConversionIssue, ConversionStatus, Routine
from tgdatabridge.utils.identifiers import quote_double

# ---------------------------------------------------------------- utilities


def _quote_db2(identifier: str, schema: Optional[str] = None) -> str:
    """Db2, like Oracle, folds *unquoted* identifiers to uppercase -- and
    since object names arrive here already uppercase (Oracle's own default),
    quoting in uppercase keeps generated references consistent with that,
    while still being safe if a name collides with a reserved word."""
    ident = quote_double(identifier.upper())
    return f"{quote_double(schema.upper())}.{ident}" if schema else ident


_STRING_LITERAL_RE = re.compile(r"'(?:[^']|'')*'")


def _apply_outside_strings(text: str, func: Callable[[str], str]) -> str:
    out: List[str] = []
    pos = 0
    for m in _STRING_LITERAL_RE.finditer(text):
        out.append(func(text[pos:m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(func(text[pos:]))
    return "".join(out)


def _apply_outside_strings_scan(text: str, start: int, scan_func):
    blanked = list(text)
    for m in _STRING_LITERAL_RE.finditer(text):
        for i in range(m.start(), m.end()):
            if blanked[i] not in ("\n",):
                blanked[i] = "x"
    blanked_text = "".join(blanked)
    return scan_func(blanked_text, start)


def _indent(text: str, spaces: int = 2) -> str:
    pad = " " * spaces
    return "\n".join((pad + line if line.strip() else line) for line in text.splitlines())


class _LabelGen:
    """Generates deterministic, per-conversion-call-unique loop labels
    (LBL_<HINT>_<N>). A fresh instance is created at the top of every
    convert_procedure_or_function/convert_trigger call, so label numbering
    is reproducible per routine rather than drifting across an entire
    process run."""
    def __init__(self):
        self._n = 0

    def next(self, hint: str) -> str:
        self._n += 1
        safe_hint = re.sub(r"[^A-Za-z0-9_]", "_", hint).upper() or "LOOP"
        return f"LBL_{safe_hint}_{self._n}"


def _convert_exit_continue(text: str, label: str) -> str:
    """EXIT/EXIT WHEN/CONTINUE/CONTINUE WHEN -> LEAVE/ITERATE against a
    specific loop's own label. Must only run on text already carved out as
    that loop's own body (see the note in _convert_loop_construct)."""
    def _rewrite(chunk: str) -> str:
        chunk = re.sub(rf"\bEXIT\s+WHEN\s+(.+?);", rf"IF \1 THEN LEAVE {label}; END IF;", chunk, flags=re.IGNORECASE | re.DOTALL)
        chunk = re.sub(r"\bEXIT\s*;", f"LEAVE {label};", chunk, flags=re.IGNORECASE)
        chunk = re.sub(rf"\bCONTINUE\s+WHEN\s+(.+?);", rf"IF \1 THEN ITERATE {label}; END IF;", chunk, flags=re.IGNORECASE | re.DOTALL)
        chunk = re.sub(r"\bCONTINUE\s*;", f"ITERATE {label};", chunk, flags=re.IGNORECASE)
        return chunk
    return _apply_outside_strings(text, _rewrite)


# ------------------------------------------------------------- body rewrites

# NO_DATA_FOUND maps to Db2's "NOT FOUND" condition keyword rather than a
# quoted SQLSTATE literal -- handled specially in _exception_condition_to_handler.
_EXCEPTION_SQLSTATE_MAP = {
    "DUP_VAL_ON_INDEX": "23505",
    "ZERO_DIVIDE": "22012",
    "TOO_MANY_ROWS": "21000",
    "INVALID_NUMBER": "22018",
}
_UNMAPPABLE_EXCEPTIONS = {
    "VALUE_ERROR", "INVALID_CURSOR", "LOGIN_DENIED", "NOT_LOGGED_ON",
    "PROGRAM_ERROR", "STORAGE_ERROR", "TIMEOUT_ON_RESOURCE",
    "ROWTYPE_MISMATCH", "SUBSCRIPT_BEYOND_COUNT", "SUBSCRIPT_OUTSIDE_LIMIT",
    "COLLECTION_IS_NULL", "CURSOR_ALREADY_OPEN",
}

# Db2 natively supports CASE *statements* (CASE ... WHEN ... THEN ... END
# CASE;), unlike T-SQL -- so, unlike tsql_converter, there's no need to flag
# "END CASE" as unsupported here; it's simply left untouched.
_MANUAL_MARKERS = [
    (re.compile(r"\bDBMS_[A-Z_]+", re.IGNORECASE), "Uses an Oracle DBMS_* package with no direct Db2 equivalent (Db2 SQL PL has no console-output statement to substitute for DBMS_OUTPUT.PUT_LINE either)."),
    (re.compile(r"\bUTL_[A-Z_]+", re.IGNORECASE), "Uses an Oracle UTL_* package with no direct Db2 equivalent."),
    (re.compile(r"\bBULK COLLECT\b", re.IGNORECASE), "BULK COLLECT has no direct equivalent; rewrite using an array data type or a loop."),
    (re.compile(r"\bFORALL\b", re.IGNORECASE), "FORALL has no direct equivalent; rewrite as a loop or a set-based statement."),
    (re.compile(r"\bCONNECT BY\b", re.IGNORECASE), "Hierarchical query (CONNECT BY) must be rewritten as a recursive common table expression (WITH ... AS)."),
    (re.compile(r"\bAUTONOMOUS_TRANSACTION\b", re.IGNORECASE), "Autonomous transactions have no equivalent; requires a federated/separate connection."),
]


def _convert_declared_type(type_text: str) -> Tuple[str, List[ConversionIssue]]:
    """Map a declared Oracle type to its Db2 equivalent. Anchored
    %TYPE/%ROWTYPE references have no Db2 equivalent (Db2 has no way to
    anchor a variable's type to a column's or row's type at parse time) so
    these are flagged and defaulted to VARCHAR(4000) rather than guessed at."""
    if re.search(r"%ROWTYPE", type_text, re.IGNORECASE):
        return "VARCHAR(4000)", [ConversionIssue(
            "error",
            f"'{type_text}' anchors to a row type (%ROWTYPE), which Db2 has no equivalent for; "
            "declare explicit columns/variables instead of a row-shaped variable.",
        )]
    if re.search(r"%TYPE", type_text, re.IGNORECASE):
        return "VARCHAR(4000)", [ConversionIssue(
            "warning",
            f"'{type_text}' anchors to a column's type (%TYPE), which Db2 has no equivalent for; "
            "defaulted to VARCHAR(4000) -- replace with the column's actual data type for best results.",
        )]
    return type_mapping.to_db2(type_text)


def _builtin_rewrites(text: str) -> Tuple[str, List[ConversionIssue]]:
    """Rewrite Oracle builtin functions/keywords with no structural impact.
    Db2's SELECT ... INTO ... FROM and EXECUTE IMMEDIATE are both native,
    identical-syntax constructs, so (unlike the T-SQL converter) neither
    needs any rewriting here at all."""
    issues: List[ConversionIssue] = []

    def _rewrite(chunk: str) -> str:
        chunk = re.sub(r"\bSYSDATE\b", "CURRENT TIMESTAMP", chunk, flags=re.IGNORECASE)
        chunk = re.sub(r"\bSYSTIMESTAMP\b", "CURRENT TIMESTAMP", chunk, flags=re.IGNORECASE)
        chunk = re.sub(r"\bNVL\s*\(", "COALESCE(", chunk, flags=re.IGNORECASE)
        chunk = re.sub(r"\bFROM\s+DUAL\b", "FROM SYSIBM.SYSDUMMY1", chunk, flags=re.IGNORECASE)
        # Oracle's `:=` assignment operator isn't valid Db2 SQL PL outside a
        # DECLARE's own DEFAULT clause -- a bare `x = y;` statement (without
        # SET) is a syntax error, so every assignment statement needs a SET
        # keyword inserted, exactly as for T-SQL.
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
        # Oracle INSTR(str, substr[, start[, occurrence]]) -- Db2's LOCATE
        # takes (substr, str[, start]) with no occurrence argument, so the
        # first two arguments swap order (same shape as T-SQL's CHARINDEX)
        # and a 4-argument call is flagged.
        if len(args) == 2:
            return f"LOCATE({args[1]}, {args[0]})"
        if len(args) == 3:
            return f"LOCATE({args[1]}, {args[0]}, {args[2]})"
        issues.append(ConversionIssue(
            "warning",
            f"INSTR with {len(args)} arguments (occurrence) has no direct LOCATE() equivalent; "
            "left as INSTR() -- review and rewrite manually.",
        ))
        return f"INSTR({', '.join(args)})"

    text, _n = transform_function_calls(text, "INSTR", _instr)

    # DBMS_LOB.GETLENGTH(lob) -> LENGTH(lob); DBMS_LOB.SUBSTR(lob, amount, offset) -> SUBSTR(lob, offset, amount)
    text, _n = transform_function_calls(text, r"DBMS_LOB\.GETLENGTH", dbms_lob_getlength_replacer("LENGTH", issues))
    text, _n = transform_function_calls(text, r"DBMS_LOB\.SUBSTR", dbms_lob_substr_replacer("SUBSTR", issues))

    # DBMS_RANDOM.VALUE (no-arg form) -> RAND()
    text = replace_dbms_random_value(text, issues, "RAND()")

    def _raise_app_error(args: List[str]) -> str:
        if len(args) >= 2:
            issues.append(ConversionIssue(
                "warning",
                f"RAISE_APPLICATION_ERROR error code {args[0]} was dropped; Db2 SIGNAL uses a 5-character "
                "SQLSTATE instead -- a generic user-defined SQLSTATE ('70000') was used; assign a specific "
                "one (in the '70000'-'99999' or 'U0000'-'U9999' user-defined ranges) if the caller depends on it.",
            ))
            return f"SIGNAL SQLSTATE '70000' SET MESSAGE_TEXT = {args[1]}"
        issues.append(ConversionIssue("error", "RAISE_APPLICATION_ERROR could not be parsed."))
        return f"RAISE_APPLICATION_ERROR({', '.join(args)})"

    text, _n = transform_function_calls(text, "RAISE_APPLICATION_ERROR", _raise_app_error)

    # sequence NEXTVAL / CURRVAL -- Db2's ANSI-style NEXT VALUE FOR / PREVIOUS
    # VALUE FOR supports both directions natively, unlike T-SQL (which has no
    # CURRVAL equivalent at all).
    text = re.sub(
        r"\b([A-Za-z_][\w$#]*)\.NEXTVAL\b",
        lambda m: f"NEXT VALUE FOR {_quote_db2(m.group(1))}", text, flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\b([A-Za-z_][\w$#]*)\.CURRVAL\b",
        lambda m: f"PREVIOUS VALUE FOR {_quote_db2(m.group(1))}", text, flags=re.IGNORECASE,
    )

    # NOTE: EXIT/EXIT WHEN/CONTINUE/CONTINUE WHEN are deliberately *not*
    # rewritten here -- see _convert_exit_continue's call site in
    # _convert_loop_construct for why this must wait until a specific loop's
    # body (and its own label) is known.

    for pattern, message in _MANUAL_MARKERS:
        if pattern.search(text):
            issues.append(ConversionIssue("error", message))

    return text, issues


# --------------------------------------------------------- implicit cursors

_ROW_FOR_HEADER_RE = re.compile(r"\bFOR\s+([A-Za-z_][\w$#]*)\s+IN\s+", re.IGNORECASE)
_NUMERIC_RANGE_RE = re.compile(r"^(REVERSE\s+)?(.+?)\.\.(.+)$", re.IGNORECASE | re.DOTALL)


def _find_matching_end_loop(text: str, start: int) -> Optional[Tuple[int, int]]:
    """text[start:] begins right after the LOOP keyword that opens the loop
    being matched. Returns the (start, end) span of the matching END LOOP."""
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


def _convert_row_for_loops(
    body_text: str, declare_text: str, issues: List[ConversionIssue], labels: _LabelGen
) -> str:
    """Rewrite every Oracle implicit-cursor/row FOR loop into Db2's native
    `FOR var AS cursor CURSOR FOR select DO ... END FOR label;` -- unlike the
    T-SQL converter, this needs no column-list extraction or rec.col
    rewriting at all: Db2, like Oracle, keeps row-variable dot access
    working on whatever the underlying SELECT actually returns."""
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

        loop_kw_m = re.search(r"\bLOOP\b", rest, re.IGNORECASE)
        if loop_kw_m and _NUMERIC_RANGE_RE.match(rest[: loop_kw_m.start()].strip()):
            # numeric range loop ("FOR i IN 1..10 LOOP") -- handled by the
            # control-flow converter, not here.
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

        if select_text is None:
            issues.append(ConversionIssue(
                "error",
                f"MANUAL CONVERSION REQUIRED: could not resolve the query for implicit-cursor FOR loop "
                f"'{loop_var}' (referenced cursor '{cursor_name_ref}' was not found in the DECLARE section).",
            ))
            out.append(body_text[pos:m.start()])
            out.append(
                f"\n  /* MANUAL CONVERSION REQUIRED -- original Oracle loop follows:\n"
                f"{body_text[m.start(): stmt_end]}\n  */\n"
            )
            pos = stmt_end
            continue

        converted_inner, inner_issues = convert_body(raw_loop_body, declare_text=declare_text, labels=labels)
        issues.extend(inner_issues)

        label = labels.next(loop_var)
        converted_inner = _convert_exit_continue(converted_inner, label)
        cursor_ident = cursor_name_ref if cursor_name_ref is not None else f"{loop_var}_cur"

        header = f"{label}: FOR {loop_var} AS {cursor_ident} CURSOR FOR\n  {select_text}\nDO"
        out.append(body_text[pos:m.start()])
        out.append(f"\n{header}\n{_indent(converted_inner)}\nEND FOR {label};\n")
        pos = stmt_end

    return "".join(out)


# ------------------------------------------------------------- control flow

_CTRL_OPENER_RE = re.compile(r"\bIF\b|\bWHILE\b|\bFOR\b|\bLOOP\b|\bBEGIN\b|\bCASE\b", re.IGNORECASE)


def _find_matching_end_if(text: str, start: int) -> Optional[dict]:
    """text[start:] begins right after the top-level IF keyword's condition
    start. CASE is tracked as an opener too (matched by END CASE) purely so
    a CASE *statement*'s own internal WHEN/THEN tokens (which use the same
    keywords as IF) don't get mistaken for this IF's own branch markers."""
    token_re = re.compile(
        r"\bIF\b|\bEND\s+IF\b|\bCASE\b|\bEND\s+CASE\b|\bTHEN\b|\bELSIF\b|\bELSE\b", re.IGNORECASE)
    depth = 0
    markers: List[Tuple[str, int, int]] = []
    end_if_span = None

    def _scan(s: str, pos: int):
        nonlocal depth, markers, end_if_span
        for m in token_re.finditer(s, pos):
            word = re.sub(r"\s+", " ", m.group(0).upper())
            if word in ("IF", "CASE"):
                depth += 1
                continue
            if word == "END IF":
                if depth == 0:
                    end_if_span = (m.start(), m.end())
                    return
                depth -= 1
                continue
            if word == "END CASE":
                depth -= 1
                continue
            if depth == 0 and word in ("THEN", "ELSIF", "ELSE"):
                markers.append((word, m.start(), m.end()))

    _apply_outside_strings_scan(text, start, _scan)
    if end_if_span is None or not markers or markers[0][0] != "THEN":
        return None
    return {"markers": markers, "end_if_start": end_if_span[0], "end_if_end": end_if_span[1]}


def _convert_if_statement(
    text: str, cond_start: int, labels: _LabelGen
) -> Optional[Tuple[str, int, List[ConversionIssue]]]:
    """Db2's IF/THEN/ELSEIF/ELSE/END IF is directly equivalent to Oracle's
    IF/THEN/ELSIF/ELSE/END IF (just a one-word keyword rename), so -- unlike
    the T-SQL converter -- this reassembles the *same* statement shape
    rather than restructuring into BEGIN/END blocks; only each branch's own
    body is recursively converted."""
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
        conv_body, sub_issues = _convert_control_flow(body, labels)
        issues.extend(sub_issues)
        if cond is not None:
            keyword = "IF" if idx == 0 else "ELSEIF"
            parts.append(f"{keyword} {cond} THEN\n{_indent(conv_body)}")
        else:
            parts.append(f"ELSE\n{_indent(conv_body)}")

    end_pos = parsed["end_if_end"]
    semi_m = re.match(r"\s*;", text[end_pos:])
    if semi_m:
        end_pos += semi_m.end()

    return "\n".join(parts) + "\nEND IF;", end_pos, issues


def _convert_loop_construct(
    text: str, kw_start: int, kw_word: str, labels: _LabelGen
) -> Optional[Tuple[str, int, List[ConversionIssue]]]:
    """text[kw_start:] begins at a WHILE/FOR/LOOP keyword that opens a loop
    (numeric-range FOR only -- row/cursor FOR loops are already removed by
    _convert_row_for_loops before this runs). Returns
    (converted_db2, position_just_after_trailing_';', issues, extra_declares)
    where extra_declares is a list of DECLARE lines (numeric-loop counters)
    that must be hoisted to the enclosing routine's top-level declare
    section, since Db2 forbids a DECLARE anywhere but the very top of a
    compound statement."""
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

    conv_body, sub_issues = _convert_control_flow(raw_body, labels)
    issues.extend(sub_issues)

    if kw_word == "LOOP":
        label = labels.next("loop")
        conv_body = _convert_exit_continue(conv_body, label)
        return f"{label}: LOOP\n{_indent(conv_body)}\nEND LOOP {label};", end_pos, issues

    if kw_word == "WHILE":
        label = labels.next("while")
        conv_body = _convert_exit_continue(conv_body, label)
        cond = header_line[len("WHILE"):].strip() if header_line.upper().startswith("WHILE") else header_line
        return f"{label}: WHILE {cond} DO\n{_indent(conv_body)}\nEND WHILE {label};", end_pos, issues

    # FOR <var> IN [REVERSE] low..high -- Db2 has no numeric-range FOR loop
    # either, so this is rewritten to a WHILE loop with an explicit counter,
    # same idea as the T-SQL converter -- except the counter's DECLARE can't
    # sit inline here (Db2 forbids DECLARE anywhere but the top of the
    # enclosing compound statement); see _find_numeric_range_loop_var_names
    # and its call site, which hoists it into the routine's own declare
    # block instead. Only the initializing SET is emitted here.
    for_m = re.match(
        r"^FOR\s+([A-Za-z_][\w$#]*)\s+IN\s+(REVERSE\s+)?(.+?)\.\.(.+)$",
        header_line, re.IGNORECASE | re.DOTALL,
    )
    if not for_m:
        issues.append(ConversionIssue(
            "error", f"MANUAL CONVERSION REQUIRED: could not parse FOR-loop header '{header_line}'."))
        return f"/* MANUAL CONVERSION REQUIRED: {header_line} */\n{conv_body}", end_pos, issues

    var, reverse, low, high = for_m.groups()
    reverse = bool(reverse)
    label = labels.next(var)
    conv_body = _convert_exit_continue(conv_body, label)
    # REVERSE counts DOWN from the upper bound to the lower bound (Oracle's
    # `FOR i IN REVERSE 1..3 LOOP` visits i = 3, 2, 1), so the starting value
    # and the bound the WHILE condition checks against are swapped relative
    # to the forward case, not just the comparison operator/step direction.
    start_val = high.strip() if reverse else low.strip()
    bound_val = low.strip() if reverse else high.strip()
    cmp_op = ">=" if reverse else "<="
    step = "- 1" if reverse else "+ 1"
    return (
        f"SET {var} = {start_val};\n"
        f"{label}: WHILE {var} {cmp_op} {bound_val} DO\n{_indent(conv_body)}\n"
        f"{_indent(f'SET {var} = {var} {step};')}\nEND WHILE {label};"
    ), end_pos, issues


def _convert_control_flow(text: str, labels: _LabelGen) -> Tuple[str, List[ConversionIssue]]:
    """Recursively rewrite Oracle IF/THEN/ELSIF/END IF (-> Db2's
    IF/THEN/ELSEIF/END IF), LOOP/END LOOP, WHILE...LOOP (-> WHILE...DO...
    END WHILE), FOR...LOOP (numeric range), and nested BEGIN...END blocks;
    everything else (assignments, SQL statements, CASE statements, RETURN,
    etc.) passes through untouched."""
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

        if word == "FOR" and not re.match(r"\s*[A-Za-z_][\w$#]*\s+IN\b", text[m.end():], re.IGNORECASE):
            out.append(text[pos:m.end()])
            pos = m.end()
            continue
        if word == "WHILE":
            next_kw_m = re.search(r"\bLOOP\b|\bBEGIN\b|\bDO\b|;", text[m.end():], re.IGNORECASE)
            if not (next_kw_m and next_kw_m.group(0).upper() == "LOOP"):
                out.append(text[pos:m.end()])
                pos = m.end()
                continue
        if word == "CASE":
            # A CASE *statement/expression* is left completely untouched --
            # Db2 supports both natively. Just skip past this token so it
            # doesn't get treated as a control-flow construct to convert.
            out.append(text[pos:m.end()])
            pos = m.end()
            continue

        out.append(text[pos:m.start()])

        result = None
        if word == "IF":
            result = _convert_if_statement(text, m.end(), labels)
        elif word == "BEGIN":
            result = _convert_begin_block(text, m.start(), labels)
        elif word in ("WHILE", "FOR", "LOOP"):
            result = _convert_loop_construct(text, m.start(), word, labels)

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

    return "".join(out), issues


# --------------------------------------------------- exception -> handlers


def _split_exception_clauses(exc_text: str) -> List[Tuple[str, str]]:
    """Split an Oracle EXCEPTION section into (condition_text, statements)
    pairs for each top-level 'WHEN ... THEN ...' clause.

    By the time this runs, a row/cursor FOR loop occurring anywhere in the
    routine body (including, in principle, inside a handler's own
    statements) has already been converted to Db2's native
    `FOR ... DO ... END FOR;` -- "END FOR" is matched here as an inert,
    depth-neutral phrase (like "END LOOP"/"END IF"/"END CASE") rather than
    falling through to the bare-"END" branch, which would otherwise
    wrongly decrement depth for an opener ("FOR") that was never counted
    in the first place, corrupting this scan's WHEN/THEN boundaries."""
    token_re = re.compile(
        r"\bBEGIN\b|\bCASE\b|\bIF\b|\bLOOP\b|\bEND\s+IF\b|\bEND\s+CASE\b|"
        r"\bEND\s+LOOP\b|\bEND\s+FOR\b|\bEND\b|\bWHEN\b|\bTHEN\b",
        re.IGNORECASE,
    )
    depth = 0
    when_marks: List[Tuple[int, int]] = []
    then_marks: List[Tuple[int, int]] = []
    awaiting_then = False

    def _scan(s: str, pos: int):
        nonlocal depth, awaiting_then
        for m in token_re.finditer(s, pos):
            word = re.sub(r"\s+", " ", m.group(0).upper())
            if word in ("BEGIN", "CASE", "IF", "LOOP"):
                depth += 1
            elif word in ("END IF", "END CASE", "END LOOP"):
                depth -= 1
            elif word == "END FOR":
                pass  # inert -- see the docstring note above
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


def _exception_condition_to_handler_condition(condition_text: str, issues: List[ConversionIssue]) -> Optional[str]:
    """Convert an Oracle exception-name condition (possibly OR-combined)
    into a Db2 DECLARE HANDLER condition-list (comma-separated SQLSTATEs
    and/or the NOT FOUND keyword), or None if this is the OTHERS handler
    (which becomes a DECLARE ... HANDLER FOR SQLEXCEPTION)."""
    names = [n.strip() for n in re.split(r"\bOR\b", condition_text, flags=re.IGNORECASE) if n.strip()]
    if len(names) == 1 and names[0].upper() == "OTHERS":
        return None
    conditions: List[str] = []
    for name in names:
        upper = name.upper()
        if upper == "OTHERS":
            continue  # OTHERS combined with other names is unusual; OTHERS is handled as its own final handler
        if upper == "NO_DATA_FOUND":
            conditions.append("NOT FOUND")
        elif upper in _EXCEPTION_SQLSTATE_MAP:
            conditions.append(f"SQLSTATE '{_EXCEPTION_SQLSTATE_MAP[upper]}'")
        else:
            issues.append(ConversionIssue(
                "error" if upper in _UNMAPPABLE_EXCEPTIONS else "warning",
                f"Exception handler 'WHEN {name} THEN' has no known Db2 SQLSTATE mapping "
                f"{'(built-in Oracle exception with no Db2 equivalent condition)' if upper in _UNMAPPABLE_EXCEPTIONS else '(user-defined exception)'}; "
                "review and adjust the generated DECLARE HANDLER condition by hand.",
            ))
            conditions.append(f"SQLSTATE '99999' /* unmappable: {name} */")
    if not conditions:
        return "SQLSTATE '99999' /* unresolved */"
    return ", ".join(conditions)


def _convert_raise_statements(text: str) -> str:
    """RAISE; (bare re-raise) -> RESIGNAL; (Db2 supports true re-signaling
    of the currently-handled condition, unlike T-SQL, which has to fake it
    with THROW;). RAISE exc_name; (a declared, user-defined exception with
    no Db2 equivalent condition) -> a SIGNAL with the exception's name as
    the message, since there's nothing else to map it to."""
    text = re.sub(r"\bRAISE\s*;", "RESIGNAL;", text, flags=re.IGNORECASE)
    text = re.sub(
        r"\bRAISE\s+([A-Za-z_][\w$#]*)\s*;",
        lambda m: f"SIGNAL SQLSTATE '70000' SET MESSAGE_TEXT = '{m.group(1)}';",
        text, flags=re.IGNORECASE,
    )
    return text


def _build_handler_declares(exc_text: str, labels: _LabelGen) -> Tuple[List[str], List[ConversionIssue]]:
    """Convert an Oracle EXCEPTION section into a list of Db2
    `DECLARE EXIT HANDLER FOR ... BEGIN ... END;` statements, meant to be
    hoisted to the top of whichever compound statement's declare section
    this EXCEPTION section was attached to."""
    issues: List[ConversionIssue] = []
    clauses = _split_exception_clauses(exc_text)
    if not clauses:
        issues.append(ConversionIssue(
            "error", "EXCEPTION section present but no WHEN...THEN clauses could be parsed from it; "
                     "manual conversion of the handler is required."))
        return [f"-- MANUAL CONVERSION REQUIRED: {exc_text.strip()}"], issues

    declares: List[str] = []
    others_body: Optional[str] = None
    for condition_text, stmts_text in clauses:
        condition = _exception_condition_to_handler_condition(condition_text, issues)
        stmts_conv, stmts_issues = _convert_control_flow(_convert_raise_statements(stmts_text), labels)
        issues.extend(stmts_issues)
        if condition is None:
            others_body = stmts_conv
            continue
        declares.append(
            f"DECLARE EXIT HANDLER FOR {condition}\nBEGIN\n{_indent(stmts_conv)}\nEND;"
        )

    if others_body is not None:
        declares.append(f"DECLARE EXIT HANDLER FOR SQLEXCEPTION\nBEGIN\n{_indent(others_body)}\nEND;")

    return declares, issues


def _find_matching_end_for_begin(text: str, body_start: int) -> Optional[dict]:
    """text[body_start:] begins right after a nested 'BEGIN' keyword. Finds
    its matching top-level EXCEPTION (if any) and END span.

    By the time this runs, any row/cursor FOR loop in the routine body has
    already been converted to Db2's native `FOR ... DO ... END FOR;` --
    "END FOR" is matched here as an inert, depth-neutral phrase (like
    "END LOOP"/"END IF"/"END CASE"), not the bare-"END" branch, which would
    otherwise wrongly decrement depth for an opener ("FOR") this scan never
    counted in the first place -- that false decrement was observed to
    truncate the routine body right at the first row-loop's own "END FOR",
    silently dropping the EXCEPTION section and everything else after it."""
    token_re = re.compile(
        r"\bBEGIN\b|\bCASE\b|\bIF\b|\bLOOP\b|\bEND\s+IF\b|\bEND\s+CASE\b|"
        r"\bEND\s+LOOP\b|\bEND\s+FOR\b|\bEND\b|\bEXCEPTION\b",
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
            if word == "END FOR":
                continue  # inert -- see the docstring note above
            if word == "END":
                if depth == 0:
                    end_span = (m.start(), m.end())
                    return
                depth -= 1

    _apply_outside_strings_scan(text, body_start, _scan)
    if end_span is None:
        return None
    return {"exc_start": exc_start, "end_start": end_span[0], "end_end": end_span[1]}


def _convert_begin_block(
    text: str, kw_start: int, labels: _LabelGen
) -> Optional[Tuple[str, int, List[ConversionIssue]]]:
    """text[kw_start:] begins at a nested 'BEGIN' keyword (an anonymous
    sub-block). Db2 lets a nested compound statement declare its own local
    handlers just like the outer routine can, so a local EXCEPTION section
    (if any) is hoisted to the top of *this* BEGIN...END, scoping it
    correctly to only this sub-block -- matching Oracle's own scoping rules
    for a nested block's EXCEPTION section."""
    body_start = kw_start + len("BEGIN")
    parsed = _find_matching_end_for_begin(text, body_start)
    if parsed is None:
        return None
    issues: List[ConversionIssue] = []
    main_end = parsed["exc_start"] if parsed["exc_start"] is not None else parsed["end_start"]
    main_text = text[body_start:main_end]

    main_conv, main_issues = _convert_control_flow(_convert_raise_statements(main_text), labels)
    issues.extend(main_issues)

    handler_declares: List[str] = []
    if parsed["exc_start"] is not None:
        exc_text = text[parsed["exc_start"] + len("EXCEPTION"): parsed["end_start"]]
        handler_declares, handler_issues = _build_handler_declares(exc_text, labels)
        issues.extend(handler_issues)

    end_pos = parsed["end_end"]
    semi_m = re.match(r"\s*;", text[end_pos:])
    if semi_m:
        end_pos += semi_m.end()

    inner = "\n".join(handler_declares + [main_conv]) if handler_declares else main_conv
    return f"BEGIN\n{_indent(inner)}\nEND;", end_pos, issues


# ------------------------------------------------------------ declare block


def convert_declare_block(text: str, skip_names: Optional[set] = None) -> Tuple[str, List[ConversionIssue], set]:
    """Convert the DECLARE section (everything between IS/AS and BEGIN) into
    Db2 `DECLARE name TYPE [DEFAULT value];` / named-cursor statements.
    Returns (declare_sql, issues, variable_names)."""
    issues: List[ConversionIssue] = []
    text, nested = extract_nested_subprograms(text)
    out_lines: List[str] = []
    for ns in nested:
        out_lines.append(
            f"-- MANUAL CONVERSION REQUIRED: nested {ns.kind} {ns.name}, declared inside this "
            f"routine's own DECLARE section -- Db2 SQL PL has no nested named-subprogram declaration.\n"
            f"-- Extract it as a standalone procedure/function, or inline its logic by hand. "
            f"Original source:\n"
            f"/*\n{ns.source}\n*/"
        )
        issues.append(ConversionIssue(
            "error",
            f"Nested {ns.kind} '{ns.name}' is declared inside this routine's own DECLARE section; "
            f"Db2 SQL PL has no nested named-subprogram declaration -- extract it as a standalone "
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
                f"Db2 SQL PL equivalent -- rewrite using an array type, a row type, or a cursor "
                f"variable as appropriate, and update every reference to it in this routine's body.",
            ))
            continue

        if re.match(r"^PRAGMA\b", stmt, re.IGNORECASE):
            out_lines.append(f"-- (removed) {stmt};  -- Db2 has no PRAGMA; review if this affected behavior")
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
                    f"Cursor '{cur_name}' takes parameters, which Db2 cursors don't support directly; "
                    "the parameter list was dropped -- reference the values via variables instead.",
                ))
            out_lines.append(f"DECLARE {cur_name} CURSOR FOR\n  {cur_query.strip()};")
            # _MANUAL_MARKERS only scans the executable body text, so a
            # CONNECT BY that's still present here -- i.e.
            # connect_by_rewriter.py already had its shot at it (from
            # convert_routine, before this function ever runs) and it
            # didn't match the supported simple shape -- would otherwise
            # never get flagged at all.
            if re.search(r"\bCONNECT\s+BY\b", cur_query, re.IGNORECASE):
                issues.append(ConversionIssue(
                    "error",
                    f"Cursor '{cur_name}': hierarchical query (CONNECT BY) must be rewritten as a recursive common table expression (WITH ... AS).",
                ))
            continue

        exc_m = re.match(r"^([A-Za-z_][\w$#]*)\s+EXCEPTION\s*$", stmt, re.IGNORECASE)
        if exc_m:
            out_lines.append(f"-- (kept for reference only) {stmt};  -- Db2 has no user-defined EXCEPTION type")
            issues.append(ConversionIssue(
                "error",
                f"Exception '{exc_m.group(1)}' is declared but Db2 has no equivalent to an Oracle "
                f"user-defined EXCEPTION; RAISE statements referencing it are converted to SIGNAL SQLSTATE "
                f"'70000' with the exception's name as the message -- review and assign a real SQLSTATE/message.",
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
                f"Db2's native FOR statement declares it automatically."
            )
            issues.append(ConversionIssue(
                "info",
                f"Dropped declaration of '{name}': it is used as a row/cursor FOR-loop variable, which "
                f"Db2's FOR statement declares automatically.",
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
                f"'{name}' was declared CONSTANT in Oracle; Db2 local variables have no immutability "
                "enforcement, so it is declared as a plain variable -- avoid reassigning it.",
            ))
        if not_null:
            issues.append(ConversionIssue(
                "warning",
                f"'{name} NOT NULL' has no Db2 local-variable equivalent; the NOT NULL constraint is not "
                "enforced -- add an explicit check if this mattered.",
            ))

        var_names.add(name)
        line = f"DECLARE {name} {mapped_type}"
        if default:
            line += f" DEFAULT {default}"
        line += ";"
        out_lines.append(line)

    return "\n".join(out_lines), issues, var_names


# ------------------------------------------------------- implicit-var names


def _find_row_loop_variable_names(body_text: str) -> set:
    names = set()
    for m in _ROW_FOR_HEADER_RE.finditer(body_text):
        rest = body_text[m.end():]
        loop_kw_m = re.search(r"\bLOOP\b", rest, re.IGNORECASE)
        header = rest[: loop_kw_m.start()].strip() if loop_kw_m else ""
        if not _NUMERIC_RANGE_RE.match(header):
            names.add(m.group(1))
    return names


def _find_numeric_range_loop_var_names(body_text: str) -> set:
    """Names used as the counter of a numeric-range FOR loop. Db2 forbids a
    DECLARE anywhere but the top of a compound statement, so (unlike the
    T-SQL converter, which can declare its counter inline at the loop site)
    every one of these must be pre-declared in the routine's own top-level
    declare section -- see convert_procedure_or_function/convert_trigger,
    which append a DECLARE line for each name this returns."""
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


# ------------------------------------------------------------ top-level body


def convert_body(text: str, declare_text: str = "", labels: Optional[_LabelGen] = None) -> Tuple[str, List[ConversionIssue]]:
    """Convert a flat (not BEGIN/EXCEPTION/END-wrapped) statement sequence
    -- used both directly (a loop body) and internally by the top-level
    routine-body converter after it has split out the outer EXCEPTION
    section."""
    labels = labels if labels is not None else _LabelGen()
    issues: List[ConversionIssue] = []
    text = _convert_row_for_loops(text, declare_text, issues, labels)
    text, builtin_issues = _builtin_rewrites(text)
    issues.extend(builtin_issues)
    text, ctrl_issues = _convert_control_flow(text, labels)
    issues.extend(ctrl_issues)
    return text, issues


def convert_routine_body(
    full_body_text: str, declare_text: str, labels: _LabelGen
) -> Tuple[str, List[str], List[ConversionIssue]]:
    """Convert the full `BEGIN ... [EXCEPTION WHEN ... THEN ...]* END;` body
    of a procedure/function/trigger into Db2 SQL PL *statement content only*
    (no outer BEGIN/END -- the caller combines this with its own DECLARE
    statements inside a single BEGIN/END). Returns
    (statements_sql, handler_declares, issues) -- handler_declares must be
    placed in the caller's declare section, after its own variable declares
    (Db2 requires handlers to be the last thing in the declare section)."""
    issues: List[ConversionIssue] = []
    text = _convert_row_for_loops(full_body_text, declare_text, issues, labels)
    text, builtin_issues = _builtin_rewrites(text)
    issues.extend(builtin_issues)

    text = text.rstrip()
    end_m = re.search(r"END\s+([A-Za-z_][\w$#]*)\s*;\s*$", text, re.IGNORECASE)
    if end_m and end_m.group(1).upper() not in ("IF", "LOOP", "CASE"):
        text = text[: end_m.start()] + "END;"

    begin_kw_m = re.search(r"\bBEGIN\b", text, re.IGNORECASE)
    if not begin_kw_m:
        issues.append(ConversionIssue("error", "Could not locate the routine's BEGIN keyword; manual conversion required."))
        return f"-- MANUAL CONVERSION REQUIRED\n/*\n{text}\n*/", [], issues

    body_start = begin_kw_m.end()
    parsed = _find_matching_end_for_begin(text, body_start)
    if parsed is None:
        issues.append(ConversionIssue("error", "Could not parse the routine body's BEGIN/END structure; manual conversion required."))
        return f"-- MANUAL CONVERSION REQUIRED\n/*\n{text}\n*/", [], issues

    main_end = parsed["exc_start"] if parsed["exc_start"] is not None else parsed["end_start"]
    main_text = text[body_start:main_end]
    main_conv, main_issues = _convert_control_flow(_convert_raise_statements(main_text), labels)
    issues.extend(main_issues)

    handler_declares: List[str] = []
    if parsed["exc_start"] is not None:
        exc_text = text[parsed["exc_start"] + len("EXCEPTION"): parsed["end_start"]]
        handler_declares, handler_issues = _build_handler_declares(exc_text, labels)
        issues.extend(handler_issues)

    return main_conv, handler_declares, issues


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
        issues.append(ConversionIssue("error", "Could not parse the routine header; manual conversion required."))
        return f"-- MANUAL CONVERSION REQUIRED for {routine.kind} {routine.name}\n/*\n{routine.source}\n*/", issues

    labels = _LabelGen()

    params = split_top_level(parsed["params_text"], ",")
    db2_params: List[str] = []
    for raw_param in params:
        if not raw_param:
            continue
        name, mode, type_text, default = parse_param(raw_param)
        mapped_type, type_issues = _convert_declared_type(type_text)
        issues.extend(type_issues)
        db2_mode = {"IN": "IN", "OUT": "OUT", "INOUT": "INOUT"}.get(mode, "IN")
        piece = f"{db2_mode} {name} {mapped_type}"
        db2_params.append(piece)
    params_clause = ",\n  ".join(db2_params)

    row_loop_vars = _find_row_loop_variable_names(parsed["body_text"])
    numeric_loop_vars = _find_numeric_range_loop_var_names(parsed["body_text"])

    declare_sql, declare_issues, _var_names = convert_declare_block(parsed["declare_text"], row_loop_vars)
    issues.extend(declare_issues)

    body_sql, handler_declares, body_issues = convert_routine_body(parsed["body_text"], parsed["declare_text"], labels)
    issues.extend(body_issues)

    declare_lines = [declare_sql] if declare_sql.strip() else []
    if numeric_loop_vars:
        declare_lines.append("\n".join(f"DECLARE {v} INT DEFAULT 0;" for v in sorted(numeric_loop_vars)))
    declare_lines.extend(handler_declares)
    full_declare = "\n".join(d for d in declare_lines if d.strip())

    inner_parts = []
    if full_declare:
        inner_parts.append(full_declare)
    inner_parts.append(body_sql)
    inner = "\n".join(inner_parts)

    if parsed["kind"] == "FUNCTION":
        if parsed["return_type"]:
            return_type, return_issues = _convert_declared_type(parsed["return_type"])
            issues.extend(return_issues)
        else:
            return_type = "VARCHAR(4000)"
            issues.append(ConversionIssue("warning", "FUNCTION had no parsable RETURN type; defaulted to VARCHAR(4000)."))
        params_block = f"(\n  {params_clause}\n)" if params_clause else "()"
        ddl = (
            f"CREATE OR REPLACE FUNCTION {_quote_db2(routine.name)}{params_block}\n"
            f"RETURNS {return_type}\n"
            f"LANGUAGE SQL\n"
            f"BEGIN\n"
            f"{_indent(inner)}\n"
            f"END;"
        )
    else:
        params_block = f"(\n  {params_clause}\n)" if params_clause else "()"
        ddl = (
            f"CREATE OR REPLACE PROCEDURE {_quote_db2(routine.name)}{params_block}\n"
            f"LANGUAGE SQL\n"
            f"BEGIN\n"
            f"{_indent(inner)}\n"
            f"END;"
        )

    return ddl, issues


def convert_trigger(routine: Routine) -> Tuple[str, List[ConversionIssue]]:
    """Db2 supports BEFORE, AFTER, and INSTEAD OF triggers natively -- unlike
    SQL Server, there's no need to flag BEFORE as unsupported. Db2's
    REFERENCING clause names row-level correlation aliases (no ':' sigil,
    unlike Oracle's :NEW/:OLD) -- fixed aliases NEW_ROW/OLD_ROW are used
    here and Oracle's :NEW./:OLD. are rewritten to reference them."""
    issues: List[ConversionIssue] = []
    labels = _LabelGen()

    timing = (routine.timing or "BEFORE").upper()
    db2_timing = "NO CASCADE BEFORE" if timing == "BEFORE" else timing

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
    declare_sql, declare_issues, _var_names = (
        convert_declare_block(declare_text, row_loop_vars) if declare_text.strip() else ("", [], set())
    )
    issues.extend(declare_issues)

    def _rewrite_new_old(chunk: str) -> str:
        chunk = re.sub(r":NEW\.(\w+)", r"NEW_ROW.\1", chunk, flags=re.IGNORECASE)
        chunk = re.sub(r":OLD\.(\w+)", r"OLD_ROW.\1", chunk, flags=re.IGNORECASE)
        return chunk

    body_text = _apply_outside_strings(body_text, _rewrite_new_old)

    body_sql, handler_declares, body_issues = convert_routine_body(body_text, declare_text, labels)
    issues.extend(body_issues)

    declare_lines = [declare_sql] if declare_sql.strip() else []
    if numeric_loop_vars:
        declare_lines.append("\n".join(f"DECLARE {v} INT DEFAULT 0;" for v in sorted(numeric_loop_vars)))
    declare_lines.extend(handler_declares)
    full_declare = "\n".join(d for d in declare_lines if d.strip())

    inner_parts = []
    if full_declare:
        inner_parts.append(full_declare)
    inner_parts.append(body_sql)
    inner = "\n".join(inner_parts)

    events = routine.events or ["INSERT"]
    table_name = routine.table_name or "UNKNOWN_TABLE"
    if routine.table_name is None:
        issues.append(ConversionIssue("error", "Trigger's target table was not captured; DDL references UNKNOWN_TABLE."))

    level = "ROW" if routine.row_level else "STATEMENT"
    referencing = "REFERENCING NEW AS NEW_ROW OLD AS OLD_ROW" if routine.row_level else (
        "REFERENCING NEW_TABLE AS NEW_ROWS OLD_TABLE AS OLD_ROWS"
    )
    if not routine.row_level and re.search(r"\bNEW_ROW\.|\bOLD_ROW\.", inner):
        issues.append(ConversionIssue(
            "warning",
            "Statement-level trigger references :NEW/:OLD, which Db2 only exposes as NEW_TABLE/OLD_TABLE "
            "result sets at the statement level, not single-row aliases; review the generated body.",
        ))

    if len(events) <= 1:
        event_clause = (events[0] if events else "INSERT")
        ddl = (
            f"CREATE OR REPLACE TRIGGER {_quote_db2(routine.name)}\n"
            f"{db2_timing} {event_clause}\n"
            f"ON {_quote_db2(table_name)}\n"
            f"{referencing}\n"
            f"FOR EACH {level}\n"
            f"BEGIN ATOMIC\n"
            f"{_indent(inner)}\n"
            f"END;"
        )
    else:
        # Db2 (unlike Oracle/SQL Server) does not support combining multiple
        # trigger events (INSERT OR UPDATE OR DELETE) in one CREATE TRIGGER
        # -- split into one CREATE TRIGGER per event instead, each with a
        # distinct, event-suffixed name.
        issues.append(ConversionIssue(
            "info",
            f"Trigger '{routine.name}' fires on multiple events ({', '.join(events)}) in Oracle; Db2 "
            "requires a separate CREATE TRIGGER per event, so this was split into "
            f"{len(events)} triggers named '{routine.name}_<EVENT>'.",
        ))
        parts = []
        for event in events:
            parts.append(
                f"CREATE OR REPLACE TRIGGER {_quote_db2(f'{routine.name}_{event}')}\n"
                f"{db2_timing} {event}\n"
                f"ON {_quote_db2(table_name)}\n"
                f"{referencing}\n"
                f"FOR EACH {level}\n"
                f"BEGIN ATOMIC\n"
                f"{_indent(inner)}\n"
                f"END;"
            )
        ddl = "\n\n".join(parts)

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
        issues.append(ConversionIssue("error", "No PROCEDURE/FUNCTION members found in package body; manual conversion required."))
        return f"-- MANUAL CONVERSION REQUIRED for PACKAGE BODY {routine.name}\n/*\n{source}\n*/", issues

    chunks: List[str] = []
    for idx, m in enumerate(header_positions):
        start = m.start()
        end = header_positions[idx + 1].start() if idx + 1 < len(header_positions) else len(source)
        chunks.append(source[start:end])

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
        "Package-level variables/constants (if the package declared any) have no direct Db2 equivalent; "
        "consider a global variable (CREATE VARIABLE) or a settings table if state needs to be shared "
        "across the flattened procedures/functions.",
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
            f"-- PACKAGE {routine.name} has no Db2 equivalent (no packaging construct). "
            f"Its public members are converted individually from the matching PACKAGE BODY. "
            f"Package-level constants/types declared only in the spec must be moved by hand."
        )
        issues = [ConversionIssue(
            "warning", "Package specs have no Db2 equivalent; only the package body's members were converted.")]
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
