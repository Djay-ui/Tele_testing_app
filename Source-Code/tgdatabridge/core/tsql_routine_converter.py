"""
Transact-SQL stored procedure / function / trigger -> MySQL or PostgreSQL.

Before this module existed, *every* routine from a SQL Server source was
flagged "Requires manual conversion" no matter how simple it was:
plsql_converter.convert_routine's first guard returned early for any
`source_engine != "Oracle"`, because the only converters that existed
(this tool's own PL/pgSQL one, tsql_converter.py, db2_converter.py) all
read Oracle PL/SQL. A SQL Server -> MySQL migration therefore converted
100% of tables and 0% of routines, which is the gap this closes.

Scope, stated honestly
----------------------
This converts the shapes that make up the overwhelming majority of
real-world T-SQL routines:

  * parameter lists, including defaults and OUTPUT parameters
  * DECLARE (with initialisers), SET and SELECT-assignment
  * IF / ELSE and WHILE, in both the `BEGIN...END` and single-statement forms
  * RETURN, PRINT, RAISERROR / THROW
  * BEGIN TRY / BEGIN CATCH
  * DECLARE CURSOR / OPEN / FETCH / @@FETCH_STATUS loops
  * plain SELECT / INSERT / UPDATE / DELETE bodies, expression-translated
    through tsql_dialect
  * AFTER triggers, including the set-based `INSERT ... SELECT FROM inserted
    JOIN deleted` audit-row idiom rewritten into the row-level `NEW`/`OLD`
    form both targets actually use

Anything outside that -- dynamic SQL, table variables, temp tables,
MERGE, table-valued functions on a MySQL target, INSTEAD OF triggers,
cross-database references -- is left in place, reported with an
"error"-severity ConversionIssue, and the routine keeps its
"Requires manual conversion" status. A converted routine is never
presented as automatic unless every construct in it was understood. That
line is the whole point: a routine this tool says it converted must be
one a DBA can read, run and trust.

Target shapes
-------------
MySQL is the closer match: it has CREATE PROCEDURE with IN/OUT
parameters, naked SELECTs that return result sets, and row-level triggers.

PostgreSQL differs in two ways that force a deliberate choice:

  * A PL/pgSQL function cannot return an ad-hoc result set the way a
    T-SQL procedure does. A procedure whose purpose is "run a SELECT and
    hand the rows back" is therefore emitted as a `RETURNS refcursor`
    function that OPENs the query -- the same shape AWS SCT produces for
    this case -- with an issue explaining how to FETCH from it. A
    procedure that returns nothing becomes a real CREATE PROCEDURE.
  * PostgreSQL triggers are a trigger *function* plus a CREATE TRIGGER
    that references it, so each converted trigger emits two statements.
"""
from __future__ import annotations

import re
from typing import List, Optional, Tuple

from tgdatabridge.core import tsql_dialect
from tgdatabridge.core.schema_model import ConversionIssue, ConversionStatus, Routine
from tgdatabridge.core.tsql_dialect import (
    MYSQL, POSTGRES, apply_outside_strings, normalize_target, scan_outside_strings,
    split_call_arguments, translate_expression,
)

_IDENT = r"(?:\[[^\]]+\]|\"[^\"]+\"|[A-Za-z_#@][\w@#$]*)"
_QUALIFIED = rf"(?:{_IDENT}\s*\.\s*)*{_IDENT}"


def _unquote(name: str) -> str:
    name = name.strip()
    if name.startswith("[") and name.endswith("]"):
        return name[1:-1]
    if name.startswith('"') and name.endswith('"'):
        return name[1:-1]
    return name


def _bare_name(qualified: str) -> str:
    """`[dbo].[usp_X]` -> `usp_X`. Neither target has a `dbo` schema, and
    this tool creates every object unqualified in the connection's own
    database/search_path."""
    parts = re.findall(_IDENT, qualified or "")
    return _unquote(parts[-1]) if parts else (qualified or "").strip()


def _quote(name: str, target: str) -> str:
    return tsql_dialect.quote_identifier(name, target)


def _find_keyword(text: str, keyword: str, start: int = 0, depth_zero: bool = True) -> int:
    """Index of the next occurrence of `keyword` as a whole word, skipping
    string literals and comments, and (when depth_zero) only at
    parenthesis depth 0. Returns -1 if not found."""
    scan = scan_outside_strings(text)
    pattern = re.compile(r"\b" + re.escape(keyword) + r"\b", re.IGNORECASE)
    depth = 0
    i = start
    while i < len(scan):
        ch = scan[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if depth == 0 or not depth_zero:
            m = pattern.match(scan, i)
            if m:
                return i
        i += 1
    return -1


# ------------------------------------------------------------ parameters

class Param:
    __slots__ = ("name", "type_text", "default", "is_output", "is_readonly")

    def __init__(self, name: str, type_text: str, default: Optional[str],
                 is_output: bool, is_readonly: bool):
        self.name = name
        self.type_text = type_text
        self.default = default
        self.is_output = is_output
        self.is_readonly = is_readonly


_PARAM_RE = re.compile(
    r"^\s*@(?P<name>[\w@#$]+)\s+(?:AS\s+)?(?P<type>[^=]+?)"
    r"(?:\s*=\s*(?P<default>.+?))?"
    r"(?P<mods>(?:\s+(?:OUT|OUTPUT|READONLY))*)\s*$",
    re.IGNORECASE | re.DOTALL,
)


def _parse_params(param_text: str, issues: List[ConversionIssue]) -> List[Param]:
    text = (param_text or "").strip()
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1]
    if not text.strip():
        return []
    params: List[Param] = []
    for raw in split_call_arguments(text):
        if not raw.strip():
            continue
        m = _PARAM_RE.match(raw)
        if not m:
            issues.append(ConversionIssue(
                "error", f"Parameter '{raw.strip()}' could not be parsed and was not converted."))
            continue
        mods = (m.group("mods") or "").upper()
        type_text = m.group("type").strip()
        # "OUT"/"OUTPUT"/"READONLY" can also trail the *type* when there is
        # no default, which the regex above folds into `mods` -- but a
        # default value can itself end with them, so re-check the type.
        for kw in ("OUTPUT", "OUT", "READONLY"):
            if type_text.upper().endswith(" " + kw):
                type_text = type_text[: -len(kw) - 1].strip()
                mods += " " + kw
        params.append(Param(
            name=m.group("name"),
            type_text=type_text,
            default=(m.group("default") or "").strip() or None,
            is_output=("OUT" in mods or "OUTPUT" in mods),
            is_readonly=("READONLY" in mods),
        ))
    return params


def _render_params(params: List[Param], target: str, kind: str,
                   issues: List[ConversionIssue]) -> str:
    rendered = []
    for p in params:
        type_text, type_issues = tsql_dialect.convert_type_name(p.type_text, target)
        issues.extend(type_issues)
        if p.is_readonly:
            issues.append(ConversionIssue(
                "error",
                f"Parameter @{p.name} is a READONLY table-valued parameter; neither target has an "
                "equivalent, so this routine must be rewritten by hand.",
            ))
        if target == MYSQL:
            direction = "INOUT" if p.is_output else "IN"
            if kind == "FUNCTION":
                # MySQL functions take no direction keyword and no defaults.
                rendered.append(f"{_var_name(p.name)} {type_text}")
                if p.default:
                    issues.append(ConversionIssue(
                        "warning",
                        f"Parameter @{p.name}'s default ({p.default}) was dropped -- MySQL function "
                        "parameters cannot have defaults; pass the value explicitly.",
                    ))
                continue
            rendered.append(f"{direction} {_var_name(p.name)} {type_text}")
            if p.default:
                issues.append(ConversionIssue(
                    "warning",
                    f"Parameter @{p.name}'s default ({p.default}) was dropped -- MySQL stored "
                    "procedures do not support parameter defaults; pass the value explicitly.",
                ))
        else:
            default_clause = ""
            if p.default:
                translated, expr_issues = translate_expression(
                    p.default, "PostgreSQL", rewrite_top=False)
                issues.extend(expr_issues)
                default_clause = f" DEFAULT {translated}"
            direction = "INOUT " if p.is_output else ""
            rendered.append(f"{direction}{_var_name(p.name)} {type_text}{default_clause}")
    return ", ".join(rendered)


