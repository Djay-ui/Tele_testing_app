"""
Split a multi-statement SQL script into individual statements for
execution one at a time.

A naive `text.split(";")` breaks the moment a PL/pgSQL function/procedure/
trigger body is in the script: `CREATE FUNCTION ... AS $$ ... $$ LANGUAGE
plpgsql;` contains its own internal `;`-terminated statements inside the
`$$ ... $$` body, and naively splitting on every `;` chops that body into
invalid fragments. This tokenizes just enough SQL (dollar-quoted strings
and '...' string literals) to only split on statement-terminating
semicolons.

T-SQL (SQL Server) routine/trigger bodies and the idempotency guards
generated for tables/sequences/foreign keys use plain `BEGIN ... END`
blocks rather than dollar-quoting, so a BEGIN/END nesting depth is tracked
too: a semicolon is only statement-terminating at depth 0. This is a no-op
for existing PostgreSQL/MySQL output, since every BEGIN/END pair Postgres
DDL ever contains is already inside a `$$ ... $$` dollar-quoted body (and
so never reaches this check at all) or a bare `DO $$ BEGIN ... END $$;`
block (same reasoning).

Db2 SQL PL routine bodies and idempotency guards use the same BEGIN...END
compound-statement shape as T-SQL, but Db2's IF/LOOP/WHILE/FOR statements
are each closed with their own two-word "END IF"/"END LOOP"/"END WHILE"/
"END FOR" (mirroring Oracle PL/SQL), not a bare "END". A bare-END scan that
didn't know about these would wrongly treat the "END" in "END IF" as
closing the enclosing BEGIN block one statement too early, splitting a
routine body's internal semicolons apart. To handle this without touching
the BEGIN/END counting above (which must stay byte-for-byte identical for
T-SQL, where "END IF"/"END LOOP"/etc. never appear at all), a bare "END"
token is only treated as a close-BEGIN when it is *not* immediately
followed by IF/LOOP/WHILE/FOR/CASE -- when it is, the whole two-word phrase
is passed through inert, leaving begin_depth untouched. This is a provable
no-op for every existing engine's output: Postgres/MySQL bodies never reach
this code path at all, and T-SQL bodies never contain the string "END IF"/
"END LOOP"/"END WHILE"/"END FOR"/"END CASE" in the first place (T-SQL has
none of those statement forms), so the lookahead condition is always false
for them and the bare-END-decrements-by-1 behavior is unchanged.

A "--" line comment (real SQL syntax on every engine this tool targets, and
also this tool's own convention for header/placeholder text such as
MANUAL-CONVERSION-REQUIRED blocks) is recognized and skipped verbatim from
the "--" through the end of the line *before* any other character-by-
character interpretation runs. Without this, a single apostrophe inside a
comment's prose (an English contraction like "tool's", or any other stray
"'") would be misread as opening a real SQL string literal, desyncing
in_single_quote for everything that follows -- silently swallowing
statement-terminating semicolons (and BEGIN/END tracking) until another,
unrelated apostrophe happens to appear later in the text and accidentally
closes it again. Recognizing "--" up front sidesteps this entirely: nothing
inside a comment can affect quote/dollar/BEGIN-END state, matching how a
real SQL parser treats comments too.

A "/* ... */" block comment is recognized the same way, consumed verbatim
from "/*" through the matching "*/" (or end of text, if unterminated).
This matters for exactly one recurring shape in this tool's own output: a
MANUAL-CONVERSION-REQUIRED placeholder (emitted for non-Oracle-sourced
routines, and unconditionally for any MongoDB-target routine/view, since
MongoDB has no stored-procedure equivalent at all) wraps the routine's
*original* source text in "/* ... */" so a human can see what needs to be
hand-ported. That original source is often itself a real BEGIN...END block
containing its own internal semicolons -- without block-comment awareness,
the tokenizer would treat those as live SQL and split the placeholder into
several dangling fragments (one of them just a bare "*/") even though the
whole thing is inert commentary. As with "--", this also matches how a
real SQL parser treats "/* */" -- so this is a correctness fix, not a
special case bolted on for one engine.
"""
from __future__ import annotations

import re
from typing import List

