"""Delimiter-safe SQL identifier quoting.

Every target dialect this tool emits DDL for delimits identifiers with a
character that can itself legally appear *inside* an identifier, and each
one escapes that character by doubling it:

    PostgreSQL / Oracle / Db2      "My""Table"
    MySQL / MariaDB                `My``Table`
    SQL Server                     [My]]Table]

Before this module existed, every quoting site in the codebase wrapped the
identifier in delimiters without escaping -- ``f'"{identifier}"'`` and
friends. That was wrong in two distinct ways, and the second is the reason
this is a shared module rather than five one-line fixes:

1. **Correctness.** An identifier containing the delimiter is legal, not
   exotic. ``CREATE TABLE "My""Table"`` is valid Oracle and valid
   PostgreSQL, and names like that turn up in schemas built by tools that
   quote defensively. Unescaped, such a name produced syntactically
   broken DDL -- a failure at least loud enough to notice.

2. **Injection.** Object names are not trusted input. They are read from
   whatever source database the tool is pointed at, and the DDL generated
   from them is subsequently *executed against the target* by "Apply DDL"
   with the migration account's privileges. A source object named

       foo"; DROP TABLE important; --

   would, unescaped, close the quoted identifier and inject arbitrary SQL
   into the generated script. That is the failure mode this module exists
   to make structurally impossible, so quoting is centralised here rather
   than re-derived correctly (or not) at each of the ~25 call sites.

Case folding deliberately stays with the callers. Each dialect has its own
convention -- PostgreSQL lowercases, Oracle and Db2 uppercase, MySQL and
SQL Server preserve -- and those choices are load-bearing, documented
decisions at the call sites (see ``ddl_generator._quote_pg``'s comment on
why lowercasing is what keeps verbatim-copied view bodies resolving). This
module does one thing: it makes the delimiter safe.
"""
from __future__ import annotations


def quote_double(identifier: str) -> str:
    """Double-quote an identifier, doubling any embedded double quote.

    The ISO SQL convention, used by PostgreSQL, Oracle and Db2.

    >>> quote_double('My"Table')
    '"My""Table"'
    """
    return '"' + str(identifier).replace('"', '""') + '"'


def quote_backtick(identifier: str) -> str:
    """Backtick-quote an identifier, doubling any embedded backtick.

    MySQL's convention.

    >>> quote_backtick('My`Table')
    '`My``Table`'
    """
    return "`" + str(identifier).replace("`", "``") + "`"


def quote_bracket(identifier: str) -> str:
    """Bracket-quote an identifier, doubling any embedded closing bracket.

    SQL Server's convention. Only the *closing* bracket needs escaping --
    an opening bracket inside a bracket-quoted identifier is an ordinary
    character, which is why this is not symmetric with the two above.

    >>> quote_bracket('My]Table')
    '[My]]Table]'
    """
    return "[" + str(identifier).replace("]", "]]") + "]"


def quote_literal(value: str) -> str:
    """Render a value as a SQL string literal, doubling any embedded
    single quote.

    Distinct from the identifier helpers above and needed for the same
    reason. Several dialects' idempotency guards compare an object's name
    against the system catalogue as a *string*, not as an identifier --

        IF NOT EXISTS (SELECT 1 FROM SYSCAT.TABLES WHERE TABNAME = 'FOO')

    -- so a name containing an apostrophe (O'Brien is the everyday case,
    and the injection case is the same shape as for identifiers) breaks
    out of the literal rather than out of a quoted identifier. Same
    defect class, different delimiter.

    >>> quote_literal("O'Brien")
    "'O''Brien'"
    """
    return "'" + str(value).replace("'", "''") + "'"