def _var_name(name: str) -> str:
    """`@Total` -> `v_Total`. Neither target allows `@` in an identifier
    (MySQL reads `@x` as a *session* variable, which would silently share
    state across calls instead of being local -- a subtle, real bug, not a
    syntax error), so every T-SQL local and parameter is renamed with a
    `v_` prefix. The prefix, rather than a bare strip, avoids colliding
    with a column of the same name in a WHERE clause."""
    return "v_" + name.lstrip("@")


_VAR_REF_RE = re.compile(r"@(?!@)([\w#$]+)")


def _rewrite_variable_refs(text: str) -> str:
    return apply_outside_strings(text, lambda chunk: _VAR_REF_RE.sub(r"v_\1", chunk))


# ------------------------------------------------------ header splitting

_CREATE_RE = re.compile(
    r"^\s*(?:/\*.*?\*/\s*|--[^\n]*\n\s*)*"
    r"CREATE\s+(?:OR\s+ALTER\s+)?(?P<kind>PROCEDURE|PROC|FUNCTION|TRIGGER|VIEW)\s+"
    r"(?P<name>" + _QUALIFIED + r")",
    re.IGNORECASE | re.DOTALL,
)


def _split_header(source: str) -> Optional[dict]:
    """Split a full `CREATE ...` routine definition into its parts.

    SQL Server's sys.sql_modules.definition (what sqlserver_introspector
    reads) hands back the *entire* original text including the CREATE, so
    unlike Oracle's ALL_SOURCE there is nothing to reconstruct -- only to
    take apart."""
    m = _CREATE_RE.match(source or "")
    if not m:
        return None
    kind = m.group("kind").upper()
    if kind == "PROC":
        kind = "PROCEDURE"
    rest = source[m.end():]
    as_idx = _find_keyword(rest, "AS")
    if as_idx == -1:
        return None
    return {
        "kind": kind,
        "name": _bare_name(m.group("name")),
        "between": rest[:as_idx],
        "body": rest[as_idx + 2:],
    }


def _strip_outer_begin_end(body: str) -> str:
    text = body.strip()
    if not re.match(r"^BEGIN\b", text, re.IGNORECASE):
        return text
    # Find the END that closes this BEGIN.
    scan = scan_outside_strings(text)
    depth = 0
    for m in re.finditer(r"\b(BEGIN|END)\b(?!\s+(?:IF|WHILE|LOOP|CASE|TRY|CATCH))", scan, re.IGNORECASE):
        word = m.group(1).upper()
        if word == "BEGIN":
            depth += 1
        else:
            depth -= 1
            if depth == 0:
                tail = text[m.end():].strip().strip(";").strip()
                if tail:
                    return text  # trailing content: not a single wrapping block
                return text[5:m.start()].strip()
    return text


# ------------------------------------------------------- body conversion

_DROP_STATEMENTS = (
    re.compile(r"^\s*SET\s+NOCOUNT\s+(ON|OFF)\s*;?\s*$", re.IGNORECASE),
    re.compile(r"^\s*SET\s+(ANSI_NULLS|QUOTED_IDENTIFIER|ANSI_WARNINGS|ARITHABORT|XACT_ABORT|"
               r"CONCAT_NULL_YIELDS_NULL|NUMERIC_ROUNDABORT)\s+(ON|OFF)\s*;?\s*$", re.IGNORECASE),
    re.compile(r"^\s*GO\s*$", re.IGNORECASE),
)

_MANUAL_MARKERS = {
    r"\bEXEC(?:UTE)?\s*\(": "Dynamic SQL (EXEC(...)) has no safe automatic conversion.",
    r"\bsp_executesql\b": "sp_executesql dynamic SQL has no safe automatic conversion.",
    r"\bMERGE\b": "MERGE must be rewritten (INSERT ... ON DUPLICATE KEY / ON CONFLICT).",
    r"#\w+": "A temporary table (#name) was referenced; rewrite using a real or temporary table "
             "the target understands.",
    r"@\w+\s+TABLE\s*\(": "A table variable (DECLARE @t TABLE) has no direct equivalent.",
    r"\bOPENQUERY\b": "OPENQUERY has no target equivalent.",
    r"\bOPENROWSET\b": "OPENROWSET has no target equivalent.",
    r"\bFOR\s+XML\b": "FOR XML must be rewritten by hand.",
    r"\bFOR\s+JSON\b": "FOR JSON must be rewritten with the target's JSON functions.",
    r"\bWAITFOR\b": "WAITFOR has no equivalent inside a stored routine.",
    r"\bGOTO\b": "GOTO has no equivalent in PL/pgSQL or MySQL stored programs.",
    r"\bALTER\s+INDEX\b": "Index maintenance DDL inside a routine has no direct equivalent.",
}


def _scan_manual_markers(text: str, issues: List[ConversionIssue]) -> bool:
    """Report every construct this converter deliberately refuses to guess
    at. Returns True if any were found, which is what keeps the routine's
    status at MANUAL."""
    scan = scan_outside_strings(text)
    found = False
    for pattern, message in _MANUAL_MARKERS.items():
        if re.search(pattern, scan, re.IGNORECASE):
            issues.append(ConversionIssue("error", message))
            found = True
    return found


