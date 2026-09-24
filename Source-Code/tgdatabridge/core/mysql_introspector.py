"""Reads MySQL's information_schema (scoped to one database -- MySQL's
closest equivalent to an Oracle "schema") and builds a
tgdatabridge.core.schema_model.Schema object, for using MySQL as a *source* --
e.g. a reverse migration back to Oracle, or a lateral one to PostgreSQL/
SQL Server/Db2/a different MySQL database.

Scope matches the "schema/tables/views only" depth this tool already
gives MySQL as a *target* for stored routines: tables/columns/
constraints/indexes/views are introspected fully and get real type
mapping (through type_mapping.from_mysql(), which reverse-maps every
native MySQL column type into this tool's shared Oracle-flavored pivot
representation -- see that function's own docstring -- so the existing
to_postgres/to_mysql/to_sqlserver/to_db2 forward-mappers already know how
to convert a MySQL-sourced column to any target with no changes needed on
that side at all). Stored routines and triggers are listed (name, kind,
and their native MySQL source text for manual reference) but never
parsed or converted -- plsql_converter.convert_routine short-circuits to
a manual-conversion flag for any Routine whose source_engine isn't
"Oracle", exactly the same way this tool already treats every MySQL-
*target* stored routine.

MySQL has no native SEQUENCE object, so there are never any Sequence
entries here; an AUTO_INCREMENT column is represented as Column.identity
= True instead (mirroring how an Oracle IDENTITY column is represented),
which every target's DDL generator already knows how to turn into an
appropriate identity/auto-increment/sequence-backed column.

One MySQL-specific wrinkle worth calling out: MySQL's PRIMARY KEY
constraint (and its backing index) is *always* named literally "PRIMARY"
for every single table -- unlike Oracle/PostgreSQL/SQL Server/Db2, where
constraint names are unique within a schema. Reusing that literal name
verbatim across every table's Constraint/Index would collide the moment
more than one table's PRIMARY KEY DDL landed in the same target schema
("constraint already exists" on the second table onward), so it's
renamed here to "<table_name>_PK" instead -- for both the Constraint and
its matching Index, so ddl_generator's existing "don't also emit an index
for what's already a PK/UNIQUE constraint" dedup logic still recognizes
them as the same object.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Optional

from tgdatabridge.core import type_mapping
from tgdatabridge.core.schema_model import (
    Column, Constraint, Index, Routine, RoutineParameter, Schema, Table, View,
)

if TYPE_CHECKING:
    from tgdatabridge.db.mysql_connector import MySQLConnector

_MYSQL_PRIMARY_INDEX_NAME = "PRIMARY"


def _fmt_data_type(row) -> str:
    """row: (data_type, character_maximum_length, numeric_precision,
    numeric_scale, column_type). Reconstructs a normal-looking MySQL type
    string (e.g. "varchar(255)", "decimal(10,2)", "enum('a','b')",
    "int(10) unsigned") for type_mapping.from_mysql to parse --
    COLUMN_TYPE from information_schema.COLUMNS already includes this
    directly for enum/set/bit (and any unsigned/zerofill modifiers) so
    it's used as-is for those; everything else is rebuilt from the more
    normalized DATA_TYPE + CHARACTER_MAXIMUM_LENGTH/NUMERIC_PRECISION/
    NUMERIC_SCALE columns, which don't carry those modifiers."""
    dtype, char_len, num_precision, num_scale, column_type = row
    dtype_upper = (dtype or "").upper()

    if dtype_upper in ("ENUM", "SET", "BIT"):
        return column_type or dtype

    if dtype_upper in ("CHAR", "VARCHAR", "BINARY", "VARBINARY") and char_len:
        return f"{dtype}({int(char_len)})"

    if dtype_upper in ("DECIMAL", "NUMERIC") and num_precision is not None:
        if num_scale:
            return f"{dtype}({int(num_precision)},{int(num_scale)})"
        return f"{dtype}({int(num_precision)})"

    if dtype_upper in ("INT", "INTEGER", "TINYINT", "SMALLINT", "MEDIUMINT", "BIGINT") and "unsigned" in (column_type or "").lower():
        return f"{dtype} unsigned"

    return dtype


