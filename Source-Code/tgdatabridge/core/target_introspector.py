"""Lightweight introspection of what currently exists in the *target*
database schema.

This is deliberately much thinner than tgdatabridge.core.introspector (which
builds full Table/Column/Constraint objects for the Oracle source): it only
needs object *names* per category, to populate a short "what's actually on
the target" pane once Apply DDL / Migrate Data has run -- not full column
or constraint definitions.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List


@dataclass
class TargetObjects:
    tables: List[str] = field(default_factory=list)
    views: List[str] = field(default_factory=list)
    sequences: List[str] = field(default_factory=list)
    routines: List[str] = field(default_factory=list)  # functions/procedures + triggers, combined

    # {table name: approximate row count}. From the engine's own
    # statistics -- PostgreSQL's pg_class.reltuples, MySQL's
    # information_schema.tables.table_rows -- so it costs one extra query
    # for the whole schema rather than a COUNT(*) per table, which on five
    # hundred tables would be five hundred full scans.
    #
    # It is there to answer the question everybody asks after "Apply DDL
    # to Target" -- "is there any data in this?" -- at a glance, without
    # leaving for another client. Approximate is the right trade: nobody
    # needs the exact number to tell empty from not-empty, and -1 (or a
    # missing entry) means the engine could not say.
    row_counts: dict = field(default_factory=dict)


def _row_counts(connector, sql: str, params: dict) -> dict:
    """Approximate row counts, or {} if they cannot be had.

    Deliberately unable to fail the caller. These are a convenience --
    "is there any data in this yet?" -- and a statistics view that is
    unreadable for want of a privilege, or a server that answers in a
    shape this does not expect, must not take the whole target pane down
    with it. The pane simply shows names, exactly as it did before counts
    existed.
    """
    try:
        counts = {}
        for row in connector.execute(sql, params):
            if not isinstance(row, (tuple, list)) or len(row) < 2:
                continue
            name, count = row[0], row[1]
            try:
                counts[name] = int(count or 0)
            except (TypeError, ValueError):
                continue
        return counts
    except Exception:  # noqa: BLE001 - statistics are never worth failing over
        return {}


def introspect_target_postgres(connector, schema_name: str) -> TargetObjects:
    result = TargetObjects()
    result.tables = [r[0] for r in connector.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = %(schema)s AND table_type = 'BASE TABLE' "
        "ORDER BY table_name",
        {"schema": schema_name},
    )]
    result.views = [r[0] for r in connector.execute(
        "SELECT table_name FROM information_schema.views "
        "WHERE table_schema = %(schema)s ORDER BY table_name",
        {"schema": schema_name},
    )]
    result.sequences = [r[0] for r in connector.execute(
        "SELECT sequence_name FROM information_schema.sequences "
        "WHERE sequence_schema = %(schema)s ORDER BY sequence_name",
        {"schema": schema_name},
    )]
    routine_names = {r[0] for r in connector.execute(
        "SELECT routine_name FROM information_schema.routines "
        "WHERE routine_schema = %(schema)s ORDER BY routine_name",
        {"schema": schema_name},
    )}
    # Postgres reports one trigger row per firing event, so de-duplicate
    # against the routine names or a single CREATE TRIGGER can appear more
    # than once.
    trigger_names = {r[0] for r in connector.execute(
        "SELECT trigger_name FROM information_schema.triggers "
        "WHERE trigger_schema = %(schema)s ORDER BY trigger_name",
        {"schema": schema_name},
    )}
    result.routines = sorted(routine_names | trigger_names)
    result.row_counts = _row_counts(
        connector,
        "SELECT c.relname, c.reltuples FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = %(schema)s AND c.relkind IN ('r', 'p')",
        {"schema": schema_name})
    return result


def introspect_target_mysql(connector, database_name: str) -> TargetObjects:
    result = TargetObjects()
    result.tables = [r[0] for r in connector.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = %(schema)s AND table_type = 'BASE TABLE' "
        "ORDER BY table_name",
        {"schema": database_name},
    )]
    result.views = [r[0] for r in connector.execute(
        "SELECT table_name FROM information_schema.views "
        "WHERE table_schema = %(schema)s ORDER BY table_name",
        {"schema": database_name},
    )]
    # MySQL has no native SEQUENCE object -- ddl_generator emulates one as a
    # helper table named "<sequence>_SEQ" (see generate_sequence_ddl_mysql),
    # which already shows up under Tables. Leave this empty rather than
    # double-counting those helper tables under both categories.
    result.sequences = []
    routine_names = {r[0] for r in connector.execute(
        "SELECT routine_name FROM information_schema.routines "
        "WHERE routine_schema = %(schema)s ORDER BY routine_name",
        {"schema": database_name},
    )}
    trigger_names = {r[0] for r in connector.execute(
        "SELECT trigger_name FROM information_schema.triggers "
        "WHERE trigger_schema = %(schema)s ORDER BY trigger_name",
        {"schema": database_name},
    )}
    result.routines = sorted(routine_names | trigger_names)
    result.row_counts = _row_counts(
        connector,
        "SELECT table_name, table_rows FROM information_schema.tables "
        "WHERE table_schema = %(schema)s AND table_type = 'BASE TABLE'",
        {"schema": database_name})
    return result


def introspect_target_sqlserver(connector, schema_name: str) -> TargetObjects:
    result = TargetObjects()
    result.tables = [r[0] for r in connector.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = %(schema)s AND table_type = 'BASE TABLE' "
        "ORDER BY table_name",
        {"schema": schema_name},
    )]
    result.views = [r[0] for r in connector.execute(
        "SELECT table_name FROM information_schema.views "
        "WHERE table_schema = %(schema)s ORDER BY table_name",
        {"schema": schema_name},
    )]
    # SQL Server sequences are a native object (2012+), but exposed via
    # sys.sequences rather than an ISO information_schema view.
    result.sequences = [r[0] for r in connector.execute(
        "SELECT name FROM sys.sequences WHERE SCHEMA_NAME(schema_id) = %(schema)s ORDER BY name",
        {"schema": schema_name},
    )]
    routine_names = {r[0] for r in connector.execute(
        "SELECT routine_name FROM information_schema.routines "
        "WHERE routine_schema = %(schema)s ORDER BY routine_name",
        {"schema": schema_name},
    )}
    # SQL Server has no information_schema.triggers (that's a MySQL/Postgres
    # ISO extension) -- sys.objects with type 'TR' is the native equivalent.
    trigger_names = {r[0] for r in connector.execute(
        "SELECT o.name FROM sys.objects o WHERE o.type = 'TR' "
        "AND SCHEMA_NAME(o.schema_id) = %(schema)s ORDER BY o.name",
        {"schema": schema_name},
    )}
    result.routines = sorted(routine_names | trigger_names)
    return result


def introspect_target_db2(connector, schema_name: str) -> TargetObjects:
    result = TargetObjects()
    result.tables = [r[0] for r in connector.execute(
        "SELECT TABNAME FROM SYSCAT.TABLES WHERE TABSCHEMA = %(schema)s AND TYPE = 'T' "
        "ORDER BY TABNAME",
        {"schema": schema_name},
    )]
    result.views = [r[0] for r in connector.execute(
        "SELECT TABNAME FROM SYSCAT.TABLES WHERE TABSCHEMA = %(schema)s AND TYPE = 'V' "
        "ORDER BY TABNAME",
        {"schema": schema_name},
    )]
    # Db2 sequences are a native object, exposed via SYSCAT.SEQUENCES rather
    # than an ISO information_schema view.
    result.sequences = [r[0] for r in connector.execute(
        "SELECT SEQNAME FROM SYSCAT.SEQUENCES WHERE SEQSCHEMA = %(schema)s ORDER BY SEQNAME",
        {"schema": schema_name},
    )]
    routine_names = {r[0] for r in connector.execute(
        "SELECT ROUTINENAME FROM SYSCAT.ROUTINES WHERE ROUTINESCHEMA = %(schema)s "
        "ORDER BY ROUTINENAME",
        {"schema": schema_name},
    )}
    trigger_names = {r[0] for r in connector.execute(
        "SELECT TRIGNAME FROM SYSCAT.TRIGGERS WHERE TRIGSCHEMA = %(schema)s ORDER BY TRIGNAME",
        {"schema": schema_name},
    )}
    result.routines = sorted(routine_names | trigger_names)
    return result


def introspect_target_oracle(connector, schema_name: str) -> TargetObjects:
    """Reads Oracle's own ALL_* data dictionary (the same views
    tgdatabridge.core.introspector's full *source*-side introspection reads, just
    name-only here), for an Oracle *target*. Uses named `:owner` binds,
    matching OracleConnector.execute()/introspector.py's own convention
    (every other introspect_target_* above binds `%(schema)s`, the
    style each of *their* connectors' underlying drivers expect)."""
    result = TargetObjects()
    result.tables = [r[0] for r in connector.execute(
        "SELECT table_name FROM all_tables WHERE owner = :owner ORDER BY table_name",
        {"owner": schema_name},
    )]
    result.views = [r[0] for r in connector.execute(
        "SELECT view_name FROM all_views WHERE owner = :owner ORDER BY view_name",
        {"owner": schema_name},
    )]
    result.sequences = [r[0] for r in connector.execute(
        "SELECT sequence_name FROM all_sequences WHERE sequence_owner = :owner ORDER BY sequence_name",
        {"owner": schema_name},
    )]
    # ALL_OBJECTS covers procedures/functions/packages/triggers in one
    # shot -- unlike the SYSCAT.ROUTINES/SYSCAT.TRIGGERS split Db2 needs
    # above, Oracle's own catalog already has every one of these object
    # types in the same view.
    result.routines = [r[0] for r in connector.execute(
        "SELECT object_name FROM all_objects WHERE owner = :owner "
        "AND object_type IN ('PROCEDURE', 'FUNCTION', 'PACKAGE', 'TRIGGER') "
        "ORDER BY object_name",
        {"owner": schema_name},
    )]
    return result


def introspect_target_mongodb(connector, database_name: str) -> TargetObjects:
    """MongoDB has no information_schema/SYSCAT catalog to query -- unlike
    every other engine above, this reads collections and indexes directly
    off the PyMongo Database exposed via `connector.db`, not through
    `connector.execute()` (which MongoConnector deliberately does not
    implement -- see its module docstring). `database_name` is accepted
    for interface-parity with the other introspect_target_* functions but
    isn't actually used: `connector.db` already resolves to the database
    the connector was configured for.

    Views and routines/triggers are always reported empty, and that's
    correct, not a gap: generate_view_ddl's MongoDB branch and
    plsql_converter.convert_routine's MongoDB branch both only ever emit
    MANUAL-CONVERSION-REQUIRED placeholder comments (see their docstrings)
    -- nothing real is ever actually created for either category, so there
    is nothing here for either category to ever find."""
    result = TargetObjects()
    all_collections = connector.db.list_collection_names()
    # The "counters" helper collection emulates CREATE SEQUENCE (one
    # document per sequence -- see generate_sequence_ddl_mongodb) and must
    # not be reported as if it were itself a converted table.
    result.tables = sorted(c for c in all_collections if c != "counters")
    if "counters" in all_collections:
        result.sequences = sorted(
            doc["_id"] for doc in connector.db["counters"].find({}, {"_id": 1})
        )
    return result


def introspect_target(connector, engine: str, schema_name: str) -> TargetObjects:
    engine_key = engine.lower().replace(" ", "")
    if engine_key.startswith("oracle"):
        return introspect_target_oracle(connector, schema_name)
    if engine_key.startswith("postgres"):
        return introspect_target_postgres(connector, schema_name)
    if engine_key.startswith("sqlserver"):
        return introspect_target_sqlserver(connector, schema_name)
    if engine_key.startswith("db2"):
        return introspect_target_db2(connector, schema_name)
    if engine_key.startswith("mongo"):
        return introspect_target_mongodb(connector, schema_name)
    return introspect_target_mysql(connector, schema_name)
