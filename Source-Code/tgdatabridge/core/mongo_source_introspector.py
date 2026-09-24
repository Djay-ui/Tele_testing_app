"""Infers a tgdatabridge.core.schema_model.Schema from a MongoDB database, for
using MongoDB as a *source* -- e.g. migrating into PostgreSQL/MySQL/SQL
Server/Db2, or laterally into a differently-shaped MongoDB database.

Unlike every other introspect_schema() in this package, MongoDB has no
fixed schema and no catalog view to read one from -- a collection's
"columns" don't exist until they're *inferred* by sampling real documents.
Two design decisions (confirmed with the user before this module was
written) drive everything below:

  - Sampling: this reads up to `sample_size` (default 1000) documents per
    collection via MongoDB's own server-side `$sample` aggregation stage
    (a pseudo-random selection, not a full collection scan) rather than
    reading every document -- deliberately approximate, the same
    "convert what's safe, flag the rest" tradeoff this tool already makes
    elsewhere (e.g. connect_by_rewriter.py's best-effort CONNECT BY
    rewrite). A field that never appears in the sample is invisible to
    this introspector no matter how common it is in the full collection.

  - Nesting and arrays: a nested sub-*object* (BSON embedded document,
    not inside an array) flattens into dotted-prefix columns on the same
    table (e.g. "address.city", "address.geo.lat") -- ddl_generator's
    `_quote_pg`/`_quote_mysql`/`_quote_sqlserver`/`_quote_db2` always
    quote identifiers, so a literal "." in a column name is safe to emit
    as-is on every target. An *array* field (of sub-documents OR of plain
    scalars) has no relational equivalent on the same row at all, so it
    is normalized into a synthesized **child table** instead: a surrogate
    integer primary key, a foreign key column back to the parent table's
    `_id`, and either one column per key (recursively flattened, for an
    array of sub-documents) or a single "value" column (for an array of
    scalars). Only one level of this normalization is attempted -- an
    array nested *inside* another array's item shape (array-of-arrays,
    or an array inside a child table's own items) is flagged with a
    warning ConversionIssue and dropped rather than chased into a second
    generation of grandchild tables, which would also need a data
    migration path this tool's single `source_array_path` per Table
    can't express (see tgdatabridge.db.mongo_connector.MongoConnector.
    fetch_batches_table and tgdatabridge.core.migrator.migrate_table).

`_id` is always the table's PRIMARY KEY regardless of its inferred
concrete BSON type -- MongoDB guarantees a unique index on `_id`
universally, unlike every other field, which may or may not turn out to
be unique.

Type inference deliberately avoids a hard runtime dependency on the
`bson` package (unlike this module's sibling introspectors, which all
import their driver's real client for query execution): BSON-specific
types like ObjectId/Decimal128/Binary are recognized purely by
`type(value).__name__`, not `isinstance(value, bson.ObjectId)` -- so this
module works identically whether it's fed real documents from a real
`pymongo` driver, or plain-Python fakes in a test that never installs
`bson` at all (this sandbox has no network access to install `pymongo`/
`bson`, so every test in tests/test_mongo_source_introspector.py exercises
this exact path). Where the same field's sampled values disagree on
type (or on object/array/scalar shape), the most-common option wins and
a warning ConversionIssue flags the field for manual review -- mirroring
the same "convert what's safe, flag the rest" pattern used throughout
this tool (e.g. type_mapping.from_mysql's ENUM/SET handling).

Indexes are introspected via `list_indexes()`, skipping the default
`_id_` index (already covered by the synthesized PRIMARY KEY constraint,
the same PK/index dedup precedent tgdatabridge.core.db2_introspector follows).
An index on a field that was normalized away into a child table can't be
represented as a single-table index on either the parent or the child, so
it is skipped with a warning ConversionIssue rather than emitting a
broken column reference.

Views are introspected via `db.list_collections(filter={"type": "view"})`
-- a MongoDB view's "definition" is a JSON aggregation pipeline, not SQL,
so it is stored as JSON text with `View.source_engine = "MongoDB"`;
ddl_generator.generate_view_ddl checks that field first and unconditionally
short-circuits to a MANUAL placeholder for it, skipping the SQL-text
heuristics entirely (see that function's own comment).

Sequences and routines are always left empty -- MongoDB has no native
SEQUENCE object and no queryable stored-procedure/trigger/function
concept, the same "nothing to introspect" precedent already established
for a MySQL source.
"""
from __future__ import annotations