def _pk_safe_name(table_name: str, name: str) -> str:
    """MySQL's PRIMARY KEY constraint/index is always literally "PRIMARY"
    on every table -- see this module's docstring for why that has to be
    disambiguated per-table before it's usable across a whole schema."""
    return f"{table_name}_PK" if name.upper() == _MYSQL_PRIMARY_INDEX_NAME else name



_STRINGY_MYSQL_TYPES = (
    "char", "varchar", "tinytext", "text", "mediumtext", "longtext",
    "binary", "varbinary", "tinyblob", "blob", "mediumblob", "longblob",
    "enum", "set", "json",
    "date", "datetime", "timestamp", "time", "year",
)

_ALREADY_SQL_RE = re.compile(
    r"^\s*(NULL|CURRENT_TIMESTAMP(\s*\(\s*\d*\s*\))?|NOW\s*\(\s*\)|"
    r"CURRENT_DATE|CURRENT_TIME|UTC_TIMESTAMP(\s*\(\s*\d*\s*\))?)\s*$",
    re.IGNORECASE)

_NUMERIC_RE = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")


def normalise_default(default, column_type, extra) -> Optional[str]:
    """Turn MySQL's COLUMN_DEFAULT into something that is valid SQL.

    MySQL's information_schema reports a literal default **unquoted**:
    a column declared `DEFAULT 'Active'` comes back as the bare word
    `Active`, and `DEFAULT '0000-00-00 00:00:00'` as
    `0000-00-00 00:00:00`. Interpolated into the generated DDL that is
    not a string at all -- PostgreSQL reads the first as an identifier
    ('column "active" does not exist') and the second as a syntax error:

        syntax error at or near "00"
        LINE 24: ..."last_modified" TIMESTAMP NOT NULL DEFAULT 0000-00-00 00:00:00

    which is where a 1061-statement script stopped dead at statement 117.

    MariaDB, and MySQL 8 for an *expression* default, already report SQL
    text -- quotes included -- so this must not double-quote those. The
    three cases are told apart by:

    * `extra` containing DEFAULT_GENERATED, which is MySQL 8's own marker
      for "this default is an expression, not a literal";
    * the value already being a quoted string, or a keyword like
      CURRENT_TIMESTAMP / NULL that is valid SQL as it stands;
    * otherwise the column's declared type -- a literal for a character,
      date/time, binary, enum, set or json column needs quoting; one for a
      numeric column does not (and is left alone unless it does not look
      like a number, in which case quoting it is the safer reading).
    """
    if default is None:
        return None
    raw = str(default)
    if not raw.strip():
        # A genuinely empty string default on a character column: the
        # empty quotes are the whole value and must survive.
        base = (column_type or "").split("(")[0].strip().lower()
        return "''" if base in _STRINGY_MYSQL_TYPES else None
    text = raw.strip()
    if "default_generated" in (extra or "").lower():
        return text
    if _ALREADY_SQL_RE.match(text):
        return text
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        return text
    base = (column_type or "").split("(")[0].strip().lower()
    if base in _STRINGY_MYSQL_TYPES or not _NUMERIC_RE.match(text):
        return "'" + text.replace("'", "''") + "'"
    return text


