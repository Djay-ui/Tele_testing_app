"""Reads PostgreSQL's information_schema (plus a handful of pg_catalog
queries for the things the ISO information_schema doesn't expose -- table
comments and index column lists) scoped to one schema, and builds a
tgdatabridge.core.schema_model.Schema object, for using PostgreSQL as a *source*
-- e.g. a reverse migration back to Oracle, or a lateral one to MySQL/
SQL Server/Db2/a different PostgreSQL database.

Scope matches the "schema/tables/views only" depth this tool already gives
MySQL as a source (see mysql_introspector.py's own docstring for the
general design rationale, which this module mirrors closely): tables/
columns/constraints/indexes/sequences/views are introspected fully and get
real type mapping (through type_mapping.from_postgres(), which
reverse-maps every native PostgreSQL column type into this tool's shared
Oracle-flavored pivot representation, so the existing to_postgres/to_mysql/
to_sqlserver/to_db2 forward-mappers already know how to convert a
PostgreSQL-sourced column to any target with no changes needed on that
side at all). Stored routines and triggers are listed (name, kind, and
their native PL/pgSQL source text for manual reference) but never parsed
or converted -- plsql_converter.convert_routine short-circuits to a
manual-conversion flag for any Routine whose source_engine isn't "Oracle".

Unlike MySQL, PostgreSQL constraint (and index) names *are* unique within
a schema by default, so -- unlike mysql_introspector's PRIMARY-name
disambiguation -- constraints/indexes here are simply keyed by their own
name with no per-table renaming needed. PostgreSQL also has native
SEQUENCE objects (introspected here, unlike MySQL which has none), and
`SERIAL`/`BIGSERIAL`/`GENERATED ... AS IDENTITY` columns are represented
as Column.identity = True (mirroring how an Oracle IDENTITY column is
represented) rather than surfacing the backing sequence as a second,
separate Sequence object.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

from tgdatabridge.core import type_mapping
from tgdatabridge.core.schema_model import (
    Column, Constraint, Index, Routine, RoutineParameter, Schema, Sequence, Table, View,
)

if TYPE_CHECKING:
    from tgdatabridge.db.postgres_connector import PostgresConnector

_NOT_NULL_CHECK_RE = re.compile(r'^"?[A-Za-z_][\w$]*"?\s+IS\s+NOT\s+NULL$', re.IGNORECASE)


def _fmt_data_type(row) -> str:
    """row: (data_type, udt_name, character_maximum_length,
    numeric_precision, numeric_scale, interval_type). Reconstructs a
    normal-looking PostgreSQL type string (e.g. "character varying(255)",
    "numeric(10,2)", "timestamp with time zone", "integer[]",
    "some_enum_type") for type_mapping.from_postgres to parse."""
    dtype, udt_name, char_len, num_precision, num_scale, interval_type = row
    dtype_lower = (dtype or "").lower()

    if dtype_lower == "array":
        # udt_name for an array column is the element type's internal name
        # prefixed with "_", e.g. "_int4" for integer[], "_text" for text[].
        return f"{(udt_name or '').lstrip('_')}[]"

    if dtype_lower == "user-defined":
        # Enums and other CREATE TYPE-defined types report udt_name as the
        # type's own name (e.g. "mood") with no further structure available
        # from information_schema alone -- from_postgres() won't recognize
        # it and will fall through to its generic "no mapping rule" case,
        # same as any other type this tool doesn't know how to reverse-map.
        return udt_name or "user-defined"

    if dtype_lower in ("character varying", "varchar") and char_len:
        return f"character varying({int(char_len)})"
    if dtype_lower in ("character", "char", "bpchar") and char_len:
        return f"character({int(char_len)})"

    if dtype_lower in ("numeric", "decimal"):
        if num_precision is not None:
            if num_scale:
                return f"numeric({int(num_precision)},{int(num_scale)})"
            return f"numeric({int(num_precision)})"
        return "numeric"

    if dtype_lower == "bit" and char_len:
        return f"bit({int(char_len)})"
    if dtype_lower in ("bit varying", "varbit") and char_len:
        return f"bit varying({int(char_len)})"

    if dtype_lower == "interval":
        return f"interval {interval_type.lower()}" if interval_type else "interval"

    return dtype_lower


def introspect_schema(conn: "PostgresConnector", schema_name: str, include_routines: bool = True) -> Schema:
    schema = Schema(name=schema_name, source_engine="PostgreSQL")

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

    # Table comments have no information_schema equivalent -- best-effort
    # only, via pg_catalog; tolerated entirely missing (e.g. insufficient
    # privileges on pg_description in some managed-Postgres setups) rather
    # than failing the whole schema load over a purely cosmetic detail.
    try:
        for (table_name, comment) in conn.execute(
            "SELECT c.relname, obj_description(c.oid, 'pg_class') FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = %(schema)s AND c.relkind = 'r'",
            {"schema": schema_name},
        ):
            table = tables_by_name.get(table_name)
            if table is not None and comment:
                table.comment = str(comment)
    except Exception:
        pass

    for (table_name, col_name, dtype, udt_name, char_len, num_precision, num_scale,
         interval_type, nullable, default, is_identity) in conn.execute(
        "SELECT table_name, column_name, data_type, udt_name, character_maximum_length, "
        "numeric_precision, numeric_scale, interval_type, is_nullable, column_default, "
        "is_identity FROM information_schema.columns "
        "WHERE table_schema = %(schema)s ORDER BY table_name, ordinal_position",
        {"schema": schema_name},
    ):
        table = tables_by_name.get(table_name)
        if table is None:
            continue
        raw_type = _fmt_data_type((dtype, udt_name, char_len, num_precision, num_scale, interval_type))
        pivot_type, mapping_issues = type_mapping.from_postgres(raw_type)
        # SERIAL/BIGSERIAL/SMALLSERIAL are just sugar for an integer column
        # with a `nextval('...')` default -- there's no separate "serial"
        # data_type reported here, so identity has to be detected either
        # from a real `GENERATED ... AS IDENTITY` column (is_identity =
        # 'YES') or from that nextval(...) default convention.
        is_serial_default = bool(default) and str(default).lower().startswith("nextval(")
        col = Column(
            name=col_name,
            data_type=pivot_type,
            nullable=(nullable == "YES"),
            default=(str(default) if default is not None and not is_serial_default else None),
            identity=(is_identity == "YES" or is_serial_default),
            source_issues=mapping_issues,
        )
        table.columns.append(col)

    # --------------------------------------------------------- constraints
    # Unlike MySQL, PostgreSQL constraint names are unique within a schema
    # by default, so (unlike mysql_introspector) a bare constraint_name key
    # is enough -- no per-table disambiguation needed.
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

    # Standard-SQL FK resolution: referential_constraints links a FOREIGN
    # KEY constraint to the *name* of the PRIMARY KEY/UNIQUE constraint it
    # references (unique_constraint_name); the referenced table/columns are
    # then whatever that constraint's own row above already collected --
    # unlike MySQL's information_schema, PostgreSQL's key_column_usage has
    # no referenced_table_name/referenced_column_name columns of its own.
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

    # CHECK constraints -- filters out the auto-generated "<col> IS NOT
    # NULL" checks PostgreSQL 12+ reports here for every NOT NULL column
    # (an implementation detail, not a real, hand-authored CHECK); this is
    # a heuristic (matching the plain single-column "<col> IS NOT NULL"
    # shape) since check_constraints alone doesn't flag these specially.
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
        clause = str(check_clause) if check_clause else ""
        if _NOT_NULL_CHECK_RE.match(clause.strip()):
            continue
        table.constraints.append(Constraint(name=cons_name, kind="CHECK", check_condition=clause or None))

    # -------------------------------------------------------------- indexes
    # No ISO information_schema view for indexes exists in PostgreSQL (that
    # part of the standard is a MySQL-only extension) -- pg_catalog is the
    # only way to get index column lists.
    indexes_by_key: dict = {}
    for (table_name, index_name, is_unique, col_name) in conn.execute(
        "SELECT t.relname, i.relname, ix.indisunique, a.attname "
        "FROM pg_index ix "
        "JOIN pg_class t ON t.oid = ix.indrelid "
        "JOIN pg_class i ON i.oid = ix.indexrelid "
        "JOIN pg_namespace n ON n.oid = t.relnamespace "
        "JOIN LATERAL unnest(ix.indkey) WITH ORDINALITY AS k(attnum, ord) ON true "
        "JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum "
        "WHERE n.nspname = %(schema)s ORDER BY t.relname, i.relname, k.ord",
        {"schema": schema_name},
    ):
        table = tables_by_name.get(table_name)
        if table is None:
            continue
        key = (table_name, index_name)
        idx = indexes_by_key.get(key)
        if idx is None:
            # PostgreSQL always names a PK/UNIQUE constraint's backing index
            # the same as the constraint itself, so no renaming is needed
            # here for ddl_generator's existing "skip an index that's
            # really just the PK/UNIQUE constraint" dedup logic (which
            # matches by name within the same table) to recognize them as
            # the same object -- unlike mysql_introspector, which has to
            # rename MySQL's always-literally-"PRIMARY" index to match.
            idx = Index(name=index_name, columns=[], unique=bool(is_unique))
            table.indexes.append(idx)
            indexes_by_key[key] = idx
        if col_name:
            idx.columns.append(col_name)

    # ------------------------------------------------------------ sequences
    for (seq_name, start_value, increment, min_value, max_value, cycle_option) in conn.execute(
        "SELECT sequence_name, start_value, increment, minimum_value, maximum_value, cycle_option "
        "FROM information_schema.sequences WHERE sequence_schema = %(schema)s ORDER BY sequence_name",
        {"schema": schema_name},
    ):
        schema.sequences.append(Sequence(
            name=seq_name, schema=schema_name,
            start_value=int(start_value) if start_value is not None else 1,
            increment_by=int(increment) if increment is not None else 1,
            min_value=int(min_value) if min_value is not None else None,
            max_value=int(max_value) if max_value is not None else None,
            cycle=(str(cycle_option).upper() == "YES"),
        ))

    # ---------------------------------------------------------------- views
    for (view_name, view_definition) in conn.execute(
        "SELECT table_name, view_definition FROM information_schema.views "
        "WHERE table_schema = %(schema)s ORDER BY table_name",
        {"schema": schema_name},
    ):
        schema.views.append(View(
            name=view_name, schema=schema_name, definition=str(view_definition or ""), source_engine="PostgreSQL",
        ))

    # ------------------------------------------------------------- routines
    if include_routines:
        # pg_proc, not information_schema.routines. Two reasons: the
        # information_schema view hides any function returning `trigger`
        # (so every trigger function was invisible), and it has no
        # parameter list -- ROUTINE_DEFINITION is the body alone, which
        # left a PostgreSQL-sourced function with nothing to put between
        # its parentheses on any target.
        bodies: dict = {}
        for (name, kind, definition, arguments, result) in conn.execute(
            "SELECT p.proname, "
            "       CASE WHEN p.prokind = 'p' THEN 'PROCEDURE' ELSE 'FUNCTION' END, "
            "       p.prosrc, "
            "       pg_catalog.pg_get_function_arguments(p.oid), "
            "       pg_catalog.pg_get_function_result(p.oid) "
            "  FROM pg_catalog.pg_proc p "
            "  JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace "
            " WHERE n.nspname = %(schema)s AND p.prokind IN ('f', 'p') "
            " ORDER BY p.proname",
            {"schema": schema_name},
        ):
            bodies[name] = str(definition or "")
            parameters = _parse_pg_arguments(str(arguments or ""))
            returns = str(result or "").strip()
            if returns.lower() == "trigger":
                # A trigger function is not a routine anyone calls; it is
                # the body of whatever trigger references it, and is
                # attached to that trigger below. Listing it separately
                # would migrate it twice and show it in the object tree as
                # a function the user never wrote.
                continue
            schema.routines.append(Routine(
                name=name, schema=schema_name, kind=kind,
                source=str(definition or ""), source_engine="PostgreSQL",
                parameters=parameters, return_type=returns or None,
            ))

        # One Routine per trigger, with the body of the function it runs.
        # information_schema.triggers has a row per firing event, and its
        # action_statement is only "EXECUTE FUNCTION foo()" -- so a
        # PostgreSQL trigger used to arrive three times over, each carrying
        # no logic at all. The function name is read from pg_trigger and
        # its body looked up in `bodies` above.
        by_name: dict = {}
        for (trig_name, table_name, event, timing, orientation, function_name) in conn.execute(
            "SELECT t.trigger_name, t.event_object_table, t.event_manipulation, "
            "       t.action_timing, t.action_orientation, p.proname "
            "  FROM information_schema.triggers t "
            "  LEFT JOIN pg_catalog.pg_trigger g "
            "         ON g.tgname = t.trigger_name "
            "  LEFT JOIN pg_catalog.pg_proc p ON p.oid = g.tgfoid "
            " WHERE t.trigger_schema = %(schema)s ORDER BY t.trigger_name",
            {"schema": schema_name},
        ):
            event_name = (event or "INSERT").upper()
            existing = by_name.get(trig_name)
            if existing is not None:
                if event_name not in existing.events:
                    existing.events.append(event_name)
                continue
            routine = Routine(
                name=trig_name, schema=schema_name, kind="TRIGGER",
                source=bodies.get(function_name, ""), source_engine="PostgreSQL",
                table_name=table_name, timing=(timing or "BEFORE").upper(),
                events=[event_name],
                # unlike MySQL (always FOR EACH ROW), PostgreSQL triggers
                # can genuinely be statement-level -- read it rather than
                # assuming.
                row_level=(str(orientation or "ROW").upper() == "ROW"),
                trigger_function=function_name,
            )
            by_name[trig_name] = routine
            schema.routines.append(routine)

    return schema


_PG_ARG_MODES = ("INOUT", "OUT", "IN", "VARIADIC")


def _parse_pg_arguments(text: str) -> list:
    """`pg_get_function_arguments` output -> RoutineParameter list.

    The string looks like `IN a integer, OUT b text DEFAULT 'x'` -- mode
    optional and defaulting to IN, name optional for a positional-only
    argument, and a DEFAULT clause that may contain commas inside a
    literal, which is why the split is paren- and quote-aware rather than
    a plain `str.split(",")`.
    """
    from tgdatabridge.core.plsql_converter import split_top_level

    parameters = []
    for index, raw in enumerate(split_top_level(text, ","), start=1):
        piece = raw.strip()
        if not piece:
            continue
        mode = "IN"
        for candidate in _PG_ARG_MODES:
            if piece.upper().startswith(candidate + " "):
                mode = "INOUT" if candidate == "VARIADIC" else candidate
                piece = piece[len(candidate):].strip()
                break
        default = None
        split = re.split(r"\bDEFAULT\b|\s:=\s", piece, maxsplit=1, flags=re.IGNORECASE)
        if len(split) == 2:
            piece, default = split[0].strip(), split[1].strip()
        words = piece.split()
        if len(words) >= 2 and re.fullmatch(r"[A-Za-z_][\w$]*", words[0]):
            name, data_type = words[0], " ".join(words[1:])
        else:
            name, data_type = f"p{index}", piece
        parameters.append(RoutineParameter(
            name=name, data_type=data_type or "text", mode=mode, default=default))
    return parameters