import datetime
import json
from collections import Counter
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

from tgdatabridge.core import type_mapping
from tgdatabridge.core.schema_model import (
    Column, ConversionIssue, Constraint, Index, Schema, Table, View,
)

if TYPE_CHECKING:
    from tgdatabridge.db.mongo_connector import MongoConnector

DEFAULT_SAMPLE_SIZE = 1000

# The helper collection MongoDB-as-*target* uses to emulate CREATE
# SEQUENCE (see ddl_generator.generate_sequence_ddl_mongodb) -- if a
# schema this tool itself created is later read back as a *source* (a
# lateral or round-trip migration), this must not be reported as if it
# were itself a converted table, mirroring
# target_introspector.introspect_target_mongodb's own exclusion of it.
_SEQUENCE_COUNTER_COLLECTION = "counters"


# --------------------------------------------------------------- shape inference


class _ShapeNode:
    """Accumulates everything observed about one field (or array-item,
    or the document root) across every sampled document. `kind_counts`
    tracks how many times this node was seen as an object/array/scalar
    value (usually just one of the three, but disagreement across the
    sample is possible and handled by taking the most common) --
    `null_count`/`present_count` together determine nullability, and
    `type_counts`/`max_len` are only meaningful when the dominant kind is
    "scalar"."""

    __slots__ = (
        "kind_counts", "type_counts", "max_len", "null_count", "present_count",
        "children", "item_shape",
    )

    def __init__(self) -> None:
        self.kind_counts: Counter = Counter()
        self.type_counts: Counter = Counter()
        self.max_len: Optional[int] = None
        self.null_count = 0
        self.present_count = 0
        self.children: Dict[str, "_ShapeNode"] = {}
        self.item_shape: Optional["_ShapeNode"] = None


def _bson_type_name(value) -> str:
    """Classify one scalar value into the BSON type-name vocabulary
    type_mapping.from_mongodb() understands. Deliberately duck-typed by
    class name for ObjectId/Decimal128/Binary rather than `isinstance`
    against the real `bson` classes -- see this module's own docstring
    for why."""
    if isinstance(value, bool):  # must precede the int check: bool is an int subclass
        return "bool"
    if isinstance(value, int):
        return "int" if -2_147_483_648 <= value <= 2_147_483_647 else "long"
    if isinstance(value, float):
        return "double"
    if isinstance(value, str):
        return "string"
    if isinstance(value, bytes):
        return "binData"
    if isinstance(value, datetime.datetime):
        return "date"
    type_name = type(value).__name__
    if type_name == "ObjectId":
        return "objectId"
    if type_name == "Decimal128":
        return "decimal"
    if type_name == "Binary":
        return "binData"
    return "unknown"


def _merge_value(node: "_ShapeNode", value) -> None:
    """Fold one observed value (a field's value in some sampled document,
    or one element of an array) into `node`, recursing into dicts/lists."""
    node.present_count += 1
    if value is None:
        node.null_count += 1
        return
    if isinstance(value, dict):
        node.kind_counts["object"] += 1
        for key, child_value in value.items():
            child = node.children.setdefault(key, _ShapeNode())
            _merge_value(child, child_value)
        return
    if isinstance(value, list):
        node.kind_counts["array"] += 1
        if value:
            if node.item_shape is None:
                node.item_shape = _ShapeNode()
            for item in value:
                _merge_value(node.item_shape, item)
        return
    node.kind_counts["scalar"] += 1
    bson_type = _bson_type_name(value)
    node.type_counts[bson_type] += 1
    if bson_type == "string":
        length = len(value)
        node.max_len = length if node.max_len is None else max(node.max_len, length)