def introspect_schema(conn: "MySQLConnector", schema_name: str, include_routines: bool = True) -> Schema:
    schema = Schema(name=schema_name, source_engine="MySQL")

    tables_by_name: dict = {}
    for (table_name, comment) in conn.execute(
        "SELECT table_name, table_comment FROM information_schema.tables "
        "WHERE table_schema = %(schema)s AND table_type = 'BASE TABLE' "
        "ORDER BY table_name",
        {"schema": schema_name},
    ):
        table = Table(name=table_name, schema=schema_name, comment=(comment or None))
        tables_by_name[table_name] = table
        schema.tables.append(table)

    for (table_name, col_name, dtype, char_len, num_precision, num_scale,
         column_type, nullable, default, extra, col_comment) in conn.execute(
        "SELECT table_name, column_name, data_type, character_maximum_length, "
        "numeric_precision, numeric_scale, column_type, is_nullable, "
        "column_default, extra, column_comment FROM information_schema.columns "
        "WHERE table_schema = %(schema)s ORDER BY table_name, ordinal_position",
        {"schema": schema_name},
    ):
        table = tables_by_name.get(table_name)
        if table is None:
            continue
        raw_type = _fmt_data_type((dtype, char_len, num_precision, num_scale, column_type))
        pivot_type, mapping_issues = type_mapping.from_mysql(raw_type)
        col = Column(
            name=col_name,
            data_type=pivot_type,
            nullable=(nullable == "YES"),
            default=normalise_default(default, column_type, extra),
            identity=("auto_increment" in (extra or "").lower()),
            comment=(col_comment or None),
            source_issues=mapping_issues,
        )
        table.columns.append(col)

    # --------------------------------------------------------- constraints
    # Keyed by (table_name, constraint_name) rather than bare constraint
    # name -- MySQL's PRIMARY KEY constraint is always literally "PRIMARY"
    # on every table, so a bare-name key would silently merge every
    # table's primary key columns into one Constraint object.
    constraints_by_key: dict = {}
    for (cons_name, table_name, cons_type) in conn.execute(
        "SELECT constraint_name, table_name, constraint_type FROM information_schema.table_constraints "
        "WHERE table_schema = %(schema)s AND constraint_type IN ('PRIMARY KEY', 'UNIQUE', 'FOREIGN KEY')",
        {"schema": schema_name},
    ):
        table = tables_by_name.get(table_name)
        if table is None:
            continue
        safe_name = _pk_safe_name(table_name, cons_name) if cons_type == "PRIMARY KEY" else cons_name
        cons = Constraint(name=safe_name, kind=cons_type)
        table.constraints.append(cons)
        constraints_by_key[(table_name, cons_name)] = cons

    for (cons_name, table_name, col_name, position) in conn.execute(
        "SELECT constraint_name, table_name, column_name, ordinal_position "
        "FROM information_schema.key_column_usage WHERE table_schema = %(schema)s "
        "ORDER BY table_name, constraint_name, ordinal_position",
        {"schema": schema_name},
    ):
        cons = constraints_by_key.get((table_name, cons_name))
        if cons is not None and col_name:
            cons.columns.append(col_name)

    for (cons_name, table_name, ref_table, ref_col, position) in conn.execute(
        "SELECT constraint_name, table_name, referenced_table_name, referenced_column_name, "
        "ordinal_position FROM information_schema.key_column_usage "
        "WHERE table_schema = %(schema)s AND referenced_table_name IS NOT NULL "
        "ORDER BY table_name, constraint_name, ordinal_position",
        {"schema": schema_name},
    ):
        cons = constraints_by_key.get((table_name, cons_name))
        if cons is not None:
            cons.ref_table = ref_table
            cons.ref_columns.append(ref_col)

    # CHECK constraints are a MySQL 8.0.16+ feature (information_schema.
    # CHECK_CONSTRAINTS doesn't exist at all on older MySQL/MariaDB) --
    # queried separately and tolerated entirely missing.
    try:
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
                name=cons_name, kind="CHECK", check_condition=str(check_clause) if check_clause else None,
            ))
    except Exception:
        pass

    # -------------------------------------------------------------- indexes
    indexes_by_key: dict = {}
    for (table_name, index_name, non_unique, col_name, seq) in conn.execute(
        "SELECT table_name, index_name, non_unique, column_name, seq_in_index "
        "FROM information_schema.statistics WHERE table_schema = %(schema)s "
        "ORDER BY table_name, index_name, seq_in_index",
        {"schema": schema_name},
    ):
        table = tables_by_name.get(table_name)
        if table is None:
            continue
        key = (table_name, index_name)
        idx = indexes_by_key.get(key)
        if idx is None:
            safe_name = _pk_safe_name(table_name, index_name)
            idx = Index(name=safe_name, columns=[], unique=(int(non_unique) == 0))
            table.indexes.append(idx)
            indexes_by_key[key] = idx
        if col_name:
            idx.columns.append(col_name)

    # ---------------------------------------------------------------- views
    for (view_name, view_definition) in conn.execute(
        "SELECT table_name, view_definition FROM information_schema.views "
        "WHERE table_schema = %(schema)s ORDER BY table_name",
        {"schema": schema_name},
    ):
        schema.views.append(View(
            name=view_name, schema=schema_name, definition=str(view_definition or ""), source_engine="MySQL",
        ))

    # ------------------------------------------------------------- routines
    if include_routines:
        # information_schema.PARAMETERS, because ROUTINE_DEFINITION is the
        # body *alone* on MySQL -- no CREATE, no parameter list, no RETURNS.
        # Without this a MySQL-sourced procedure could not be rebuilt for
        # any target, not even MySQL itself: there was nothing to put
        # between the parentheses. ORDINAL_POSITION 0 is a function's
        # return type, 1..n are the real parameters.
        parameters: dict = {}
        returns: dict = {}
        for (routine_name, ordinal, mode, param_name, dtd) in conn.execute(
            "SELECT specific_name, ordinal_position, parameter_mode, parameter_name, "
            "dtd_identifier FROM information_schema.parameters "
            "WHERE specific_schema = %(schema)s ORDER BY specific_name, ordinal_position",
            {"schema": schema_name},
        ):
            if int(ordinal or 0) == 0:
                returns[routine_name] = str(dtd or "")
                continue
            parameters.setdefault(routine_name, []).append(RoutineParameter(
                name=str(param_name or f"p{ordinal}"),
                data_type=str(dtd or "TEXT"),
                mode=str(mode or "IN").upper(),
            ))

        for (name, routine_type, definition) in conn.execute(
            "SELECT routine_name, routine_type, routine_definition "
            "FROM information_schema.routines WHERE routine_schema = %(schema)s "
            "ORDER BY routine_name",
            {"schema": schema_name},
        ):
            schema.routines.append(Routine(
                name=name, schema=schema_name, kind=routine_type,
                source=str(definition or ""), source_engine="MySQL",
                parameters=parameters.get(name, []),
                return_type=returns.get(name),
            ))

        # One Routine per trigger, not one per firing event.
        # information_schema.triggers has a row per event, and passing those
        # straight through gave the rest of the tool N copies of the same
        # trigger under one name: N identical CREATE blocks in the DDL, N
        # DROPs in the rollback, and N indistinguishable rows in the object
        # tree that a single checkbox controlled. MySQL only ever declares
        # one event per trigger today, so this is normally a no-op -- but it
        # is the same collapse PostgreSQL and SQL Server genuinely need, and
        # doing it here keeps every source's shape the same downstream.
        by_name: dict = {}
        for (trig_name, table_name, event, timing, action_statement) in conn.execute(
            "SELECT trigger_name, event_object_table, event_manipulation, action_timing, "
            "action_statement FROM information_schema.triggers WHERE trigger_schema = %(schema)s "
            "ORDER BY trigger_name",
            {"schema": schema_name},
        ):
            existing = by_name.get(trig_name)
            if existing is not None:
                event_name = (event or "INSERT").upper()
                if event_name not in existing.events:
                    existing.events.append(event_name)
                continue
            routine = Routine(
                name=trig_name, schema=schema_name, kind="TRIGGER",
                source=str(action_statement or ""), source_engine="MySQL",
                table_name=table_name, timing=(timing or "BEFORE").upper(),
                events=[(event or "INSERT").upper()],
                row_level=True,  # MySQL triggers are always FOR EACH ROW
            )
            by_name[trig_name] = routine
            schema.routines.append(routine)

    return schema
