"""Best-effort automatic rewriting of *simple* Oracle CONNECT BY
hierarchical queries into an equivalent recursive common table expression
(CTE) -- shared by ddl_generator.generate_view_ddl (view definitions) and
each of plsql_converter.py / tsql_converter.py / db2_converter.py's
convert_routine (cursor declarations, row-FOR-loop headers, SELECT...INTO,
anywhere else a hierarchical SELECT can appear in a routine body).

Only the common "single table, single PRIOR equality" shape is rewritten:

    SELECT <col1, col2, ..., [LEVEL]>
    FROM <table> [[AS] alias]
    START WITH <start_condition>
    CONNECT BY [NOCYCLE] PRIOR <col_a> = <col_b>   -- or <col_b> = PRIOR <col_a>
    [ORDER BY <...>]

becomes

    WITH [RECURSIVE] <cte> (<col1>, <col2>, ..., [LEVEL]) AS (
      SELECT <col1, col2, ..., [1]>
      FROM <table> [alias]
      WHERE <start_condition>
      UNION ALL
      SELECT <col1, col2, ..., [<cte>.LEVEL + 1]>
      FROM <table> <recursive_alias>
      JOIN <cte> ON <recursive_alias>.<child_col> = <cte>.<parent_col>
    )
    SELECT <col1, col2, ...>
    FROM <cte>
    [ORDER BY <...>]

Anything outside this shape -- an extra WHERE filter, multiple/joined
source tables, ORDER SIBLINGS BY, CONNECT_BY_ROOT, SYS_CONNECT_BY_PATH,
CONNECT_BY_ISLEAF/ISCYCLE, a SELECT * or an unaliased expression column,
or a CONNECT BY condition with more than one equality -- is deliberately
left completely untouched. Each converter's own CONNECT BY manual-
conversion marker (in `_MANUAL_MARKERS` / sql_translator's complexity
scorer) still scans the text afterwards, so anything this module didn't
rewrite continues to be flagged for manual conversion exactly as before;
nothing here ever raises or silently drops a query it doesn't fully
understand.

`WITH RECURSIVE` is required by PostgreSQL and MySQL (8.0+); SQL Server
and Db2 (LUW) both use a plain `WITH` and infer the recursion themselves
-- callers pass the right keyword in via `cte_keyword`.
"""
from __future__ import annotations

import re
from typing import List, Optional, Tuple

from tgdatabridge.core.schema_model import ConversionIssue, Routine

_IDENT = r"[A-Za-z_][\w$#]*"
_QUALIFIED_IDENT = rf"{_IDENT}(?:\.{_IDENT})?"

_STRING_LITERAL_RE = re.compile(r"'(?:[^']|'')*'")
_CONNECT_BY_RE = re.compile(r"\bCONNECT\s+BY\b", re.IGNORECASE)
_SELECT_RE = re.compile(r"\bSELECT\b", re.IGNORECASE)

_QUERY_RE = re.compile(
    r"^\s*SELECT\s+"
    r"(?P<select_list>.+?)\s+"
    rf"FROM\s+(?P<table>{_QUALIFIED_IDENT}(?:\s+(?:AS\s+)?{_IDENT})?)\s+"
    r"START\s+WITH\s+(?P<start_with>.+?)\s+"
    r"CONNECT\s+BY\s+(?:(?P<nocycle>NOCYCLE)\s+)?(?P<connect_by>.+?)"
    r"(?:\s+ORDER\s+(?!SIBLINGS\b)BY\s+(?P<order_by>.+?))?"
    r"\s*$",
    re.IGNORECASE | re.DOTALL,
)

_TABLE_RE = re.compile(
    rf"^(?P<name>{_QUALIFIED_IDENT})(?:\s+(?:AS\s+)?(?P<alias>{_IDENT}))?$",
    re.IGNORECASE,
)

_CONNECT_BY_COND_PRIOR_FIRST = re.compile(
    rf"^\s*PRIOR\s+(?P<parent>{_QUALIFIED_IDENT})\s*=\s*(?P<child>{_QUALIFIED_IDENT})\s*$",
    re.IGNORECASE,
)
_CONNECT_BY_COND_PRIOR_SECOND = re.compile(
    rf"^\s*(?P<child>{_QUALIFIED_IDENT})\s*=\s*PRIOR\s+(?P<parent>{_QUALIFIED_IDENT})\s*$",
    re.IGNORECASE,
)