def _dominant_kind(node: "_ShapeNode") -> Optional[str]:
    if not node.kind_counts:
        return None
    return node.kind_counts.most_common(1)[0][0]


# --------------------------------------------------------- column construction


def _scalar_column(name: str, node: "_ShapeNode", parent_present_count: int) -> Column:
    issues: List[ConversionIssue] = []
    if node.type_counts:
        dominant_type = node.type_counts.most_common(1)[0][0]
        if len(node.type_counts) > 1:
            issues.append(ConversionIssue(
                "warning",
                f"Field '{name}': more than one BSON type observed across the sampled documents "
                f"({dict(node.type_counts)}); mapped using the most common ('{dominant_type}') -- "
                "values of a different type will need manual review or cleanup.",
            ))
        pivot_type, map_issues = type_mapping.from_mongodb(dominant_type, node.max_len)
    else:
        # Every sampled occurrence of this field was null -- no real type
        # was ever observed for it.
        pivot_type, map_issues = type_mapping.from_mongodb("unknown")
    issues.extend(map_issues)
    nullable = node.null_count > 0 or node.present_count < parent_present_count
    return Column(name=name, data_type=pivot_type, nullable=nullable, source_issues=issues)


def _flatten_object(
    node: "_ShapeNode", prefix: str, table_issues: List[ConversionIssue],
    arrays_out: List[Tuple[str, "_ShapeNode"]],
) -> List[Column]:
    """Recursively walk an object-shaped node's children, emitting one
    Column per scalar leaf (dotted-prefixed for anything nested inside a
    sub-object) and collecting `(dotted_path, array_node)` for every array
    field found anywhere in the subtree into `arrays_out` rather than
    turning it into a column here -- the caller normalizes those into
    child tables separately."""
    columns: List[Column] = []
    for key in sorted(node.children):
        child = node.children[key]
        path = f"{prefix}.{key}" if prefix else key
        dominant = _dominant_kind(child)
        if len(child.kind_counts) > 1:
            table_issues.append(ConversionIssue(
                "warning",
                f"Field '{path}': observed as more than one shape across the sampled documents "
                f"(counts: {dict(child.kind_counts)}); treated as '{dominant}', the most common -- "
                "review any documents where it differs.",
            ))
        if dominant == "object":
            columns.extend(_flatten_object(child, path, table_issues, arrays_out))
        elif dominant == "array":
            arrays_out.append((path, child))
        else:
            # "scalar", or None (every occurrence was null and the field
            # never carried an object/array/scalar value at all).
            columns.append(_scalar_column(path, child, node.present_count))
    return columns


# --------------------------------------------------------- table construction


