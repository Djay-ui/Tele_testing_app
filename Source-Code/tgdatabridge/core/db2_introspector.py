"""Reads Db2 (LUW)'s SYSCAT.* catalog views (Db2 has no ISO
information_schema at all -- SYSCAT is the native equivalent, the same
fallback target_introspector.introspect_target_db2 already uses for
object-name-only lookups) scoped to one schema, and builds a
tgdatabridge.core.schema_model.Schema object, for using Db2 as a *source* -- e.g.
a reverse migration back to Oracle, or a lateral one to MySQL/PostgreSQL/
SQL Server/a different Db2 database.

Scope matches the "schema/tables/views only" depth this tool already gives
MySQL/PostgreSQL/SQL Server as a source (see those modules' own docstrings
for the general design rationale, which this module mirrors closely):
tables/columns/constraints/indexes/sequences/views are introspected fully
and get real type mapping (through type_mapping.from_db2(), which
reverse-maps every native Db2 column type into this tool's shared
Oracle-flavored pivot representation, so the existing to_postgres/to_mysql/
to_sqlserver/to_db2 forward-mappers already know how to convert a
Db2-sourced column to any target with no changes needed on that side at
all). Stored procedures/functions/triggers are listed (name, kind, and
their native SQL PL source text for manual reference) but never parsed or
converted -- plsql_converter.convert_routine short-circuits to a
manual-conversion flag for any Routine whose source_engine isn't "Oracle".

Like Oracle/PostgreSQL/SQL Server (and unlike MySQL), Db2 constraint and
index names are unique within a schema by default, so no per-table
disambiguation is needed. Db2 also has native SEQUENCE objects
(introspected here), and IDENTITY columns are represented as
Column.identity = True (mirroring how an Oracle IDENTITY column is
represented) rather than a separate backing-sequence object.

Two things make Db2 triggers simpler to introspect than MySQL/PostgreSQL/
SQL Server's: a Db2 trigger is defined for exactly *one* firing event
(INSERT, UPDATE, or DELETE -- never a combined "INSERT OR UPDATE" the way
the other three engines allow), so SYSCAT.TRIGGERS already has one row per
trigger with no "one row per event" fan-out to handle; and Db2 genuinely
supports BEFORE triggers (unlike SQL Server, which only has AFTER/
INSTEAD OF), so TRIGTIME maps directly to Routine.timing with no
approximation needed.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from tgdatabridge.core import type_mapping
from tgdatabridge.core.schema_model import (
    Column, Constraint, Index, Routine, Schema, Sequence, Table, View,
)

if TYPE_CHECKING:
    from tgdatabridge.db.db2_connector import Db2Connector

_CONSTRAINT_KIND = {"P": "PRIMARY KEY", "U": "UNIQUE", "F": "FOREIGN KEY"}
_ROUTINE_KIND = {"F": "FUNCTION", "P": "PROCEDURE"}
_TRIGGER_TIMING = {"B": "BEFORE", "A": "AFTER", "I": "INSTEAD OF"}
_TRIGGER_EVENT = {"I": "INSERT", "U": "UPDATE", "D": "DELETE"}


def _fmt_data_type(row) -> str:
    """row: (typename, length, scale). Reconstructs a normal-looking Db2
    type string (e.g. "varchar(255)", "decimal(10,2)", "graphic(10)",
    "timestamp") for type_mapping.from_db2 to parse."""
    typename, length, scale = row
    name = (typename or "").lower()

    if name in ("char", "character", "varchar", "graphic", "vargraphic", "binary", "varbinary"):
        return f"{name}({int(length)})" if length else name

    if name in ("decimal", "numeric", "dec"):
        if length is not None:
            if scale:
                return f"{name}({int(length)},{int(scale)})"
            return f"{name}({int(length)})"
        return name

    if name == "decfloat" and length:
        return f"decfloat({int(length)})"

    return name


def introspect_schema(conn: "Db2Connector", schema_name: str, include_routines: bool = True) -> Schema:
    schema = Schema(name=schema_name, source_engine="Db2")

    tables_by_name: dict = {}
    for (table_name, remarks) in conn.execute(
        "SELECT TABNAME, REMARKS FROM SYSCAT.TABLES WHERE TABSCHEMA = %(schema)s AND TYPE = 'T' "
        "ORDER BY TABNAME",
        {"schema": schema_name},
    ):
        table = Table(name=table_name, schema=schema_name, comment=(str(remarks) if remarks else None))
        tables_by_name[table_name] = table
        schema.tables.append(table)

    for (table_name, col_name, type_name, length, scale, nulls, default, identity) in conn.execute(
        "SELECT TABNAME, COLNAME, TYPENAME, LENGTH, SCALE, NULLS, \"DEFAULT\", IDENTITY "
        "FROM SYSCAT.COLUMNS WHERE TABSCHEMA = %(schema)s ORDER BY TABNAME, COLNO",
        {"schema": schema_name},
    ):
        table = tables_by_name.get(table_name)
        if table is None:
            continue
        raw_type = _fmt_data_type((type_name, length, scale))
        pivot_type, mapping_issues = type_mapping.from_db2(raw_type)
        col = Column(
            name=col_name,
            data_type=pivot_type,
            nullable=(nulls == "Y"),
            default=(str(default) if default is not None else None),
            identity=(identity == "Y"),
            source_issues=mapping_issues,
        )
        table.columns.append(col)

    # --------------------------------------------------------- constraints
    # Like Oracle/PostgreSQL/SQL Server (and unlike MySQL), Db2 constraint
    # names are unique within a schema by default -- a bare constraint_name
    # key is enough, no per-table disambiguation needed.
    constraints_by_name: dict = {}
    for (cons_name, table_name, cons_type) in conn.execute(
        "SELECT CONSTNAME, TABNAME, TYPE FROM SYSCAT.TABCONST "
        "WHERE TABSCHEMA = %(schema)s AND TYPE IN ('P', 'U', 'F')",
        {"schema": schema_name},
    ):
        table = tables_by_name.get(table_name)
        if table is None:
            continue
        cons = Constraint(name=cons_name, kind=_CONSTRAINT_KIND.get(cons_type, cons_type))
        table.constraints.append(cons)
        constraints_by_name[cons_name] = cons

    for (cons_name, table_name, col_name, colseq) in conn.execute(
        "SELECT CONSTNAME, TABNAME, COLNAME, COLSEQ FROM SYSCAT.KEYCOLUSE "
        "WHERE TABSCHEMA = %(schema)s ORDER BY TABNAME, CONSTNAME, COLSEQ",
        {"schema": schema_name},
    ):
        cons = constraints_by_name.get(cons_name)
        if cons is not None and col_name:
            cons.columns.append(col_name)

    # Db2's SYSCAT.REFERENCES gives the referenced table name directly
    # (REFTABNAME), plus the name of the PK/UNIQUE constraint it references
    # (REFKEYNAME) -- simpler than the standard-SQL referential_constraints
    # join postgres_introspector/sqlserver_introspector need, since those
    # only give the referenced *constraint* name and require a second
    # lookup for its table.
    for (fk_name, ref_table, ref_key_name) in conn.execute(
        "SELECT CONSTNAME, REFTABNAME, REFKEYNAME FROM SYSCAT.REFERENCES WHERE TABSCHEMA = %(schema)s",
        {"schema": schema_name},
    ):
        fk = constraints_by_name.get(fk_name)
        ref_cons = constraints_by_name.get(ref_key_name)
        if fk is not None:
            fk.ref_table = ref_table
            if ref_cons is not None:
                fk.ref_columns = list(ref_cons.columns)

    for (cons_name, table_name, check_text) in conn.execute(
        "SELECT CONSTNAME, TABNAME, TEXT FROM SYSCAT.CHECKS WHERE TABSCHEMA = %(schema)s",
        {"schema": schema_name},
    ):
        table = tables_by_name.get(table_name)
        if table is None:
            continue
        table.constraints.append(Constraint(
            name=cons_name, kind="CHECK", check_condition=(str(check_text) if check_text else None),
        ))

    # -------------------------------------------------------------- indexes
    # No ISO information_schema view for indexes exists in Db2 either (same
    # gap as PostgreSQL/SQL Server) -- SYSCAT.INDEXES/SYSCAT.INDEXCOLUSE is
    # the native equivalent. UNIQUERULE is 'P' (backs a primary key), 'U'
    # (a genuine unique index), or 'D' (duplicates allowed, i.e. non-unique).
    indexes_by_key: dict = {}
    for (table_name, index_name, unique_rule, col_name) in conn.execute(
        "SELECT i.TABNAME, i.INDNAME, i.UNIQUERULE, ic.COLNAME "
        "FROM SYSCAT.INDEXES i "
        "JOIN SYSCAT.INDEXCOLUSE ic ON ic.INDSCHEMA = i.INDSCHEMA AND ic.INDNAME = i.INDNAME "
        "WHERE i.TABSCHEMA = %(schema)s ORDER BY i.TABNAME, i.INDNAME, ic.COLSEQ",
        {"schema": schema_name},
    ):
        table = tables_by_name.get(table_name)
        if table is None:
            continue
        key = (table_name, index_name)
        idx = indexes_by_key.get(key)
        if idx is None:
            # Db2, like PostgreSQL/SQL Server, names a PK/UNIQUE
            # constraint's backing index the same as the constraint itself
            # by default -- no renaming needed for ddl_generator's existing
            # by-name dedup.
            idx = Index(name=index_name, columns=[], unique=(unique_rule in ("P", "U")))
            table.indexes.append(idx)
            indexes_by_key[key] = idx
        if col_name:
            idx.columns.append(col_name)

    # ------------------------------------------------------------ sequences
    for (seq_name, start_value, increment, min_value, max_value, cycle) in conn.execute(
        "SELECT SEQNAME, START, INCREMENT, MINVALUE, MAXVALUE, CYCLE "
        "FROM SYSCAT.SEQUENCES WHERE SEQSCHEMA = %(schema)s ORDER BY SEQNAME",
        {"schema": schema_name},
    ):
        schema.sequences.append(Sequence(
            name=seq_name, schema=schema_name,
            start_value=int(start_value) if start_value is not None else 1,
            increment_by=int(increment) if increment is not None else 1,
            min_value=int(min_value) if min_value is not None else None,
            max_value=int(max_value) if max_value is not None else None,
            cycle=(cycle == "Y"),
        ))

    # ---------------------------------------------------------------- views
    for (view_name, view_text) in conn.execute(
        "SELECT VIEWNAME, TEXT FROM SYSCAT.VIEWS WHERE VIEWSCHEMA = %(schema)s ORDER BY VIEWNAME",
        {"schema": schema_name},
    ):
        schema.views.append(View(
            name=view_name, schema=schema_name, definition=str(view_text or ""), source_engine="Db2",
        ))

    # ------------------------------------------------------------- routines
    if include_routines:
        for (name, routine_type, routine_text) in conn.execute(
            "SELECT ROUTINENAME, ROUTINETYPE, TEXT FROM SYSCAT.ROUTINES "
            "WHERE ROUTINESCHEMA = %(schema)s ORDER BY ROUTINENAME",
            {"schema": schema_name},
        ):
            schema.routines.append(Routine(
                name=name, schema=schema_name,
                kind=_ROUTINE_KIND.get(routine_type, routine_type),
                source=str(routine_text or ""), source_engine="Db2",
            ))

        # A Db2 trigger is defined for exactly one firing event -- unlike
        # MySQL/PostgreSQL/SQL Server, SYSCAT.TRIGGERS already has one row
        # per trigger, no "one row per event" fan-out to collapse or expand.
        for (trig_name, table_name, trig_time, trig_event, trig_text) in conn.execute(
            "SELECT TRIGNAME, TABNAME, TRIGTIME, TRIGEVENT, TEXT "
            "FROM SYSCAT.TRIGGERS WHERE TRIGSCHEMA = %(schema)s ORDER BY TRIGNAME",
            {"schema": schema_name},
        ):
            schema.routines.append(Routine(
                name=trig_name, schema=schema_name, kind="TRIGGER",
                source=str(trig_text or ""), source_engine="Db2",
                table_name=table_name,
                timing=_TRIGGER_TIMING.get(trig_time, "AFTER"),
                events=[_TRIGGER_EVENT.get(trig_event, "INSERT")],
                row_level=True,
            ))

    return schema