_SELECT_ITEM_RE = re.compile(
    rf"^(?P<expr>{_QUALIFIED_IDENT})(?:\s+(?:AS\s+)?(?P<alias>{_IDENT}))?$",
    re.IGNORECASE,
)


def _split_top_level(text: str, sep: str = ",") -> List[str]:
    """Local copy of plsql_converter.split_top_level (kept independent here
    to avoid a circular import: plsql_converter.py itself calls into this
    module). Splits on `sep` at paren-depth 0, respecting '...' string
    literals."""
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


def _blank_strings(text: str) -> str:
    """Same-length copy of `text` with every '...' string literal's
    contents replaced by 'x' (newlines preserved), so keyword scans never
    trigger on text that happens to appear inside a string literal, while
    every match position still lines up with the original text."""
    blanked = list(text)
    for m in _STRING_LITERAL_RE.finditer(text):
        for i in range(m.start(), m.end()):
            if blanked[i] != "\n":
                blanked[i] = "x"
    return "".join(blanked)


def _find_statement_end(text: str, start: int) -> int:
    """The CONNECT BY-governed SELECT beginning at `start` ends at the
    first `;` encountered at paren-depth 0 relative to `start` (an
    ordinary statement terminator -- e.g. a CURSOR ... IS ... ; or a plain
    view definition), or at a `)` that would take that relative depth
    negative (the query is itself inside a `(...)` it didn't open -- e.g.
    `FOR rec IN (SELECT ...) LOOP`), whichever comes first. Falls back to
    the end of the text if neither is found."""
    depth = 0
    in_string = False
    i = start
    while i < len(text):
        ch = text[i]
        if ch == "'":
            in_string = not in_string
        elif not in_string:
            if ch == "(":
                depth += 1
            elif ch == ")":
                if depth == 0:
                    return i
                depth -= 1
            elif ch == ";" and depth == 0:
                return i
        i += 1
    return len(text)


def _parse_select_items(select_list_text: str) -> Optional[List[dict]]:
    """Parse a CONNECT BY query's column list into simple column
    references this module knows how to safely re-emit on both sides of a
    recursive CTE's UNION ALL. Returns None (unsupported -- caller must
    leave the query untouched) for SELECT *, any expression/function-call
    column, or a duplicate output column name."""
    items = _split_top_level(select_list_text, ",")
    if not items:
        return None
    parsed: List[dict] = []
    seen: set = set()
    for raw in items:
        raw = raw.strip()
        if not raw or raw == "*" or raw.endswith(".*") or "(" in raw:
            return None
        m = _SELECT_ITEM_RE.match(raw)
        if not m:
            return None
        expr = m.group("expr")
        alias = m.group("alias")
        bare = expr.split(".")[-1]
        is_level = bare.upper() == "LEVEL" and "." not in expr
        cte_col = alias or bare
        if cte_col.upper() in seen:
            return None
        seen.add(cte_col.upper())
        parsed.append({"expr": expr, "alias": alias, "cte_col": cte_col, "is_level": is_level})
    return parsed