def _build_table(
    schema_name: str, collection_name: str, sample_docs: List[dict],
) -> Tuple[Table, List[Table], List[str]]:
    """Returns (table, child_tables, array_field_paths) for one collection
    -- `array_field_paths` is every top-level-or-nested array field that
    was normalized into one of `child_tables`, needed by the caller to
    also skip any index that references one of those fields (see this
    module's own docstring)."""
    root = _ShapeNode()
    for doc in sample_docs:
        _merge_value(root, doc)

    issues: List[ConversionIssue] = []
    table = Table(
        name=collection_name, schema=schema_name,
        source_collection=collection_name, source_array_path=None,
        row_count_estimate=len(sample_docs),
    )

    id_node = root.children.get("_id")
    if id_node is not None and id_node.type_counts:
        id_type = id_node.type_counts.most_common(1)[0][0]
        id_max_len = id_node.max_len
    else:
        # _id missing from the sample entirely (shouldn't happen for a
        # real MongoDB collection, but be defensive), or every sampled
        # value was itself null/an object/an array -- default to Mongo's
        # own default _id type rather than guessing further.
        id_type = "objectId"
        id_max_len = None
    id_pivot_type, id_issues = type_mapping.from_mongodb(id_type, id_max_len)
    id_column = Column(name="_id", data_type=id_pivot_type, nullable=False, source_issues=id_issues)
    table.columns.append(id_column)
    table.constraints.append(Constraint(name=f"{collection_name}_PK", kind="PRIMARY KEY", columns=["_id"]))

    # Reuse _flatten_object for every field except _id (already handled
    # above, and always the PK regardless of its inferred type) by
    # wrapping the remaining children in a throwaway node.
    remainder = _ShapeNode()
    remainder.children = {k: v for k, v in root.children.items() if k != "_id"}
    remainder.present_count = root.present_count
    arrays_found: List[Tuple[str, _ShapeNode]] = []
    table.columns.extend(_flatten_object(remainder, "", issues, arrays_found))

    if not sample_docs:
        issues.append(ConversionIssue(
            "warning",
            f"Collection '{collection_name}' returned no sampled documents (empty, or the sample "
            "came back empty); no fields could be inferred beyond '_id'.",
        ))

    child_tables: List[Table] = []
    for path, array_node in arrays_found:
        child_tables.append(_build_child_table(schema_name, collection_name, table, path, array_node))

    table.issues = issues
    return table, child_tables, [path for path, _ in arrays_found]


def _build_child_table(
    schema_name: str, source_collection: str, parent_table: Table, array_path: str, array_node: "_ShapeNode",
) -> Table:
    """Synthesize a child table for one array field: a surrogate integer
    primary key, a foreign key back to `parent_table`'s own `_id`, and
    either one column per item key (array of sub-documents) or a single
    "value" column (array of scalars)."""
    child_name = f"{parent_table.name}_{array_path.replace('.', '_')}"
    child = Table(
        name=child_name, schema=schema_name,
        source_collection=source_collection, source_array_path=array_path,
    )

    pk_col = Column(name=f"{child_name}_ID", data_type="NUMBER(19)", nullable=False)
    child.columns.append(pk_col)
    child.constraints.append(Constraint(name=f"{child_name}_PK", kind="PRIMARY KEY", columns=[pk_col.name]))

    parent_id_col = parent_table.columns[0]  # always "_id", built first in _build_table
    fk_col_name = f"{parent_table.name}_ID"
    fk_col = Column(name=fk_col_name, data_type=parent_id_col.data_type, nullable=False)
    child.columns.append(fk_col)
    child.constraints.append(Constraint(
        name=f"{child_name}_{fk_col_name}_FK", kind="FOREIGN KEY", columns=[fk_col_name],
        ref_table=parent_table.name, ref_columns=[parent_id_col.name],
    ))
    child.indexes.append(Index(name=f"{child_name}_{fk_col_name}_IDX", columns=[fk_col_name], unique=False))

    issues: List[ConversionIssue] = []
    item_shape = array_node.item_shape
    if item_shape is None:
        issues.append(ConversionIssue(
            "info",
            f"Array field '{array_path}' was empty in every sampled document; no item fields could "
            f"be inferred -- '{child_name}' was created with only its surrogate key and the foreign "
            "key back to its parent.",
        ))
    else:
        item_kind = _dominant_kind(item_shape)
        if len(item_shape.kind_counts) > 1:
            issues.append(ConversionIssue(
                "warning",
                f"Array field '{array_path}': its elements were not all the same shape across the "
                f"sample (counts: {dict(item_shape.kind_counts)}); treated as '{item_kind}', the "
                "most common -- review any elements where it differs.",
            ))
        if item_kind == "object":
            nested_arrays: List[Tuple[str, _ShapeNode]] = []
            child.columns.extend(_flatten_object(item_shape, "", issues, nested_arrays))
            for nested_path, _ in nested_arrays:
                issues.append(ConversionIssue(
                    "warning",
                    f"Array field '{array_path}.{nested_path}' is nested inside another array "
                    "(array-of-arrays); this shape is not automatically normalized into a further "
                    "child table and was dropped -- migrate or model it by hand if it's needed.",
                ))
        elif item_kind == "array":
            issues.append(ConversionIssue(
                "warning",
                f"Array field '{array_path}' contains further nested arrays (array-of-arrays); this "
                "shape is not automatically normalized into a child table and its items were "
                "skipped -- migrate or model it by hand if it's needed.",
            ))
        else:
            child.columns.append(_scalar_column("value", item_shape, item_shape.present_count))

    child.issues = issues
    return child


