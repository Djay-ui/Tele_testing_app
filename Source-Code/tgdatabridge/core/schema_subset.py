"""Migrating only part of a schema.

The object tree has always had a checkbox on every table, view, sequence
and routine, but only the data-migration step ever read them: "Convert
Schema" converted the whole loaded schema regardless, so unticking a
table changed nothing in the generated DDL, and the tick state was reset
the moment the tree was rebuilt after a conversion.

`subset_schema` is the missing piece -- it turns "these are the objects
the user still has ticked" into a Schema the rest of the pipeline can be
run against unchanged.

The work is not simply filtering four lists. Leaving an object out breaks
whatever pointed at it, and each of those has to be handled or the
generated DDL fails on the target:

* A foreign key on a kept table that references an excluded table cannot
  be created -- MySQL answers 1005/150 or 3734, PostgreSQL "relation does
  not exist". The constraint is dropped from the subset and reported.
* A trigger belongs to a table. If that table is excluded, the trigger has
  nothing to attach to, so it is dropped and reported.
* A view whose FROM/JOIN clause names an excluded table cannot be created
  either -- unlike a stored routine, a view's body is resolved when it is
  created, so the server rejects it outright ("Table 'x' doesn't exist").
  It is dropped from the subset and reported, naming the table to re-tick
  if the view is wanted. Only real table positions count, so a view that
  merely happens to contain the word elsewhere is unaffected.

Stored procedures, functions and triggers are deliberately *not* checked
this way: their bodies are not resolved at creation time on any of the
supported targets, so one that reads an excluded table still installs
cleanly and starts working the moment that table exists.

Nothing here mutates the loaded schema: the user can untick a table,
convert, re-tick it and convert again, and the second run sees the
original object graph intact.
"""
from __future__ import annotations

import copy
import re
from typing import Iterable, List, Optional, Sequence, Set, Tuple

from tgdatabridge.core.schema_model import Schema, Table


def _names(objects: Optional[Iterable]) -> Optional[Set[str]]:
    if objects is None:
        return None
    return {getattr(o, "name", str(o)) for o in objects}


def _folded(names: Set[str]) -> Set[str]:
    return {n.lower() for n in names}


def subset_schema(
    schema: Schema,
    tables: Optional[Sequence] = None,
    views: Optional[Sequence] = None,
    sequences: Optional[Sequence] = None,
    routines: Optional[Sequence] = None,
) -> Tuple[Schema, List[str]]:
    """A Schema holding only the selected objects, plus the notes to show.

    Each argument is the list of objects to keep for that category, or
    None to keep every object in it (so `subset_schema(schema)` returns
    an equivalent schema and no notes).

    Kept objects are the *same instances* as in `schema` wherever
    possible, so a conversion run against the subset writes its status,
    issues and converted source straight back onto the objects the tree
    is showing. The one exception is a table that loses a foreign key,
    which is shallow-copied so the original keeps its full constraint
    list -- `sync_conversion_results` copies that table's conversion
    outcome back afterwards.
    """
    notes: List[str] = []

    keep_tables = _names(tables)
    keep_views = _names(views)
    keep_sequences = _names(sequences)
    keep_routines = _names(routines)

    kept_tables = [t for t in schema.tables
                   if keep_tables is None or t.name in keep_tables]
    kept_table_names = {t.name for t in kept_tables}
    kept_folded = _folded(kept_table_names)

    excluded_tables = [t for t in schema.tables if t.name not in kept_table_names]
    excluded_folded = _folded({t.name for t in excluded_tables})

    # ------------------------------------------- foreign keys that dangle
    final_tables: List[Table] = []
    for table in kept_tables:
        dangling = [
            c for c in table.constraints
            if c.kind == "FOREIGN KEY" and c.ref_table
            and c.ref_table.lower() not in kept_folded
        ]
        if not dangling:
            final_tables.append(table)
            continue
        trimmed = copy.copy(table)
        trimmed.constraints = [c for c in table.constraints if c not in dangling]
        final_tables.append(trimmed)
        for c in dangling:
            notes.append(
                f"{table.name}: foreign key {c.name} was left out -- it references "
                f"{c.ref_table}, which is not selected.")

    # ------------------------------------------------ triggers with no table
    kept_routines = []
    for routine in schema.routines:
        if keep_routines is not None and routine.name not in keep_routines:
            continue
        if routine.kind == "TRIGGER" and routine.table_name \
                and routine.table_name.lower() not in kept_folded:
            notes.append(
                f"Trigger {routine.name} was left out -- its table "
                f"{routine.table_name} is not selected.")
            continue
        kept_routines.append(routine)

    # ------------------------------------------- views over excluded tables
    kept_views = []
    for view in schema.views:
        if keep_views is not None and view.name not in keep_views:
            continue
        if excluded_folded and view.source_engine != "MongoDB":
            referenced = _table_references(view.definition or "") & excluded_folded
            if referenced:
                pretty = ", ".join(sorted(
                    t.name for t in excluded_tables if t.name.lower() in referenced))
                notes.append(
                    f"View {view.name} was left out -- it selects from {pretty}, "
                    f"which {'is' if len(referenced) == 1 else 'are'} not selected. "
                    f"Tick {pretty} as well if you want this view.")
                continue
        kept_views.append(view)

    subset = Schema(
        name=schema.name,
        source_engine=schema.source_engine,
        target_engine=schema.target_engine,
        tables=final_tables,
        views=kept_views,
        sequences=[s for s in schema.sequences
                   if keep_sequences is None or s.name in keep_sequences],
        routines=kept_routines,
    )
    return subset, notes


_IDENT = r"[`\"\[]?([A-Za-z_][A-Za-z_0-9$#]*)[`\"\]]?"
_TABLE_REF_RE = re.compile(
    r"\b(?:FROM|JOIN|INTO|UPDATE)\s+"
    r"(?:" + _IDENT + r"\s*\.\s*)?" + _IDENT,
    re.IGNORECASE)


def _table_references(sql: str) -> Set[str]:
    """The table names a statement actually reads from or writes to,
    lower-cased and unqualified.

    Only identifiers in a table position -- straight after FROM, JOIN,
    INTO or UPDATE -- are returned, so a column, alias or literal that
    happens to share a table's name is not mistaken for a dependency.
    That precision is what makes it safe to *exclude* a view on the
    strength of this rather than only warn about it.
    """
    found = set()
    for qualifier, name in _TABLE_REF_RE.findall(sql or ""):
        _ = qualifier
        found.add(name.lower())
    return found


def sync_conversion_results(subset: Schema, schema: Schema) -> None:
    """Copy conversion outcomes from a subset back onto the loaded schema.

    Almost every object in a subset *is* the object in `schema`, so this
    is a no-op for them. It matters for the tables that had a dangling
    foreign key trimmed: those are copies, and without this their status
    icon in the tree would still read as unconverted after a conversion
    that in fact processed them.
    """
    by_name = {t.name: t for t in schema.tables}
    for table in subset.tables:
        original = by_name.get(table.name)
        if original is None or original is table:
            continue
        original.status = table.status
        original.issues = list(table.issues)
