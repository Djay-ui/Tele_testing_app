"""Compares the loaded Oracle *source* Schema against an already-
introspected *target* (tgdatabridge.core.target_introspector.TargetObjects) and
reports, per object category, what's only in the source (not migrated
yet), only on the target (created outside this tool, or left over from a
prior/different schema), or present in both -- so an iterative or
incremental conversion run can see at a glance what's still outstanding
without re-reading the full DDL or report.

Name matching is case-insensitive throughout: every supported target
engine folds unquoted identifiers to a fixed case of its own rather than
preserving whatever case the source used (Db2/Oracle -> upper, PostgreSQL
-> lower; see the _quote_* helpers in ddl_generator.py for exactly what
each engine does), so a naive case-sensitive comparison would report
spurious mismatches for objects that actually converted cleanly.

Routines need extra care because their target-side shape isn't always a
plain 1:1 name mapping:
  - A PACKAGE spec produces no target object at all (see
    plsql_converter.convert_routine's PACKAGE branch: it's emitted only
    as a comment). It's excluded from the "expected on target" set
    entirely so it never shows up as a false "only in source" entry.
  - A PACKAGE BODY is flattened into one routine per member, named
    "<package>_<member>" (see convert_package_body and its tsql/db2
    equivalents) -- the same member-name extraction is repeated here so
    the diff compares against the actual flattened names, not the
    package's own name.
  - A multi-event TRIGGER is split into one CREATE TRIGGER per event only
    for a Db2 target (Db2 has no combined-event trigger syntax -- see
    db2_converter.convert_trigger); PostgreSQL and SQL Server keep a
    single combined-event trigger, so only the Db2 case expands into
    "<trigger>_<EVENT>" names here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List

from tgdatabridge.core.schema_model import Schema
from tgdatabridge.core.target_introspector import TargetObjects

_MEMBER_HEADER_RE = re.compile(r"\b(?:PROCEDURE|FUNCTION)\s+([A-Za-z_][\w$#]*)", re.IGNORECASE)


@dataclass
class CategoryDiff:
    label: str
    only_in_source: List[str] = field(default_factory=list)
    only_in_target: List[str] = field(default_factory=list)
    in_both: List[str] = field(default_factory=list)

    @property
    def in_sync(self) -> bool:
        return not self.only_in_source and not self.only_in_target


@dataclass
class SchemaDiff:
    tables: CategoryDiff
    views: CategoryDiff
    sequences: CategoryDiff
    routines: CategoryDiff

    def categories(self) -> List[CategoryDiff]:
        return [self.tables, self.views, self.sequences, self.routines]

    @property
    def fully_in_sync(self) -> bool:
        return all(c.in_sync for c in self.categories())


def _expected_routine_names(schema: Schema) -> List[str]:
    """The routine/trigger names actually expected to land on the target,
    given how plsql_converter/tsql_converter/db2_converter reshape
    packages and multi-event triggers (see module docstring)."""
    engine = schema.target_engine.lower().replace(" ", "")
    is_db2 = engine.startswith("db2")
    # MySQL allows one event per trigger too, so its converters split a
    # multi-event trigger the same way Db2's does. Expecting only the base
    # name reported one false "only in source" plus one false "only in
    # target" per event, on every migrated multi-event trigger.
    splits_events = is_db2 or engine.startswith(("mysql", "mariadb", "sqlserver"))
    # A PostgreSQL trigger's body lives in a companion function that the
    # converter creates as `<name>_fn`. It is a real object in
    # information_schema, so the target introspector sees it -- and without
    # this every single migrated trigger produced a spurious "only in
    # target" entry for it.
    is_postgres = engine.startswith("postgres")
    names: List[str] = []
    for routine in schema.routines:
        if routine.kind == "TRIGGER" and is_postgres:
            names.append(f"{routine.name}_fn")
        if routine.kind == "PACKAGE":
            continue  # spec-only; no target object is ever created for it
        if routine.kind == "PACKAGE BODY":
            members = _MEMBER_HEADER_RE.findall(routine.source)
            if members:
                names.extend(f"{routine.name}_{member}" for member in members)
            else:
                # No PROCEDURE/FUNCTION header found -- convert_package_body
                # itself falls back to a manual-conversion comment block in
                # this case, so there's nothing meaningful to expect on the
                # target under a flattened name; fall back to the package's
                # own name so it still shows up as outstanding rather than
                # silently disappearing from the diff.
                names.append(routine.name)
            continue
        if routine.kind == "TRIGGER" and splits_events and len(routine.events) > 1:
            names.extend(f"{routine.name}_{event}" for event in routine.events)
            continue
        names.append(routine.name)
    return names


def _diff_category(label: str, source_names: List[str], target_names: List[str]) -> CategoryDiff:
    # Case-insensitive dedup/lookup, keeping the first-seen original
    # casing on each side for display purposes.
    source_by_key = {}
    for name in source_names:
        source_by_key.setdefault(name.upper(), name)
    target_by_key = {}
    for name in target_names:
        target_by_key.setdefault(name.upper(), name)

    source_keys = set(source_by_key)
    target_keys = set(target_by_key)

    only_source = sorted(source_by_key[k] for k in source_keys - target_keys)
    only_target = sorted(target_by_key[k] for k in target_keys - source_keys)
    both = sorted(source_by_key[k] for k in source_keys & target_keys)
    return CategoryDiff(label=label, only_in_source=only_source, only_in_target=only_target, in_both=both)


def compute_diff(schema: Schema, target: TargetObjects) -> SchemaDiff:
    """Case-insensitive name diff between what's loaded from the Oracle
    source (`schema`) and what's actually on the target right now
    (`target`, from target_introspector.introspect_target)."""
    return SchemaDiff(
        tables=_diff_category("Tables", [t.name for t in schema.tables], target.tables),
        views=_diff_category("Views", [v.name for v in schema.views], target.views),
        sequences=_diff_category("Sequences", [s.name for s in schema.sequences], target.sequences),
        routines=_diff_category("Routines / Triggers", _expected_routine_names(schema), target.routines),
    )