# ------------------------------------------------------------------- indexes


def _references_array_path(field_paths: List[str], array_paths: List[str]) -> bool:
    return any(fp == ap or fp.startswith(ap + ".") for ap in array_paths for fp in field_paths)


def _introspect_indexes(conn: "MongoConnector", table: Table, array_paths: List[str]) -> None:
    try:
        raw_indexes = list(conn.db[table.name].list_indexes())
    except Exception:  # noqa: BLE001 - a collection with no indexes beyond the default is fine
        raw_indexes = []
    for idx in raw_indexes:
        name = idx.get("name")
        if name == "_id_":
            continue  # already covered by the synthesized PRIMARY KEY constraint
        key = idx.get("key") or {}
        field_paths = list(key.keys())
        if _references_array_path(field_paths, array_paths):
            table.issues.append(ConversionIssue(
                "warning",
                f"Index '{name}' on '{table.name}' references a field normalized into a separate "
                "child table; this cannot be represented as a single-table index and was skipped -- "
                "create an equivalent index on the child table by hand if it's needed.",
            ))
            continue
        table.indexes.append(Index(
            name=name or f"{table.name}_IDX_{len(table.indexes)}",
            columns=field_paths, unique=bool(idx.get("unique", False)),
        ))


# --------------------------------------------------------------------- views


def _introspect_views(conn: "MongoConnector", schema: Schema, view_infos: List[dict]) -> None:
    for info in sorted(view_infos, key=lambda c: c.get("name", "")):
        name = info.get("name", "")
        options = info.get("options") or {}
        definition = json.dumps(
            {"viewOn": options.get("viewOn", ""), "pipeline": options.get("pipeline", [])},
            indent=2, default=str,
        )
        schema.views.append(View(name=name, schema=schema.name, definition=definition, source_engine="MongoDB"))


# --------------------------------------------------------------- entry point


def introspect_schema(conn: "MongoConnector", schema_name: str, sample_size: int = DEFAULT_SAMPLE_SIZE) -> Schema:
    """Build a Schema by sampling every collection in `conn.db` (the
    database the connector was configured for -- `schema_name` is
    accepted for interface parity with every other introspect_schema()
    in this package, matching how introspect_target_mongodb accepts and
    ignores `database_name` for the same reason, since MongoDB has no
    separate schema concept distinct from the database itself)."""
    schema = Schema(name=schema_name, source_engine="MongoDB")
    db = conn.db

    all_collections = list(db.list_collections())
    real_collections = [
        c for c in all_collections
        if c.get("type", "collection") == "collection"
        and not c.get("name", "").startswith("system.")
        and c.get("name") != _SEQUENCE_COUNTER_COLLECTION
    ]
    view_collections = [c for c in all_collections if c.get("type") == "view"]

    for info in sorted(real_collections, key=lambda c: c.get("name", "")):
        name = info.get("name", "")
        sample_docs = list(db[name].aggregate([{"$sample": {"size": sample_size}}]))
        table, child_tables, array_paths = _build_table(schema_name, name, sample_docs)
        _introspect_indexes(conn, table, array_paths)
        schema.tables.append(table)
        schema.tables.extend(child_tables)

    _introspect_views(conn, schema, view_collections)

    return schema
