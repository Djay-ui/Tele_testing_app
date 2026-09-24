"""Reads SQL Server's information_schema (plus sys.* catalog views for the
things the ISO information_schema doesn't expose on SQL Server: index
column lists, sequence definitions, and trigger metadata/source -- the
same gap target_introspector.introspect_target_sqlserver already works
around for object-name-only lookups) scoped to one schema, and builds a
tgdatabridge.core.schema_model.Schema object, for using SQL Server as a *source*
-- e.g. a reverse migration back to Oracle, or a lateral one to MySQL/
PostgreSQL/Db2/a different SQL Server database.

Scope matches the "schema/tables/views only" depth this tool already gives
MySQL/PostgreSQL as a source (see mysql_introspector.py and
postgres_introspector.py's own docstrings for the general design
rationale, which this module mirrors closely): tables/columns/
constraints/indexes/sequences/views are introspected fully and get real
type mapping (through type_mapping.from_sqlserver(), which reverse-maps
every native SQL Server column type into this tool's shared
Oracle-flavored pivot representation, so the existing to_postgres/to_mysql/
to_sqlserver/to_db2 forward-mappers already know how to convert a SQL
Server-sourced column to any target with no changes needed on that side at
all). Stored procedures/functions/triggers are listed (name, kind, and
their native T-SQL source text for manual reference) but never parsed or
converted -- plsql_converter.convert_routine short-circuits to a
manual-conversion flag for any Routine whose source_engine isn't "Oracle".

Like PostgreSQL (and unlike MySQL), SQL Server constraint and index names
are unique within a schema by default, so no per-table disambiguation is
needed the way mysql_introspector needs for MySQL's always-literally-
"PRIMARY" naming. SQL Server also has native SEQUENCE objects (2012+,
introspected here), and IDENTITY columns are represented as
Column.identity = True (mirroring how an Oracle IDENTITY column is
represented) rather than a separate backing-sequence object.

SQL Server triggers have no true "BEFORE" timing (only AFTER and INSTEAD
OF) and no ISO information_schema.triggers view at all -- both read here
from sys.triggers/sys.trigger_events/sys.sql_modules instead, the same
sys.* fallback target_introspector.introspect_target_sqlserver already
uses just for trigger *names*.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from tgdatabridge.core import type_mapping
from tgdatabridge.core.schema_model import (
    Column, Constraint, Index, Routine, Schema, Sequence, Table, View,
)

if TYPE_CHECKING:
    from tgdatabridge.db.sqlserver_connector import SqlServerConnector

_UNICODE_TEXT_TYPES = {"nchar", "nvarchar"}


def _fmt_data_type(row) -> str:
    """row: (type_name, max_length, precision, scale). Reconstructs a
    normal-looking SQL Server type string (e.g. "varchar(255)",
    "nvarchar(100)", "decimal(10,2)", "varbinary(max)", "datetime2") for
    type_mapping.from_sqlserver to parse. sys.columns.max_length is in
    *bytes*, not characters -- nchar/nvarchar store UTF-16 (2 bytes/char),
    so their reported max_length is halved back into a character count
    here; a max_length of -1 means MAX for any of the "(n)"-sized types."""
    type_name, max_length, precision, scale = row
    name = (type_name or "").lower()

    if name in ("varchar", "char", "varbinary", "binary"):
        if max_length is None:
            return name
        if max_length == -1:
            return f"{name}(max)"
        return f"{name}({int(max_length)})"

    if name in _UNICODE_TEXT_TYPES:
        if max_length is None:
            return name
        if max_length == -1:
            return f"{name}(max)"
        return f"{name}({int(max_length) // 2})"

    if name in ("decimal", "numeric"):
        if precision is not None:
            if scale:
                return f"{name}({int(precision)},{int(scale)})"
            return f"{name}({int(precision)})"
        return name

    if name in ("datetime2", "time", "datetimeoffset") and precision is not None:
        return f"{name}({int(precision)})"

    return name


def _is_serial_identity(is_identity) -> bool:
    return bool(is_identity)


def introspect_schema(conn: "SqlServerConnector", schema_name: str, include_routines: bool = True) -> Schema:
    schema = Schema(name=schema_name, source_engine="SQL Server")

    tables_by_name: dict = {}
    for (table_name,) in conn.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = %(schema)s AND table_type = 'BASE TABLE' "
        "ORDER BY table_name",
        {"schema": schema_name},
    ):
        table = Table(name=table_name, schema=schema_name)
        tables_by_name[table_name] = table
        schema.tables.append(table)

    for (table_name, col_name, type_name, max_length, precision, scale,
         nullable, is_identity, default_def) in conn.execute(
        "SELECT t.name, c.name, ty.name, c.max_length, c.precision, c.scale, "
        "c.is_nullable, c.is_identity, dc.definition "
        "FROM sys.columns c "
        "JOIN sys.tables t ON t.object_id = c.object_id "
        "JOIN sys.types ty ON ty.user_type_id = c.user_type_id "
        "JOIN sys.schemas s ON s.schema_id = t.schema_id "
        "LEFT JOIN sys.default_constraints dc ON dc.object_id = c.default_object_id "
        "WHERE s.name = %(schema)s ORDER BY t.name, c.column_id",
        {"schema": schema_name},
    ):
        table = tables_by_name.get(table_name)
        if table is None:
            continue
        raw_type = _fmt_data_type((type_name, max_length, precision, scale))
        pivot_type, mapping_issues = type_mapping.from_sqlserver(raw_type)
        col = Column(
            name=col_name,
            data_type=pivot_type,
            nullable=bool(nullable),
            default=(str(default_def) if default_def is not None else None),
            identity=_is_serial_identity(is_identity),
            source_issues=mapping_issues,
        )
        table.columns.append(col)

    # --------------------------------------------------------- constraints
    # Like PostgreSQL (and unlike MySQL), SQL Server constraint names are
    # unique within a schema by default -- a bare constraint_name key is
    # enough, no per-table disambiguation needed.
    constraints_by_name: dict = {}
    constraint_table: dict = {}
    for (cons_name, table_name, cons_type) in conn.execute(
        "SELECT constraint_name, table_name, constraint_type FROM information_schema.table_constraints "
        "WHERE table_schema = %(schema)s AND constraint_type IN ('PRIMARY KEY', 'UNIQUE', 'FOREIGN KEY')",
        {"schema": schema_name},
    ):
        table = tables_by_name.get(table_name)
        if table is None:
            continue
        cons = Constraint(name=cons_name, kind=cons_type)
        table.constraints.append(cons)
        constraints_by_name[cons_name] = cons
        constraint_table[cons_name] = table_name

    for (cons_name, table_name, col_name, position) in conn.execute(
        "SELECT constraint_name, table_name, column_name, ordinal_position "
        "FROM information_schema.key_column_usage WHERE table_schema = %(schema)s "
        "ORDER BY table_name, constraint_name, ordinal_position",
        {"schema": schema_name},
    ):
        cons = constraints_by_name.get(cons_name)
        if cons is not None and col_name:
            cons.columns.append(col_name)

    # Standard-SQL FK resolution, same approach as postgres_introspector:
    # referential_constraints links a FOREIGN KEY constraint to the *name*
    # of the PRIMARY KEY/UNIQUE constraint it references.
    for (fk_name, unique_cons_name) in conn.execute(
        "SELECT constraint_name, unique_constraint_name FROM information_schema.referential_constraints "
        "WHERE constraint_schema = %(schema)s",
        {"schema": schema_name},
    ):
        fk = constraints_by_name.get(fk_name)
        ref_cons = constraints_by_name.get(unique_cons_name)
        if fk is not None and ref_cons is not None:
            fk.ref_table = constraint_table.get(unique_cons_name)
            fk.ref_columns = list(ref_cons.columns)

    for (cons_name, table_name, check_clause) in conn.execute(
        "SELECT tc.constraint_name, tc.table_name, cc.check_clause "
        "FROM information_schema.table_constraints tc "
        "JOIN information_schema.check_constraints cc "
        "  ON cc.constraint_schema = tc.constraint_schema AND cc.constraint_name = tc.constraint_name "
        "WHERE tc.table_schema = %(schema)s AND tc.constraint_type = 'CHECK'",
        {"schema": schema_name},
    ):
        table = tables_by_name.get(table_name)
        if table is None:
            continue
        table.constraints.append(Constraint(
            name=cons_name, kind="CHECK", check_condition=(str(check_clause) if check_clause else None),
        ))

    # -------------------------------------------------------------- indexes
    # No ISO information_schema view for indexes exists in SQL Server
    # either (same gap as PostgreSQL) -- sys.indexes/sys.index_columns is
    # the native equivalent. i.type > 0 excludes heaps (tables with no
    # clustered index have a "phantom" index row with a NULL name).
    indexes_by_key: dict = {}
    for (table_name, index_name, is_unique, col_name) in conn.execute(
        "SELECT t.name, i.name, i.is_unique, c.name "
        "FROM sys.indexes i "
        "JOIN sys.tables t ON t.object_id = i.object_id "
        "JOIN sys.schemas s ON s.schema_id = t.schema_id "
        "JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id "
        "JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id "
        "WHERE s.name = %(schema)s AND i.name IS NOT NULL AND i.type > 0 "
        "ORDER BY t.name, i.name, ic.key_ordinal",
        {"schema": schema_name},
    ):
        table = tables_by_name.get(table_name)
        if table is None:
            continue
        key = (table_name, index_name)
        idx = indexes_by_key.get(key)
        if idx is None:
            # SQL Server, like PostgreSQL, names a PK/UNIQUE constraint's
            # backing index the same as the constraint itself by default --
            # no renaming needed for ddl_generator's existing by-name dedup.
            idx = Index(name=index_name, columns=[], unique=bool(is_unique))
            table.indexes.append(idx)
            indexes_by_key[key] = idx
        if col_name:
            idx.columns.append(col_name)

    # ------------------------------------------------------------ sequences
    for (seq_name, start_value, increment, min_value, max_value, is_cycling) in conn.execute(
        "SELECT s.name, s.start_value, s.increment, s.minimum_value, s.maximum_value, s.is_cycling "
        "FROM sys.sequences s WHERE SCHEMA_NAME(s.schema_id) = %(schema)s ORDER BY s.name",
        {"schema": schema_name},
    ):
        schema.sequences.append(Sequence(
            name=seq_name, schema=schema_name,
            start_value=int(start_value) if start_value is not None else 1,
            increment_by=int(increment) if increment is not None else 1,
            min_value=int(min_value) if min_value is not None else None,
            max_value=int(max_value) if max_value is not None else None,
            cycle=bool(is_cycling),
        ))

    # ---------------------------------------------------------------- views
    for (view_name, view_definition) in conn.execute(
        "SELECT table_name, view_definition FROM information_schema.views "
        "WHERE table_schema = %(schema)s ORDER BY table_name",
        {"schema": schema_name},
    ):
        # SQL Server's view_definition is the *whole* original statement,
        # header included -- unlike Oracle/MySQL/PostgreSQL, which return
        # only the SELECT that View.definition is documented to hold. Left
        # as-is it produces "CREATE OR REPLACE VIEW x AS CREATE VIEW x AS
        # SELECT ..." on the target: error 1064, a syntax error at the
        # second CREATE. See tsql_dialect.strip_create_view_header.
        from tgdatabridge.core.tsql_dialect import strip_create_view_header
        schema.views.append(View(
            name=view_name, schema=schema_name,
            definition=strip_create_view_header(str(view_definition or "")),
            source_engine="SQL Server",
        ))

    # ------------------------------------------------------------- routines
    if include_routines:
        for (name, routine_type, definition) in conn.execute(
            "SELECT routine_name, routine_type, routine_definition "
            "FROM information_schema.routines WHERE routine_schema = %(schema)s "
            "ORDER BY routine_name",
            {"schema": schema_name},
        ):
            schema.routines.append(Routine(
                name=name, schema=schema_name, kind=routine_type,
                source=str(definition or ""), source_engine="SQL Server",
            ))

        # No information_schema.triggers on SQL Server -- sys.triggers +
        # sys.trigger_events (one row per firing event) + sys.sql_modules
        # for the actual CREATE TRIGGER source text.
        #
        # The event rows are collapsed into ONE Routine per trigger. They
        # used to be passed through as one Routine each, all under the same
        # name and each carrying a byte-identical copy of the full CREATE
        # text -- so a three-event trigger was converted three times, wrote
        # three identical blocks into the DDL script, was counted as three
        # objects in the assessment, and produced three rollback DROPs.
        by_name: dict = {}
        for (trig_name, table_name, is_instead_of, definition, event_type) in conn.execute(
            "SELECT tr.name, t.name, tr.is_instead_of_trigger, sm.definition, te.type_desc "
            "FROM sys.triggers tr "
            "JOIN sys.tables t ON t.object_id = tr.parent_id "
            "JOIN sys.schemas s ON s.schema_id = t.schema_id "
            "JOIN sys.sql_modules sm ON sm.object_id = tr.object_id "
            "JOIN sys.trigger_events te ON te.object_id = tr.object_id "
            "WHERE s.name = %(schema)s AND tr.parent_class = 1 "
            "ORDER BY tr.name, te.type_desc",
            {"schema": schema_name},
        ):
            event_name = str(event_type or "INSERT").upper()
            existing = by_name.get(trig_name)
            if existing is not None:
                if event_name not in existing.events:
                    existing.events.append(event_name)
                continue
            routine = Routine(
                name=trig_name, schema=schema_name, kind="TRIGGER",
                source=str(definition or ""), source_engine="SQL Server",
                table_name=table_name,
                # SQL Server has no true BEFORE trigger -- only AFTER (the
                # default) and INSTEAD OF.
                timing=("INSTEAD OF" if is_instead_of else "AFTER"),
                events=[event_name],
                # SQL Server has no per-row/per-statement trigger distinction
                # in T-SQL itself (every trigger fires once per statement and
                # operates against the whole Inserted/Deleted tables) -- kept
                # True for consistency with the row_level field's meaning
                # elsewhere, since ddl_generator/tsql_converter don't branch
                # on it for SQL Server anyway.
                row_level=True,
            )
            by_name[trig_name] = routine
            schema.routines.append(routine)

    return schema