def _try_rewrite_one(stmt_text: str, cte_keyword: str, cte_index: int) -> Tuple[str, List[ConversionIssue]]:
    """Attempt to rewrite one already-isolated `SELECT ... CONNECT BY ...`
    statement. Returns (new_text, issues) -- new_text is unchanged from
    stmt_text (and issues is empty) if the query doesn't match the
    supported simple shape."""
    m = _QUERY_RE.match(stmt_text)
    if not m:
        return stmt_text, []

    items = _parse_select_items(m.group("select_list"))
    if items is None:
        return stmt_text, []

    table_m = _TABLE_RE.match(m.group("table").strip())
    if not table_m:
        return stmt_text, []
    table_name, table_alias = table_m.group("name"), table_m.group("alias")

    connect_by_cond = m.group("connect_by").strip()
    cond_m = _CONNECT_BY_COND_PRIOR_FIRST.match(connect_by_cond) or _CONNECT_BY_COND_PRIOR_SECOND.match(connect_by_cond)
    if not cond_m:
        return stmt_text, []
    parent_col = cond_m.group("parent").split(".")[-1]
    child_col = cond_m.group("child").split(".")[-1]

    start_with = m.group("start_with").strip()
    order_by = m.group("order_by")
    nocycle = bool(m.group("nocycle"))

    cte_name = f"cb_cte_{cte_index}"
    recursive_alias = table_alias or "_cb_t"

    cte_cols = [i["cte_col"] for i in items]

    anchor_items = ["1" if i["is_level"] else i["expr"] for i in items]
    recursive_items = [
        f"{cte_name}.{i['cte_col']} + 1" if i["is_level"] else f"{recursive_alias}.{i['expr'].split('.')[-1]}"
        for i in items
    ]

    anchor_from = f"{table_name} {table_alias}" if table_alias else table_name
    recursive_from = f"{table_name} {recursive_alias}"

    cte_sql = (
        f"{cte_keyword} {cte_name} ({', '.join(cte_cols)}) AS (\n"
        f"  SELECT {', '.join(anchor_items)}\n"
        f"  FROM {anchor_from}\n"
        f"  WHERE {start_with}\n"
        f"  UNION ALL\n"
        f"  SELECT {', '.join(recursive_items)}\n"
        f"  FROM {recursive_from}\n"
        f"  JOIN {cte_name} ON {recursive_alias}.{child_col} = {cte_name}.{parent_col}\n"
        f")\n"
        f"SELECT {', '.join(cte_cols)}\n"
        f"FROM {cte_name}"
    )
    if order_by:
        cte_sql += f"\nORDER BY {order_by.strip()}"

    issues = [ConversionIssue(
        "info",
        f"Hierarchical query (CONNECT BY) on {table_name} was automatically rewritten as a recursive CTE "
        f"({cte_name}) -- review the generated join/anchor conditions.",
    )]
    if nocycle:
        issues.append(ConversionIssue(
            "info",
            f"CONNECT BY NOCYCLE on {table_name} was rewritten without an equivalent cycle guard; add one "
            "(e.g. a visited-path column, or the target engine's own CTE CYCLE clause) if the source data "
            "can actually contain a cycle.",
        ))
    return cte_sql, issues


def find_and_rewrite(text: str, cte_keyword: str = "WITH RECURSIVE") -> Tuple[str, List[ConversionIssue]]:
    """Scan `text` for every Oracle CONNECT BY hierarchical query and
    replace each one that matches the supported simple shape (see module
    docstring) with an equivalent recursive CTE. Occurrences that don't
    match are left completely untouched."""
    if not _CONNECT_BY_RE.search(text):
        return text, []

    blanked = _blank_strings(text)
    issues: List[ConversionIssue] = []
    out: List[str] = []
    pos = 0
    cte_index = 0

    while True:
        cb_m = _CONNECT_BY_RE.search(blanked, pos)
        if not cb_m:
            out.append(text[pos:])
            break

        select_start = None
        for sm in _SELECT_RE.finditer(blanked, 0, cb_m.start()):
            select_start = sm.start()

        if select_start is None or select_start < pos:
            out.append(text[pos:cb_m.end()])
            pos = cb_m.end()
            continue

        stmt_end = _find_statement_end(blanked, select_start)
        if stmt_end <= cb_m.start():
            out.append(text[pos:cb_m.end()])
            pos = cb_m.end()
            continue

        cte_index += 1
        rewritten, stmt_issues = _try_rewrite_one(text[select_start:stmt_end], cte_keyword, cte_index)
        if stmt_issues:
            out.append(text[pos:select_start])
            out.append(rewritten)
            issues.extend(stmt_issues)
            pos = stmt_end
        else:
            cte_index -= 1  # this occurrence wasn't actually used
            out.append(text[pos:cb_m.end()])
            pos = cb_m.end()

    return "".join(out), issues


def rewrite_routine_source(routine: Routine, cte_keyword: str) -> List[ConversionIssue]:
    """Rewrite any supported CONNECT BY queries found anywhere in
    `routine.source` (a cursor declaration, a row-FOR-loop header, a plain
    SELECT ... INTO, etc.) in place, and return the resulting issues (empty
    if the routine has no CONNECT BY at all, or none of its occurrences
    matched the supported simple shape)."""
    if "CONNECT BY" not in routine.source.upper():
        return []
    new_source, issues = find_and_rewrite(routine.source, cte_keyword=cte_keyword)
    if new_source != routine.source:
        routine.source = new_source
    return issues