def _split_statements(body: str) -> List[str]:
    """Split a T-SQL block into top-level statements. `BEGIN...END`,
    `BEGIN TRY...END TRY`, `CASE...END` and parenthesised subqueries all
    keep their internal semicolons."""
    scan = scan_outside_strings(body)
    statements: List[str] = []
    depth = 0          # parentheses
    block_depth = 0    # BEGIN/CASE nesting
    start = 0
    i = 0
    n = len(scan)
    while i < n:
        ch = scan[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch.isalpha() and (i == 0 or not (scan[i - 1].isalnum() or scan[i - 1] in "_@#")):
            m = re.match(r"[A-Za-z_][A-Za-z0-9_]*", scan[i:])
            if m:
                word = m.group(0).upper()
                if word in ("BEGIN", "CASE"):
                    block_depth += 1
                elif word == "END":
                    block_depth -= 1
                    # `END TRY` / `END CATCH` still close exactly one block,
                    # so no lookahead adjustment is needed here.
                i += len(m.group(0))
                continue
        if ch == ";" and depth == 0 and block_depth == 0:
            statements.append(body[start:i])
            start = i + 1
        i += 1
    tail = body[start:]
    if tail.strip():
        statements.append(tail)
    return [s for s in (st.strip() for st in statements) if s]


_STATEMENT_KEYWORDS = (
    "BEGIN", "SELECT", "INSERT", "UPDATE", "DELETE", "SET", "RETURN", "PRINT", "RAISERROR",
    "THROW", "EXEC", "EXECUTE", "IF", "WHILE", "BREAK", "CONTINUE", "DECLARE", "FETCH",
    "OPEN", "CLOSE", "DEALLOCATE", "TRUNCATE", "COMMIT", "ROLLBACK", "SAVE", "WITH",
)


def _take_statement(text: str) -> Tuple[str, str]:
    """Split off exactly one T-SQL statement from the front of `text`,
    returning `(statement, remainder)`.

    This is what makes `IF cond BEGIN ... END <more statements>` work.
    T-SQL puts no terminator after a BEGIN...END block, so a plain
    semicolon split cannot tell where an IF's branch stops and the next
    statement starts -- everything after the block used to be swallowed
    into the branch, which is exactly the kind of silent mis-conversion
    this converter must not produce."""
    body = text.lstrip()
    if not body:
        return "", ""
    scan = scan_outside_strings(body)
    first = re.match(r"[A-Za-z_]+", scan)
    word = first.group(0).upper() if first else ""

    if word == "BEGIN" and re.match(r"BEGIN\s+TRY\b", scan, re.IGNORECASE):
        m = re.search(r"\bEND\s+CATCH\b", scan, re.IGNORECASE)
        if m:
            end = m.end()
            rest = body[end:].lstrip()
            if rest.startswith(";"):
                rest = rest[1:]
            return body[:end], rest
        return body, ""

    if word == "BEGIN":
        depth = 0
        for m in re.finditer(r"\b(BEGIN|CASE|END)\b", scan, re.IGNORECASE):
            token = m.group(1).upper()
            if token in ("BEGIN", "CASE"):
                depth += 1
            else:
                depth -= 1
                if depth == 0:
                    end = m.end()
                    rest = body[end:].lstrip()
                    if rest.startswith(";"):
                        rest = rest[1:]
                    return body[:end], rest
        return body, ""

    if word in ("IF", "WHILE"):
        # Condition runs up to the first token that can start a statement.
        head_len = len(word)
        idx = _first_statement_keyword(scan, head_len)
        if idx == -1:
            return body, ""
        inner, rest = _take_statement(body[idx:])
        consumed = idx + (len(body[idx:]) - len(rest))
        statement = body[:consumed]
        remainder = rest
        if word == "IF":
            stripped = remainder.lstrip()
            if re.match(r"ELSE\b", scan_outside_strings(stripped), re.IGNORECASE):
                after_else = stripped[4:]
                else_stmt, remainder = _take_statement(after_else)
                statement = body[:consumed] + stripped[:4] + after_else[:len(after_else) - len(remainder)]
        return statement, remainder

    # Plain statement: ends at the first top-level ';', or at the next
    # statement keyword if the author omitted the terminator entirely.
    depth = 0
    block_depth = 0
    i = 0
    n = len(scan)
    while i < n:
        ch = scan[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch.isalpha() and (i == 0 or not (scan[i - 1].isalnum() or scan[i - 1] in "_@#.")):
            m = re.match(r"[A-Za-z_][A-Za-z0-9_]*", scan[i:])
            token = m.group(0).upper()
            if token in ("BEGIN", "CASE"):
                block_depth += 1
            elif token == "END":
                if block_depth == 0:
                    # `END` closing the block we are *inside* -- stop here.
                    return body[:i], body[i:]
                block_depth -= 1
            elif (token in ("ELSE",) and depth == 0 and block_depth == 0 and i > 0):
                return body[:i], body[i:]
            i += len(m.group(0))
            continue
        if ch == ";" and depth == 0 and block_depth == 0:
            return body[:i], body[i + 1:]
        i += 1
    return body, ""


def _first_statement_keyword(scan: str, start: int) -> int:
    depth = 0
    i = start
    while i < len(scan):
        ch = scan[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif depth == 0 and ch.isalpha() and (i == 0 or not (scan[i - 1].isalnum() or scan[i - 1] in "_@#.")):
            m = re.match(r"[A-Za-z_][A-Za-z0-9_]*", scan[i:])
            if m.group(0).upper() in _STATEMENT_KEYWORDS:
                return i
            i += len(m.group(0))
            continue
        i += 1
    return -1


_ALIAS_RE = re.compile(r"\s+AS\s+(?:\[[^\]]+\]|\"[^\"]+\"|[A-Za-z_]\w*)\s*$", re.IGNORECASE)


def _strip_select_alias(item: str) -> str:
    """`d.Salary AS OldSalary` -> `d.Salary`. A column alias is meaningful
    in a SELECT list and a syntax error inside a VALUES list, which is
    where the trigger rewrite moves these expressions to."""
    out = _ALIAS_RE.sub("", item.strip())
    return out.strip()


class _BodyConverter:
    """Converts one T-SQL statement block into the target's procedural
    dialect. Held as a class only so the declared-variable set, the issue
    list and the target flavour do not have to be threaded through a dozen
    free functions."""

    #: MySQL label wrapped around a procedure/trigger body, so a T-SQL
    #: `RETURN` (which means "stop here", not "return a value") has
    #: something to LEAVE. MySQL rejects a bare RETURN outside a stored
    #: *function*, so without this an early-exit guard -- the single most
    #: common shape in a validation procedure -- produced a routine the
    #: server refuses to create.
    EXIT_LABEL = "proc_exit"

    def __init__(self, target: str, issues: List[ConversionIssue], *,
                 trigger_context: Optional[dict] = None, routine_kind: str = "PROCEDURE"):
        self.target = target
        self.issues = issues
        self.declarations: List[str] = []
        self.trigger = trigger_context or {}
        self.routine_kind = routine_kind
        self.uses_exit_label = False

    # -- helpers ---------------------------------------------------------

    def expr(self, text: str) -> str:
        out, expr_issues = translate_expression(
            text, "PostgreSQL" if self.target == POSTGRES else "MySQL", rewrite_top=True)
        self.issues.extend(expr_issues)
        out = self._rewrite_trigger_pseudo_tables(out)
        return _rewrite_variable_refs(out).strip()

    def _rewrite_trigger_pseudo_tables(self, text: str) -> str:
        if not self.trigger:
            return text
        aliases = self.trigger.get("aliases", {})
        def repl(chunk: str) -> str:
            for alias, replacement in aliases.items():
                chunk = re.sub(r"\b" + re.escape(alias) + r"\s*\.", replacement + ".", chunk,
                               flags=re.IGNORECASE)
            chunk = re.sub(r"\binserted\s*\.", "NEW.", chunk, flags=re.IGNORECASE)
            chunk = re.sub(r"\bdeleted\s*\.", "OLD.", chunk, flags=re.IGNORECASE)
            return chunk
        return apply_outside_strings(text, repl)

    # -- statement dispatch ----------------------------------------------

    def convert_block(self, body: str, indent: int = 2) -> str:
        lines: List[str] = []
        for statement in _split_statements(body):
            converted = self.convert_statement(statement)
            if converted is None:
                continue
            lines.append(converted)
        pad = " " * indent
        return "\n".join(
            "\n".join(pad + ln if ln.strip() else ln for ln in block.splitlines())
            for block in lines
        )

    def convert_statement(self, statement: str) -> Optional[str]:
        text = statement.strip()
        if not text:
            return None
        for pattern in _DROP_STATEMENTS:
            if pattern.match(text):
                return None

        head = re.match(r"[A-Za-z_]+", text)
        keyword = head.group(0).upper() if head else ""

        handler = {
            "BEGIN": self._begin,
            "DECLARE": self._declare,
            "SET": self._set,
            "SELECT": self._select,
            "IF": self._if,
            "WHILE": self._while,
            "RETURN": self._return,
            "PRINT": self._print,
            "RAISERROR": self._raise,
            "THROW": self._raise,
            "OPEN": self._cursor_open,
            "CLOSE": self._cursor_close,
            "DEALLOCATE": self._cursor_deallocate,
            "FETCH": self._fetch,
            "INSERT": self._dml,
            "UPDATE": self._dml,
            "DELETE": self._dml,
            "TRUNCATE": self._dml,
            "EXEC": self._exec,
            "EXECUTE": self._exec,
            "BREAK": lambda _t: "LEAVE loop_label;" if self.target == MYSQL else "EXIT;",
            "CONTINUE": lambda _t: ("ITERATE loop_label;" if self.target == MYSQL else "CONTINUE;"),
        }.get(keyword)

        if handler is not None:
            return handler(text)

        if keyword in ("COMMIT", "ROLLBACK", "SAVE"):
            self.issues.append(ConversionIssue(
                "error",
                f"{keyword} TRANSACTION inside a routine was left as-is -- transaction control is not "
                "permitted in a MySQL trigger or a PL/pgSQL function and must be moved to the caller.",
            ))
            return f"-- MANUAL: {text};"
        if keyword == "WITH":
            return self._dml(text)

        self.issues.append(ConversionIssue(
            "error", f"Statement starting with '{keyword or text[:20]}' was not recognised and was "
                     "left unconverted.")
        )
        return f"-- MANUAL: {text};"

    # -- individual statement kinds ---------------------------------------

    def _begin(self, text: str) -> Optional[str]:
        if re.match(r"^BEGIN\s+TRY\b", text, re.IGNORECASE):
            return self._try_catch(text)
        if re.match(r"^BEGIN\s+(TRAN|TRANSACTION)\b", text, re.IGNORECASE):
            self.issues.append(ConversionIssue(
                "error",
                "BEGIN TRANSACTION inside a routine was left as-is -- transaction control is not "
                "permitted in a MySQL trigger or a PL/pgSQL function and must be moved to the caller.",
            ))
            return f"-- MANUAL: {text};"
        inner = _strip_outer_begin_end(text)
        if inner == text.strip():
            self.issues.append(ConversionIssue("error", "An unbalanced BEGIN block was found."))
            return f"-- MANUAL: {text};"
        # Emitted flat, not as a nested BEGIN...END: a T-SQL `BEGIN ... END`
        # is only ever a statement *grouping* (T-SQL has no block-scoped
        # DECLARE), and every DECLARE inside it has already been hoisted to
        # the routine's own declaration section -- so re-wrapping it would
        # add a redundant block and, on MySQL, an illegal one if a handler
        # declaration were hoisted out of it.
        return self.convert_block(inner, indent=0)

    def _declare(self, text: str) -> Optional[str]:
        if re.search(r"\bCURSOR\b", text, re.IGNORECASE):
            return self._declare_cursor(text)
        body = text[len("DECLARE"):].strip()
        for part in split_call_arguments(body):
            part = part.strip()
            if not part:
                continue
            m = re.match(r"^@(?P<name>[\w#$]+)\s+(?:AS\s+)?(?P<type>.+?)(?:\s*=\s*(?P<init>.+))?$",
                         part, re.IGNORECASE | re.DOTALL)
            if not m:
                self.issues.append(ConversionIssue(
                    "error", f"DECLARE clause '{part}' could not be parsed."))
                continue
            type_text, type_issues = tsql_dialect.convert_type_name(m.group("type"), self.target)
            self.issues.extend(type_issues)
            name = _var_name(m.group("name"))
            init = m.group("init")
            if self.target == MYSQL:
                default = f" DEFAULT {self.expr(init)}" if init else ""
                self.declarations.append(f"DECLARE {name} {type_text}{default};")
            else:
                default = f" := {self.expr(init)}" if init else ""
                self.declarations.append(f"{name} {type_text}{default};")
        return None  # declarations are hoisted, not emitted inline

    def _declare_cursor(self, text: str) -> Optional[str]:
        m = re.match(
            r"^DECLARE\s+(?P<name>[\w@#$]+)\s+(?:INSENSITIVE\s+|SCROLL\s+|STATIC\s+|FAST_FORWARD\s+|"
            r"LOCAL\s+|GLOBAL\s+|FORWARD_ONLY\s+|READ_ONLY\s+|KEYSET\s+|DYNAMIC\s+)*CURSOR\s+"
            r"(?:(?:LOCAL|GLOBAL|FORWARD_ONLY|STATIC|KEYSET|DYNAMIC|FAST_FORWARD|READ_ONLY|"
            r"SCROLL_LOCKS|OPTIMISTIC|TYPE_WARNING)\s+)*FOR\s+(?P<query>.+)$",
            text, re.IGNORECASE | re.DOTALL,
        )
        if not m:
            self.issues.append(ConversionIssue(
                "error", "A cursor declaration could not be parsed and was left unconverted."))
            return f"-- MANUAL: {text};"
        name = m.group("name").lstrip("@")
        query = self.expr(m.group("query").strip().rstrip(";"))
        if self.target == MYSQL:
            self.declarations.append(f"DECLARE cur_{name} CURSOR FOR {query};")
        else:
            self.declarations.append(f"cur_{name} CURSOR FOR {query};")
        return None

    def _cursor_open(self, text: str) -> Optional[str]:
        m = re.match(r"^OPEN\s+([\w@#$]+)", text, re.IGNORECASE)
        if not m:
            return f"-- MANUAL: {text};"
        return f"OPEN cur_{m.group(1).lstrip('@')};"

    def _cursor_close(self, text: str) -> Optional[str]:
        m = re.match(r"^CLOSE\s+([\w@#$]+)", text, re.IGNORECASE)
        if not m:
            return f"-- MANUAL: {text};"
        return f"CLOSE cur_{m.group(1).lstrip('@')};"

    def _cursor_deallocate(self, _text: str) -> Optional[str]:
        # Neither target has DEALLOCATE; CLOSE already releases the cursor.
        return None

    def _fetch(self, text: str) -> Optional[str]:
        m = re.match(
            r"^FETCH\s+(?:NEXT\s+|PRIOR\s+|FIRST\s+|LAST\s+)?FROM\s+(?P<cur>[\w@#$]+)\s+"
            r"INTO\s+(?P<targets>.+)$",
            text, re.IGNORECASE | re.DOTALL,
        )
        if not m:
            self.issues.append(ConversionIssue(
                "error", "A FETCH statement shape this converter does not handle was left as-is."))
            return f"-- MANUAL: {text};"
        targets = ", ".join(_var_name(t.strip()) for t in m.group("targets").split(","))
        return f"FETCH cur_{m.group('cur').lstrip('@')} INTO {targets};"

    def _set(self, text: str) -> Optional[str]:
        m = re.match(r"^SET\s+@(?P<name>[\w#$]+)\s*=\s*(?P<value>.+)$", text,
                     re.IGNORECASE | re.DOTALL)
        if not m:
            self.issues.append(ConversionIssue(
                "error", f"SET statement '{text[:60]}' is a session-option or shape this converter "
                         "does not handle; it was left as-is."))
            return f"-- MANUAL: {text};"
        name = _var_name(m.group("name"))
        value = self.expr(m.group("value"))
        if self.target == MYSQL:
            return f"SET {name} = {value};"
        return f"{name} := {value};"

    _SELECT_ASSIGN_RE = re.compile(r"^SELECT\s+(?P<assigns>@[\w#$]+\s*=.+?)(?P<rest>\bFROM\b.*)?$",
                                   re.IGNORECASE | re.DOTALL)

    def _select(self, text: str) -> Optional[str]:
        scan = scan_outside_strings(text)
        # `SELECT @x = ...` is an assignment, not a result set.
        if re.match(r"^SELECT\s+@[\w#$]+\s*=", scan, re.IGNORECASE):
            return self._select_assign(text)
        if re.search(r"\bINTO\s+(?:#|@)", scan, re.IGNORECASE):
            self.issues.append(ConversionIssue(
                "error", "SELECT ... INTO a temp table / table variable has no direct equivalent."))
            return f"-- MANUAL: {text};"
        # A plain result-returning SELECT.
        query = self.expr(text)
        if self.target == MYSQL:
            return query.rstrip(";") + ";"
        self.issues.append(ConversionIssue(
            "info",
            "A result-returning SELECT was found; on PostgreSQL it is returned through an open "
            "refcursor rather than as a naked result set (see the routine header).",
        ))
        return f"OPEN result_cursor FOR {query.rstrip(';')};"

    def _select_assign(self, text: str) -> Optional[str]:
        body = text[len("SELECT"):]
        from_idx = _find_keyword(body, "FROM")
        assigns_text = body if from_idx == -1 else body[:from_idx]
        rest = "" if from_idx == -1 else body[from_idx:]
        names: List[str] = []
        values: List[str] = []
        for part in split_call_arguments(assigns_text):
            m = re.match(r"^\s*@(?P<name>[\w#$]+)\s*=\s*(?P<value>.+)$", part,
                         re.IGNORECASE | re.DOTALL)
            if not m:
                self.issues.append(ConversionIssue(
                    "error", f"SELECT-assignment clause '{part.strip()}' could not be parsed."))
                return f"-- MANUAL: {text};"
            names.append(_var_name(m.group("name")))
            values.append(m.group("value").strip())
        if not rest.strip():
            # Pure assignment, no query.
            if len(names) == 1:
                value = self.expr(values[0])
                return (f"SET {names[0]} = {value};" if self.target == MYSQL
                        else f"{names[0]} := {value};")
            if self.target == MYSQL:
                return "\n".join(f"SET {n} = {self.expr(v)};" for n, v in zip(names, values))
            return "\n".join(f"{n} := {self.expr(v)};" for n, v in zip(names, values))
        select_list = ", ".join(values)
        query = self.expr(f"SELECT {select_list} {rest}")
        # Both targets spell this the same way: SELECT <cols> INTO <vars> FROM ...
        m = re.match(r"^SELECT\s+(?P<cols>.*?)\s+(?P<tail>FROM\b.*)$", query,
                     re.IGNORECASE | re.DOTALL)
        cols = m.group("cols") if m else select_list
        tail = m.group("tail") if m else rest
        return f"SELECT {cols} INTO {', '.join(names)} {tail.rstrip(';')};"

    def _if(self, text: str) -> Optional[str]:
        parsed = self._split_if(text[2:])
        if parsed is None:
            self.issues.append(ConversionIssue(
                "error", "An IF statement could not be split into condition/branches."))
            return f"-- MANUAL: {text};"
        cond_text, then_part, else_part, trailing = parsed
        condition = self.expr(cond_text)
        out = [f"IF {condition} THEN", self._branch(then_part)]
        if else_part is not None:
            stripped = else_part.strip()
            elif_kw = "ELSEIF" if self.target == MYSQL else "ELSIF"
            if re.match(r"^IF\b", stripped, re.IGNORECASE):
                nested = self.convert_statement(stripped)
                # Flatten `ELSE IF` into ELSEIF/ELSIF so the chain closes once.
                if nested and nested.startswith("IF "):
                    nested = elif_kw + nested[2:]
                    if nested.rstrip().endswith("END IF;"):
                        nested = nested.rstrip()[: -len("END IF;")].rstrip()
                    out.append(nested)
                else:
                    out.append("ELSE")
                    out.append(self._branch(else_part))
            else:
                out.append("ELSE")
                out.append(self._branch(else_part))
        out.append("END IF;")
        block = "\n".join(part for part in out if part.strip() or part == "ELSE")
        tail = self.convert_block(trailing, indent=0) if trailing.strip() else ""
        return block + ("\n" + tail if tail.strip() else "")

    def _branch(self, text: str) -> str:
        """Render one IF/WHILE branch, unwrapping a `BEGIN ... END` grouping
        so the target gets a flat statement list rather than a redundant
        nested block."""
        return self.convert_block(_strip_outer_begin_end(text))

    def _split_if(self, rest: str) -> Optional[Tuple[str, str, Optional[str], str]]:
        """Split `<condition> <then-statement> [ELSE <else-statement>]
        <trailing statements>`.

        T-SQL has no THEN keyword, so the condition ends where its first
        statement begins, and the branches end where `_take_statement`
        says they do -- everything after that belongs to the enclosing
        block, not to the IF."""
        scan = scan_outside_strings(rest)
        cond_end = _first_statement_keyword(scan, 0)
        if cond_end <= 0:
            return None
        condition = rest[:cond_end].strip()
        then_part, remainder = _take_statement(rest[cond_end:])
        else_part = None
        stripped = remainder.lstrip()
        if re.match(r"ELSE\b", scan_outside_strings(stripped), re.IGNORECASE):
            else_part, remainder = _take_statement(stripped[4:])
        return condition, then_part, else_part, remainder

    def _while(self, text: str) -> Optional[str]:
        parsed = self._split_if(text[len("WHILE"):])
        if parsed is None:
            self.issues.append(ConversionIssue(
                "error", "A WHILE statement could not be split into condition/body."))
            return f"-- MANUAL: {text};"
        cond, body, _else, trailing = parsed
        # `WHILE @@FETCH_STATUS = 0` is the T-SQL cursor-loop idiom; both
        # targets signal exhaustion through a NOT FOUND handler instead.
        if re.search(r"@@FETCH_STATUS", cond, re.IGNORECASE):
            loop = self._cursor_loop(body)
        else:
            condition = self.expr(cond)
            inner = self._branch(body)
            loop = (f"WHILE {condition} DO\n{inner}\nEND WHILE;" if self.target == MYSQL
                    else f"WHILE {condition} LOOP\n{inner}\nEND LOOP;")
        tail = self.convert_block(trailing, indent=0) if trailing.strip() else ""
        return loop + ("\n" + tail if tail.strip() else "")

    def _cursor_loop(self, body: str) -> str:
        inner_source = _strip_outer_begin_end(body)
        # The FETCH that re-arms the loop lives at the *end* of a T-SQL
        # cursor body; both targets want it at the top of the loop, so it
        # is hoisted rather than duplicated.
        statements = _split_statements(inner_source)
        fetch_stmts = [s for s in statements if re.match(r"^\s*FETCH\b", s, re.IGNORECASE)]
        rest_stmts = [s for s in statements if s not in fetch_stmts]
        fetch_line = self.convert_statement(fetch_stmts[0]) if fetch_stmts else None
        inner = "\n".join(filter(None, (self.convert_statement(s) for s in rest_stmts)))
        inner = "\n".join("  " + ln if ln.strip() else ln for ln in inner.splitlines())
        if self.target == MYSQL:
            self.declarations.append("DECLARE done_flag INT DEFAULT 0;")
            self.declarations.append(
                "DECLARE CONTINUE HANDLER FOR NOT FOUND SET done_flag = 1;")
            return (
                "read_loop: LOOP\n"
                f"  {fetch_line or ''}\n"
                "  IF done_flag = 1 THEN\n    LEAVE read_loop;\n  END IF;\n"
                f"{inner}\n"
                "END LOOP read_loop;"
            )
        return (
            "LOOP\n"
            f"  {fetch_line or ''}\n"
            "  EXIT WHEN NOT FOUND;\n"
            f"{inner}\n"
            "END LOOP;"
        )

    def _return(self, text: str) -> Optional[str]:
        value = text[len("RETURN"):].strip().rstrip(";")
        if self.routine_kind == "FUNCTION":
            if not value:
                self.issues.append(ConversionIssue(
                    "error", "A bare RETURN was found in a function; both targets require a value."))
                return "-- MANUAL: RETURN;"
            return f"RETURN {self.expr(value)};"

        # T-SQL allows `RETURN` (and `RETURN <int>` as a status code) inside
        # a procedure or trigger purely to stop executing.
        if value:
            self.issues.append(ConversionIssue(
                "info",
                f"RETURN {value} was a T-SQL procedure status code; neither target has an "
                "equivalent, so the value was dropped and only the early exit was kept.",
            ))
        if self.target == MYSQL:
            self.uses_exit_label = True
            return f"LEAVE {self.EXIT_LABEL};"
        if self.trigger:
            return "RETURN NEW;"
        return "RETURN;"

    def _print(self, text: str) -> Optional[str]:
        value = self.expr(text[len("PRINT"):].strip().rstrip(";"))
        if self.target == POSTGRES:
            return f"RAISE NOTICE '%', {value};"
        # MySQL stored programs have no PRINT/console equivalent.
        self.issues.append(ConversionIssue(
            "info", "PRINT has no MySQL equivalent and was commented out."))
        return f"-- PRINT {value};"

    def _raise(self, text: str) -> Optional[str]:
        message = "Error raised by converted routine"
        m = re.search(r"'((?:[^']|'')*)'", text)
        if m:
            message = m.group(1)
        if self.target == POSTGRES:
            return "RAISE EXCEPTION '%s';" % message.replace("%", "%%")
        return ("SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '%s';"
                % message.replace("'", "''"))

    def _dml(self, text: str) -> Optional[str]:
        if self.trigger:
            rewritten = self._trigger_insert_select(text)
            if rewritten is not None:
                return rewritten
        return self.expr(text).rstrip(";") + ";"

    def _trigger_insert_select(self, text: str) -> Optional[str]:
        """Rewrite the audit-table idiom

            INSERT INTO Audit (a, b) SELECT i.x, d.y FROM inserted i
              INNER JOIN deleted d ON i.id = d.id WHERE <pred>

        into the row-level form both targets use:

            IF <pred> THEN INSERT INTO Audit (a, b) VALUES (NEW.x, OLD.y);

        Only applied when the FROM clause mentions nothing but `inserted`
        and `deleted` -- as soon as a real table joins in, the statement is
        genuinely set-based and is left for a human."""
        m = re.match(
            r"^INSERT\s+INTO\s+(?P<table>" + _QUALIFIED + r")\s*"
            r"(?P<cols>\([^()]*\))?\s*"
            r"(?P<select>SELECT\b.+)$",
            text, re.IGNORECASE | re.DOTALL,
        )
        if not m:
            return None
        select_text = m.group("select")
        from_idx = _find_keyword(select_text, "FROM")
        if from_idx == -1:
            return None
        select_list = select_text[len("SELECT"):from_idx]
        from_clause = select_text[from_idx:]
        where_idx = _find_keyword(from_clause, "WHERE")
        joins_and_tables = from_clause[:where_idx] if where_idx != -1 else from_clause
        where_clause = from_clause[where_idx + len("WHERE"):] if where_idx != -1 else None

        # Every table referenced must be inserted/deleted.
        table_refs = re.findall(
            r"(?:\bFROM\b|\bJOIN\b)\s+(" + _QUALIFIED + r")(?:\s+(?:AS\s+)?([A-Za-z_]\w*))?",
            joins_and_tables, re.IGNORECASE)
        aliases = {}
        for ref, alias in table_refs:
            base = _bare_name(ref).lower()
            if base not in ("inserted", "deleted"):
                return None
            if alias and alias.upper() not in ("ON", "INNER", "LEFT", "RIGHT", "FULL", "WHERE"):
                aliases[alias] = "NEW" if base == "inserted" else "OLD"
        if not table_refs:
            return None

        self.trigger.setdefault("aliases", {}).update(aliases)
        target_table = _quote(_bare_name(m.group("table")), self.target)
        cols = m.group("cols") or ""
        if cols:
            col_names = ", ".join(
                _quote(_bare_name(c), self.target) for c in split_call_arguments(cols[1:-1]))
            cols = f" ({col_names})"
        values = ", ".join(
            self.expr(_strip_select_alias(v)) for v in split_call_arguments(select_list))
        insert = f"INSERT INTO {target_table}{cols} VALUES ({values});"
        if where_clause and where_clause.strip():
            condition = self.expr(where_clause.strip().rstrip(";"))
            return f"IF {condition} THEN\n  {insert}\nEND IF;"
        return insert

    def _exec(self, text: str) -> Optional[str]:
        m = re.match(r"^EXEC(?:UTE)?\s+(?P<name>" + _QUALIFIED + r")\s*(?P<args>.*)$",
                     text, re.IGNORECASE | re.DOTALL)
        if not m:
            self.issues.append(ConversionIssue(
                "error", "An EXEC statement shape this converter does not handle was left as-is."))
            return f"-- MANUAL: {text};"
        name = _quote(_bare_name(m.group("name")), self.target)
        args = m.group("args").strip().rstrip(";")
        args = self.expr(args) if args else ""
        return f"CALL {name}({args});"

    def _try_catch(self, text: str) -> Optional[str]:
        m = re.match(
            r"^BEGIN\s+TRY\b(?P<try>.*?)\bEND\s+TRY\b\s*BEGIN\s+CATCH\b(?P<catch>.*?)\bEND\s+CATCH\b",
            text, re.IGNORECASE | re.DOTALL,
        )
        if not m:
            self.issues.append(ConversionIssue(
                "error", "A TRY/CATCH block could not be parsed and was left unconverted."))
            return f"-- MANUAL: {text};"
        try_body = self.convert_block(m.group("try"))
        catch_body = self.convert_block(m.group("catch"))
        if self.target == POSTGRES:
            return (
                "BEGIN\n"
                f"{try_body}\n"
                "EXCEPTION\n"
                "  WHEN OTHERS THEN\n"
                f"{catch_body or '    NULL;'}\n"
                "END;"
            )
        # MySQL has no TRY/CATCH: a declared EXIT HANDLER inside its own
        # BEGIN...END block is the equivalent, and must be declared before
        # any statement in that block.
        handler_body = catch_body or "    BEGIN END;"
        return (
            "BEGIN\n"
            "  DECLARE EXIT HANDLER FOR SQLEXCEPTION\n"
            "  BEGIN\n"
            f"{handler_body}\n"
            "  END;\n"
            f"{try_body}\n"
            "END;"
        )


# ---------------------------------------------------------- entry points

def _render_declarations(declarations: List[str], target: str) -> str:
    if not declarations:
        return ""
    seen = []
    for d in declarations:
        if d not in seen:
            seen.append(d)
    if target == MYSQL:
        return "\n".join("  " + d for d in seen) + "\n"
    return "DECLARE\n" + "\n".join("  " + d for d in seen) + "\n"


def _status_from(issues: List[ConversionIssue]) -> ConversionStatus:
    if any(i.severity == "error" for i in issues):
        return ConversionStatus.MANUAL
    if any(i.severity == "warning" for i in issues):
        return ConversionStatus.AUTOMATIC_WITH_WARNINGS
    return ConversionStatus.AUTOMATIC


def _manual_placeholder(routine: Routine, target_engine: str, reason: str) -> str:
    return (
        f"-- MANUAL CONVERSION REQUIRED for {routine.kind} {routine.name}\n"
        f"-- {reason}\n"
        f"/*\n{routine.source}\n*/"
    )


def convert_procedure(routine: Routine, target: str, parts: dict) -> Tuple[str, List[ConversionIssue]]:
    issues: List[ConversionIssue] = []
    params = _parse_params(parts["between"], issues)
    body = _strip_outer_begin_end(parts["body"])
    _scan_manual_markers(body, issues)

    converter = _BodyConverter(target, issues, routine_kind="PROCEDURE")
    converted_body = converter.convert_block(body)
    declarations = converter.declarations
    name = _quote(parts["name"], target)

    if target == MYSQL:
        param_text = _render_params(params, target, "PROCEDURE", issues)
        label = f"{_BodyConverter.EXIT_LABEL}: " if converter.uses_exit_label else ""
        end_label = f" {_BodyConverter.EXIT_LABEL}" if converter.uses_exit_label else ""
        return (
            f"DROP PROCEDURE IF EXISTS {name};\n"
            f"CREATE PROCEDURE {name}({param_text})\n"
            f"{label}BEGIN\n"
            f"{_render_declarations(declarations, target)}"
            f"{converted_body}\n"
            f"END{end_label};"
        ), issues

    returns_rows = "OPEN result_cursor FOR" in converted_body
    param_text = _render_params(params, target, "PROCEDURE", issues)
    if returns_rows:
        issues.append(ConversionIssue(
            "warning",
            "PostgreSQL functions cannot return an ad-hoc result set the way a T-SQL procedure "
            f"does, so {parts['name']} was converted to a function returning a refcursor. Call it "
            "inside a transaction and FETCH ALL from the returned cursor.",
        ))
        decl = _render_declarations(declarations + ["result_cursor refcursor;"], target)
        # An early `RETURN;` is legal in a T-SQL procedure and in a
        # PL/pgSQL *procedure*, but not in the refcursor-returning function
        # this shape becomes -- PostgreSQL requires a value there. The
        # cursor variable is NULL until OPEN runs, so returning it is both
        # valid and the honest answer for "exited before producing rows".
        converted_body = re.sub(
            r"(?m)^(\s*)RETURN;\s*$", r"\1RETURN result_cursor;", converted_body)
        return (
            f"CREATE OR REPLACE FUNCTION {name}({param_text})\n"
            f"RETURNS refcursor AS $$\n"
            f"{decl}"
            f"BEGIN\n"
            f"{converted_body}\n"
            f"  RETURN result_cursor;\n"
            f"END;\n"
            f"$$ LANGUAGE plpgsql;"
        ), issues
    decl = _render_declarations(declarations, target)
    return (
        f"CREATE OR REPLACE PROCEDURE {name}({param_text})\n"
        f"LANGUAGE plpgsql AS $$\n"
        f"{decl}"
        f"BEGIN\n"
        f"{converted_body}\n"
        f"END;\n"
        f"$$;"
    ), issues


_RETURNS_RE = re.compile(r"\bRETURNS\s+(?P<returns>.+?)\s*$", re.IGNORECASE | re.DOTALL)


def convert_function(routine: Routine, target: str, parts: dict) -> Tuple[str, List[ConversionIssue]]:
    issues: List[ConversionIssue] = []
    between = parts["between"]
    m = _RETURNS_RE.search(between)
    if not m:
        issues.append(ConversionIssue(
            "error", "The function's RETURNS clause could not be located."))
        return _manual_placeholder(routine, target, "RETURNS clause could not be parsed."), issues

    returns_text = m.group("returns").strip()
    # Trailing WITH SCHEMABINDING / EXECUTE AS options are not part of the type.
    returns_text = re.split(r"\bWITH\b", returns_text, maxsplit=1, flags=re.IGNORECASE)[0].strip()
    param_text_raw = between[:m.start()]

    if re.match(r"^TABLE\b", returns_text, re.IGNORECASE) or "@" in returns_text.split()[0:1]:
        if target == MYSQL:
            issues.append(ConversionIssue(
                "error",
                "MySQL has no table-valued function; convert this to a VIEW (if it takes no "
                "parameters) or to a stored procedure returning a result set.",
            ))
            return _manual_placeholder(
                routine, target, "MySQL has no table-valued-function equivalent."), issues
        issues.append(ConversionIssue(
            "warning",
            "A table-valued function was converted to a PL/pgSQL function returning a refcursor; "
            "verify the column list and consider RETURNS TABLE(...) with explicit column types.",
        ))

    params = _parse_params(param_text_raw, issues)
    body = _strip_outer_begin_end(parts["body"])
    _scan_manual_markers(body, issues)

    converter = _BodyConverter(target, issues, routine_kind="FUNCTION")
    converted_body = converter.convert_block(body)
    return_type, type_issues = tsql_dialect.convert_type_name(returns_text, target)
    issues.extend(type_issues)
    name = _quote(parts["name"], target)

    if target == MYSQL:
        param_text = _render_params(params, target, "FUNCTION", issues)
        # MySQL refuses to create a stored function without a determinism
        # characteristic when binary logging is on (ER_BINLOG_UNSAFE_ROUTINE),
        # which is the default on any replicated server -- so one is always
        # emitted, chosen from whether the body touches tables.
        touches_tables = bool(re.search(r"\b(SELECT|INSERT|UPDATE|DELETE)\b",
                                        scan_outside_strings(body), re.IGNORECASE))
        writes = bool(re.search(r"\b(INSERT|UPDATE|DELETE)\b",
                                scan_outside_strings(body), re.IGNORECASE))
        characteristic = ("MODIFIES SQL DATA" if writes
                          else ("READS SQL DATA" if touches_tables else "DETERMINISTIC"))
        return (
            f"DROP FUNCTION IF EXISTS {name};\n"
            f"CREATE FUNCTION {name}({param_text})\n"
            f"RETURNS {return_type}\n"
            f"{characteristic}\n"
            f"BEGIN\n"
            f"{_render_declarations(converter.declarations, target)}"
            f"{converted_body}\n"
            f"END;"
        ), issues

    param_text = _render_params(params, target, "FUNCTION", issues)
    decl = _render_declarations(converter.declarations, target)
    return (
        f"CREATE OR REPLACE FUNCTION {name}({param_text})\n"
        f"RETURNS {return_type} AS $$\n"
        f"{decl}"
        f"BEGIN\n"
        f"{converted_body}\n"
        f"END;\n"
        f"$$ LANGUAGE plpgsql;"
    ), issues


_TRIGGER_ON_RE = re.compile(
    r"\bON\s+(?P<table>" + _QUALIFIED + r")\s+"
    r"(?P<timing>AFTER|FOR|INSTEAD\s+OF)\s+(?P<events>[\w\s,]+?)\s*$",
    re.IGNORECASE | re.DOTALL,
)


def convert_trigger(routine: Routine, target: str, parts: dict) -> Tuple[str, List[ConversionIssue]]:
    issues: List[ConversionIssue] = []
    m = _TRIGGER_ON_RE.search(parts["between"])
    if m:
        table_name = _bare_name(m.group("table"))
        timing = re.sub(r"\s+", " ", m.group("timing").strip().upper())
        events = [e.strip().upper() for e in m.group("events").split(",") if e.strip()]
    else:
        table_name = routine.table_name or ""
        timing = (routine.timing or "AFTER").upper()
        events = [e.upper() for e in (routine.events or ["UPDATE"])]
    if timing == "FOR":
        timing = "AFTER"  # T-SQL's FOR is a synonym for AFTER

    if timing == "INSTEAD OF":
        issues.append(ConversionIssue(
            "error",
            "INSTEAD OF triggers exist on a SQL Server view/table but have no MySQL equivalent and "
            "need a rule or BEFORE trigger on PostgreSQL; this must be rewritten by hand.",
        ))
        return _manual_placeholder(
            routine, target, "INSTEAD OF triggers have no automatic equivalent."), issues
    if not table_name:
        issues.append(ConversionIssue("error", "The trigger's target table could not be determined."))
        return _manual_placeholder(routine, target, "Target table could not be determined."), issues

    body = _strip_outer_begin_end(parts["body"])
    _scan_manual_markers(body, issues)

    # `IF UPDATE(col)` -- a T-SQL statement-level "was this column in the
    # SET list" test. Row-level triggers express the same intent as a
    # value comparison, which is what both targets have.
    def update_repl(match: re.Match) -> str:
        col = _bare_name(match.group(1))
        quoted = _quote(col, target)
        if target == POSTGRES:
            return f"NEW.{quoted} IS DISTINCT FROM OLD.{quoted}"
        return f"NOT (NEW.{quoted} <=> OLD.{quoted})"
    body = re.sub(r"\bUPDATE\s*\(\s*(" + _IDENT + r")\s*\)", update_repl, body, flags=re.IGNORECASE)
    if re.search(r"\bCOLUMNS_UPDATED\s*\(", body, re.IGNORECASE):
        issues.append(ConversionIssue(
            "error", "COLUMNS_UPDATED() has no target equivalent; test the individual columns."))

    converter = _BodyConverter(target, issues, trigger_context={"aliases": {}},
                               routine_kind="TRIGGER")
    converted_body = converter.convert_block(body)

    # Any surviving bare reference to the pseudo-tables means the body was
    # doing something genuinely set-based that the row-level rewrite could
    # not absorb -- reported rather than shipped.
    if re.search(r"\b(inserted|deleted)\b",
                 scan_outside_strings(converted_body), re.IGNORECASE):
        issues.append(ConversionIssue(
            "error",
            "The trigger body still references the `inserted`/`deleted` pseudo-tables. Both targets "
            "fire triggers per row with NEW/OLD instead, and this statement's set-based shape could "
            "not be rewritten automatically.",
        ))

    if len(events) > 1:
        issues.append(ConversionIssue(
            "warning" if target == POSTGRES else "info",
            f"The source trigger fires on {', '.join(events)}. "
            + ("PostgreSQL supports a combined event list, which is what was emitted."
               if target == POSTGRES else
               "MySQL allows only one event per trigger, so one trigger per event was emitted."),
        ))

    name = parts["name"]
    table_quoted = _quote(table_name, target)
    declarations = _render_declarations(converter.declarations, target)

    if target == MYSQL:
        blocks = []
        for event in events:
            trigger_name = name if len(events) == 1 else f"{name}_{event.lower()}"
            label = f"{_BodyConverter.EXIT_LABEL}: " if converter.uses_exit_label else ""
            end_label = f" {_BodyConverter.EXIT_LABEL}" if converter.uses_exit_label else ""
            blocks.append(
                f"DROP TRIGGER IF EXISTS {_quote(trigger_name, target)};\n"
                f"CREATE TRIGGER {_quote(trigger_name, target)}\n"
                f"{timing} {event} ON {table_quoted}\n"
                f"FOR EACH ROW\n"
                f"{label}BEGIN\n"
                f"{declarations}"
                f"{converted_body}\n"
                f"END{end_label};"
            )
        return "\n\n".join(blocks), issues

    fn_name = _quote(f"{name}_fn", target)
    return (
        f"CREATE OR REPLACE FUNCTION {fn_name}() RETURNS trigger AS $$\n"
        f"{declarations}"
        f"BEGIN\n"
        f"{converted_body}\n"
        f"  RETURN NEW;\n"
        f"END;\n"
        f"$$ LANGUAGE plpgsql;\n"
        f"DROP TRIGGER IF EXISTS {_quote(name, target)} ON {table_quoted};\n"
        f"CREATE TRIGGER {_quote(name, target)}\n"
        f"{timing} {' OR '.join(events)} ON {table_quoted}\n"
        f"FOR EACH ROW EXECUTE FUNCTION {fn_name}();"
    ), issues


def convert_routine(routine: Routine, target_engine: str) -> Routine:
    """Populate `routine.converted_source` / `.status` / `.issues` in place
    for a SQL Server-sourced routine, and return it.

    Only MySQL and PostgreSQL targets are handled here -- a SQL Server
    target needs no conversion at all, and Oracle/Db2/MongoDB targets keep
    their existing manual-conversion behaviour (this tool has no T-SQL to
    PL/SQL or SQL PL translator)."""
    target = normalize_target(target_engine)
    issues: List[ConversionIssue] = []

    if target not in (MYSQL, POSTGRES):
        routine.converted_source = _manual_placeholder(
            routine, target_engine,
            f"Automatic T-SQL conversion is implemented for MySQL and PostgreSQL targets; "
            f"{target_engine} is not one of them.")
        routine.issues = [ConversionIssue(
            "error",
            f"{routine.kind} {routine.name}: automatic conversion from SQL Server to "
            f"{target_engine} is not supported; rewrite by hand.")]
        routine.status = ConversionStatus.MANUAL
        routine.complexity_score = 5
        return routine

    source = (routine.source or "").strip()
    if not source:
        routine.converted_source = _manual_placeholder(
            routine, target_engine,
            "The source database returned no definition text for this routine (it may be "
            "encrypted with WITH ENCRYPTION, or the login may lack VIEW DEFINITION).")
        routine.issues = [ConversionIssue(
            "error",
            f"{routine.kind} {routine.name}: no source text was available to convert.")]
        routine.status = ConversionStatus.MANUAL
        routine.complexity_score = 5
        return routine

    parts = _split_header(source)
    if parts is None:
        routine.converted_source = _manual_placeholder(
            routine, target_engine,
            "The CREATE ... AS header could not be parsed, so nothing was converted.")
        routine.issues = [ConversionIssue(
            "error",
            f"{routine.kind} {routine.name}: the routine header could not be parsed.")]
        routine.status = ConversionStatus.MANUAL
        routine.complexity_score = 5
        return routine

    kind = parts["kind"]
    try:
        if kind == "TRIGGER":
            ddl, issues = convert_trigger(routine, target, parts)
        elif kind == "FUNCTION":
            ddl, issues = convert_function(routine, target, parts)
        else:
            ddl, issues = convert_procedure(routine, target, parts)
    except Exception as exc:  # noqa: BLE001
        # A converter bug must degrade to "needs a human", never to a
        # crashed conversion run that loses every other object's result.
        ddl = _manual_placeholder(
            routine, target_engine,
            f"Automatic conversion failed internally ({type(exc).__name__}: {exc}).")
        issues = [ConversionIssue(
            "error",
            f"{routine.kind} {routine.name}: automatic conversion failed internally "
            f"({type(exc).__name__}: {exc}); the original source was preserved for manual "
            "conversion.")]

    routine.converted_source = ddl
    routine.issues = issues
    routine.status = _status_from(issues)
    routine.complexity_score = sum(
        3 if i.severity == "error" else (1 if i.severity == "warning" else 0) for i in issues)
    return routine


def convert_all_routines(routines: List[Routine], target_engine: str) -> List[Routine]:
    return [convert_routine(r, target_engine) for r in routines]
