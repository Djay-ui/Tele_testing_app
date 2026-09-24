"""Explaining a foreign key the target refuses to create.

    insert or update on table "team_members" violates foreign key
    constraint "team_members_team_id_foreign"
    DETAIL: Key (team_id)=(7f304fcf-...) is not present in table "teams".

This one is not a conversion problem. The constraint is right, the types
are right, the tables are right -- the *rows* do not satisfy it: some
`team_members` row points at a `teams` row that is not there. Adding the
constraint would make the table's existing contents illegal, so the
server refuses.

Almost always the orphans came with the data. MySQL only enforces foreign
keys on InnoDB, and only while `FOREIGN_KEY_CHECKS` is on -- a MyISAM
table, a table converted to InnoDB after the fact, or any bulk load done
with the checks off can accumulate rows whose parent has been deleted.
Nothing complains until something tries to add the constraint for real,
which is what a migration to PostgreSQL does.

The other possibility is that the parent table did not fully migrate, and
that one matters much more, so this module distinguishes them: it counts
the orphans on the target, counts the parent's rows, and hands back the
`SELECT` that lists the offending rows so the answer is a copy-paste away
rather than a schema diff by hand.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

from tgdatabridge.utils.identifiers import quote_backtick, quote_bracket, quote_double

#: PostgreSQL 23503 foreign_key_violation, MySQL 1452 / 1216, SQL Server
#: 547, Oracle ORA-02291, Db2 SQLSTATE 23503 -- all "a row does not
#: satisfy this reference".
_VIOLATION_CODES = {1452, 1216, 547, 2291}
_VIOLATION_SQLSTATES = {"23503", "23000"}
_VIOLATION_TEXT = re.compile(
    r"violates foreign key constraint|foreign key constraint fails|"
    r"integrity constraint .* violated|conflicted with the .*FOREIGN KEY",
    re.IGNORECASE)


def is_foreign_key_violation(exc: BaseException) -> bool:
    """The rows don't satisfy the constraint -- as opposed to the
    constraint being malformed, which is a different problem with a
    different fix."""
    text = str(exc)
    if _VIOLATION_TEXT.search(text):
        return True
    for attr in ("errno", "number", "code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int) and value in _VIOLATION_CODES:
            return True
    if re.search(r"ORA-0*2291", text):
        return True
    state = getattr(exc, "sqlstate", None) or getattr(
        getattr(exc, "diag", None), "sqlstate", None)
    return isinstance(state, str) and state.upper() in _VIOLATION_SQLSTATES


@dataclass
class ForeignKey:
    constraint: str = ""
    child_table: str = ""
    child_columns: List[str] = field(default_factory=list)
    parent_table: str = ""
    parent_columns: List[str] = field(default_factory=list)
    #: The table references exactly as the failed statement spelled them,
    #: schema prefix and quoting included. `child_table` / `parent_table`
    #: stay bare because they are what the user reads in the message; these
    #: are what any SQL built from the parse must actually use, because a
    #: bare name re-quoted from scratch would drop the schema and resolve
    #: against search_path instead of the table that actually failed.
    child_table_sql: str = ""
    parent_table_sql: str = ""
    #: `ON DELETE CASCADE ON UPDATE SET NULL` and friends. This tool's own
    #: generator does not emit them today, but a statement can also arrive
    #: from a hand-edited script, and rebuilding the constraint without
    #: them would silently change its behaviour.
    actions: str = ""


#: `t`, `"t"`, `[t]`, `` `t` ``, and any of those with a schema in front.
_IDENT = r"(?:[`\"\[]?[\w$#]+[`\"\]]?\.)?[`\"\[]?[\w$#]+[`\"\]]?"

_ADD_FK_RE = re.compile(
    r"ALTER\s+TABLE\s+(?P<child>" + _IDENT + r")\s+"
    r"(?:WITH\s+(?:NO)?CHECK\s+)?"
    r"ADD\s+CONSTRAINT\s+(?P<name>[`\"\[]?[\w$#]+[`\"\]]?)\s+"
    r"FOREIGN\s+KEY\s*\((?P<cols>[^)]*)\)\s*"
    r"REFERENCES\s+(?P<parent>" + _IDENT + r")\s*\((?P<refcols>[^)]*)\)"
    r"(?P<actions>(?:\s+ON\s+(?:DELETE|UPDATE)\s+"
    r"(?:CASCADE|RESTRICT|SET\s+NULL|SET\s+DEFAULT|NO\s+ACTION))*)",
    re.IGNORECASE)


def _bare(identifier: str) -> str:
    """The object's own name, with quoting and any schema prefix removed."""
    text = identifier.strip()
    # Split on the last dot that is not inside quotes -- schema names in
    # this tool are always simple identifiers, so a plain rsplit is safe
    # once the surrounding quotes are off each part.
    if "." in text:
        text = text.rsplit(".", 1)[-1]
    return text.strip().strip('`"[]')


def parse_add_foreign_key(statement: str) -> Optional[ForeignKey]:
    """Pull the four names out of the ALTER TABLE that failed. Returns
    None for anything that is not an ADD CONSTRAINT ... FOREIGN KEY, so
    the caller can fall back to the driver's own message."""
    match = _ADD_FK_RE.search(" ".join((statement or "").split()))
    if not match:
        return None
    return ForeignKey(
        constraint=_bare(match.group("name")),
        child_table=_bare(match.group("child")),
        child_columns=[_bare(c) for c in match.group("cols").split(",") if c.strip()],
        parent_table=_bare(match.group("parent")),
        parent_columns=[_bare(c) for c in match.group("refcols").split(",") if c.strip()],
        child_table_sql=match.group("child").strip(),
        parent_table_sql=match.group("parent").strip(),
        actions=" ".join(match.group("actions").split()),
    )


