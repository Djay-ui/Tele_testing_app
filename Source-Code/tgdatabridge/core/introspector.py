"""
Reads Oracle's data dictionary (ALL_* views, scoped to one schema/owner)
and builds a tgdatabridge.core.schema_model.Schema object.

Uses ALL_* views rather than USER_* so the tool can introspect a schema
other than the one it connected as, provided the connecting user has been
granted SELECT_CATALOG_ROLE or equivalent read access.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from tgdatabridge.core.schema_model import (
    Column, Constraint, Index, Partition, PartitionScheme, Routine, Schema,
    Sequence, Table, View,
)

if TYPE_CHECKING:
    from tgdatabridge.db.oracle_connector import OracleConnector


def _fmt_data_type(row) -> str:
    """row: (data_type, data_length, data_precision, data_scale)"""
    dtype, length, precision, scale = row
    dtype = dtype.upper()
    if dtype in ("VARCHAR2", "NVARCHAR2", "CHAR", "NCHAR", "RAW"):
        return f"{dtype}({length})" if length else dtype
    if dtype == "NUMBER":
        if precision is None and scale is None:
            return "NUMBER"
        if scale is None or scale == 0:
            return f"NUMBER({int(precision)})" if precision is not None else "NUMBER"
        return f"NUMBER({int(precision)},{int(scale)})"
    return dtype


def introspect_schema(conn: "OracleConnector", schema_name: str, include_routines: bool = True) -> Schema:
    schema = Schema(name=schema_name, source_engine="Oracle")

    tables_by_name: dict[str, Table] = {}
    for (table_name, comments) in conn.execute(
        "SELECT t.table_name, c.comments FROM all_tables t "
        "LEFT JOIN all_tab_comments c ON c.owner = t.owner AND c.table_name = t.table_name "
        "WHERE t.owner = :owner ORDER BY t.table_name",
        {"owner": schema_name},
    ):
        table = Table(name=table_name, schema=schema_name, comment=comments)
        tables_by_name[table_name] = table
        schema.tables.append(table)

    for (table_name, col_name, dtype, length, precision, scale, nullable, default) in conn.execute(
        "SELECT table_name, column_name, data_type, data_length, data_precision, "
        "data_scale, nullable, data_default FROM all_tab_columns "
        "WHERE owner = :owner ORDER BY table_name, column_id",
        {"owner": schema_name},
    ):
        table = tables_by_name.get(table_name)
        if table is None:
            continue
        raw_type = _fmt_data_type((dtype, length, precision, scale))
        col = Column(
            name=col_name,
            data_type=raw_type,
            nullable=(nullable == "Y"),
            default=(str(default).strip() if default is not None else None),
        )
        table.columns.append(col)

    identity_cols = set(
        (t, c) for (t, c) in conn.execute(
            "SELECT table_name, column_name FROM all_tab_identity_cols WHERE owner = :owner",
            {"owner": schema_name},
        )
    )
    for table in schema.tables:
        for col in table.columns:
            if (table.name, col.name) in identity_cols:
                col.identity = True

    constraints_by_name: dict[str, Constraint] = {}
    for (table_name, cons_name, cons_type, r_owner, r_cons_name) in conn.execute(
        "SELECT table_name, constraint_name, constraint_type, r_owner, r_constraint_name "
        "FROM all_constraints WHERE owner = :owner AND constraint_type IN ('P','R','U','C') "
        "AND status = 'ENABLED'",
        {"owner": schema_name},
    ):
        table = tables_by_name.get(table_name)
        if table is None:
            continue
        kind = {"P": "PRIMARY KEY", "R": "FOREIGN KEY", "U": "UNIQUE", "C": "CHECK"}[cons_type]
        cons = Constraint(name=cons_name, kind=kind)
        table.constraints.append(cons)
        constraints_by_name[cons_name] = cons

    for (cons_name, col_name, position) in conn.execute(
        "SELECT constraint_name, column_name, position FROM all_cons_columns "
        "WHERE owner = :owner ORDER BY constraint_name, position",
        {"owner": schema_name},
    ):
        cons = constraints_by_name.get(cons_name)
        if cons is not None:
            cons.columns.append(col_name)

    for (cons_name, ref_cons_name) in conn.execute(
        "SELECT constraint_name, r_constraint_name FROM all_constraints "
        "WHERE owner = :owner AND constraint_type = 'R'",
        {"owner": schema_name},
    ):
        cons = constraints_by_name.get(cons_name)
        ref = constraints_by_name.get(ref_cons_name)
        if cons is not None and ref is not None:
            for table in schema.tables:
                if ref in table.constraints:
                    cons.ref_table = table.name
                    cons.ref_columns = ref.columns
                    break

    indexes_by_name: dict[str, Index] = {}
    for (table_name, index_name, uniqueness) in conn.execute(
        "SELECT table_name, index_name, uniqueness FROM all_indexes "
        "WHERE owner = :owner AND index_type NOT IN ('LOB')",
        {"owner": schema_name},
    ):
        table = tables_by_name.get(table_name)
        if table is None:
            continue
        idx = Index(name=index_name, columns=[], unique=(uniqueness == "UNIQUE"))
        table.indexes.append(idx)
        indexes_by_name[index_name] = idx

    # A function-based index (CREATE INDEX ... ON t (LOWER(email))) has no
    # real column to report: Oracle backs it with a hidden virtual column
    # instead, and that is what ALL_IND_COLUMNS.COLUMN_NAME reports for it
    # -- something like "SYS_NC00011$". Carrying that name straight
    # through into the generated target DDL produces a real error on
    # every target this tool creates ("column \"sys_nc00011$\" does not
    # exist"), because that column was never created there -- only Oracle
    # creates it, and only for this one purpose. ALL_IND_EXPRESSIONS has
    # the *real* expression such a hidden column stands in for, keyed by
    # the same (index_name, column_position); this builds that mapping
    # first, then substitutes the expression in place of a hidden-column
    # name below.
    expressions_by_index_position: dict[tuple[str, int], str] = {}
    for (index_name, position, expression) in conn.execute(
        "SELECT index_name, column_position, column_expression FROM all_ind_expressions "
        "WHERE index_owner = :owner",
        {"owner": schema_name},
    ):
        if expression:
            expressions_by_index_position[(index_name, int(position))] = str(expression)

    for (index_name, col_name, position) in conn.execute(
        "SELECT index_name, column_name, column_position FROM all_ind_columns "
        "WHERE index_owner = :owner ORDER BY index_name, column_position",
        {"owner": schema_name},
    ):
        idx = indexes_by_name.get(index_name)
        if idx is None or not col_name:
            continue
        expression = expressions_by_index_position.get((index_name, int(position)))
        idx.columns.append(expression if expression is not None else col_name)

    # Oracle RANGE/LIST/HASH (and composite RANGE-HASH/RANGE-LIST/...)
    # partitioning. A partitioned table's parent row already came through
    # the all_tables/all_tab_columns/all_constraints/all_indexes loops
    # above exactly like any other table -- Oracle lists it there once,
    # not once per partition -- so this only adds the partitioning
    # metadata onto the same Table object; ddl_generator decides what to
    # do with it (see _generate_partition_ddl_postgres).
    partition_meta: dict[str, dict] = {}
    for (table_name, part_type, subpart_type) in conn.execute(
        "SELECT table_name, partitioning_type, subpartitioning_type FROM all_part_tables "
        "WHERE owner = :owner",
        {"owner": schema_name},
    ):
        if table_name in tables_by_name:
            partition_meta[table_name] = {
                "kind": part_type,
                "subpart_kind": None if (subpart_type or "NONE").upper() == "NONE" else subpart_type,
                "columns": [],
                "partitions": [],
            }

    if partition_meta:
        for (table_name, col_name, position) in conn.execute(
            "SELECT name, column_name, column_position FROM all_part_key_columns "
            "WHERE owner = :owner AND object_type = 'TABLE' ORDER BY name, column_position",
            {"owner": schema_name},
        ):
            meta = partition_meta.get(table_name)
            if meta is not None:
                meta["columns"].append(col_name)

        for (table_name, part_name, high_value, position) in conn.execute(
            "SELECT table_name, partition_name, high_value, partition_position "
            "FROM all_tab_partitions WHERE table_owner = :owner ORDER BY table_name, partition_position",
            {"owner": schema_name},
        ):
            meta = partition_meta.get(table_name)
            if meta is not None:
                meta["partitions"].append(Partition(
                    name=part_name,
                    high_value=str(high_value) if high_value is not None else None,
                    position=int(position) if position is not None else len(meta["partitions"]),
                ))

        for table_name, meta in partition_meta.items():
            tables_by_name[table_name].partition_scheme = PartitionScheme(
                kind=(meta["kind"] or "").upper(),
                columns=meta["columns"],
                subpartitioning_type=meta["subpart_kind"],
                partitions=meta["partitions"],
            )

    # Materialized views. Unlike an ordinary view, a materialized view
    # never appears in ALL_VIEWS -- only its backing container table does,
    # in ALL_TABLES, which is why it already came through as a plain Table
    # above (columns, constraints, indexes and all). Migrating it as one
    # would silently copy today's snapshot data and lose the defining
    # query entirely -- the container table's name has no query, no
    # BUILD_MODE, no REFRESH_MODE/METHOD, none of what makes it a
    # materialized view rather than an ordinary table. ALL_MVIEWS has all
    # of that, keyed by the same name, so any table found there is moved
    # out of schema.tables and reintroduced as a View with
    # is_materialized=True instead (see ddl_generator.generate_view_ddl's
    # materialized branch for what happens to it from there).
    mview_meta: dict[str, dict] = {}
    for (mview_name, query, build_mode, refresh_mode, refresh_method) in conn.execute(
        "SELECT mview_name, query, build_mode, refresh_mode, refresh_method FROM all_mviews "
        "WHERE owner = :owner",
        {"owner": schema_name},
    ):
        if mview_name in tables_by_name:
            mview_meta[mview_name] = {
                "query": str(query) if query is not None else "",
                "build_mode": build_mode,
                "refresh_mode": refresh_mode,
                "refresh_method": refresh_method,
            }

    if mview_meta:
        schema.tables = [t for t in schema.tables if t.name not in mview_meta]
        for mview_name, meta in mview_meta.items():
            tables_by_name.pop(mview_name, None)
            schema.views.append(View(
                name=mview_name, schema=schema_name, definition=meta["query"],
                is_materialized=True,
                mview_build_mode=meta["build_mode"],
                mview_refresh_mode=meta["refresh_mode"],
                mview_refresh_method=meta["refresh_method"],
            ))

    for (seq_name, min_v, max_v, incr, cycle) in conn.execute(
        "SELECT sequence_name, min_value, max_value, increment_by, cycle_flag "
        "FROM all_sequences WHERE sequence_owner = :owner",
        {"owner": schema_name},
    ):
        schema.sequences.append(Sequence(
            name=seq_name, schema=schema_name,
            start_value=int(min_v) if min_v is not None else 1,
            increment_by=int(incr) if incr is not None else 1,
            min_value=int(min_v) if min_v is not None else None,
            max_value=int(max_v) if max_v is not None else None,
            cycle=(cycle == "Y"),
        ))

    for (view_name, text) in conn.execute(
        "SELECT view_name, text FROM all_views WHERE owner = :owner",
        {"owner": schema_name},
    ):
        schema.views.append(View(name=view_name, schema=schema_name, definition=str(text)))

    if include_routines:
        source_by_obj: dict[tuple[str, str], list[str]] = {}
        for (name, type_, line, text) in conn.execute(
            "SELECT name, type, line, text FROM all_source WHERE owner = :owner "
            "AND type IN ('PROCEDURE','FUNCTION','PACKAGE','PACKAGE BODY') "
            "ORDER BY name, type, line",
            {"owner": schema_name},
        ):
            source_by_obj.setdefault((name, type_), []).append(text or "")

        for (name, type_), lines in source_by_obj.items():
            schema.routines.append(Routine(
                name=name, schema=schema_name, kind=type_, source="".join(lines),
            ))

        for (trig_name, table_name, trig_type, triggering_event, desc, body) in conn.execute(
            "SELECT trigger_name, table_name, trigger_type, triggering_event, "
            "description, trigger_body FROM all_triggers WHERE owner = :owner",
            {"owner": schema_name},
        ):
            timing, row_level = _parse_trigger_type(trig_type)
            events = _parse_triggering_event(triggering_event)
            schema.routines.append(Routine(
                name=trig_name, schema=schema_name, kind="TRIGGER",
                source=str(body or ""),
                table_name=table_name, timing=timing, events=events, row_level=row_level,
            ))

    return schema


def _parse_trigger_type(trig_type: str) -> tuple[str, bool]:
    """'BEFORE EACH ROW' -> ('BEFORE', True); 'AFTER STATEMENT' -> ('AFTER', False)."""
    text = (trig_type or "").upper()
    row_level = "EACH ROW" in text
    if text.startswith("INSTEAD OF"):
        timing = "INSTEAD OF"
    elif text.startswith("BEFORE"):
        timing = "BEFORE"
    elif text.startswith("AFTER"):
        timing = "AFTER"
    else:
        timing = "BEFORE"
    return timing, row_level


def _parse_triggering_event(triggering_event: str) -> list[str]:
    """'INSERT OR UPDATE OF COL1, COL2 OR DELETE' -> ['INSERT', 'UPDATE', 'DELETE']."""
    text = (triggering_event or "").upper()
    events = []
    for part in text.split(" OR "):
        part = part.strip()
        for candidate in ("INSERT", "UPDATE", "DELETE"):
            if part.startswith(candidate) and candidate not in events:
                events.append(candidate)
                break
    return events or ["INSERT"]