_DOLLAR_TAG_RE = re.compile(r"\$[A-Za-z_]*\$")
_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def split_sql_statements(sql_text: str) -> List[str]:
    statements: List[str] = []
    current: List[str] = []
    i = 0
    n = len(sql_text)
    in_single_quote = False
    dollar_tag = None  # e.g. "$$" or "$body$" while inside a dollar-quoted block
    begin_depth = 0  # T-SQL BEGIN...END nesting (see module docstring)
    case_depth = 0   # CASE ... END, which also closes with a bare END

    while i < n:
        ch = sql_text[i]

        if dollar_tag is not None:
            if sql_text.startswith(dollar_tag, i):
                current.append(dollar_tag)
                i += len(dollar_tag)
                dollar_tag = None
            else:
                current.append(ch)
                i += 1
            continue

        if in_single_quote:
            current.append(ch)
            if ch == "'":
                in_single_quote = False
            i += 1
            continue

        if ch == "-" and sql_text[i:i + 2] == "--":
            # Line comment: everything through the end of the line (or end
            # of text) is inert -- see module docstring. Must be checked
            # before the single-quote-open branch below so a stray
            # apostrophe in comment prose is never mistaken for the start
            # of a real SQL string literal.
            newline_idx = sql_text.find("\n", i)
            end = newline_idx if newline_idx != -1 else n
            current.append(sql_text[i:end])
            i = end
            continue

        if ch == "/" and sql_text[i:i + 2] == "/*":
            # Block comment: everything through the matching "*/" (or end
            # of text, if unterminated) is inert -- see module docstring.
            # Protects wrapped original-source text (e.g. MANUAL-CONVERSION
            # placeholders) from having its own internal BEGIN/END/";"
            # tokens misread as live, statement-terminating SQL.
            close_idx = sql_text.find("*/", i + 2)
            end = close_idx + 2 if close_idx != -1 else n
            current.append(sql_text[i:end])
            i = end
            continue

        if ch == "'":
            in_single_quote = True
            current.append(ch)
            i += 1
            continue

        if ch == "$":
            m = _DOLLAR_TAG_RE.match(sql_text, i)
            if m:
                dollar_tag = m.group(0)
                current.append(dollar_tag)
                i += len(dollar_tag)
                continue
            current.append(ch)
            i += 1
            continue

        if ch.isalpha() and (i == 0 or not (sql_text[i - 1].isalnum() or sql_text[i - 1] == "_")):
            m = _WORD_RE.match(sql_text, i)
            if m:
                word = m.group(0).upper()
                if word == "BEGIN":
                    begin_depth += 1
                elif word == "CASE":
                    # A CASE *expression* closes with a bare END -- the same
                    # token a BEGIN block closes with. Counting CASE as its
                    # own opener is what keeps the two apart. Without it,
                    #
                    #   BEGIN ... SET x = CASE WHEN a THEN 1 ELSE 2 END; ...
                    #
                    # drops begin_depth to 0 at that END, so the very next
                    # ';' is treated as the end of the routine and the
                    # server is sent half a CREATE FUNCTION -- reported as
                    # error 1064 "near ''". Any converted routine
                    # containing a CASE hits this, including one produced
                    # from an Oracle DECODE.
                    case_depth += 1
                elif word == "END":
                    # Peek past the word (and any whitespace) for a
                    # IF/LOOP/WHILE/FOR that turns this into Db2/Oracle's
                    # two-word "END IF" etc. closer -- see the module
                    # docstring for why this must NOT decrement begin_depth
                    # (no matching BEGIN was ever opened for it) while a
                    # genuinely bare "END" still does.
                    tail_m = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)", sql_text[m.end():])
                    next_word = tail_m.group(1).upper() if tail_m else ""
                    if next_word == "CASE":
                        # "END CASE" closes a CASE *statement*, whose own
                        # CASE keyword opened case_depth above.
                        case_depth = max(0, case_depth - 1)
                    elif next_word in ("IF", "LOOP", "WHILE", "FOR"):
                        pass
                    elif case_depth > 0:
                        case_depth -= 1
                    else:
                        begin_depth = max(0, begin_depth - 1)
                current.append(m.group(0))
                i = m.end()
                continue

        if ch == ";":
            if begin_depth > 0:
                current.append(ch)
                i += 1
                continue
            statements.append("".join(current))
            current = []
            i += 1
            continue

        current.append(ch)
        i += 1

    tail = "".join(current)
    if tail.strip():
        statements.append(tail)

    return [s for s in (_clean(stmt) for stmt in statements) if s]


_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)


def has_executable_sql(statement: str) -> bool:
    """True when `statement` contains anything a server would actually run.

    split_sql_statements deliberately *keeps* comment-only statements --
    a "MANUAL CONVERSION REQUIRED" placeholder wrapping the original
    routine source in `/* ... */`, for instance -- because dropping them
    would silently renumber every "Statement N/M" the user is shown and
    hide from a saved script the fact that an object needs hand-porting.

    Sending one to a server is a different matter: MySQL answers a
    comment-only statement with "Query was empty" and psycopg refuses to
    execute one at all, so a script containing a single unconverted
    routine failed part-way through for a reason that had nothing to do
    with the schema. Callers that execute statements filter with this;
    callers that display or save them do not."""
    text = _BLOCK_COMMENT_RE.sub(" ", statement or "")
    text = "\n".join(
        ln for ln in text.split("\n") if not ln.strip().startswith("--")
    )
    return bool(text.strip().strip(";").strip())


def _clean(statement: str) -> str:
    """Drop pure comment lines and surrounding whitespace; return '' if the
    statement has no executable content once comment-only lines are removed."""
    lines = [ln for ln in statement.split("\n") if not ln.strip().startswith("--")]
    stripped = "\n".join(lines).strip()
    return statement.strip() if stripped else ""
