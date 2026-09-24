"""Finishing a migration whose data breaks its own foreign keys.

`fk_violations` explains one of these failures. This module ends it.

The situation, which turns out to be the normal one rather than the
exception: a long-lived MySQL database has rows whose parent was
deleted. MySQL never noticed -- it enforces foreign keys only on InnoDB
and only while `FOREIGN_KEY_CHECKS` is on, so a MyISAM table, a later
conversion to InnoDB, or any bulk load done with the checks off quietly
accumulates orphans. The migration is the first thing in that database's
life that ever actually checks, and PostgreSQL will not create a
constraint the existing rows already break. So statement 1568 of 1572
fails, and four statements that had nothing to do with it never run.

Stopping there is the wrong answer twice over. The schema is left
incomplete over one bad row in a million; and the user is asked to hand-fix
data at the exact moment they wanted a finished database.

Every engine here has a way to say "create this constraint, enforce it
from now on, and do not re-litigate the rows that are already in the
table":

    PostgreSQL   ... FOREIGN KEY (...) REFERENCES ... NOT VALID
    Oracle       ... REFERENCES ... ENABLE NOVALIDATE
    SQL Server   ALTER TABLE t WITH NOCHECK ADD CONSTRAINT ...
    Db2          ... REFERENCES ... NOT ENFORCED
    MySQL        SET FOREIGN_KEY_CHECKS = 0 around the ALTER

That is the default policy, and it is the only one that is safe to run
without asking, because it is the only one that changes no data: nothing
is deleted, nothing is blanked, the constraint exists and every row
written from that moment on is checked against it. The orphans stay
exactly where they were, listed in the summary, and can be cleaned up
afterwards at leisure -- after which one `VALIDATE CONSTRAINT` promotes
the constraint to fully checked with no downtime.

The destructive policies exist because sometimes the orphans genuinely
are rubbish and the user knows it. They are never the default, they are
never chosen automatically, and each one reports the exact number of rows
it touched.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from tgdatabridge.core.fk_violations import (
    Diagnosis, ForeignKey, diagnose, orphan_query, parse_add_foreign_key, table_sql)
from tgdatabridge.utils.identifiers import quote_backtick, quote_bracket, quote_double

#: Create the constraint but exempt the rows already in the table. Changes
#: no data. The default, and the only policy applied without being asked
#: for.
POLICY_NOT_VALID = "not_valid"
#: Blank the offending foreign-key values, then create the constraint
#: fully checked. Only possible when every column of the key is nullable.
POLICY_NULL_ORPHANS = "null_orphans"
#: Delete the offending child rows, then create the constraint fully
#: checked. Destructive; opt-in only.
POLICY_DELETE_ORPHANS = "delete_orphans"
#: Leave the constraint off entirely and carry on.
POLICY_SKIP = "skip"
#: The old behaviour: stop and let the user decide, one dialog at a time.
POLICY_STOP = "stop"

POLICIES = (POLICY_NOT_VALID, POLICY_NULL_ORPHANS, POLICY_DELETE_ORPHANS,
            POLICY_SKIP, POLICY_STOP)

POLICY_LABELS = {
    POLICY_NOT_VALID:
        "Create it anyway, unvalidated (recommended -- no data is changed)",
    POLICY_NULL_ORPHANS:
        "Blank the orphaned values, then create it fully checked",
    POLICY_DELETE_ORPHANS:
        "Delete the orphaned rows, then create it fully checked",
    POLICY_SKIP:
        "Leave the constraint off and carry on",
    POLICY_STOP:
        "Stop and ask me each time",
}


def dialect_of(target) -> str:
    """Which of the five SQL flavours `target` speaks.

    Read off the connector's class name, the same way source_sql.quoter_for
    and target_shape._quote_for do, so no engine string has to be threaded
    down here from the GUI.
    """
    name = type(target).__name__.lower()
    for key in ("postgres", "mysql", "sqlserver", "oracle", "db2", "mongo"):
        if key in name:
            return key
    return "postgres"


def _quoter(target):
    d = dialect_of(target)
    if d == "mysql":
        return quote_backtick
    if d == "sqlserver":
        return quote_bracket
    return quote_double


def supports_unvalidated(target) -> bool:
    """MongoDB has no foreign keys at all, so there is nothing to create
    unvalidated; every SQL engine this tool targets has some spelling of
    it."""
    return dialect_of(target) != "mongo"


def unvalidated_statements(target, fk: ForeignKey) -> List[str]:
    """The statement(s) that create `fk` without checking existing rows.

    A list rather than a string because MySQL has no clause for this and
    has to be bracketed by session-variable statements instead. Rebuilt
    from the parsed constraint rather than patched into the original text:
    the original may be wrapped in a PL/pgSQL DO block (this tool's own
    "no ADD CONSTRAINT IF NOT EXISTS" workaround), and appending to that
    would put NOT VALID after the END.
    """
    d = dialect_of(target)
    quote = _quoter(target)
    child = table_sql(target, fk, "child")
    parent = table_sql(target, fk, "parent")
    cols = ", ".join(quote(c) for c in fk.child_columns)
    refs = ", ".join(quote(c) for c in fk.parent_columns)
    actions = f" {fk.actions}" if fk.actions else ""
    core = (f"ADD CONSTRAINT {quote(fk.constraint)} FOREIGN KEY ({cols}) "
            f"REFERENCES {parent} ({refs}){actions}")

    if d == "sqlserver":
        # WITH NOCHECK goes before ADD, not after the references list, and
        # leaves the constraint "not trusted" -- the exact analogue of NOT
        # VALID.
        return [f"ALTER TABLE {child} WITH NOCHECK {core};"]
    if d == "oracle":
        return [f"ALTER TABLE {child} {core} ENABLE NOVALIDATE"]
    if d == "db2":
        return [f"ALTER TABLE {child} {core} NOT ENFORCED"]
    if d == "mysql":
        # MySQL/MariaDB have no unvalidated constraint. Turning the checks
        # off for the length of the ALTER is the documented way to add one
        # over existing data; the constraint that results is a normal,
        # fully enforcing constraint from that point on. Restored in the
        # same batch, and again in a finally by the caller, so a failure
        # part-way cannot leave the session with checks off.
        return ["SET FOREIGN_KEY_CHECKS = 0;",
                f"ALTER TABLE {child} {core};",
                "SET FOREIGN_KEY_CHECKS = 1;"]
    return [f"ALTER TABLE {child} {core} NOT VALID;"]


def validate_statement(target, fk: ForeignKey) -> Optional[str]:
    """The statement that promotes an unvalidated constraint to fully
    checked, once the orphans are gone.

    Handed to the user in the summary rather than run: it is only correct
    after they have decided what the orphaned rows should be, and on a
    large table it is a long scan they should choose the moment for.
    """
    d = dialect_of(target)
    quote = _quoter(target)
    child = table_sql(target, fk, "child")
    name = quote(fk.constraint)
    if d == "postgres":
        return f"ALTER TABLE {child} VALIDATE CONSTRAINT {name};"
    if d == "db2":
        return f"ALTER TABLE {child} ALTER FOREIGN KEY {name} ENFORCED;"
    if d == "oracle":
        return f"ALTER TABLE {child} MODIFY CONSTRAINT {name} ENABLE VALIDATE"
    if d == "sqlserver":
        return f"ALTER TABLE {child} WITH CHECK CHECK CONSTRAINT {name};"
    # MySQL's constraint is already fully enforcing, and MongoDB has none.
    return None


def _join_condition(quote, fk: ForeignKey, left: str, right: str) -> str:
    return " AND ".join(
        f"{left}.{quote(c)} = {right}.{quote(p)}"
        for c, p in zip(fk.child_columns, fk.parent_columns))


def null_orphans_statement(target, fk: ForeignKey) -> str:
    """Blank the foreign-key columns of every orphaned child row.

    A NULL foreign key is not a violation on any engine, so this makes the
    rows legal without deleting anything -- the row and all its other
    columns survive, it simply stops claiming a parent that is not there.

    Written two ways because MySQL refuses to read the table an UPDATE is
    writing to inside a subquery (error 1093, "You can't specify target
    table for update in FROM clause") and offers multi-table UPDATE
    instead; everything else takes the portable NOT EXISTS form.
    """
    quote = _quoter(target)
    child = table_sql(target, fk, "child")
    parent = table_sql(target, fk, "parent")

    if dialect_of(target) == "mysql":
        sets = ", ".join(f"c.{quote(c)} = NULL" for c in fk.child_columns)
        not_null = " AND ".join(f"c.{quote(c)} IS NOT NULL" for c in fk.child_columns)
        return (f"UPDATE {child} c LEFT JOIN {parent} p "
                f"ON {_join_condition(quote, fk, 'c', 'p')} "
                f"SET {sets} "
                f"WHERE {not_null} AND p.{quote(fk.parent_columns[0])} IS NULL")
    # No alias on the outer table: PostgreSQL, Oracle, SQL Server and Db2
    # each spell aliasing an UPDATE target differently (and Oracle refuses
    # AS), while all four accept the table's own name as the correlation
    # name inside the subquery.
    sets = ", ".join(f"{quote(c)} = NULL" for c in fk.child_columns)
    not_null = " AND ".join(f"{quote(c)} IS NOT NULL" for c in fk.child_columns)
    exists = " AND ".join(
        f"p.{quote(p)} = {child}.{quote(c)}"
        for c, p in zip(fk.child_columns, fk.parent_columns))
    return (f"UPDATE {child} SET {sets} WHERE {not_null} "
            f"AND NOT EXISTS (SELECT 1 FROM {parent} p WHERE {exists})")


def delete_orphans_statement(target, fk: ForeignKey) -> str:
    """Remove every orphaned child row. Destructive, hence opt-in only."""
    quote = _quoter(target)
    child = table_sql(target, fk, "child")
    parent = table_sql(target, fk, "parent")

    if dialect_of(target) == "mysql":
        not_null = " AND ".join(f"c.{quote(c)} IS NOT NULL" for c in fk.child_columns)
        return (f"DELETE c FROM {child} c LEFT JOIN {parent} p "
                f"ON {_join_condition(quote, fk, 'c', 'p')} "
                f"WHERE {not_null} AND p.{quote(fk.parent_columns[0])} IS NULL")
    not_null = " AND ".join(f"{quote(c)} IS NOT NULL" for c in fk.child_columns)
    exists = " AND ".join(
        f"p.{quote(p)} = {child}.{quote(c)}"
        for c, p in zip(fk.child_columns, fk.parent_columns))
    return (f"DELETE FROM {child} WHERE {not_null} "
            f"AND NOT EXISTS (SELECT 1 FROM {parent} p WHERE {exists})")


def plain_statement(target, fk: ForeignKey) -> str:
    """The constraint, fully checked -- what the original statement was
    trying to do, rebuilt cleanly for the second attempt after the data
    has been cleaned up."""
    quote = _quoter(target)
    cols = ", ".join(quote(c) for c in fk.child_columns)
    refs = ", ".join(quote(c) for c in fk.parent_columns)
    actions = f" {fk.actions}" if fk.actions else ""
    return (f"ALTER TABLE {table_sql(target, fk, 'child')} ADD CONSTRAINT "
            f"{quote(fk.constraint)} FOREIGN KEY ({cols}) REFERENCES "
            f"{table_sql(target, fk, 'parent')} ({refs}){actions};")


@dataclass
class Recovery:
    """What was actually done about one refused foreign key."""
    fk: ForeignKey
    policy: str = POLICY_NOT_VALID
    #: The policy that ended up being used -- not always the one asked
    #: for, because a NOT NULL foreign-key column cannot be blanked and a
    #: fully-checked retry is worth attempting before giving up.
    applied: str = ""
    ok: bool = False
    rows_changed: Optional[int] = None
    orphan_rows: Optional[int] = None
    parent_rows: Optional[int] = None
    child_rows: Optional[int] = None
    parent_looks_empty: bool = False
    validate_sql: str = ""
    orphan_sql: str = ""
    statements: List[str] = field(default_factory=list)
    error: str = ""
    notes: List[str] = field(default_factory=list)

    @property
    def constraint(self) -> str:
        return self.fk.constraint

    def one_line(self) -> str:
        """The line this constraint gets in the end-of-run summary."""
        fk = self.fk
        where = f"{fk.child_table} -> {fk.parent_table}"
        if not self.ok:
            return f"{fk.constraint} ({where}): NOT created -- {self.error}"
        count = ("" if self.orphan_rows is None
                 else f"{self.orphan_rows:,} orphan row(s); ")
        if self.applied == POLICY_NOT_VALID:
            tail = "created unvalidated, no data changed"
            if self.parent_looks_empty:
                tail += f" -- WARNING: {fk.parent_table} is EMPTY, check it migrated"
            return f"{fk.constraint} ({where}): {count}{tail}"
        if self.applied == POLICY_NULL_ORPHANS:
            return (f"{fk.constraint} ({where}): {count}"
                    f"{self.rows_changed:,} row(s) blanked, constraint fully checked")
        if self.applied == POLICY_DELETE_ORPHANS:
            return (f"{fk.constraint} ({where}): {count}"
                    f"{self.rows_changed:,} row(s) deleted, constraint fully checked")
        return f"{fk.constraint} ({where}): {count}left off"

    def detail(self) -> str:
        """The paragraph shown when the user opens one of these up."""
        lines = [self.one_line()]
        if self.orphan_sql:
            lines.append(f"  see them:  {self.orphan_sql};")
        if self.validate_sql and self.applied == POLICY_NOT_VALID:
            lines.append(f"  once clean: {self.validate_sql}")
        lines.extend(f"  {n}" for n in self.notes)
        return "\n".join(lines)


def _run(target, statements: List[str]) -> None:
    for sql in statements:
        target.execute_ddl(sql)


def _count(target, sql: str) -> Optional[int]:
    try:
        return int(list(target.execute(sql))[0][0])
    except Exception:  # noqa: BLE001
        return None


def recover(target, statement: str, policy: str = POLICY_NOT_VALID,
            found: Optional[Diagnosis] = None) -> Optional[Recovery]:
    """Deal with one foreign key the target refused, and say what was done.

    Returns None only when the statement is not an ADD CONSTRAINT ...
    FOREIGN KEY at all, or when the policy is POLICY_STOP -- in both cases
    the caller falls back to reporting the failure.

    Never raises. A recovery that cannot be carried out comes back with
    `ok=False` and the reason in `error`, and the caller treats that
    exactly as it would have treated the original failure.
    """
    if policy == POLICY_STOP:
        return None
    fk = parse_add_foreign_key(statement)
    if fk is None or not fk.child_columns or not fk.parent_columns:
        return None
    if len(fk.child_columns) != len(fk.parent_columns):
        return None

    result = Recovery(fk=fk, policy=policy)
    if found is None:
        try:
            found = diagnose(target, statement)
        except Exception:  # noqa: BLE001
            found = None
    if found is not None:
        result.orphan_rows = found.orphan_rows
        result.parent_rows = found.parent_rows
        result.child_rows = found.child_rows
        result.parent_looks_empty = found.parent_looks_empty
    try:
        result.orphan_sql = orphan_query(target, fk)
        result.validate_sql = validate_statement(target, fk) or ""
    except Exception:  # noqa: BLE001
        pass

    if policy == POLICY_SKIP:
        result.applied, result.ok = POLICY_SKIP, True
        return result

    # The two destructive policies clean the data first and then create the
    # constraint for real. If either half fails, fall through to the
    # unvalidated form rather than leaving the constraint off: a constraint
    # that exists is strictly better than one that does not, and nothing
    # has been lost by trying.
    if policy in (POLICY_NULL_ORPHANS, POLICY_DELETE_ORPHANS):
        builder = (null_orphans_statement if policy == POLICY_NULL_ORPHANS
                   else delete_orphans_statement)
        try:
            before = _count(target, result.orphan_sql.replace(
                "SELECT c.*", "SELECT COUNT(*)", 1))
            target.execute_ddl(builder(target, fk) + ";")
            after = _count(target, result.orphan_sql.replace(
                "SELECT c.*", "SELECT COUNT(*)", 1))
            if before is not None and after is not None:
                result.rows_changed = before - after
            target.execute_ddl(plain_statement(target, fk))
            result.applied, result.ok = policy, True
            return result
        except Exception as exc:  # noqa: BLE001
            reason = str(exc).splitlines()[0]
            result.notes.append(
                f"could not {'blank' if policy == POLICY_NULL_ORPHANS else 'delete'} "
                f"the orphans ({reason}) -- created unvalidated instead")

    if not supports_unvalidated(target):
        result.applied, result.ok = POLICY_SKIP, True
        result.notes.append("this target has no foreign keys to create")
        return result

    statements = unvalidated_statements(target, fk)
    result.statements = statements
    try:
        _run(target, statements)
        result.applied, result.ok = POLICY_NOT_VALID, True
    except Exception as exc:  # noqa: BLE001
        result.error = str(exc).splitlines()[0]
        result.ok = False
    finally:
        # Belt and braces: if the ALTER blew up between the two SET
        # statements above, the session would otherwise carry on with
        # foreign-key checking disabled and let bad rows into every table
        # loaded afterwards.
        if dialect_of(target) == "mysql":
            try:
                target.execute_ddl("SET FOREIGN_KEY_CHECKS = 1;")
            except Exception:  # noqa: BLE001
                pass
    return result


def summarise(recoveries: List[Recovery]) -> str:
    """The block of text the end-of-run dialog shows when foreign keys had
    to be recovered. Empty when none did."""
    if not recoveries:
        return ""
    made = [r for r in recoveries if r.ok and r.applied != POLICY_SKIP]
    left = [r for r in recoveries if r.ok and r.applied == POLICY_SKIP]
    failed = [r for r in recoveries if not r.ok]
    empty_parents = [r for r in recoveries if r.parent_looks_empty]

    n = len(recoveries)
    parts = [
        f"{n} foreign key{'' if n == 1 else 's'} could not be created as written, because "
        f"rows already in those tables break {'it' if n == 1 else 'them'}. This is data that "
        f"came from the source: MySQL enforces foreign keys only on InnoDB and only while "
        f"FOREIGN_KEY_CHECKS is on, so orphaned rows accumulate there unnoticed until "
        f"something checks."]
    if made:
        parts.append(
            f"{len(made)} {'was' if len(made) == 1 else 'were'} created anyway and "
            f"{'is' if len(made) == 1 else 'are'} enforced from now on:\n  "
            + "\n  ".join(r.detail() for r in made))
    if left:
        parts.append(f"{len(left)} {'was' if len(left) == 1 else 'were'} left off:\n  "
                     + "\n  ".join(r.detail() for r in left))
    if failed:
        parts.append(f"{len(failed)} could not be created at all:\n  "
                     + "\n  ".join(r.detail() for r in failed))
    if empty_parents:
        parts.append(
            "Worth checking before you go further: the parent table is EMPTY for "
            + ", ".join(sorted({r.fk.parent_table for r in empty_parents}))
            + ". An empty parent usually means that table did not migrate, which is a "
              "bigger problem than a few orphaned rows.")
    if any(r.applied == POLICY_NOT_VALID for r in made):
        parts.append(
            "An unvalidated constraint is real: every insert and update from now on is "
            "checked against it. It simply has not re-checked the rows that were already "
            "there. Clean those up whenever you like, then run the \"once clean\" "
            "statement above to promote it to fully validated.")
    return "\n\n".join(parts)