def _quoter(target):
    name = type(target).__name__.lower()
    if "mysql" in name:
        return quote_backtick
    if "sqlserver" in name:
        return quote_bracket
    return quote_double


def table_sql(target, fk: ForeignKey, side: str) -> str:
    """How to name one of the two tables in SQL sent to `target`.

    Prefers the spelling the failed statement itself used, so a
    schema-qualified reference stays qualified; falls back to quoting the
    bare name for a ForeignKey built by hand or by an older parse.
    """
    quote = _quoter(target)
    verbatim = fk.child_table_sql if side == "child" else fk.parent_table_sql
    if not verbatim:
        return quote(fk.child_table if side == "child" else fk.parent_table)
    # Re-quoted part by part rather than passed through: the schema prefix
    # has to survive (a bare name would resolve against search_path instead
    # of the table that actually failed), but the quote character must be
    # the target's own. Those two are usually already the same -- the
    # statement was generated for this engine -- but a hand-edited or
    # pasted script can carry the wrong one, and `"t"` is not an identifier
    # on MySQL unless ANSI_QUOTES happens to be set.
    parts = [p.strip().strip('`"[]') for p in verbatim.split(".")]
    return ".".join(quote(p) for p in parts if p)


def orphan_query(target, fk: ForeignKey) -> str:
    """The SELECT that lists the child rows with no parent.

    Written as a LEFT JOIN rather than NOT IN so it behaves the same when
    the key is NULL-able (a NULL foreign key is not a violation on any
    engine, and NOT IN would wrongly report it as one)."""
    quote = _quoter(target)
    child, parent = table_sql(target, fk, "child"), table_sql(target, fk, "parent")
    on = " AND ".join(
        f"c.{quote(c)} = p.{quote(p)}"
        for c, p in zip(fk.child_columns, fk.parent_columns))
    not_null = " AND ".join(f"c.{quote(c)} IS NOT NULL" for c in fk.child_columns)
    first_parent = quote(fk.parent_columns[0]) if fk.parent_columns else quote("id")
    return (f"SELECT c.* FROM {child} c LEFT JOIN {parent} p ON {on} "
            f"WHERE {not_null} AND p.{first_parent} IS NULL")


@dataclass
class Diagnosis:
    fk: ForeignKey
    orphan_rows: Optional[int] = None
    child_rows: Optional[int] = None
    parent_rows: Optional[int] = None
    query: str = ""
    error: Optional[str] = None

    @property
    def parent_looks_empty(self) -> bool:
        """The far more serious reading: the parent table did not migrate,
        so *every* child row is an orphan."""
        return bool(self.parent_rows == 0 and self.child_rows)

    def message(self) -> str:
        fk = self.fk
        head = (f"Foreign key {fk.constraint} could not be created: rows in "
                f"{fk.child_table} point at {fk.parent_table} rows that are not there.")
        if self.error:
            return head + f"\n\n(The tool could not count them: {self.error})"
        if self.parent_looks_empty:
            return (
                f"{head}\n\n{fk.parent_table} is EMPTY on the target while "
                f"{fk.child_table} has {self.child_rows:,} row(s). That points at the "
                f"parent table not having migrated rather than at the data -- check "
                f"whether {fk.parent_table} failed or was left out, migrate it, and "
                f"apply this step again.")
        counted = (f"{self.orphan_rows:,} of {self.child_rows:,} row(s) in "
                   f"{fk.child_table} have no matching {fk.parent_table} row "
                   f"({self.parent_rows:,} rows).")
        return (
            f"{head}\n\n{counted}\n\n"
            f"Those orphans almost certainly came with the data: MySQL enforces foreign "
            f"keys only on InnoDB and only while FOREIGN_KEY_CHECKS is on, so a MyISAM "
            f"table, a later conversion to InnoDB, or any bulk load done with the checks "
            f"off leaves rows whose parent was deleted. PostgreSQL will not accept a "
            f"constraint the existing rows already break.\n\n"
            f"To see them:\n\n    {self.query};\n\n"
            f"Then either delete or repoint those rows and apply this step again, or "
            f"leave this one constraint off -- \"Apply the rest and list every failure\" "
            f"will carry on and show you every foreign key in the same position.")


def diagnose(target, statement: str) -> Optional[Diagnosis]:
    """Count the orphans behind a foreign-key violation.

    Never raises and never blocks: a diagnosis that cannot be produced
    simply is not shown, and the driver's own message still reaches the
    user.
    """
    fk = parse_add_foreign_key(statement)
    if fk is None or not fk.child_columns or not fk.parent_columns:
        return None
    execute = getattr(target, "execute", None)
    if execute is None:
        return None

    result = Diagnosis(fk=fk, query=orphan_query(target, fk))
    try:
        counted = orphan_query(target, fk).replace("SELECT c.*", "SELECT COUNT(*)", 1)
        result.orphan_rows = int(list(execute(counted))[0][0])
        result.child_rows = int(list(execute(
            f"SELECT COUNT(*) FROM {table_sql(target, fk, 'child')}"))[0][0])
        result.parent_rows = int(list(execute(
            f"SELECT COUNT(*) FROM {table_sql(target, fk, 'parent')}"))[0][0])
    except Exception as exc:  # noqa: BLE001 - a diagnosis is a bonus, never a blocker
        result.error = str(exc).splitlines()[0]
    return result
