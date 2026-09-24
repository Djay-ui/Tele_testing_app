"""
Generates target-engine DDL (Oracle, PostgreSQL, MySQL, SQL Server, Db2,
or MongoDB) from a populated tgdatabridge.core.schema_model.Schema, and annotates
each object with a ConversionStatus + list of issues for the assessment
report.

MongoDB is a document database, not a relational one -- the four
generate_*_ddl_mongodb functions below don't emit SQL at all. They emit a
small, tightly-controlled subset of mongosh-flavored JavaScript
(`db.createCollection("name", {...})`, `db[<collection>].createIndex(...)`
/ `.updateOne(...)`) built entirely from json.dumps(...), specifically so
that mongo_connector.MongoConnector.execute_ddl -- which has no real SQL
engine to hand the text off to for parsing, unlike every other target --
can safely parse each statement's arguments back out with json.loads and
re-issue them as real pymongo calls. See mongo_connector.py's module
docstring for the parser side of this contract.
"""
from __future__ import annotations

import json
import re
from typing import Callable, List, NamedTuple, Optional, Tuple

from tgdatabridge.core import type_mapping
from tgdatabridge.core import tsql_dialect
from tgdatabridge.core.schema_model import (
    ConversionIssue, ConversionStatus, Routine, Schema, Sequence, Table, View,
)
from tgdatabridge.utils.identifiers import (
    quote_backtick, quote_bracket, quote_double, quote_literal,
)


def _default_clause(
    col, target_engine: str, source_engine: str, issues: List[ConversionIssue],
) -> str:
    """Return the ` DEFAULT <expr>` fragment for one column, or `''`.

    Column defaults used to be interpolated straight into the generated
    DDL (`line += f" DEFAULT {col.default}"`), which is only correct when
    the source and target happen to spell the expression the same way. For
    a SQL Server source that is almost never true: sys.default_constraints
    reports `(getdate())`, and both MySQL ("Default value expression of
    column 'CreatedDate' contains a disallowed function: getdate", error
    3770) and PostgreSQL ("function getdate() does not exist") reject the
    very first CREATE TABLE carrying one -- which is exactly the failure
    this routes around.

    Translation, the target's own syntax rules (MySQL wants a parenthesised
    expression default, and none at all on TEXT/BLOB/JSON), and the
    decision to *drop* a default the target cannot express rather than emit
    invalid DDL all live in tsql_dialect.translate_default. Anything
    dropped is recorded as an issue, so it surfaces in the assessment
    report instead of disappearing.
    """
    if not col.default or col.identity:
        return ""
    clause, default_issues = tsql_dialect.translate_default(
        col.default, source_engine, target_engine,
        column_name=col.name, target_type=col.target_type or "",
    )
    if default_issues:
        col.issues = list(col.issues) + default_issues
        issues.extend(default_issues)
    if not clause:
        return ""
    return f" DEFAULT {clause}"


_TARGET_MAPPERS = {
    "postgres": type_mapping.to_postgres,
    "mysql": type_mapping.to_mysql,
    "mariadb": type_mapping.to_mysql,
    "sqlserver": type_mapping.to_sqlserver,
    "db2": type_mapping.to_db2,
    "oracle": type_mapping.to_oracle,
}


def _mapper_for(target_engine: str):
    key = (target_engine or "").lower().replace(" ", "")
    for prefix, fn in _TARGET_MAPPERS.items():
        if key.startswith(prefix):
            return fn
    return None


def align_foreign_key_column_types(schema: Schema, target_engine: str) -> List[ConversionIssue]:
    """Make every foreign-key column's generated type identical to the type
    of the column it references.

    Every engine requires the two to match, and two columns of the *same*
    source type can still end up different on the target: an identity
    column is widened to BIGINT so the target will accept it as
    auto-increment (see _identity_type), while a plain column of that same
    source type keeps the faithful mapping. A SQL Server `bigint IDENTITY`
    primary key therefore becomes BIGINT while the `bigint` columns
    referencing it become DECIMAL(19,0), and MySQL rejects the constraint:

        1005 (HY000): Can't create table ... (errno: 150 "Foreign key
        constraint is incorrectly formed")

    -- with no indication that a type mismatch is the cause. Running this
    before any table DDL is generated keeps the two sides in step by
    construction, rather than leaving each generator to rediscover it.

    Returns the issues raised, and records each on the column so the
    change is visible in the assessment report rather than silent.
    """
    issues: List[ConversionIssue] = []
    mapper = _mapper_for(target_engine)
    if mapper is None:
        return issues

    by_name = {t.name.lower(): t for t in schema.tables}

    def effective_type(table: Table, column) -> Optional[str]:
        if column.target_type_override:
            return column.target_type_override
        mapped, _ = mapper(column.data_type)
        if column.identity:
            mapped = _identity_type(column, mapped, target_engine, [])
        return mapped

    for table in schema.tables:
        for cons in table.constraints:
            if cons.kind != "FOREIGN KEY" or not cons.ref_table:
                continue
            ref_table = by_name.get(cons.ref_table.lower())
            if ref_table is None:
                continue
            for local_name, ref_name in zip(cons.columns, cons.ref_columns):
                local = next((c for c in table.columns
                              if c.name.lower() == local_name.lower()), None)
                ref = next((c for c in ref_table.columns
                            if c.name.lower() == ref_name.lower()), None)
                if local is None or ref is None:
                    continue
                wanted = effective_type(ref_table, ref)
                current = effective_type(table, local)
                if not wanted or wanted == current:
                    continue
                local.target_type_override = wanted
                issue = ConversionIssue(
                    "info",
                    f"Column {table.name}.{local.name} was generated as {wanted} to match "
                    f"{ref_table.name}.{ref.name}, which foreign key {cons.name} references; "
                    f"the two must have the same type on {target_engine}.",
                )
                local.issues = list(local.issues) + [issue]
                issues.append(issue)
    return issues


def mysql_must_key_identity(table: Table) -> bool:
    """True when a MySQL CREATE TABLE for `table` must carry a key inline
    even though constraints are being deferred.

    InnoDB requires an AUTO_INCREMENT column to be part of a key *at the
    moment the table is created* -- "there can be only one auto column and
    it must be defined as a key", error 1075. Deferring the primary key of
    a table with an identity column therefore does not merely postpone
    index maintenance, it makes the CREATE TABLE itself illegal: with
    "Defer constraints" ticked, *every* table with an identity column
    failed, and then every post-load ALTER failed after it because none of
    the tables existed. On the demo schema that is the entire migration.

    The fix keeps the primary key (or, absent one, a plain index on the
    identity column) inline for exactly those tables and defers everything
    else -- secondary indexes, unique and check constraints, foreign keys
    and triggers -- so the two-phase load keeps almost all of its benefit.
    A primary key on an auto-increment surrogate is also the cheapest
    index there is to maintain during a load: values arrive in ascending
    order, so every insert appends to the rightmost page.
    """
    return any(col.identity for col in table.columns)


_MYSQL_AUTOINC_TYPES = ("TINYINT", "SMALLINT", "MEDIUMINT", "INT", "INTEGER", "BIGINT")
_POSTGRES_IDENTITY_TYPES = ("SMALLINT", "INT", "INTEGER", "BIGINT")


def _identity_type(
    col, target_type: str, target_engine: str, issues: List[ConversionIssue],
) -> str:
    """Coerce an identity column's target type to one the target actually
    allows an auto-increment on.

    The pivot type mapping is faithful rather than target-aware: a SQL
    Server `BIGINT` becomes `NUMBER(19)` and then `DECIMAL(19,0)` on
    MySQL / `NUMERIC(19)` on PostgreSQL. Both are perfectly good numeric
    types and completely invalid as auto-increment columns -- MySQL
    answers with error 1063 "Incorrect column specifier for column", and
    PostgreSQL with "identity column type must be smallint, integer, or
    bigint". The table simply cannot be created.

    Widening to BIGINT is safe in both directions here: it is the widest
    integer either engine offers, and an identity column is a surrogate
    key whose only requirements are uniqueness and monotonicity. Oracle,
    SQL Server and Db2 all accept a fixed-point identity, so they are
    deliberately left alone."""
    engine = target_engine.lower()
    base = (target_type or "").split("(")[0].strip().upper()
    if engine.startswith("mysql") or engine.startswith("mariadb"):
        allowed, name = _MYSQL_AUTOINC_TYPES, "MySQL"
    elif engine.startswith("postgres"):
        allowed, name = _POSTGRES_IDENTITY_TYPES, "PostgreSQL"
    else:
        return target_type
    if base in allowed:
        return target_type
    issue = ConversionIssue(
        "info",
        f"Column {col.name} is an identity/auto-increment column typed {target_type}, which "
        f"{name} does not accept for one; it was widened to BIGINT.",
    )
    col.issues = list(col.issues) + [issue]
    issues.append(issue)
    return "BIGINT"


def _check_condition(
    cons, target_engine: str, source_engine: str, issues: List[ConversionIssue],
) -> Optional[str]:
    """Same treatment as _default_clause, for a CHECK constraint body --
    a SQL Server CHECK arrives as `([Salary]>(0))`, whose bracket-quoting
    is a syntax error on both MySQL and PostgreSQL."""
    if not cons.check_condition:
        return None
    condition, check_issues = tsql_dialect.translate_check_condition(
        cons.check_condition, source_engine, target_engine, constraint_name=cons.name)
    issues.extend(check_issues)
    return condition


def _worst_status(issues: List[ConversionIssue]) -> ConversionStatus:
    if any(i.severity == "error" for i in issues):
        return ConversionStatus.MANUAL
    if any(i.severity == "warning" for i in issues):
        return ConversionStatus.AUTOMATIC_WITH_WARNINGS
    return ConversionStatus.AUTOMATIC


def _quote_pg(identifier: str) -> str:
    # Lowercased on purpose: PostgreSQL folds *unquoted* identifiers to
    # lowercase, and view/routine bodies are copied through from Oracle
    # largely verbatim with their original (unquoted) references — e.g. a
    # view's "FROM CUSTOMER" is parsed by Postgres as "FROM customer". If we
    # quoted-and-preserved the original uppercase here instead, our objects
    # (created as "CUSTOMER") would never match those lowercase-folded
    # references, producing "relation \"customer\" does not exist" the
    # moment anything references the object by its unquoted name. Quoting
    # in lowercase keeps identifiers safe if they collide with a reserved
    # word while staying consistent with everything Postgres itself folds.
    #
    # Delimiter escaping lives in utils.identifiers -- see that module for
    # why an unescaped f-string here was both a correctness bug and an
    # injection vector.
    return quote_double(identifier.lower())


def _quote_mysql(identifier: str) -> str:
    return quote_backtick(identifier)


def _quote_sqlserver(identifier: str, schema: Optional[str] = None) -> str:
    # Case-preserved and bracket-quoted, matching MySQL's approach above
    # rather than Postgres's lowercase-and-quote: SQL Server's default
    # collation is case-insensitive, so there's no equivalent risk of an
    # unquoted verbatim-copied reference in a view/routine body silently
    # resolving to a different-cased object.
    ident = quote_bracket(identifier)
    return f"{quote_bracket(schema)}.{ident}" if schema else ident


def _quote_db2(identifier: str, schema: Optional[str] = None) -> str:
    # Uppercased and double-quoted: Db2, like Oracle, folds *unquoted*
    # identifiers to uppercase, and object names arrive here already
    # uppercase (Oracle's own unquoted default) -- uppercasing here keeps
    # generated references, and this module's own SYSCAT existence-check
    # literals, consistent with each other and with what Db2's catalog
    # actually stores for a quoted-but-uppercase identifier.
    ident = quote_double(identifier.upper())
    return f"{quote_double(schema.upper())}.{ident}" if schema else ident


def _quote_oracle(identifier: str, schema: Optional[str] = None) -> str:
    # Uppercased and double-quoted, exactly mirroring _quote_db2 above (Db2
    # and Oracle share the same unquoted-identifier-folds-to-uppercase
    # convention) -- object names arrive here already uppercase for an
    # Oracle-sourced schema (Oracle's own unquoted default), but forcing
    # uppercase here too keeps a MySQL/PostgreSQL-sourced schema's
    # naturally-lowercase names consistent with Oracle's own folding,
    # instead of silently creating a case-sensitive, always-needs-quoting
    # object no other tool/script connecting to this target would expect.
    ident = quote_double(identifier.upper())
    return f"{quote_double(schema.upper())}.{ident}" if schema else ident


#: A plain Oracle column name is a bare identifier -- never contains "(".
#: An Index.columns entry that does is not a column name at all: it is the
#: expression text of an Oracle function-based index, substituted by
#: introspector.py (see its own comment) in place of the meaningless
#: SYS_NC#####$ hidden-column name Oracle's data dictionary otherwise
#: reports for one. This is the one signal available to tell the two
#: apart without adding a new field threaded through every caller of
#: Index.columns.
def _is_index_expression(column_or_expression: str) -> bool:
    return "(" in column_or_expression


def _index_column_sql(quote_fn: Callable[..., str], column_or_expression: str, *quote_args) -> str:
    """One entry of an Index.columns list, rendered for a CREATE INDEX
    statement: `quote_fn(column_or_expression, *quote_args)` for an
    ordinary column (unchanged behavior), or the expression rendered
    as-is (not quoted as if it were a single identifier) for a
    function-based index column -- see _is_index_expression.

    Oracle's ALL_IND_EXPRESSIONS stores an expression with its own
    identifiers already double-quoted where Oracle chose to preserve
    exact case (e.g. `LOWER("EMAIL")`). Left in place, those quotes
    would make the *target* database look for a column matching that
    exact quoted case -- which fails on every target this tool creates,
    since every one of them was just uppercased or lowercased by the
    _quote_* function above, never quoted-and-case-preserved. Stripping
    the quotes turns each identifier back into a bare, unquoted token,
    which every target here then folds to the same case it already
    folds a plain column reference to (lowercase on PostgreSQL,
    uppercase on Oracle/Db2, case-preserved-but-effectively-consistent
    on MySQL/SQL Server) -- exactly matching how this tool created that
    same column, with no per-dialect casing logic needed here at all.

    This is a best-effort passthrough, not a real expression translator:
    a simple case like `LOWER(EMAIL)` or `UPPER(NAME)` survives as valid
    SQL on every target here unchanged, but an expression built from an
    Oracle-only function (NVL, DECODE, ...) would need the same kind of
    real translation plsql_converter.py does for procedure/function
    bodies, which this does not attempt. See the ConversionIssue this
    is always paired with at each call site.
    """
    if _is_index_expression(column_or_expression):
        return column_or_expression.replace('"', "")
    return quote_fn(column_or_expression, *quote_args)


def _index_expression_issue(idx_name: str, expression: str) -> ConversionIssue:
    return ConversionIssue(
        "warning",
        f"Index {idx_name} is built on an expression ({expression}), not a plain column -- Oracle "
        "reports these via a hidden SYS_NC#####$ column name, which this tool resolves back to the "
        "real expression via ALL_IND_EXPRESSIONS. The expression is carried over as-is (not "
        "translated the way a procedure/function body would be), so a simple case like LOWER(...) "
        "or UPPER(...) should work unchanged, but an expression using an Oracle-only function may "
        "need manual adjustment on the target.",
    )


def _mongo_index_has_expression_column(idx) -> bool:
    return any(_is_index_expression(c) for c in idx.columns)


def _mongo_index_expression_issue(idx_name: str) -> ConversionIssue:
    return ConversionIssue(
        "warning",
        f"Index {idx_name} is based on an Oracle expression (a function-based index), which "
        "MongoDB has no equivalent for -- an index there is always on a real field, never a "
        "computed expression. This index was not created; if the application needs to query by "
        "this computed value, store it as its own field (maintained by the application) instead.",
    )


#: Oracle-internal, undocumented functions Oracle itself weaves into an
#: index's expression for its own bookkeeping -- most commonly
#: SYS_OP_MAP_NONNULL, wrapped around every column of the unique index
#: Oracle automatically creates on a materialized view's container table
#: (named I_SNAP$_<mv name>) to support fast refresh, so that two NULLs
#: compare as distinct for its own internal maintenance. Unlike
#: LOWER(...)/UPPER(...) in _index_column_sql above, this is not a "best
#: effort passthrough might still work" case: SYS_OP_* has no meaning and
#: no equivalent on any target here, and enforces nothing a real business
#: rule ever depended on -- it is Oracle's own plumbing, resolved via
#: ALL_IND_EXPRESSIONS the same way a real function-based index's
#: expression is (see introspector.py), but never something migrated data
#: needs replicated.
_ORACLE_INTERNAL_FUNCTION_RE = re.compile(r"\bSYS_OP_[A-Z0-9_]+\s*\(", re.IGNORECASE)


def _is_oracle_internal_expression(column_or_expression: str) -> bool:
    return bool(_ORACLE_INTERNAL_FUNCTION_RE.search(column_or_expression))


def _has_oracle_internal_expression(idx) -> bool:
    return any(_is_oracle_internal_expression(c) for c in idx.columns)


def _oracle_internal_index_issue(idx_name: str) -> ConversionIssue:
    return ConversionIssue(
        "info",
        f"Index {idx_name} was not created -- it looks like an object Oracle generates for its "
        "own internal bookkeeping rather than a real user index (for example, the unique index "
        "Oracle automatically creates on a materialized view's container table to support fast "
        "refresh), built on an expression that calls an undocumented, Oracle-only function. There "
        "is nothing to replicate on the target: nothing here relies on it, and no target database "
        "has an equivalent.",
    )


def _oracle_idempotent(inner_sql: str, ignore_codes: Tuple[int, ...] = (-955,)) -> str:
    """Wraps one DDL statement (no trailing ';') in the standard Oracle
    idiom for idempotent DDL: run it via EXECUTE IMMEDIATE inside a PL/SQL
    block, swallowing exactly the "already exists"-flavored ORA-xxxxx
    error(s) in `ignore_codes` -- Oracle has no CREATE ... IF NOT EXISTS
    for any object type (unlike Postgres/MySQL above), so re-running the
    same generated script against a target that's already been set up
    would otherwise abort outright. Default -955 ("name is already used
    by an existing object") covers CREATE TABLE/SEQUENCE/INDEX/VIEW-
    without-OR-REPLACE, since tables/sequences/indexes/views all share one
    namespace per schema in Oracle; ALTER TABLE ADD CONSTRAINT passes its
    own different code(s) instead -- see generate_foreign_key_ddl_oracle."""
    codes = " AND ".join(f"SQLCODE != {c}" for c in ignore_codes)
    return (
        f"BEGIN\n"
        f"  EXECUTE IMMEDIATE {_sql_quote(inner_sql)};\n"
        f"EXCEPTION\n"
        f"  WHEN OTHERS THEN\n"
        f"    IF {codes} THEN\n"
        f"      RAISE;\n"
        f"    END IF;\n"
        f"END;"
    )


def _db2_idempotent(
    inner_sql: str, catalog_table: str, name_column: str, object_name: str,
    schema_column: str = "", schema: Optional[str] = None,
) -> str:
    """The generic form of the SYSCAT-existence-check-then-EXECUTE-
    IMMEDIATE idiom generate_table_ddl_db2/generate_sequence_ddl_db2 above
    each hand-roll inline for CREATE (guarded with IF NOT EXISTS) --
    reused here by generate_rollback_ddl's DROP statements with the
    condition inverted (IF EXISTS), since Db2 has no DROP ... IF EXISTS
    for any object type either. `catalog_table`/`name_column`/
    `schema_column` vary per object type (e.g. "SYSCAT.TABLES"/"TABNAME"/
    "TABSCHEMA" for a table or view, "SYSCAT.SEQUENCES"/"SEQNAME"/
    "SEQSCHEMA" for a sequence) -- see target_introspector.
    introspect_target_db2 for the same catalog shapes used read-only."""
    schema_check = f"{schema_column} = {quote_literal(schema.upper())} AND " if (schema and schema_column) else ""
    return (
        f"BEGIN\n"
        f"  IF EXISTS (SELECT 1 FROM {catalog_table} WHERE {schema_check}{name_column} = {quote_literal(object_name.upper())}) THEN\n"
        f"    EXECUTE IMMEDIATE {_sql_quote(inner_sql)};\n"
        f"  END IF;\n"
        f"END;"
    )


def generate_table_ddl_oracle(
    table: Table, schema: Optional[str] = None, defer_constraints: bool = False,
    source_engine: str = "Oracle",
) -> Tuple[str, List[ConversionIssue]]:
    """Generates real Oracle DDL for an Oracle *target* -- structurally
    identical to generate_table_ddl_db2 above (same column-line/PK/UNIQUE/
    CHECK/index construction), but using type_mapping.to_oracle() (close
    to an identity mapping -- see its own docstring) and _oracle_idempotent
    (catching ORA-00955) instead of Db2's SYSCAT.TABLES-existence-check
    idiom for idempotent re-runs."""
    issues: List[ConversionIssue] = []
    col_lines = []
    for col in table.columns:
        target_type, col_issues = type_mapping.to_oracle(col.data_type)
        if col.target_type_override:
            target_type = col.target_type_override
        col.target_type = target_type
        # Recombine (not append-to-and-keep-growing) any source-side
        # reverse-mapping issues already on this column -- see the matching
        # comment in generate_table_ddl_postgres for why.
        combined_col_issues = col.source_issues + col_issues
        col.issues = combined_col_issues
        issues.extend(combined_col_issues)

        line = f"  {_quote_oracle(col.name)} {target_type}"
        if col.identity:
            line += " GENERATED ALWAYS AS IDENTITY"
        if not col.nullable:
            line += " NOT NULL"
        line += _default_clause(col, "Oracle", source_engine, issues)
        col_lines.append(line)

    if not defer_constraints:
        pk = next((c for c in table.constraints if c.kind == "PRIMARY KEY"), None)
        if pk:
            cols = ", ".join(_quote_oracle(c) for c in pk.columns)
            col_lines.append(f"  CONSTRAINT {_quote_oracle(pk.name)} PRIMARY KEY ({cols})")

        for cons in table.constraints:
            if cons.kind == "UNIQUE":
                cols = ", ".join(_quote_oracle(c) for c in cons.columns)
                col_lines.append(f"  CONSTRAINT {_quote_oracle(cons.name)} UNIQUE ({cols})")
            elif cons.kind == "CHECK" and cons.check_condition:
                condition = _check_condition(cons, "Oracle", source_engine, issues)
                col_lines.append(f"  CONSTRAINT {_quote_oracle(cons.name)} CHECK ({condition})")

    qualified = _quote_oracle(table.name, schema)
    inner_ddl = f"CREATE TABLE {qualified} (\n" + ",\n".join(f"  {line.strip()}" for line in col_lines) + "\n)"
    ddl = _oracle_idempotent(inner_ddl)

    # Foreign keys are deliberately NOT emitted here — see
    # generate_foreign_key_ddl_oracle and its call site in generate_schema_ddl.

    if not defer_constraints:
        for idx in table.indexes:
            if idx.name in (c.name for c in table.constraints if c.kind in ("PRIMARY KEY", "UNIQUE")):
                continue  # already created implicitly by the constraint
            if _has_oracle_internal_expression(idx):
                issues.append(_oracle_internal_index_issue(idx.name))
                continue
            unique_kw = "UNIQUE " if idx.unique else ""
            cols = ", ".join(_index_column_sql(_quote_oracle, c) for c in idx.columns)
            if any(_is_index_expression(c) for c in idx.columns):
                issues.append(_index_expression_issue(idx.name, cols))
            idx_ddl = f"CREATE {unique_kw}INDEX {_quote_oracle(idx.name, schema)} ON {qualified} ({cols})"
            ddl += "\n" + _oracle_idempotent(idx_ddl)

    if table.comment:
        # COMMENT ON is idempotent on its own (it just (re)sets the comment
        # value, no "already exists" failure mode), so this needs no guard.
        ddl += f"\nCOMMENT ON TABLE {qualified} IS {_sql_quote(table.comment)};"

    table.status = _worst_status(issues)
    table.issues = issues
    return ddl, issues


def _mongo_collection_ref(collection_name: str) -> str:
    # Bracket notation (db["name"], not db.name) so this is always valid
    # JS regardless of what characters the collection name contains --
    # unlike the four SQL _quote_* helpers above, there's no reserved-word/
    # case-folding concern to encode here, just "make this a syntactically
    # safe property access." mongo_connector.py's execute_ddl parser
    # specifically expects this exact db[<json-string>].method(...) shape.
    return f"db[{json.dumps(collection_name)}]"


class _PartitionPlan(NamedTuple):
    by_clause: str                 # e.g. 'PARTITION BY RANGE ("sale_date")'
    child_statements: List[str]    # one 'CREATE TABLE IF NOT EXISTS ... PARTITION OF ...;' per partition
    dropped_constraints: set       # names of PK/UNIQUE constraints that must not be emitted at all


_RANGE_BOUND_LITERAL_RE = re.compile(r"^'(?:[^']|'')*'$")
# Oracle's ALL_TAB_PARTITIONS.HIGH_VALUE stores a RANGE bound as a literal
# SQL expression, most commonly a TO_DATE/TO_TIMESTAMP call (Oracle always
# needs the format-mask argument to interpret the date/timestamp string
# literal it's also storing there) -- e.g.
# TO_DATE(' 2024-02-01 00:00:00', 'SYYYY-MM-DD HH24:MI:SS'). PostgreSQL
# infers a partition bound's type from the partitioning column itself, so
# the format mask is irrelevant on this side -- only the value string
# Oracle already formatted through it matters, and Oracle's own dictionary
# output for this idiom is close enough to ISO 8601 that passing it
# through as a bare quoted literal (no cast operator needed at this
# syntactic position) parses correctly.
_TO_DATE_BOUND_RE = re.compile(
    r"^\s*TO_(?:DATE|TIMESTAMP)\s*\(\s*'((?:[^']|'')*)'\s*,\s*'[^']*'\s*(?:,\s*'[^']*'\s*)?\)\s*$",
    re.IGNORECASE,
)


def _translate_range_bound(raw: str) -> Optional[str]:
    """One comma-separated element of a RANGE partition's HIGH_VALUE,
    translated into a PostgreSQL partition-bound literal -- or None if this
    isn't a shape this tool is confident it understands (see
    _plan_partition_ddl_postgres, which falls back to a plain,
    unpartitioned table rather than risk a silently wrong boundary)."""
    raw = raw.strip()
    if raw.upper() == "MAXVALUE":
        return "MAXVALUE"
    to_date_m = _TO_DATE_BOUND_RE.match(raw)
    if to_date_m:
        return "'" + to_date_m.group(1) + "'"
    if _RANGE_BOUND_LITERAL_RE.match(raw) or re.match(r"^-?\d+(\.\d+)?$", raw):
        return raw
    return None


def _partition_key_covered(covering_columns, partition_columns) -> bool:
    covering_upper = {c.upper() for c in covering_columns}
    return all(pc.upper() in covering_upper for pc in partition_columns)


def _plan_partition_ddl_postgres(table: Table) -> Tuple[Optional[_PartitionPlan], Optional[str]]:
    """Turns table.partition_scheme into PostgreSQL native declarative-
    partitioning DDL, or declines to (returning (None, reason)) so the
    caller migrates the table as a single, plain table instead --
    matching ora2pg's own documented scope decision for the cases this
    doesn't cover (composite/subpartitioned schemes, HASH, and a RANGE
    boundary expression this tool doesn't recognize as safely translatable)
    rather than emitting a partitioning scheme that might not mean what
    Oracle's did, or worse, DDL Postgres rejects outright at apply time.

    Returns (plan, None) on success, or (None, reason) when this table
    should be migrated unpartitioned -- `reason` is already a complete
    sentence ready to become a warning ConversionIssue. dropped_constraints
    on a successful plan names every PK/UNIQUE constraint that must be
    left out of the emitted DDL entirely (both the immediate CREATE TABLE
    path and the deferred/post-load ALTER TABLE path): PostgreSQL rejects
    any unique constraint on a partitioned table whose columns don't cover
    every partition-key column outright ("unique constraint on partitioned
    table must include all partitioning columns"), so there is no safe way
    to emit one that doesn't -- the caller is expected to turn each into
    its own warning issue naming the specific constraint.
    """
    scheme = table.partition_scheme
    if scheme is None:
        return None, None

    if scheme.subpartitioning_type or scheme.kind not in ("RANGE", "LIST"):
        # Composite (any SUBPARTITIONING_TYPE other than NONE) and HASH are
        # both out of scope here, the same limitation ora2pg itself
        # documents for HASH ("explicitly unsupported and skipped with a
        # warning") -- migrated as a single ordinary table rather than
        # losing the table (and, if this fired instead of the caller
        # falling back safely, its data) outright.
        kind_desc = f"{scheme.kind}-{scheme.subpartitioning_type}" if scheme.subpartitioning_type else scheme.kind
        return None, (
            f"Table {table.name} is {kind_desc}-partitioned in Oracle; native PostgreSQL declarative "
            f"partitioning is only generated here for a simple RANGE or LIST scheme, so it was "
            f"migrated as a single, unpartitioned table instead. Repartition it by hand afterward if "
            f"partition-level maintenance (pruning, independent reindexing/vacuum, fast bulk drop) "
            f"still matters on the target."
        )

    if not scheme.columns:
        return None, (
            f"Table {table.name} is reported as {scheme.kind}-partitioned but no partition-key column "
            f"could be resolved; migrated as a single, unpartitioned table instead."
        )

    from tgdatabridge.core.plsql_converter import split_top_level

    by_clause = f"PARTITION BY {scheme.kind} ({', '.join(_quote_pg(c) for c in scheme.columns)})"
    children: List[str] = []
    sorted_partitions = sorted(scheme.partitions, key=lambda p: p.position)

    if scheme.kind == "RANGE":
        previous_bound: Optional[List[str]] = None
        for part in sorted_partitions:
            raw_parts = split_top_level(part.high_value or "", ",")
            translated = [_translate_range_bound(p) for p in raw_parts]
            if not raw_parts or any(t is None for t in translated):
                return None, (
                    f"Table {table.name}: partition '{part.name}'s boundary ({part.high_value!r}) is "
                    f"not a plain literal, MAXVALUE, or a simple TO_DATE/TO_TIMESTAMP(...) call this "
                    f"tool is confident it can translate correctly -- the whole table was migrated as "
                    f"a single, unpartitioned table instead rather than risk a wrong boundary. "
                    f"Repartition it by hand after migration."
                )
            from_clause = ", ".join(previous_bound) if previous_bound else ", ".join(
                "MINVALUE" for _ in translated)
            children.append(
                f"CREATE TABLE IF NOT EXISTS {_quote_pg(part.name)} PARTITION OF {_quote_pg(table.name)} "
                f"FOR VALUES FROM ({from_clause}) TO ({', '.join(translated)});"
            )
            previous_bound = translated
    else:  # LIST
        for part in sorted_partitions:
            raw = (part.high_value or "").strip()
            if raw.upper() == "DEFAULT":
                children.append(
                    f"CREATE TABLE IF NOT EXISTS {_quote_pg(part.name)} PARTITION OF "
                    f"{_quote_pg(table.name)} DEFAULT;"
                )
            else:
                children.append(
                    f"CREATE TABLE IF NOT EXISTS {_quote_pg(part.name)} PARTITION OF "
                    f"{_quote_pg(table.name)} FOR VALUES IN ({raw});"
                )

    if not children:
        return None, (
            f"Table {table.name} is {scheme.kind}-partitioned but no partitions were found in the "
            f"catalog; migrated as a single, unpartitioned table instead."
        )

    # PostgreSQL flatly rejects a PRIMARY KEY/UNIQUE constraint on a
    # partitioned table that doesn't cover every partition-key column
    # ("unique constraint on partitioned table must include all
    # partitioning columns") -- confirmed directly against a live
    # PostgreSQL 16 server while building this. There's no safe rewrite
    # (widening the key changes what "unique" means), so any constraint
    # like this is left out of the DDL entirely rather than shipped to
    # fail "Apply DDL to Target".
    dropped = {
        cons.name for cons in table.constraints
        if cons.kind in ("PRIMARY KEY", "UNIQUE")
        and not _partition_key_covered(cons.columns, scheme.columns)
    }

    return _PartitionPlan(by_clause=by_clause, child_statements=children, dropped_constraints=dropped), None


def generate_table_ddl_postgres(
    table: Table, defer_constraints: bool = False, source_engine: str = "Oracle",
) -> Tuple[str, List[ConversionIssue]]:
    """`defer_constraints=True` emits the table's columns only -- no
    PRIMARY KEY/UNIQUE/CHECK and no indexes -- so a bulk load into it
    isn't paying index maintenance and constraint checking per row. The
    omitted objects are emitted by generate_deferred_ddl_postgres instead,
    to be applied after the data lands. See generate_schema_ddl_phased and
    SCALE.md section 1.3.

    NOT NULL, DEFAULT and IDENTITY are deliberately *not* deferred: they
    cost essentially nothing per row (no index to maintain, no second
    structure to update), and NOT NULL in particular is far cheaper to
    enforce during the load than to add afterwards, when the whole table
    has to be re-scanned to prove it holds."""
    issues: List[ConversionIssue] = []
    partition_plan, partition_reason = _plan_partition_ddl_postgres(table)
    if partition_reason:
        issues.append(ConversionIssue("warning", partition_reason))
    dropped_constraints = partition_plan.dropped_constraints if partition_plan else set()
    for dropped_name in dropped_constraints:
        cons = next(c for c in table.constraints if c.name == dropped_name)
        issues.append(ConversionIssue(
            "warning",
            f"Constraint {cons.name} ({cons.kind}) on partitioned table {table.name} was not created: "
            f"PostgreSQL requires every unique constraint on a partitioned table to include all of its "
            f"partition-key column(s) ({', '.join(table.partition_scheme.columns)}), which "
            f"({', '.join(cons.columns)}) does not. Either add the partition-key column(s) to this key, "
            f"or recreate it as a plain (non-unique) index, by hand.",
        ))
    col_lines = []
    for col in table.columns:
        target_type, col_issues = type_mapping.to_postgres(col.data_type)
        if col.target_type_override:
            target_type = col.target_type_override
        col.target_type = target_type
        # Recombine (not append-to-and-keep-growing) any source-side
        # reverse-mapping issues already on this column -- e.g. from
        # type_mapping.from_mysql for a MySQL-sourced schema -- with this
        # target engine's own fresh issues, so DDL generation continues to
        # be safely re-runnable (switching target engine, or regenerating
        # for the same one, never duplicates issues) while no longer
        # silently discarding the source-side ones the way a bare
        # `col.issues = col_issues` would. Always a no-op merge for an
        # Oracle-sourced column, since col.source_issues is always empty there.
        combined_col_issues = col.source_issues + col_issues
        col.issues = combined_col_issues
        issues.extend(combined_col_issues)

        if col.identity:
            target_type = _identity_type(col, target_type, "PostgreSQL", issues)
            col.target_type = target_type
        line = f"  {_quote_pg(col.name)} {target_type}"
        if col.identity:
            line += " GENERATED ALWAYS AS IDENTITY"
        if not col.nullable:
            line += " NOT NULL"
        line += _default_clause(col, "PostgreSQL", source_engine, issues)
        col_lines.append(line)

    if not defer_constraints:
        pk = next((c for c in table.constraints if c.kind == "PRIMARY KEY"), None)
        if pk and pk.name not in dropped_constraints:
            cols = ", ".join(_quote_pg(c) for c in pk.columns)
            col_lines.append(f"  CONSTRAINT {_quote_pg(pk.name)} PRIMARY KEY ({cols})")

        for cons in table.constraints:
            if cons.kind == "UNIQUE":
                if cons.name in dropped_constraints:
                    continue
                cols = ", ".join(_quote_pg(c) for c in cons.columns)
                col_lines.append(f"  CONSTRAINT {_quote_pg(cons.name)} UNIQUE ({cols})")
            elif cons.kind == "CHECK" and cons.check_condition:
                condition = _check_condition(cons, "PostgreSQL", source_engine, issues)
                col_lines.append(f"  CONSTRAINT {_quote_pg(cons.name)} CHECK ({condition})")

    # IF NOT EXISTS: makes re-running the same generated DDL against a target
    # that already has this table a no-op instead of "relation already
    # exists" — routine when iterating on "Apply DDL to Target" against a
    # partially-populated target from a previous run. The PARTITION BY
    # clause -- when this table qualifies, see _plan_partition_ddl_postgres
    # -- has to be part of this same CREATE TABLE statement: PostgreSQL has
    # no way to declare a table partitioned after the fact, so unlike every
    # other constraint/index here this can never be deferred to the
    # post-load phase (SCALE.md section 1.3) -- it, and every one of its
    # child partitions, must exist before any row can be loaded at all.
    header = f"CREATE TABLE IF NOT EXISTS {_quote_pg(table.name)} ("
    if partition_plan:
        ddl = header + "\n" + ",\n".join(col_lines) + f"\n) {partition_plan.by_clause};"
        for child_ddl in partition_plan.child_statements:
            ddl += "\n" + child_ddl
    else:
        ddl = header + "\n" + ",\n".join(col_lines) + "\n);"

    # Foreign keys are deliberately NOT emitted here — see generate_foreign_key_ddl_postgres
    # and its call site in generate_schema_ddl for why.

    if not defer_constraints:
        for idx in table.indexes:
            if idx.name in (c.name for c in table.constraints if c.kind in ("PRIMARY KEY", "UNIQUE")):
                continue  # already created implicitly by the constraint
            if _has_oracle_internal_expression(idx):
                issues.append(_oracle_internal_index_issue(idx.name))
                continue
            if (idx.unique and partition_plan
                    and not _partition_key_covered(idx.columns, table.partition_scheme.columns)):
                # Same PostgreSQL restriction as a PRIMARY KEY/UNIQUE
                # constraint above, for a unique index that isn't backed by
                # one -- see _plan_partition_ddl_postgres's docstring.
                issues.append(ConversionIssue(
                    "warning",
                    f"Unique index {idx.name} on partitioned table {table.name} was not created: "
                    f"PostgreSQL requires every unique index on a partitioned table to include all of "
                    f"its partition-key column(s) ({', '.join(table.partition_scheme.columns)}), which "
                    f"({', '.join(idx.columns)}) does not. Recreate it as a plain (non-unique) index, "
                    f"or include the partition-key column(s), by hand.",
                ))
                continue
            unique_kw = "UNIQUE " if idx.unique else ""
            cols = ", ".join(_index_column_sql(_quote_pg, c) for c in idx.columns)
            if any(_is_index_expression(c) for c in idx.columns):
                issues.append(_index_expression_issue(idx.name, cols))
            ddl += f"\nCREATE {unique_kw}INDEX IF NOT EXISTS {_quote_pg(idx.name)} ON {_quote_pg(table.name)} ({cols});"

    if table.comment:
        ddl += f"\nCOMMENT ON TABLE {_quote_pg(table.name)} IS {_sql_quote(table.comment)};"

    table.status = _worst_status(issues)
    table.issues = issues
    return ddl, issues


def generate_table_ddl_mysql(
    table: Table, defer_constraints: bool = False, source_engine: str = "Oracle",
) -> Tuple[str, List[ConversionIssue]]:
    """`defer_constraints`: see generate_table_ddl_postgres."""
    issues: List[ConversionIssue] = []
    col_lines = []
    for col in table.columns:
        target_type, col_issues = type_mapping.to_mysql(col.data_type)
        if col.target_type_override:
            target_type = col.target_type_override
        col.target_type = target_type
        # Recombine (not append-to-and-keep-growing) any source-side
        # reverse-mapping issues already on this column -- e.g. from
        # type_mapping.from_mysql for a MySQL-sourced schema -- with this
        # target engine's own fresh issues, so DDL generation continues to
        # be safely re-runnable (switching target engine, or regenerating
        # for the same one, never duplicates issues) while no longer
        # silently discarding the source-side ones the way a bare
        # `col.issues = col_issues` would. Always a no-op merge for an
        # Oracle-sourced column, since col.source_issues is always empty there.
        combined_col_issues = col.source_issues + col_issues
        col.issues = combined_col_issues
        issues.extend(combined_col_issues)

        if col.identity:
            target_type = _identity_type(col, target_type, "MySQL", issues)
            col.target_type = target_type
        line = f"  {_quote_mysql(col.name)} {target_type}"
        if col.identity:
            line += " AUTO_INCREMENT"
        if not col.nullable:
            line += " NOT NULL"
        line += _default_clause(col, "MySQL", source_engine, issues)
        col_lines.append(line)

    pk = next((c for c in table.constraints if c.kind == "PRIMARY KEY"), None)
    if not defer_constraints:
        if pk:
            cols = ", ".join(_quote_mysql(c) for c in pk.columns)
            col_lines.append(f"  PRIMARY KEY ({cols})")
    elif mysql_must_key_identity(table):
        # InnoDB rejects a CREATE TABLE whose AUTO_INCREMENT column is not
        # part of a key (error 1075), so this one key cannot be deferred --
        # see mysql_must_key_identity for the full reasoning. Everything
        # else about the deferral is unchanged.
        identity_cols = [c.name for c in table.columns if c.identity]
        if pk:
            cols = ", ".join(_quote_mysql(c) for c in pk.columns)
            col_lines.append(f"  PRIMARY KEY ({cols})")
        else:
            cols = ", ".join(_quote_mysql(c) for c in identity_cols)
            col_lines.append(f"  KEY {_quote_mysql('idx_' + table.name + '_autoinc')} ({cols})")
        issues.append(ConversionIssue(
            "info",
            f"Table {table.name} has an AUTO_INCREMENT column, which MySQL requires to be part of a "
            f"key when the table is created, so its "
            f"{'primary key' if pk else 'auto-increment index'} was kept inline instead of being "
            f"deferred. Every other constraint, index and trigger was still deferred.",
        ))

    if not defer_constraints:
        for cons in table.constraints:
            if cons.kind == "UNIQUE":
                cols = ", ".join(_quote_mysql(c) for c in cons.columns)
                col_lines.append(f"  UNIQUE KEY {_quote_mysql(cons.name)} ({cols})")
            elif cons.kind == "CHECK" and cons.check_condition:
                condition = _check_condition(cons, "MySQL", source_engine, issues)
                col_lines.append(f"  CONSTRAINT {_quote_mysql(cons.name)} CHECK ({condition})")
            # Foreign keys are deliberately NOT inlined here — see
            # generate_foreign_key_ddl_mysql and its call site in generate_schema_ddl.

    comment_clause = f" COMMENT={_sql_quote(table.comment)}" if table.comment else ""
    # IF NOT EXISTS: see the matching comment in generate_table_ddl_postgres —
    # makes re-applying the same DDL against a partially-created target safe.
    ddl = (
        f"CREATE TABLE IF NOT EXISTS {_quote_mysql(table.name)} (\n" + ",\n".join(col_lines) +
        f"\n) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4{comment_clause};"
    )

    if not defer_constraints:
        for idx in table.indexes:
            if idx.name in (c.name for c in table.constraints if c.kind in ("PRIMARY KEY", "UNIQUE")):
                continue
            if _has_oracle_internal_expression(idx):
                issues.append(_oracle_internal_index_issue(idx.name))
                continue
            unique_kw = "UNIQUE " if idx.unique else ""
            cols = ", ".join(_index_column_sql(_quote_mysql, c) for c in idx.columns)
            if any(_is_index_expression(c) for c in idx.columns):
                issues.append(_index_expression_issue(idx.name, cols))
            ddl += f"\nCREATE {unique_kw}INDEX {_quote_mysql(idx.name)} ON {_quote_mysql(table.name)} ({cols});"

    table.status = _worst_status(issues)
    table.issues = issues
    return ddl, issues


def generate_table_ddl_sqlserver(
    table: Table, schema: Optional[str] = None, defer_constraints: bool = False,
    source_engine: str = "Oracle",
) -> Tuple[str, List[ConversionIssue]]:
    """`defer_constraints`: see generate_table_ddl_postgres."""
    issues: List[ConversionIssue] = []
    col_lines = []
    for col in table.columns:
        target_type, col_issues = type_mapping.to_sqlserver(col.data_type)
        if col.target_type_override:
            target_type = col.target_type_override
        col.target_type = target_type
        # Recombine (not append-to-and-keep-growing) any source-side
        # reverse-mapping issues already on this column -- e.g. from
        # type_mapping.from_mysql for a MySQL-sourced schema -- with this
        # target engine's own fresh issues, so DDL generation continues to
        # be safely re-runnable (switching target engine, or regenerating
        # for the same one, never duplicates issues) while no longer
        # silently discarding the source-side ones the way a bare
        # `col.issues = col_issues` would. Always a no-op merge for an
        # Oracle-sourced column, since col.source_issues is always empty there.
        combined_col_issues = col.source_issues + col_issues
        col.issues = combined_col_issues
        issues.extend(combined_col_issues)

        line = f"  {_quote_sqlserver(col.name)} {target_type}"
        if col.identity:
            line += " IDENTITY(1,1)"
        if not col.nullable:
            line += " NOT NULL"
        line += _default_clause(col, "SQL Server", source_engine, issues)
        col_lines.append(line)

    if not defer_constraints:
        pk = next((c for c in table.constraints if c.kind == "PRIMARY KEY"), None)
        if pk:
            cols = ", ".join(_quote_sqlserver(c) for c in pk.columns)
            col_lines.append(f"  CONSTRAINT {_quote_sqlserver(pk.name)} PRIMARY KEY ({cols})")

        for cons in table.constraints:
            if cons.kind == "UNIQUE":
                cols = ", ".join(_quote_sqlserver(c) for c in cons.columns)
                col_lines.append(f"  CONSTRAINT {_quote_sqlserver(cons.name)} UNIQUE ({cols})")
            elif cons.kind == "CHECK" and cons.check_condition:
                condition = _check_condition(cons, "SQL Server", source_engine, issues)
                col_lines.append(f"  CONSTRAINT {_quote_sqlserver(cons.name)} CHECK ({condition})")

    qualified = _quote_sqlserver(table.name, schema)
    # SQL Server has no CREATE TABLE IF NOT EXISTS -- OBJECT_ID(...) IS NULL
    # is the standard idempotency guard, making re-running the same
    # generated DDL against a target that already has this table a no-op
    # instead of "There is already an object named ... in the database."
    ddl = (
        f"IF OBJECT_ID(N{quote_literal(qualified)}, N'U') IS NULL\n"
        f"BEGIN\n"
        f"  CREATE TABLE {qualified} (\n"
        + ",\n".join(f"  {line.strip()}" for line in col_lines)
        + "\n  );\n"
        f"END;"
    )

    # Foreign keys are deliberately NOT emitted here — see generate_foreign_key_ddl_sqlserver
    # and its call site in generate_schema_ddl for why.

    if not defer_constraints:
        for idx in table.indexes:
            if idx.name in (c.name for c in table.constraints if c.kind in ("PRIMARY KEY", "UNIQUE")):
                continue  # already created implicitly by the constraint
            if _has_oracle_internal_expression(idx):
                issues.append(_oracle_internal_index_issue(idx.name))
                continue
            unique_kw = "UNIQUE " if idx.unique else ""
            cols = ", ".join(_index_column_sql(_quote_sqlserver, c) for c in idx.columns)
            if any(_is_index_expression(c) for c in idx.columns):
                issues.append(_index_expression_issue(idx.name, cols))
            ddl += (
                f"\nIF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = {quote_literal(idx.name)} "
                f"AND object_id = OBJECT_ID(N{quote_literal(qualified)}))\n"
                f"BEGIN\n"
                f"  CREATE {unique_kw}INDEX {_quote_sqlserver(idx.name)} ON {qualified} ({cols});\n"
                f"END;"
            )

    if table.comment:
        # Full extended-property support (sp_addextendedproperty) needs an
        # assumed/hardcoded schema name to address the object reliably;
        # kept as a plain SQL comment instead to avoid that assumption.
        ddl += f"\n-- {table.name}: {table.comment}"

    table.status = _worst_status(issues)
    table.issues = issues
    return ddl, issues


def generate_table_ddl_db2(
    table: Table, schema: Optional[str] = None, defer_constraints: bool = False,
    source_engine: str = "Oracle",
) -> Tuple[str, List[ConversionIssue]]:
    """`defer_constraints`: see generate_table_ddl_postgres."""
    issues: List[ConversionIssue] = []
    col_lines = []
    for col in table.columns:
        target_type, col_issues = type_mapping.to_db2(col.data_type)
        if col.target_type_override:
            target_type = col.target_type_override
        col.target_type = target_type
        # Recombine (not append-to-and-keep-growing) any source-side
        # reverse-mapping issues already on this column -- e.g. from
        # type_mapping.from_mysql for a MySQL-sourced schema -- with this
        # target engine's own fresh issues, so DDL generation continues to
        # be safely re-runnable (switching target engine, or regenerating
        # for the same one, never duplicates issues) while no longer
        # silently discarding the source-side ones the way a bare
        # `col.issues = col_issues` would. Always a no-op merge for an
        # Oracle-sourced column, since col.source_issues is always empty there.
        combined_col_issues = col.source_issues + col_issues
        col.issues = combined_col_issues
        issues.extend(combined_col_issues)

        line = f"  {_quote_db2(col.name)} {target_type}"
        if col.identity:
            line += " GENERATED ALWAYS AS IDENTITY"
        if not col.nullable:
            line += " NOT NULL"
        line += _default_clause(col, "Db2", source_engine, issues)
        col_lines.append(line)

    if not defer_constraints:
        pk = next((c for c in table.constraints if c.kind == "PRIMARY KEY"), None)
        if pk:
            cols = ", ".join(_quote_db2(c) for c in pk.columns)
            col_lines.append(f"  CONSTRAINT {_quote_db2(pk.name)} PRIMARY KEY ({cols})")

        for cons in table.constraints:
            if cons.kind == "UNIQUE":
                cols = ", ".join(_quote_db2(c) for c in cons.columns)
                col_lines.append(f"  CONSTRAINT {_quote_db2(cons.name)} UNIQUE ({cols})")
            elif cons.kind == "CHECK" and cons.check_condition:
                condition = _check_condition(cons, "Db2", source_engine, issues)
                col_lines.append(f"  CONSTRAINT {_quote_db2(cons.name)} CHECK ({condition})")

    qualified = _quote_db2(table.name, schema)
    schema_check = f"TABSCHEMA = {quote_literal(schema.upper())} AND " if schema else ""
    inner_ddl = f"CREATE TABLE {qualified} (\n" + ",\n".join(f"  {line.strip()}" for line in col_lines) + "\n)"
    # Db2 has no CREATE TABLE IF NOT EXISTS -- a SYSCAT.TABLES existence
    # check wrapped in an anonymous compound statement, executing the DDL
    # itself via EXECUTE IMMEDIATE (a bare CREATE TABLE can't appear
    # directly inside an IF/THEN in SQL PL), is the equivalent idempotency
    # guard, making re-running the same generated DDL against a target that
    # already has this table a no-op instead of "SQL0601N ... already exists".
    ddl = (
        f"BEGIN\n"
        f"  IF NOT EXISTS (SELECT 1 FROM SYSCAT.TABLES WHERE {schema_check}TABNAME = {quote_literal(table.name.upper())}) THEN\n"
        f"    EXECUTE IMMEDIATE {_sql_quote(inner_ddl)};\n"
        f"  END IF;\n"
        f"END;"
    )

    # Foreign keys are deliberately NOT emitted here — see generate_foreign_key_ddl_db2
    # and its call site in generate_schema_ddl for why.

    if not defer_constraints:
        for idx in table.indexes:
            if idx.name in (c.name for c in table.constraints if c.kind in ("PRIMARY KEY", "UNIQUE")):
                continue  # already created implicitly by the constraint
            if _has_oracle_internal_expression(idx):
                issues.append(_oracle_internal_index_issue(idx.name))
                continue
            unique_kw = "UNIQUE " if idx.unique else ""
            cols = ", ".join(_index_column_sql(_quote_db2, c) for c in idx.columns)
            if any(_is_index_expression(c) for c in idx.columns):
                issues.append(_index_expression_issue(idx.name, cols))
            idx_ddl = f"CREATE {unique_kw}INDEX {_quote_db2(idx.name, schema)} ON {qualified} ({cols})"
            ddl += (
                f"\nBEGIN\n"
                f"  IF NOT EXISTS (SELECT 1 FROM SYSCAT.INDEXES WHERE {schema_check}INDNAME = {quote_literal(idx.name.upper())}) THEN\n"
                f"    EXECUTE IMMEDIATE {_sql_quote(idx_ddl)};\n"
                f"  END IF;\n"
                f"END;"
            )

    if table.comment:
        # COMMENT ON is idempotent on its own (it just (re)sets the comment
        # value, no "already exists" failure mode), so this needs no guard.
        ddl += f"\nCOMMENT ON TABLE {qualified} IS {_sql_quote(table.comment)};"

    table.status = _worst_status(issues)
    table.issues = issues
    return ddl, issues


# Best-effort CHECK -> $jsonSchema translation for the two most common
# single-column shapes -- a numeric comparison ("salary > 0") or an
# IN-list ("status IN ('ACTIVE','INACTIVE')") -- mirroring
# connect_by_rewriter.py's "handle the one well-known shape automatically,
# flag everything else for manual review" approach rather than attempting
# a general SQL-expression parser. Optionally wrapped in exactly one layer
# of parens, since Oracle's own CHECK constraint text commonly is.
_MONGO_CHECK_NUMERIC_RE = re.compile(
    r'^\(?\s*"?([A-Za-z_][A-Za-z0-9_]*)"?\s*(>=|<=|>|<)\s*(-?\d+(?:\.\d+)?)\s*\)?$'
)
_MONGO_CHECK_IN_RE = re.compile(
    r'^\(?\s*"?([A-Za-z_][A-Za-z0-9_]*)"?\s+IN\s*\(([^)]+)\)\s*\)?$', re.IGNORECASE
)
_MONGO_CHECK_COMPARATOR_KEYS = {
    ">": "exclusiveMinimum", ">=": "minimum", "<": "exclusiveMaximum", "<=": "maximum",
}


def _find_property_case_insensitive(properties: dict, col_name: str) -> Optional[dict]:
    """Oracle identifiers are case-insensitive when unquoted (folded to
    uppercase internally), and this tool's own columns/constraints always
    arrive already-uppercased from the Oracle introspector -- but the raw
    CHECK constraint *text* is copied through verbatim from
    ALL_CONSTRAINTS.SEARCH_CONDITION, which commonly preserves whatever
    case the original DDL author actually typed (often lowercase). An
    exact-case dict lookup would silently fail to match "salary > 0"
    against a "SALARY" column the vast majority of the time -- this does
    the case-insensitive lookup Oracle's own identifier resolution would."""
    if col_name in properties:
        return properties[col_name]
    lowered = col_name.lower()
    for key, prop in properties.items():
        if key.lower() == lowered:
            return prop
    return None


def _apply_check_to_mongo_schema(check_condition: str, properties: dict) -> bool:
    """Mutates `properties` (the $jsonSchema "properties" dict being built
    by generate_table_ddl_mongodb) in place if `check_condition` matches
    one of the recognized shapes above, referencing a column that's
    actually in `properties`. Returns whether it did -- callers should
    flag the constraint for manual review when this comes back False."""
    condition = check_condition.strip()

    m = _MONGO_CHECK_NUMERIC_RE.match(condition)
    if m:
        col_name, op, value = m.group(1), m.group(2), m.group(3)
        prop = _find_property_case_insensitive(properties, col_name)
        if prop is None:
            return False
        prop[_MONGO_CHECK_COMPARATOR_KEYS[op]] = float(value) if "." in value else int(value)
        return True

    m = _MONGO_CHECK_IN_RE.match(condition)
    if m:
        col_name = m.group(1)
        prop = _find_property_case_insensitive(properties, col_name)
        if prop is None:
            return False
        literals: List = []
        for raw_lit in m.group(2).split(","):
            lit = raw_lit.strip()
            if lit.startswith("'") and lit.endswith("'") and len(lit) >= 2:
                literals.append(lit[1:-1].replace("''", "'"))
            else:
                try:
                    literals.append(int(lit))
                except ValueError:
                    try:
                        literals.append(float(lit))
                    except ValueError:
                        return False
        prop["enum"] = literals
        return True

    return False


def generate_table_ddl_mongodb(
    table: Table, defer_constraints: bool = False,
) -> Tuple[str, List[ConversionIssue]]:
    """Generates a db.createCollection(...) call with a $jsonSchema
    validator (NOT NULL -> "required", column types -> "bsonType", the two
    common CHECK shapes -> numeric bounds / "enum" -- see
    _apply_check_to_mongo_schema above) plus one db[...].createIndex(...)
    call per PRIMARY KEY/UNIQUE constraint and secondary index -- those are
    the two pieces of a relational schema MongoDB can actually *enforce*
    server-side. FOREIGN KEY has no enforcement in MongoDB at all (there's
    no ALTER TABLE ADD CONSTRAINT equivalent); see
    generate_foreign_key_ddl_mongodb for the documentation-only comment
    that's emitted for it instead, and the warning issue added below."""
    issues: List[ConversionIssue] = []
    properties: dict = {}
    required: List[str] = []

    for col in table.columns:
        bson_type, col_issues = type_mapping.to_mongodb(col.data_type)
        col.target_type = bson_type
        # Recombine (not append-to-and-keep-growing) any source-side
        # reverse-mapping issues already on this column -- see the matching
        # comment in generate_table_ddl_postgres for why -- plus two
        # MongoDB-specific notes neither of the other four targets need:
        # IDENTITY has no native equivalent here, and DEFAULT isn't
        # something $jsonSchema can express at all.
        combined_col_issues = list(col.source_issues) + col_issues
        if col.identity:
            combined_col_issues.append(ConversionIssue(
                "info",
                f"{col.name}: MongoDB has no native auto-increment column -- the source IDENTITY "
                "was not carried over. Use the same counters-collection sequence emulation this "
                "tool generates for an Oracle SEQUENCE (see generate_sequence_ddl_mongodb) if a "
                "monotonically increasing value is still needed, or rely on the default ObjectId "
                "_id MongoDB already assigns every document.",
            ))
        if col.default:
            combined_col_issues.append(ConversionIssue(
                "info",
                f"{col.name}: MongoDB's $jsonSchema validator has no DEFAULT-value concept -- the "
                f"source default ({col.default}) was not carried over and must be applied by the "
                "inserting application code or driver instead.",
            ))
        col.issues = combined_col_issues
        issues.extend(combined_col_issues)

        description = col.data_type + (" NOT NULL" if not col.nullable else "")
        properties[col.name] = {"bsonType": bson_type, "description": description}
        if not col.nullable:
            required.append(col.name)

    for cons in table.constraints:
        if cons.kind == "CHECK" and cons.check_condition:
            if not _apply_check_to_mongo_schema(cons.check_condition, properties):
                issues.append(ConversionIssue(
                    "warning",
                    f"CHECK constraint {cons.name} ('{cons.check_condition}') could not be "
                    "automatically translated into a $jsonSchema rule; enforce it by hand via a "
                    "custom validator expression or in application code.",
                ))
        elif cons.kind == "FOREIGN KEY" and cons.ref_table:
            issues.append(ConversionIssue(
                "warning",
                f"FOREIGN KEY {cons.name} ({', '.join(cons.columns)} -> {cons.ref_table}."
                f"{', '.join(cons.ref_columns)}) has no enforcement in MongoDB; this relationship "
                "must be maintained by application code (or validated with a $lookup-based check) "
                "instead -- see the documentation-only comment in the generated DDL.",
            ))

    json_schema: dict = {"bsonType": "object", "properties": properties}
    if required:
        json_schema["required"] = sorted(required)
    options = {"validator": {"$jsonSchema": json_schema}}

    ddl = f"db.createCollection({json.dumps(table.name)}, {json.dumps(options, indent=2)});"

    index_lines = []
    pk = next((c for c in table.constraints if c.kind == "PRIMARY KEY"), None)
    if pk:
        keys = {c: 1 for c in pk.columns}
        index_lines.append(
            f"{_mongo_collection_ref(table.name)}.createIndex({json.dumps(keys)}, "
            f'{json.dumps({"unique": True, "name": pk.name})});'
        )
    for cons in table.constraints:
        if cons.kind == "UNIQUE":
            keys = {c: 1 for c in cons.columns}
            index_lines.append(
                f"{_mongo_collection_ref(table.name)}.createIndex({json.dumps(keys)}, "
                f'{json.dumps({"unique": True, "name": cons.name})});'
            )
    for idx in table.indexes:
        if idx.name in (c.name for c in table.constraints if c.kind in ("PRIMARY KEY", "UNIQUE")):
            continue  # already created implicitly by the constraint above
        if _mongo_index_has_expression_column(idx):
            # See _mongo_index_expression_issue: unlike the SQL targets
            # above, MongoDB has nothing to fall back to here -- an index
            # is always on a real field, never a computed expression --
            # so this index is skipped entirely rather than created
            # against a made-up field name.
            issues.append(_mongo_index_expression_issue(idx.name))
            continue
        keys = {c: 1 for c in idx.columns}
        opts: dict = {"name": idx.name}
        if idx.unique:
            opts["unique"] = True
        index_lines.append(f"{_mongo_collection_ref(table.name)}.createIndex({json.dumps(keys)}, {json.dumps(opts)});")

    # Only the index calls are deferred for a MongoDB target. The
    # $jsonSchema validator stays inline: it isn't index maintenance (the
    # thing deferral exists to avoid paying per row), and moving it would
    # mean a separate collMod round trip for no measurable gain. See
    # generate_deferred_ddl_mongodb.
    if index_lines and not defer_constraints:
        ddl += "\n" + "\n".join(index_lines)

    table.status = _worst_status(issues)
    table.issues = issues
    return ddl, issues


# ------------------------------------------- deferred constraints & indexes
#
# The counterpart of `defer_constraints=True` on the generate_table_ddl_*
# functions above: everything those omit, emitted as ALTER TABLE ADD
# CONSTRAINT / CREATE INDEX statements to run *after* the data is loaded
# (SCALE.md section 1.3). Building an index once over a fully-populated
# table is substantially faster than maintaining it row by row during the
# load, and produces a denser, less bloated index at the end.
#
# Each function below reuses the exact idempotency idiom that engine's own
# FK generator already uses, so a post-load script is as safely re-runnable
# as the rest of the generated DDL.
#
# The honest trade-off, stated once here rather than buried: deferring a
# constraint means a load that would have failed on its first bad row now
# fails at constraint-creation time, after everything has been copied. That
# is the right trade for speed at scale, but it makes the dry-run/plan step
# and a rehearsal against representative data materially more important,
# not less.


def _deferrable_indexes(table: Table):
    """Indexes that need creating explicitly -- i.e. not the ones a PRIMARY
    KEY or UNIQUE constraint already creates implicitly. Matches the same
    filter the inline path in each generate_table_ddl_* uses, so deferring
    can never create an index the inline path wouldn't have."""
    constraint_names = {c.name for c in table.constraints if c.kind in ("PRIMARY KEY", "UNIQUE")}
    return [idx for idx in table.indexes if idx.name not in constraint_names]


def generate_deferred_ddl_postgres(table: Table, source_engine: str = "Oracle") -> str:
    # See generate_table_ddl_postgres's own use of _plan_partition_ddl_postgres:
    # a PK/UNIQUE constraint (or unique index, below) that doesn't cover
    # every partition-key column is never emitted at all, immediate or
    # deferred -- Postgres rejects it outright either way. The warning
    # issue for each dropped one is already raised once, from
    # generate_table_ddl_postgres (which always runs for every table,
    # phased migration or not); this just needs the same silent skip so
    # the deferred ALTER TABLE doesn't try to add back what the immediate
    # CREATE TABLE correctly left out.
    partition_plan, _reason = _plan_partition_ddl_postgres(table)
    dropped_constraints = partition_plan.dropped_constraints if partition_plan else set()
    partition_columns = table.partition_scheme.columns if partition_plan else []

    lines = []
    for cons in table.constraints:
        if cons.name in dropped_constraints:
            continue
        if cons.kind == "PRIMARY KEY":
            cols = ", ".join(_quote_pg(c) for c in cons.columns)
            body = (f"ALTER TABLE {_quote_pg(table.name)} ADD CONSTRAINT "
                    f"{_quote_pg(cons.name)} PRIMARY KEY ({cols});")
        elif cons.kind == "UNIQUE":
            cols = ", ".join(_quote_pg(c) for c in cons.columns)
            body = (f"ALTER TABLE {_quote_pg(table.name)} ADD CONSTRAINT "
                    f"{_quote_pg(cons.name)} UNIQUE ({cols});")
        elif cons.kind == "CHECK" and cons.check_condition:
            condition = _check_condition(cons, "PostgreSQL", source_engine, [])
            body = (f"ALTER TABLE {_quote_pg(table.name)} ADD CONSTRAINT "
                    f"{_quote_pg(cons.name)} CHECK ({condition});")
        else:
            continue  # FOREIGN KEY has its own pass -- see generate_foreign_key_ddl_postgres
        # Postgres has no "ADD CONSTRAINT IF NOT EXISTS"; same DO-block
        # guard generate_foreign_key_ddl_postgres uses.
        lines.append("DO $$ BEGIN\n" f"  {body}\n" "EXCEPTION\n  WHEN duplicate_object THEN NULL;\nEND $$;")

    for idx in _deferrable_indexes(table):
        if _has_oracle_internal_expression(idx):
            continue  # see generate_table_ddl_postgres's own handling/issue for this
        if idx.unique and partition_plan and not _partition_key_covered(idx.columns, partition_columns):
            continue  # see generate_table_ddl_postgres's own handling/issue for this
        unique_kw = "UNIQUE " if idx.unique else ""
        cols = ", ".join(_index_column_sql(_quote_pg, c) for c in idx.columns)
        lines.append(
            f"CREATE {unique_kw}INDEX IF NOT EXISTS {_quote_pg(idx.name)} "
            f"ON {_quote_pg(table.name)} ({cols});"
        )
    return "\n".join(lines)


def generate_deferred_ddl_mysql(table: Table, source_engine: str = "Oracle") -> str:
    lines = []
    for cons in table.constraints:
        if cons.kind == "PRIMARY KEY":
            if mysql_must_key_identity(table):
                # Already created inline by generate_table_ddl_mysql --
                # InnoDB would not have accepted the table otherwise (error
                # 1075). Re-adding it here is error 1068, "Multiple primary
                # key defined", which would fail the whole post-load script.
                continue
            # No constraint name: MySQL's primary key is always internally
            # named "PRIMARY", and a name given here is accepted and then
            # silently ignored. The inline path in generate_table_ddl_mysql
            # omits it for the same reason.
            cols = ", ".join(_quote_mysql(c) for c in cons.columns)
            lines.append(f"ALTER TABLE {_quote_mysql(table.name)} ADD PRIMARY KEY ({cols});")
        elif cons.kind == "UNIQUE":
            cols = ", ".join(_quote_mysql(c) for c in cons.columns)
            lines.append(
                f"ALTER TABLE {_quote_mysql(table.name)} ADD CONSTRAINT "
                f"{_quote_mysql(cons.name)} UNIQUE ({cols});"
            )
        elif cons.kind == "CHECK" and cons.check_condition:
            condition = _check_condition(cons, "MySQL", source_engine, [])
            lines.append(
                f"ALTER TABLE {_quote_mysql(table.name)} ADD CONSTRAINT "
                f"{_quote_mysql(cons.name)} CHECK ({condition});"
            )

    for idx in _deferrable_indexes(table):
        if _has_oracle_internal_expression(idx):
            continue  # see generate_table_ddl_mysql's own handling/issue for this
        unique_kw = "UNIQUE " if idx.unique else ""
        cols = ", ".join(_index_column_sql(_quote_mysql, c) for c in idx.columns)
        lines.append(
            f"CREATE {unique_kw}INDEX {_quote_mysql(idx.name)} "
            f"ON {_quote_mysql(table.name)} ({cols});"
        )
    return "\n".join(lines)


def generate_deferred_ddl_sqlserver(table: Table, schema: Optional[str] = None, source_engine: str = "Oracle") -> str:
    lines = []
    qualified = _quote_sqlserver(table.name, schema)
    for cons in table.constraints:
        if cons.kind == "PRIMARY KEY":
            cols = ", ".join(_quote_sqlserver(c) for c in cons.columns)
            body = (f"ALTER TABLE {qualified} ADD CONSTRAINT "
                    f"{_quote_sqlserver(cons.name)} PRIMARY KEY ({cols});")
        elif cons.kind == "UNIQUE":
            cols = ", ".join(_quote_sqlserver(c) for c in cons.columns)
            body = (f"ALTER TABLE {qualified} ADD CONSTRAINT "
                    f"{_quote_sqlserver(cons.name)} UNIQUE ({cols});")
        elif cons.kind == "CHECK" and cons.check_condition:
            condition = _check_condition(cons, "SQL Server", source_engine, [])
            body = (f"ALTER TABLE {qualified} ADD CONSTRAINT "
                    f"{_quote_sqlserver(cons.name)} CHECK ({condition});")
        else:
            continue
        # sys.objects rather than sys.foreign_keys (the FK generator's
        # check) -- PK/UNIQUE/CHECK constraints don't appear in
        # sys.foreign_keys, but every one of them is a named object.
        lines.append(
            f"IF NOT EXISTS (SELECT 1 FROM sys.objects WHERE name = {quote_literal(cons.name)})\n"
            f"BEGIN\n  {body}\nEND;"
        )

    for idx in _deferrable_indexes(table):
        if _has_oracle_internal_expression(idx):
            continue  # see generate_table_ddl_sqlserver's own handling/issue for this
        unique_kw = "UNIQUE " if idx.unique else ""
        cols = ", ".join(_index_column_sql(_quote_sqlserver, c) for c in idx.columns)
        lines.append(
            f"IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = {quote_literal(idx.name)} "
            f"AND object_id = OBJECT_ID(N{quote_literal(qualified)}))\n"
            f"BEGIN\n"
            f"  CREATE {unique_kw}INDEX {_quote_sqlserver(idx.name)} ON {qualified} ({cols});\n"
            f"END;"
        )
    return "\n".join(lines)


def generate_deferred_ddl_db2(table: Table, schema: Optional[str] = None, source_engine: str = "Oracle") -> str:
    lines = []
    qualified = _quote_db2(table.name, schema)
    schema_check = f"TABSCHEMA = {quote_literal(schema.upper())} AND " if schema else ""
    index_schema_check = f"INDSCHEMA = {quote_literal(schema.upper())} AND " if schema else ""
    for cons in table.constraints:
        if cons.kind == "PRIMARY KEY":
            cols = ", ".join(_quote_db2(c) for c in cons.columns)
            body = (f"ALTER TABLE {qualified} ADD CONSTRAINT "
                    f"{_quote_db2(cons.name)} PRIMARY KEY ({cols})")
        elif cons.kind == "UNIQUE":
            cols = ", ".join(_quote_db2(c) for c in cons.columns)
            body = (f"ALTER TABLE {qualified} ADD CONSTRAINT "
                    f"{_quote_db2(cons.name)} UNIQUE ({cols})")
        elif cons.kind == "CHECK" and cons.check_condition:
            body = (f"ALTER TABLE {qualified} ADD CONSTRAINT "
                    f"{_quote_db2(cons.name)} CHECK ({cons.check_condition})")
        else:
            continue
        lines.append(
            f"BEGIN\n"
            f"  IF NOT EXISTS (SELECT 1 FROM SYSCAT.TABCONST WHERE {schema_check}"
            f"CONSTNAME = {quote_literal(cons.name.upper())}) THEN\n"
            f"    EXECUTE IMMEDIATE {_sql_quote(body)};\n"
            f"  END IF;\n"
            f"END;"
        )

    for idx in _deferrable_indexes(table):
        if _has_oracle_internal_expression(idx):
            continue  # see generate_table_ddl_db2's own handling/issue for this
        unique_kw = "UNIQUE " if idx.unique else ""
        cols = ", ".join(_index_column_sql(_quote_db2, c) for c in idx.columns)
        idx_ddl = f"CREATE {unique_kw}INDEX {_quote_db2(idx.name, schema)} ON {qualified} ({cols})"
        lines.append(
            f"BEGIN\n"
            f"  IF NOT EXISTS (SELECT 1 FROM SYSCAT.INDEXES WHERE {index_schema_check}"
            f"INDNAME = {quote_literal(idx.name.upper())}) THEN\n"
            f"    EXECUTE IMMEDIATE {_sql_quote(idx_ddl)};\n"
            f"  END IF;\n"
            f"END;"
        )
    return "\n".join(lines)


def generate_deferred_ddl_oracle(table: Table, schema: Optional[str] = None, source_engine: str = "Oracle") -> str:
    lines = []
    qualified = _quote_oracle(table.name, schema)
    for cons in table.constraints:
        if cons.kind == "PRIMARY KEY":
            cols = ", ".join(_quote_oracle(c) for c in cons.columns)
            body = (f"ALTER TABLE {qualified} ADD CONSTRAINT "
                    f"{_quote_oracle(cons.name)} PRIMARY KEY ({cols})")
        elif cons.kind == "UNIQUE":
            cols = ", ".join(_quote_oracle(c) for c in cons.columns)
            body = (f"ALTER TABLE {qualified} ADD CONSTRAINT "
                    f"{_quote_oracle(cons.name)} UNIQUE ({cols})")
        elif cons.kind == "CHECK" and cons.check_condition:
            body = (f"ALTER TABLE {qualified} ADD CONSTRAINT "
                    f"{_quote_oracle(cons.name)} CHECK ({cons.check_condition})")
        else:
            continue
        # ORA-02264 ("name already used by an existing constraint") and
        # ORA-02261 ("such unique or primary key already exists in the
        # table") -- the same reasoning as generate_foreign_key_ddl_oracle's
        # -2264/-2275 pair, for the PK/UNIQUE/CHECK case.
        lines.append(_oracle_idempotent(body, ignore_codes=(-2264, -2261)))

    for idx in _deferrable_indexes(table):
        if _has_oracle_internal_expression(idx):
            continue  # see generate_table_ddl_oracle's own handling/issue for this
        unique_kw = "UNIQUE " if idx.unique else ""
        cols = ", ".join(_index_column_sql(_quote_oracle, c) for c in idx.columns)
        idx_ddl = f"CREATE {unique_kw}INDEX {_quote_oracle(idx.name, schema)} ON {qualified} ({cols})"
        lines.append(_oracle_idempotent(idx_ddl))
    return "\n".join(lines)


def generate_deferred_ddl_mongodb(table: Table) -> str:
    """The createIndex calls generate_table_ddl_mongodb omits under
    `defer_constraints=True`. The $jsonSchema validator is not deferred --
    see that function's own comment for why."""
    lines = []
    pk = next((c for c in table.constraints if c.kind == "PRIMARY KEY"), None)
    if pk:
        keys = {c: 1 for c in pk.columns}
        lines.append(
            f"{_mongo_collection_ref(table.name)}.createIndex({json.dumps(keys)}, "
            f'{json.dumps({"unique": True, "name": pk.name})});'
        )
    for cons in table.constraints:
        if cons.kind == "UNIQUE":
            keys = {c: 1 for c in cons.columns}
            lines.append(
                f"{_mongo_collection_ref(table.name)}.createIndex({json.dumps(keys)}, "
                f'{json.dumps({"unique": True, "name": cons.name})});'
            )
    for idx in _deferrable_indexes(table):
        if _mongo_index_has_expression_column(idx):
            continue  # see generate_table_ddl_mongodb's own handling/issue for this
        keys = {c: 1 for c in idx.columns}
        opts: dict = {"name": idx.name}
        if idx.unique:
            opts["unique"] = True
        lines.append(
            f"{_mongo_collection_ref(table.name)}.createIndex({json.dumps(keys)}, {json.dumps(opts)});")
    return "\n".join(lines)


def generate_deferred_ddl(
    table: Table, target_engine: str, schema: Optional[str] = None, source_engine: str = "Oracle",
) -> str:
    """Engine dispatch for the six generate_deferred_ddl_* functions
    above, mirroring generate_schema_ddl's own if/elif chain."""
    engine = target_engine.lower()
    if engine.startswith("postgres"):
        return generate_deferred_ddl_postgres(table, source_engine)
    if engine.replace(" ", "").startswith("sqlserver"):
        return generate_deferred_ddl_sqlserver(table, schema, source_engine)
    if engine.startswith("db2"):
        return generate_deferred_ddl_db2(table, schema, source_engine)
    if engine.startswith("oracle"):
        return generate_deferred_ddl_oracle(table, schema, source_engine)
    if engine.startswith("mongo"):
        return generate_deferred_ddl_mongodb(table)
    return generate_deferred_ddl_mysql(table, source_engine)


def generate_foreign_key_ddl_postgres(table: Table) -> str:
    """ALTER TABLE ... ADD CONSTRAINT ... FOREIGN KEY statements for one
    table, meant to run only after every table in the schema already
    exists — see the call site in generate_schema_ddl for why."""
    lines = []
    for cons in table.constraints:
        if cons.kind == "FOREIGN KEY" and cons.ref_table:
            cols = ", ".join(_quote_pg(c) for c in cons.columns)
            ref_cols = ", ".join(_quote_pg(c) for c in cons.ref_columns)
            alter_stmt = (
                f"ALTER TABLE {_quote_pg(table.name)} ADD CONSTRAINT {_quote_pg(cons.name)} "
                f"FOREIGN KEY ({cols}) REFERENCES {_quote_pg(cons.ref_table)} ({ref_cols});"
            )
            # Postgres has no "ADD CONSTRAINT IF NOT EXISTS", so re-running
            # the same DDL against a target that already has this FK from a
            # previous run would otherwise abort with "constraint ... already
            # exists". Swallow exactly that one error via a DO block.
            lines.append(
                "DO $$ BEGIN\n"
                f"  {alter_stmt}\n"
                "EXCEPTION\n"
                "  WHEN duplicate_object THEN NULL;\n"
                "END $$;"
            )
    return "\n".join(lines)


def generate_foreign_key_ddl_mysql(table: Table) -> str:
    """Same as generate_foreign_key_ddl_postgres, MySQL dialect."""
    lines = []
    for cons in table.constraints:
        if cons.kind == "FOREIGN KEY" and cons.ref_table:
            cols = ", ".join(_quote_mysql(c) for c in cons.columns)
            ref_cols = ", ".join(_quote_mysql(c) for c in cons.ref_columns)
            lines.append(
                f"ALTER TABLE {_quote_mysql(table.name)} ADD CONSTRAINT {_quote_mysql(cons.name)} "
                f"FOREIGN KEY ({cols}) REFERENCES {_quote_mysql(cons.ref_table)} ({ref_cols});"
            )
    return "\n".join(lines)


def generate_foreign_key_ddl_sqlserver(table: Table, schema: Optional[str] = None) -> str:
    """Same as generate_foreign_key_ddl_postgres, SQL Server dialect: SQL
    Server has no "ADD CONSTRAINT IF NOT EXISTS" either, so re-running the
    same DDL is guarded with a sys.foreign_keys existence check instead."""
    lines = []
    qualified = _quote_sqlserver(table.name, schema)
    for cons in table.constraints:
        if cons.kind == "FOREIGN KEY" and cons.ref_table:
            cols = ", ".join(_quote_sqlserver(c) for c in cons.columns)
            ref_cols = ", ".join(_quote_sqlserver(c) for c in cons.ref_columns)
            ref_qualified = _quote_sqlserver(cons.ref_table, schema)
            lines.append(
                f"IF NOT EXISTS (SELECT 1 FROM sys.foreign_keys WHERE name = {quote_literal(cons.name)})\n"
                f"BEGIN\n"
                f"  ALTER TABLE {qualified} ADD CONSTRAINT {_quote_sqlserver(cons.name)} "
                f"FOREIGN KEY ({cols}) REFERENCES {ref_qualified} ({ref_cols});\n"
                f"END;"
            )
    return "\n".join(lines)


def generate_foreign_key_ddl_db2(table: Table, schema: Optional[str] = None) -> str:
    """Same as generate_foreign_key_ddl_postgres, Db2 dialect: Db2 has no
    "ADD CONSTRAINT IF NOT EXISTS" either, so re-running the same DDL is
    guarded with a SYSCAT.TABCONST existence check instead."""
    lines = []
    qualified = _quote_db2(table.name, schema)
    schema_check = f"TABSCHEMA = {quote_literal(schema.upper())} AND " if schema else ""
    for cons in table.constraints:
        if cons.kind == "FOREIGN KEY" and cons.ref_table:
            cols = ", ".join(_quote_db2(c) for c in cons.columns)
            ref_cols = ", ".join(_quote_db2(c) for c in cons.ref_columns)
            ref_qualified = _quote_db2(cons.ref_table, schema)
            alter_stmt = (
                f"ALTER TABLE {qualified} ADD CONSTRAINT {_quote_db2(cons.name)} "
                f"FOREIGN KEY ({cols}) REFERENCES {ref_qualified} ({ref_cols})"
            )
            lines.append(
                f"BEGIN\n"
                f"  IF NOT EXISTS (SELECT 1 FROM SYSCAT.TABCONST WHERE {schema_check}CONSTNAME = {quote_literal(cons.name.upper())}) THEN\n"
                f"    EXECUTE IMMEDIATE {_sql_quote(alter_stmt)};\n"
                f"  END IF;\n"
                f"END;"
            )
    return "\n".join(lines)


def generate_foreign_key_ddl_oracle(table: Table, schema: Optional[str] = None) -> str:
    """Same as generate_foreign_key_ddl_postgres, Oracle dialect: Oracle
    has no "ADD CONSTRAINT IF NOT EXISTS" either, so re-running the same
    DDL is guarded via _oracle_idempotent instead of a catalog existence
    check -- ORA-02264 ("name already used by an existing constraint") is
    what a duplicate constraint *name* raises (distinct from -955, which
    covers CREATE TABLE/SEQUENCE/INDEX's shared-namespace collisions
    above, not ALTER TABLE ADD CONSTRAINT); ORA-02275 ("such a referential
    constraint already exists") is caught too, for the rarer case where an
    identical FK relationship already exists under a different name."""
    lines = []
    qualified = _quote_oracle(table.name, schema)
    for cons in table.constraints:
        if cons.kind == "FOREIGN KEY" and cons.ref_table:
            cols = ", ".join(_quote_oracle(c) for c in cons.columns)
            ref_cols = ", ".join(_quote_oracle(c) for c in cons.ref_columns)
            ref_qualified = _quote_oracle(cons.ref_table, schema)
            alter_stmt = (
                f"ALTER TABLE {qualified} ADD CONSTRAINT {_quote_oracle(cons.name)} "
                f"FOREIGN KEY ({cols}) REFERENCES {ref_qualified} ({ref_cols})"
            )
            lines.append(_oracle_idempotent(alter_stmt, ignore_codes=(-2264, -2275)))
    return "\n".join(lines)


def generate_foreign_key_ddl_mongodb(table: Table) -> str:
    """MongoDB has no foreign-key enforcement at all -- no ALTER TABLE ADD
    CONSTRAINT equivalent exists to emit here. Unlike the other three
    engines' same-named function, this produces documentation-only "--"
    comment lines (never anything mongo_connector.py's execute_ddl needs to
    parse as a real statement) so the relationship is still visible in the
    generated DDL/report; the matching warning issue is raised in
    generate_table_ddl_mongodb instead, since that's the function whose
    return value already flows into the assessment report."""
    lines = []
    for cons in table.constraints:
        if cons.kind == "FOREIGN KEY" and cons.ref_table:
            lines.append(
                f"-- NOTE: {table.name}.{', '.join(cons.columns)} references "
                f"{cons.ref_table}.{', '.join(cons.ref_columns)} -- MongoDB has no foreign-key "
                f"enforcement; this relationship must be maintained by application code."
            )
    return "\n".join(lines)


# PostgreSQL sequences are backed by bigint; values outside this range raise
# "value ... is out of range for type bigint" at CREATE SEQUENCE time.
_PG_BIGINT_MAX = 9223372036854775807
_PG_BIGINT_MIN = -9223372036854775808


def generate_sequence_ddl_postgres(seq: Sequence) -> Tuple[str, List[ConversionIssue]]:
    issues: List[ConversionIssue] = []
    # IF NOT EXISTS: see the matching comment in generate_table_ddl_postgres —
    # makes re-applying the same DDL against a target that already has this
    # sequence a no-op instead of "relation ... already exists".
    ddl = (
        f"CREATE SEQUENCE IF NOT EXISTS {_quote_pg(seq.name)} START WITH {seq.start_value} "
        f"INCREMENT BY {seq.increment_by}"
    )

    # Oracle sequences created without an explicit MINVALUE/MAXVALUE default to
    # +/-9999999999999999999999999999 (28 nines) rather than NULL, which blows
    # past PostgreSQL's bigint-backed sequence range. Treat anything outside
    # that range as "unbounded" and just omit the clause (Postgres then uses
    # its own bigint min/max), instead of emitting a value CREATE SEQUENCE
    # will reject outright.
    if seq.min_value is not None:
        if seq.min_value < _PG_BIGINT_MIN:
            issues.append(ConversionIssue(
                "info",
                f"Oracle MINVALUE {seq.min_value} is below PostgreSQL's bigint sequence range; "
                "MINVALUE clause omitted (defaults to PostgreSQL's bigint minimum).",
            ))
        else:
            ddl += f" MINVALUE {seq.min_value}"
    if seq.max_value is not None:
        if seq.max_value > _PG_BIGINT_MAX:
            issues.append(ConversionIssue(
                "info",
                f"Oracle MAXVALUE {seq.max_value} exceeds PostgreSQL's bigint sequence range; "
                "MAXVALUE clause omitted (defaults to PostgreSQL's bigint maximum, "
                f"{_PG_BIGINT_MAX}).",
            ))
        else:
            ddl += f" MAXVALUE {seq.max_value}"

    ddl += " CYCLE" if seq.cycle else " NO CYCLE"
    ddl += ";"
    seq.status = _worst_status(issues) if issues else ConversionStatus.AUTOMATIC
    seq.issues = issues
    return ddl, issues


def generate_sequence_ddl_mysql(seq: Sequence) -> Tuple[str, List[ConversionIssue]]:
    """A helper table plus the two functions that make it behave like a
    sequence.

    This used to emit the table and then a *comment* describing the
    UPDATE/SELECT pair a caller would have to write. That left the claim
    "converted to a helper table plus a NEXTVAL-style stored function"
    half true -- the function was never generated -- and left every
    converted routine calling something that did not exist, because
    `ORDER_SEQ.NEXTVAL` has to become an expression, not two statements.
    So the functions are real now, and plsql_mysql_converter rewrites
    `<seq>.NEXTVAL` / `.CURRVAL` to call them.

    `LAST_INSERT_ID(expr)` is what makes this safe under concurrency: it
    both sets the row and returns the value this session wrote, atomically
    within the UPDATE, so two sessions incrementing at once cannot be
    handed the same number. It also gives CURRVAL the same session-scoped
    meaning Oracle gives it.
    """
    issues = [ConversionIssue(
        "warning",
        f"MySQL has no native SEQUENCE object. '{seq.name}' became a helper table plus "
        f"{seq.name}_NEXTVAL() and {seq.name}_CURRVAL() functions. Converted routines call "
        f"those; any hand-written SQL still using {seq.name}.NEXTVAL must be updated.",
    )]
    # `<name>_SEQ` for a sequence not already named that way -- Oracle
    # sequences are conventionally called ORDER_SEQ, and the old
    # unconditional suffix turned that into ORDER_SEQ_SEQ.
    helper_table = seq.name if seq.name.upper().endswith("_SEQ") else f"{seq.name}_SEQ"
    increment = seq.increment_by or 1
    if seq.cycle:
        issues.append(ConversionIssue(
            "warning",
            f"Sequence {seq.name} is CYCLE; the emulation does not wrap around at MAXVALUE."))
    ddl = (
        f"CREATE TABLE IF NOT EXISTS {_quote_mysql(helper_table)} (\n"
        f"  next_val BIGINT NOT NULL\n"
        f") ENGINE=InnoDB;\n"
        f"INSERT INTO {_quote_mysql(helper_table)} (next_val)\n"
        f"  SELECT {seq.start_value - increment} FROM DUAL\n"
        f"  WHERE NOT EXISTS (SELECT 1 FROM {_quote_mysql(helper_table)});\n"
        f"DROP FUNCTION IF EXISTS {_quote_mysql(seq.name + '_NEXTVAL')};\n"
        f"CREATE FUNCTION {_quote_mysql(seq.name + '_NEXTVAL')}() RETURNS BIGINT\n"
        f"MODIFIES SQL DATA\n"
        f"BEGIN\n"
        f"  UPDATE {_quote_mysql(helper_table)}\n"
        f"     SET next_val = LAST_INSERT_ID(next_val + {increment});\n"
        f"  -- Copied into a user variable because CURRVAL cannot read\n"
        f"  -- LAST_INSERT_ID(): MySQL saves and restores that value around a\n"
        f"  -- stored function call, so it is back to whatever it was before\n"
        f"  -- by the time the caller's next statement runs. A user variable\n"
        f"  -- is plain session state and survives, which also gives CURRVAL\n"
        f"  -- the session scope Oracle gives it.\n"
        f"  SET @{seq.name}_CURRVAL = LAST_INSERT_ID();\n"
        f"  RETURN @{seq.name}_CURRVAL;\n"
        f"END;\n"
        f"DROP FUNCTION IF EXISTS {_quote_mysql(seq.name + '_CURRVAL')};\n"
        f"CREATE FUNCTION {_quote_mysql(seq.name + '_CURRVAL')}() RETURNS BIGINT\n"
        f"NOT DETERMINISTIC NO SQL\n"
        f"BEGIN\n"
        f"  IF @{seq.name}_CURRVAL IS NULL THEN\n"
        f"    -- Oracle raises ORA-08002 here rather than inventing a value,\n"
        f"    -- and so does this: a CURRVAL before any NEXTVAL in the same\n"
        f"    -- session is a bug in the caller, not a number to guess.\n"
        f"    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT =\n"
        f"      '{seq.name}.CURRVAL is not yet defined in this session"
        f" -- call {seq.name}_NEXTVAL() first';\n"
        f"  END IF;\n"
        f"  RETURN @{seq.name}_CURRVAL;\n"
        f"END;"
    )
    seq.status = ConversionStatus.AUTOMATIC_WITH_WARNINGS
    seq.issues = issues
    return ddl, issues


def generate_sequence_ddl_sqlserver(seq: Sequence, schema: Optional[str] = None) -> Tuple[str, List[ConversionIssue]]:
    """SQL Server has native sequences (2012+), so unlike MySQL this doesn't
    need a helper-table emulation. Sequences are backed by the declared
    type's range -- AS BIGINT gives it the same range as PostgreSQL's
    bigint-backed sequences, so the same out-of-range clamping applies."""
    issues: List[ConversionIssue] = []
    qualified = _quote_sqlserver(seq.name, schema)
    ddl_body = f"CREATE SEQUENCE {qualified} AS BIGINT START WITH {seq.start_value} INCREMENT BY {seq.increment_by}"

    if seq.min_value is not None:
        if seq.min_value < _PG_BIGINT_MIN:
            issues.append(ConversionIssue(
                "info",
                f"Oracle MINVALUE {seq.min_value} is below the BIGINT sequence range; MINVALUE clause "
                "omitted (defaults to SQL Server's bigint minimum).",
            ))
        else:
            ddl_body += f" MINVALUE {seq.min_value}"
    if seq.max_value is not None:
        if seq.max_value > _PG_BIGINT_MAX:
            issues.append(ConversionIssue(
                "info",
                f"Oracle MAXVALUE {seq.max_value} exceeds the BIGINT sequence range; MAXVALUE clause "
                f"omitted (defaults to SQL Server's bigint maximum, {_PG_BIGINT_MAX}).",
            ))
        else:
            ddl_body += f" MAXVALUE {seq.max_value}"

    ddl_body += " CYCLE" if seq.cycle else " NO CYCLE"
    ddl_body += ";"

    # No CREATE SEQUENCE IF NOT EXISTS in T-SQL -- guard with sys.sequences.
    ddl = (
        f"IF NOT EXISTS (SELECT 1 FROM sys.sequences WHERE name = {quote_literal(seq.name)})\n"
        f"BEGIN\n"
        f"  {ddl_body}\n"
        f"END;"
    )
    seq.status = _worst_status(issues) if issues else ConversionStatus.AUTOMATIC
    seq.issues = issues
    return ddl, issues


def generate_sequence_ddl_db2(seq: Sequence, schema: Optional[str] = None) -> Tuple[str, List[ConversionIssue]]:
    """Db2 has native sequences, same as SQL Server -- backed by the
    declared type's range; AS BIGINT gives it the same range as
    PostgreSQL's/SQL Server's bigint-backed sequences, so the same
    out-of-range clamping applies."""
    issues: List[ConversionIssue] = []
    qualified = _quote_db2(seq.name, schema)
    ddl_body = f"CREATE SEQUENCE {qualified} AS BIGINT START WITH {seq.start_value} INCREMENT BY {seq.increment_by}"

    if seq.min_value is not None:
        if seq.min_value < _PG_BIGINT_MIN:
            issues.append(ConversionIssue(
                "info",
                f"Oracle MINVALUE {seq.min_value} is below the BIGINT sequence range; MINVALUE clause "
                "omitted (defaults to Db2's bigint minimum).",
            ))
        else:
            ddl_body += f" MINVALUE {seq.min_value}"
    if seq.max_value is not None:
        if seq.max_value > _PG_BIGINT_MAX:
            issues.append(ConversionIssue(
                "info",
                f"Oracle MAXVALUE {seq.max_value} exceeds the BIGINT sequence range; MAXVALUE clause "
                f"omitted (defaults to Db2's bigint maximum, {_PG_BIGINT_MAX}).",
            ))
        else:
            ddl_body += f" MAXVALUE {seq.max_value}"

    ddl_body += " CYCLE" if seq.cycle else " NO CYCLE"

    # No CREATE SEQUENCE IF NOT EXISTS in Db2 -- guard with SYSCAT.SEQUENCES.
    schema_check = f"SEQSCHEMA = {quote_literal(schema.upper())} AND " if schema else ""
    ddl = (
        f"BEGIN\n"
        f"  IF NOT EXISTS (SELECT 1 FROM SYSCAT.SEQUENCES WHERE {schema_check}SEQNAME = {quote_literal(seq.name.upper())}) THEN\n"
        f"    EXECUTE IMMEDIATE {_sql_quote(ddl_body)};\n"
        f"  END IF;\n"
        f"END;"
    )
    seq.status = _worst_status(issues) if issues else ConversionStatus.AUTOMATIC
    seq.issues = issues
    return ddl, issues


def generate_sequence_ddl_oracle(seq: Sequence, schema: Optional[str] = None) -> Tuple[str, List[ConversionIssue]]:
    """Oracle has native sequences with no bigint-backed range limit the
    way Postgres/SQL Server/Db2 above have (Oracle sequences support up to
    28-digit precision) -- so unlike those three, this never needs to
    clamp or drop an out-of-range MINVALUE/MAXVALUE; whatever was read
    from the source (Oracle's own ALL_SEQUENCES, or Db2's SYSCAT.SEQUENCES
    for a Db2 source -- the only two source engines with a native
    SEQUENCE object to populate Sequence.min_value/max_value from at all)
    is passed straight through. No CREATE SEQUENCE IF NOT EXISTS in
    Oracle either, so re-running the same DDL is guarded via
    _oracle_idempotent, same as generate_table_ddl_oracle."""
    issues: List[ConversionIssue] = []
    qualified = _quote_oracle(seq.name, schema)
    ddl_body = f"CREATE SEQUENCE {qualified} START WITH {seq.start_value} INCREMENT BY {seq.increment_by}"
    if seq.min_value is not None:
        ddl_body += f" MINVALUE {seq.min_value}"
    if seq.max_value is not None:
        ddl_body += f" MAXVALUE {seq.max_value}"
    ddl_body += " CYCLE" if seq.cycle else " NOCYCLE"

    ddl = _oracle_idempotent(ddl_body)
    seq.status = ConversionStatus.AUTOMATIC
    seq.issues = issues
    return ddl, issues


def generate_sequence_ddl_mongodb(seq: Sequence) -> Tuple[str, List[ConversionIssue]]:
    """MongoDB has no native SEQUENCE object, same as MySQL -- emulated
    here as one document per sequence inside a single shared "counters"
    collection (a well-known MongoDB pattern), rather than MySQL's approach
    of one helper *table* per sequence: `db.createCollection("counters")`
    re-runs harmlessly for every sequence in the schema (mongo_connector.py
    treats "already exists" as a no-op, the same idempotent-DDL tolerance
    every other engine's own guard gives), and each sequence gets exactly
    one upserted `{_id: "<name>", seq: <start_value>}` bootstrap document."""
    issues = [ConversionIssue(
        "warning",
        f"MongoDB has no native SEQUENCE object. '{seq.name}' was emulated as a document in a "
        "shared \"counters\" collection; review calling PL/SQL that used <sequence>.NEXTVAL / "
        ".CURRVAL syntax and replace it with, e.g., "
        f'db["counters"].findOneAndUpdate({{"_id": {json.dumps(seq.name)}}}, '
        f'{{"$inc": {{"seq": {seq.increment_by}}}}}, {{"returnDocument": "after"}}) at insert time.',
    )]
    ddl = (
        f'db.createCollection({json.dumps("counters")});\n'
        f'{_mongo_collection_ref("counters")}.updateOne({json.dumps({"_id": seq.name})}, '
        f'{json.dumps({"$setOnInsert": {"seq": seq.start_value}})}, {json.dumps({"upsert": True})});'
    )
    seq.status = ConversionStatus.AUTOMATIC_WITH_WARNINGS
    seq.issues = issues
    return ddl, issues


def generate_view_ddl(
    view: View, target_engine: str, schema: Optional[str] = None
) -> Tuple[str, List[ConversionIssue]]:
    """Best-effort view conversion: most ANSI SELECT syntax is portable.
    Oracle-specific constructs are flagged for manual review rather than
    silently mistranslated. `schema` is only used for a SQL Server or Db2
    target (Postgres/MySQL callers never pass it, preserving their
    existing, unqualified output exactly)."""
    issues: List[ConversionIssue] = []
    text = view.definition.strip().rstrip(";")
    if view.source_engine == "SQL Server":
        # Defensive: the introspector already normalises this, but a View
        # constructed elsewhere (a saved schema, a test, the CLI) must not
        # be able to produce CREATE-inside-CREATE either.
        text = tsql_dialect.strip_create_view_header(text).strip().rstrip(";")
    engine_key = target_engine.lower().replace(" ", "")

    if view.is_materialized and not engine_key.startswith("postgres"):
        # Contained-scope decision, matching ora2pg's own Oracle-to-Postgres-
        # only focus: a real native materialized-view translation is only
        # implemented for a PostgreSQL target (see the postgres branch
        # below). MySQL has no materialized-view concept at all; SQL
        # Server's closest analog (an indexed view) has different rules
        # entirely (no aggregates/outer joins, SCHEMABINDING required) that
        # would need a real rewrite, not a syntax rename; Db2 could in
        # principle become a summary table (MQT), but this tool has not
        # verified that translation closely enough to ship it with
        # confidence. Flagging rather than guessing here follows the same
        # reasoning as every other "no safe mechanical translation" case in
        # this tool.
        ddl = (
            f"-- MANUAL CONVERSION REQUIRED for MATERIALIZED VIEW {view.name}\n"
            f"-- {target_engine} has no automatic equivalent this tool generates for an Oracle "
            f"materialized view -- recreate its query as a regular view refreshed by application/ETL "
            f"code, or (Db2) as a summary table (CREATE TABLE ... AS ... DATA INITIALLY DEFERRED "
            f"REFRESH DEFERRED), by hand.\n"
            f"/*\n{text}\n*/"
        )
        issues.append(ConversionIssue(
            "error",
            f"MATERIALIZED VIEW {view.name}: automatic conversion to {target_engine} is not "
            "supported; recreate it by hand.",
        ))
        view.converted_definition = ddl
        view.status = ConversionStatus.MANUAL
        view.issues = issues
        return ddl, issues

    if view.source_engine == "MongoDB":
        # A MongoDB-sourced view's `definition` is a JSON-serialized
        # aggregation pipeline (see mongo_source_introspector), not SQL --
        # running the CONNECT-BY-rewrite/Oracle-marker scan below against
        # JSON text could "succeed" by accident (e.g. a pipeline stage
        # that happens to contain the substring "SYSDATE" in a string
        # literal) and silently produce wrong output instead of safely
        # flagging the view for manual review, so this short-circuits
        # unconditionally regardless of target engine -- mirrors
        # plsql_converter.convert_routine's own non-Oracle-source guard.
        ddl = (
            f"-- MANUAL CONVERSION REQUIRED for VIEW {view.name}\n"
            f"-- Source is a MongoDB aggregation pipeline, not SQL -- this tool cannot translate "
            f"pipeline stages into target SQL/DDL automatically; rewrite by hand or handle in "
            f"application-layer code.\n"
            f"/*\n{text}\n*/"
        )
        issues.append(ConversionIssue(
            "error",
            f"VIEW {view.name}: source is a MongoDB aggregation pipeline; automatic conversion to "
            "SQL is not supported and this view must be rewritten by hand.",
        ))
        view.converted_definition = ddl
        view.status = ConversionStatus.MANUAL
        view.issues = issues
        return ddl, issues

    if engine_key.startswith("oracle"):
        # An Oracle target is the one place a view's `definition` might
        # already be *exactly* valid target syntax verbatim -- but only if
        # it actually came from an Oracle source; a MySQL/PostgreSQL/SQL
        # Server/Db2-sourced view's text is in *that* engine's own SQL
        # dialect, not Oracle's, so passing it through unchanged here
        # would silently emit invalid Oracle DDL. This mirrors
        # plsql_converter.convert_routine's own source-engine check for
        # routines -- non-Oracle-sourced SQL is always flagged manual
        # rather than guessed at, since this tool has no
        # dialect-to-dialect SQL translator.
        if view.source_engine == "Oracle":
            ddl = f"CREATE OR REPLACE VIEW {_quote_oracle(view.name, schema)} AS\n{text};"
            view.converted_definition = ddl
            view.status = ConversionStatus.AUTOMATIC
            view.issues = []
            return ddl, []
        ddl = (
            f"-- MANUAL CONVERSION REQUIRED for VIEW {view.name}\n"
            f"-- Source engine is {view.source_engine}, not Oracle -- this tool has no "
            f"{view.source_engine}-SQL-to-Oracle-SQL translator, so this view's text was not "
            f"assumed to be valid Oracle syntax and was left for manual review.\n"
            f"/*\n{text}\n*/"
        )
        issues.append(ConversionIssue(
            "error",
            f"VIEW {view.name}: source engine is {view.source_engine}, not Oracle; automatic "
            "conversion to Oracle SQL is not supported and this view must be rewritten by hand.",
        ))
        view.converted_definition = ddl
        view.status = ConversionStatus.MANUAL
        view.issues = issues
        return ddl, issues

    if engine_key.startswith("mongo"):
        # Unlike the four SQL targets, there's no partial-credit path here:
        # MongoDB's closest equivalent (db.createView with an aggregation
        # pipeline) requires translating arbitrary SQL SELECT syntax into
        # pipeline stages, which is a fundamentally harder, unsolved
        # problem this tool doesn't attempt -- so every view is flagged for
        # manual conversion unconditionally, without running the CONNECT
        # BY-rewrite/oracle_markers scan below at all (there's no partial
        # automation to report on either way, and running them first could
        # misleadingly suggest there is).
        ddl = (
            f"-- MANUAL CONVERSION REQUIRED for VIEW {view.name}\n"
            f"-- MongoDB has no automatic SQL-view equivalent this tool can generate -- rewrite as "
            f"a db.createView(...) aggregation pipeline, or resolve it in application-layer code.\n"
            f"/*\n{text}\n*/"
        )
        issues.append(ConversionIssue(
            "error",
            f"VIEW {view.name}: automatic conversion to a MongoDB aggregation pipeline is not "
            "supported; this view must be rewritten by hand as a db.createView(...) pipeline or "
            "handled in application code.",
        ))
        view.converted_definition = ddl
        view.status = ConversionStatus.MANUAL
        view.issues = issues
        return ddl, issues

    if (view.source_engine == "SQL Server"
            and (engine_key.startswith("postgres") or engine_key.startswith("mysql")
                 or engine_key.startswith("mariadb"))):
        # A SQL Server view's body is T-SQL: `[dbo].[Employees]`, ISNULL(),
        # TOP n, GETDATE(). All of that used to be copied through verbatim
        # and reported as "Converted automatically", so the view looked fine
        # in the tree and then failed the moment "Apply DDL to Target" ran.
        # Translating it here is the same fix as column DEFAULTs, applied to
        # the other place raw source SQL reaches the target.
        #
        # A CREATE VIEW body is only ever a SELECT, so the statement-level
        # machinery in tsql_routine_converter is not needed -- the
        # expression/query translator is enough.
        translated, view_issues = tsql_dialect.translate_expression(text, target_engine)
        issues.extend(view_issues)
        text = translated
        if any(i.severity == "error" for i in view_issues):
            ddl = (
                f"-- MANUAL CONVERSION REQUIRED for VIEW {view.name}\n"
                f"-- Parts of this T-SQL view body have no automatic {target_engine} equivalent "
                f"(see the assessment report for the specific constructs).\n"
                f"/*\n{translated}\n*/"
            )
            view.converted_definition = ddl
            view.status = ConversionStatus.MANUAL
            view.issues = issues
            return ddl, issues

    if view.source_engine == "Oracle":
        # Safe, mechanical, semantics-preserving rewrites -- the same ones
        # sql_translator.py already applies to a stored routine's body (see
        # its _SUBSTITUTIONS) -- applied here too, because a view's SELECT
        # text reaches the target exactly the way a routine's body does.
        # Without this, a view using either used to be reported "Converted
        # automatically" (at best flagged with a warning by the
        # oracle_markers scan below, which only *detects*, never fixes) and
        # then fail outright the moment "Apply DDL to Target" actually ran
        # it -- neither Postgres, MySQL, SQL Server nor Db2 has an NVL() or
        # SYSDATE function. NVL(a, b) and COALESCE(a, b) are identical for
        # Oracle's always-exactly-two-argument NVL, and CURRENT_TIMESTAMP is
        # the ANSI equivalent of SYSDATE every target here accepts -- both
        # rewrites are lossless, unlike DECODE/(+) below, which genuinely
        # need restructuring a human should do, so those stay flagged
        # rather than guessed at. ROWNUM's top-N idiom is no longer in that
        # flagged-only list either -- see rewrite_rownum_topn below.
        text = re.sub(r"\bNVL\s*\(", "COALESCE(", text, flags=re.IGNORECASE)
        text = re.sub(r"\bSYSDATE\b", "CURRENT_TIMESTAMP", text, flags=re.IGNORECASE)

        if engine_key.startswith("postgres"):
            # Four more gaps, specific to a PostgreSQL target (different
            # target syntax on MySQL/SQL Server/Db2, so these run only
            # here): ROWNUM's top-N idiom, LISTAGG, the REGEXP_* family,
            # and ADD_MONTHS/LAST_DAY/TRUNC(date, 'fmt') date arithmetic.
            # See each function's own docstring in plsql_converter.py for
            # exactly what is and isn't rewritten -- shared with
            # convert_body, which applies the same four to a routine's own
            # embedded SELECTs.
            from tgdatabridge.core.plsql_converter import (
                rewrite_listagg, rewrite_oracle_date_functions,
                rewrite_regexp_functions, rewrite_rownum_topn,
            )
            text = rewrite_rownum_topn(text, issues)
            text = rewrite_listagg(text, issues)
            text = rewrite_regexp_functions(text, issues)
            text = rewrite_oracle_date_functions(text, issues)

    # A CONNECT BY hierarchical query gets one shot at an automatic
    # recursive-CTE rewrite (see connect_by_rewriter.py's module docstring
    # for exactly which shapes qualify) before falling through to the
    # unconditional "must be rewritten by hand" marker below -- if it
    # rewrote successfully, the literal "CONNECT BY" text is gone from
    # `text` by the time that marker scan runs, so it no longer fires.
    if "CONNECT BY" in text.upper():
        from tgdatabridge.core.connect_by_rewriter import find_and_rewrite
        cte_keyword = "WITH" if engine_key.startswith("sqlserver") or engine_key.startswith("db2") else "WITH RECURSIVE"
        text, connect_by_issues = find_and_rewrite(text, cte_keyword=cte_keyword)
        issues.extend(connect_by_issues)

    oracle_markers = {
        "(+)": "Oracle outer-join operator (+) is not supported; rewrite using ANSI LEFT/RIGHT JOIN.",
        "CONNECT BY": "Hierarchical query (CONNECT BY) has no direct equivalent; rewrite using a recursive CTE.",
        "ROWNUM": "ROWNUM must be rewritten using LIMIT/OFFSET or ROW_NUMBER() OVER (...).",
        "DECODE(": "DECODE() must be rewritten as a CASE expression.",
        "SYSDATE": "SYSDATE must be replaced with CURRENT_TIMESTAMP or NOW().",
        "NVL(": "NVL() must be rewritten as COALESCE().",
    }
    upper_text = text.upper()
    for marker, message in oracle_markers.items():
        if marker in upper_text:
            issues.append(ConversionIssue("warning", message))

    if engine_key.startswith("postgres"):
        if view.is_materialized:
            # WITH NO DATA unconditionally, regardless of Oracle's own
            # BUILD_MODE -- "Apply DDL to Target" runs before this tool's
            # separate data-migration step, so a BUILD IMMEDIATE materialized
            # view populated by actually running its query here would just
            # run it against still-empty target base tables, silently
            # "succeeding" with zero rows and no indication anything was
            # wrong. Report the source's original refresh settings instead
            # of guessing at reproducing them: PostgreSQL has no fast/
            # on-commit refresh equivalent at all (ora2pg documents the same
            # limitation for its own MV export), so only a full
            # REFRESH MATERIALIZED VIEW the customer runs themselves --
            # after the underlying tables actually have data -- is possible
            # here regardless of what Oracle's REFRESH_MODE/METHOD were.
            quoted_name = _quote_pg(view.name)
            ddl = f"CREATE MATERIALIZED VIEW IF NOT EXISTS {quoted_name} AS\n{text}\nWITH NO DATA;"
            source_settings = ", ".join(
                f"{label}={value}" for label, value in (
                    ("build_mode", view.mview_build_mode),
                    ("refresh_mode", view.mview_refresh_mode),
                    ("refresh_method", view.mview_refresh_method),
                ) if value
            ) or "unknown"
            issues.append(ConversionIssue(
                "info",
                f"MATERIALIZED VIEW {view.name}: created empty (WITH NO DATA) -- PostgreSQL has no "
                f"equivalent to Oracle's fast/on-commit refresh (source refresh settings: "
                f"{source_settings}), so only a full refresh is possible, and only once this view's "
                f"underlying tables have been migrated. Run `REFRESH MATERIALIZED VIEW {quoted_name};` "
                f"after that data migration completes, and schedule further refreshes yourself (e.g. "
                f"via pg_cron) if the source refreshed automatically.",
            ))
        else:
            ddl = f"CREATE OR REPLACE VIEW {_quote_pg(view.name)} AS\n{text};"
    elif engine_key.startswith("sqlserver"):
        # CREATE VIEW must be the only statement in its batch, so unlike
        # tables/sequences/FKs this can't be wrapped in an IF...BEGIN...END
        # existence guard -- CREATE OR ALTER VIEW (2016 SP1+) is T-SQL's own
        # idempotent-creation syntax for exactly this case.
        ddl = f"CREATE OR ALTER VIEW {_quote_sqlserver(view.name, schema)} AS\n{text};"
    elif engine_key.startswith("db2"):
        # Db2 supports CREATE OR REPLACE VIEW natively, same as Postgres/MySQL.
        ddl = f"CREATE OR REPLACE VIEW {_quote_db2(view.name, schema)} AS\n{text};"
    else:
        ddl = f"CREATE OR REPLACE VIEW {_quote_mysql(view.name)} AS\n{text};"

    view.converted_definition = ddl
    view.status = _worst_status(issues) if issues else ConversionStatus.AUTOMATIC
    view.issues = issues
    return ddl, issues


def _sql_quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"



def uniquify_object_names(schema: Schema, target_engine: str) -> List[ConversionIssue]:
    """Make every constraint and index name unique across the schema.

    MySQL names *every* table's primary key literally `PRIMARY`, and
    scopes index names per table, so a schema of five hundred tables
    routinely carries five hundred constraints called PRIMARY and any
    number of repeated index names. That is legal there and nowhere else:
    PostgreSQL, Oracle, SQL Server and Db2 all scope these names to the
    schema.

    Emitted verbatim, the first table is created and the second is
    rejected:

        CREATE TABLE "drop_down_lang" (... CONSTRAINT "primary" PRIMARY KEY ("id"))
        ERROR: relation "primary" already exists

    -- and because that message says "already exists", the apply loop's
    already-there handling took it for a table that was previously
    created and skipped it, so the table silently never existed at all.
    The damage surfaced much later and somewhere else entirely, as

        there is no unique constraint matching given keys for referenced
        table "dropdown_lists"

    from a foreign key pointing at a table whose primary key had never
    been created.

    Renaming is deterministic and idempotent -- the same schema in gives
    the same names out, and running it twice changes nothing the second
    time -- so a re-converted schema produces a script identical to the
    one already applied, which is what makes re-applying safe.

    A MySQL target is left completely alone: these names are per-table
    there, and renaming them would gratuitously change DDL that works.
    """
    issues: List[ConversionIssue] = []
    engine_key = (target_engine or "").lower().replace(" ", "")
    if engine_key.startswith("mysql") or engine_key.startswith("mongo"):
        return issues

    limit = 30 if engine_key.startswith("oracle") else 63
    taken: set = set()
    renamed = 0

    def claim(preferred: str, table_name: str, kind: str) -> str:
        candidate = _fit_identifier(preferred, limit)
        if candidate.lower() not in taken:
            taken.add(candidate.lower())
            return candidate
        # Qualify with the table, then with a counter -- both derived
        # only from the input, so the result never depends on when this
        # ran or what else was in memory.
        qualified = _fit_identifier(f"{table_name}_{preferred}", limit)
        if qualified.lower() not in taken:
            taken.add(qualified.lower())
            return qualified
        n = 2
        while True:
            numbered = _fit_identifier(f"{table_name}_{preferred}_{n}", limit)
            if numbered.lower() not in taken:
                taken.add(numbered.lower())
                return numbered
            n += 1

    for table in schema.tables:
        for constraint in table.constraints:
            original = constraint.name or ""
            if constraint.kind == "PRIMARY KEY" and original.upper() in ("PRIMARY", ""):
                # MySQL's fixed name for every primary key. Renamed to
                # PostgreSQL's own convention rather than to
                # "table_primary", so the result reads like something a
                # person would have written.
                preferred = f"{table.name}_pkey"
            else:
                preferred = original or f"{table.name}_{constraint.kind.lower().replace(' ', '_')}"
            new_name = claim(preferred, table.name, constraint.kind)
            if new_name != original:
                renamed += 1
                if original and original.upper() != "PRIMARY":
                    issues.append(ConversionIssue(
                        "info",
                        f"{table.name}: constraint {original} was renamed to {new_name} -- "
                        f"{target_engine} requires constraint names to be unique across the "
                        f"whole schema, and this one was already taken.",
                    ))
                constraint.name = new_name

        for index in table.indexes:
            original = index.name or f"{table.name}_idx"
            new_name = claim(original, table.name, "INDEX")
            if new_name != original:
                renamed += 1
                issues.append(ConversionIssue(
                    "info",
                    f"{table.name}: index {original} was renamed to {new_name} -- "
                    f"{target_engine} requires index names to be unique across the whole "
                    f"schema, and this one was already taken.",
                ))
                index.name = new_name

    if renamed:
        issues.insert(0, ConversionIssue(
            "info",
            f"{renamed} constraint/index name(s) were made unique for {target_engine}, which "
            f"scopes them to the schema rather than to the table. A MySQL source names every "
            f"table's primary key \"PRIMARY\", so this is expected and not a problem.",
        ))
    return issues


def _fit_identifier(name: str, limit: int) -> str:
    """Keep an identifier inside the target's length limit without losing
    what makes it distinct: over-long names keep a prefix plus a short
    hash of the whole thing, so two different long names never collapse
    into the same truncation."""
    if len(name) <= limit:
        return name
    import hashlib

    digest = hashlib.blake2b(name.encode("utf-8"), digest_size=4).hexdigest()
    return f"{name[:limit - len(digest) - 1]}_{digest}"


def generate_schema_ddl(
    schema: Schema, target_engine: str, target_schema: Optional[str] = None,
    progress_cb: Optional[Callable[[int, int], None]] = None,
    defer_constraints: bool = False,
    include_deferred: bool = True,
) -> Tuple[str, List[ConversionIssue]]:
    """Generate one combined DDL script for the whole schema, in dependency
    order: sequences, tables (PK/UNIQUE/CHECK + indexes only), foreign keys
    (as a separate pass once every table exists), views, then routines/
    triggers (converted to PL/pgSQL for a PostgreSQL target).

    `target_schema`, when given for a PostgreSQL target, is emitted as a
    leading CREATE SCHEMA IF NOT EXISTS + search_path statement so the
    script is self-contained if it's saved and run outside this tool
    (PostgresConnector applies the same statements itself when connecting,
    so this only matters for the "Save DDL..." export / manual review). For
    a SQL Server or Db2 target, `target_schema` is instead threaded through
    as an explicit `[schema].[object]` / `"SCHEMA"."OBJECT"` qualifier on
    every table/sequence/FK/view (neither has a search_path equivalent to
    set once for the session the way Postgres does). An Oracle target uses
    the same qualifier-threading approach as SQL Server/Db2, but never
    emits a leading CREATE SCHEMA statement at all -- Oracle has no such
    statement (a "schema" *is* a user account, created with CREATE USER
    plus separate GRANT/quota administration this tool has no business
    performing on someone's behalf), so `target_schema` for an Oracle
    target must already refer to an existing user for every generated
    `"SCHEMA"."OBJECT"` reference to resolve.

    `progress_cb(done, total)`, if given, is called once per sequence/
    table/view/routine processed -- on a schema with tens of thousands of
    objects (a large banking core, say), this step can take a while, and an
    indeterminate spinner in the GUI gives no sense of how far along it is.

    `defer_constraints=True` omits PRIMARY KEY/UNIQUE/CHECK constraints,
    indexes, foreign keys and triggers, producing the *pre-load* half of a
    two-phase migration (SCALE.md section 1.3); `include_deferred=False`
    additionally drops the FK/trigger sections without affecting the table
    bodies. Both default to the pre-existing single-script behavior, so
    every existing caller is unchanged. Most callers should use
    generate_schema_ddl_phased() rather than these two flags directly."""
    all_issues: List[ConversionIssue] = []
    parts: List[str] = []

    # Which dialect the schema's DEFAULT expressions, CHECK conditions,
    # view bodies and routine bodies are actually written in. Everything
    # downstream needs it to translate rather than copy verbatim -- see
    # _default_clause and tsql_dialect for what that verbatim copy used to
    # cost on a SQL Server source.
    source_engine = getattr(schema, "source_engine", "Oracle") or "Oracle"

    # Foreign-key columns must carry the same type as the column they
    # reference. Done once, up front, because the per-table generators
    # below each see only their own table -- see
    # align_foreign_key_column_types for the mismatch this prevents.
    all_issues.extend(align_foreign_key_column_types(schema, target_engine))

    # Constraint and index names are per-table on MySQL and per-schema
    # everywhere else, so a MySQL source's five hundred primary keys --
    # every one of them named PRIMARY -- collide on the target. See
    # uniquify_object_names for what that failure looked like.
    all_issues.extend(uniquify_object_names(schema, target_engine))

    is_postgres = target_engine.lower().startswith("postgres")
    is_sqlserver = target_engine.lower().replace(" ", "").startswith("sqlserver")
    is_db2 = target_engine.lower().startswith("db2")
    is_mongodb = target_engine.lower().startswith("mongo")
    is_oracle = target_engine.lower().startswith("oracle")
    # mysql is implicitly "none of the above", preserving the exact
    # pre-existing 2-way behavior for every caller that only ever passed
    # "PostgreSQL" or "MySQL" here.

    total_objects = len(schema.sequences) + len(schema.tables) + len(schema.views) + len(schema.routines)
    done = 0

    def _tick() -> None:
        nonlocal done
        done += 1
        if progress_cb:
            progress_cb(done, total_objects)

    if is_postgres and target_schema:
        parts.append(
            f'-- Target schema\n'
            f"CREATE SCHEMA IF NOT EXISTS {quote_double(target_schema)};\n"
            f"SET search_path TO {quote_double(target_schema)};"
        )
    elif is_sqlserver and target_schema:
        # CREATE SCHEMA must be the only statement in its batch, so it's
        # wrapped in dynamic SQL (EXEC(...)) to run inside the same
        # IF-guarded idempotency pattern used for tables/sequences/FKs below.
        parts.append(
            f"-- Target schema\n"
            f"IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = N{quote_literal(target_schema)})\n"
            f"  EXEC('CREATE SCHEMA ' + QUOTENAME(N{quote_literal(target_schema)}));"
        )
    elif is_db2 and target_schema:
        # Same idea as the SQL Server branch above, Db2 dialect: guarded
        # with a SYSCAT.SCHEMATA existence check, executing the DDL itself
        # via EXECUTE IMMEDIATE inside an anonymous compound statement.
        inner = f"CREATE SCHEMA {_quote_db2(target_schema)}"
        parts.append(
            f"-- Target schema\n"
            f"BEGIN\n"
            f"  IF NOT EXISTS (SELECT 1 FROM SYSCAT.SCHEMATA WHERE SCHEMANAME = {quote_literal(target_schema.upper())}) THEN\n"
            f"    EXECUTE IMMEDIATE {_sql_quote(inner)};\n"
            f"  END IF;\n"
            f"END;"
        )

    sqlserver_schema = target_schema if is_sqlserver else None
    db2_schema = target_schema if is_db2 else None
    oracle_schema = target_schema if is_oracle else None
    qualifying_schema = sqlserver_schema or db2_schema or oracle_schema  # only one of the three is ever set

    parts.append(f"-- Sequences ({len(schema.sequences)})")
    for seq in schema.sequences:
        if is_postgres:
            ddl, issues = generate_sequence_ddl_postgres(seq)
        elif is_sqlserver:
            ddl, issues = generate_sequence_ddl_sqlserver(seq, sqlserver_schema)
        elif is_db2:
            ddl, issues = generate_sequence_ddl_db2(seq, db2_schema)
        elif is_oracle:
            ddl, issues = generate_sequence_ddl_oracle(seq, oracle_schema)
        elif is_mongodb:
            ddl, issues = generate_sequence_ddl_mongodb(seq)
        else:
            ddl, issues = generate_sequence_ddl_mysql(seq)
        parts.append(ddl)
        all_issues.extend(issues)
        _tick()

    heading = "Tables (columns only -- constraints/indexes deferred)" if defer_constraints else "Tables"
    parts.append(f"\n-- {heading} ({len(schema.tables)})")
    for table in schema.tables:
        if is_postgres:
            ddl, issues = generate_table_ddl_postgres(
                table, defer_constraints, source_engine=source_engine)
        elif is_sqlserver:
            ddl, issues = generate_table_ddl_sqlserver(
                table, sqlserver_schema, defer_constraints, source_engine=source_engine)
        elif is_db2:
            ddl, issues = generate_table_ddl_db2(
                table, db2_schema, defer_constraints, source_engine=source_engine)
        elif is_oracle:
            ddl, issues = generate_table_ddl_oracle(
                table, oracle_schema, defer_constraints, source_engine=source_engine)
        elif is_mongodb:
            ddl, issues = generate_table_ddl_mongodb(table, defer_constraints)
        else:
            ddl, issues = generate_table_ddl_mysql(
                table, defer_constraints, source_engine=source_engine)
        parts.append(ddl)
        all_issues.extend(issues)
        _tick()

    # Foreign keys run as their own pass *after* every table has been created,
    # regardless of what order schema.tables happens to be in. A table's FK
    # can reference a table that comes later in that list (or, for a pair of
    # tables that reference each other, a table that could never come first
    # no matter the order) — emitting FKs inline per-table breaks the moment
    # the referenced table hasn't been created yet.
    if is_postgres:
        fk_statements = [generate_foreign_key_ddl_postgres(table) for table in schema.tables]
    elif is_sqlserver:
        fk_statements = [generate_foreign_key_ddl_sqlserver(table, sqlserver_schema) for table in schema.tables]
    elif is_db2:
        fk_statements = [generate_foreign_key_ddl_db2(table, db2_schema) for table in schema.tables]
    elif is_oracle:
        fk_statements = [generate_foreign_key_ddl_oracle(table, oracle_schema) for table in schema.tables]
    elif is_mongodb:
        fk_statements = [generate_foreign_key_ddl_mongodb(table) for table in schema.tables]
    else:
        fk_statements = [generate_foreign_key_ddl_mysql(table) for table in schema.tables]
    fk_statements = [fk for fk in fk_statements if fk]
    if not include_deferred:
        # The post-load script owns these instead -- see
        # generate_schema_ddl_phased.
        fk_statements = []
    if fk_statements:
        parts.append(f"\n-- Foreign Keys ({len(fk_statements)} table(s), applied after all tables exist)")
        parts.append("\n".join(fk_statements))

    parts.append(f"\n-- Views ({len(schema.views)})")
    for view in schema.views:
        ddl, issues = generate_view_ddl(view, target_engine, qualifying_schema)
        parts.append(ddl)
        all_issues.extend(issues)
        _tick()

    # A trigger that exists while data is being bulk-loaded fires once per
    # migrated row -- catastrophic for load throughput, and usually wrong:
    # the source rows already reflect whatever the trigger would do, so
    # letting it fire again re-applies its effects on top. Procedures and
    # functions are harmless during a load (nothing calls them) and stay
    # here, where a trigger created later can depend on them.
    emitted_routines = (
        schema.routines if include_deferred
        else [r for r in schema.routines if r.kind != "TRIGGER"]
    )
    parts.append(f"\n-- Routines / Triggers ({len(emitted_routines)})")
    from tgdatabridge.core.plsql_converter import convert_routine  # local import: avoids a hard dependency at module load
    for routine in emitted_routines:
        convert_routine(routine, target_engine)
        header = f"-- {routine.kind} {routine.name} ({routine.status.value})"
        body = routine.converted_source or f"-- MANUAL CONVERSION REQUIRED for {routine.kind} {routine.name}"
        block = f"{header}\n{_replace_prefix(routine, target_engine, qualifying_schema)}{body}"
        # Force every routine's rendered block to end with its own ';':
        # comment-only blocks (a PACKAGE spec placeholder with no Postgres
        # equivalent, or a "MANUAL CONVERSION REQUIRED" stub) have no
        # terminating semicolon of their own, so without this the statement
        # splitter used by "Apply DDL to Target" never flushes them — they
        # silently merge into whatever CREATE FUNCTION/PROCEDURE/TRIGGER
        # comes next, which corrupts that statement's reported line numbers
        # and lumps two unrelated objects under one "Statement N" entry.
        # The terminator must go on its OWN line: split_sql_statements
        # treats "--"-through-end-of-line as an inert comment (so a real
        # apostrophe in comment prose is never mistaken for a SQL string
        # literal), so a ';' appended directly onto the end of a comment
        # line would itself be swallowed as part of that same comment and
        # never seen as a statement terminator at all.
        if not block.rstrip().endswith(";"):
            block += "\n;"
        parts.append(block)
        all_issues.extend(routine.issues)
        _tick()

    # total_objects counted every routine, including any trigger skipped
    # above -- tick the remainder so a progress bar still reaches 100%.
    for _ in range(len(schema.routines) - len(emitted_routines)):
        _tick()

    return "\n\n".join(parts), all_issues


# --------------------------------------------------------- two-phase DDL


def generate_post_load_ddl(
    schema: Schema, target_engine: str, target_schema: Optional[str] = None,
) -> Tuple[str, List[ConversionIssue]]:
    """Everything `generate_schema_ddl(..., defer_constraints=True,
    include_deferred=False)` left out: primary keys, unique and check
    constraints, indexes, foreign keys, and triggers -- to be applied once
    the data has landed. See SCALE.md section 1.3."""
    all_issues: List[ConversionIssue] = []
    parts: List[str] = []

    engine = target_engine.lower()
    is_postgres = engine.startswith("postgres")
    is_sqlserver = engine.replace(" ", "").startswith("sqlserver")
    is_db2 = engine.startswith("db2")
    is_oracle = engine.startswith("oracle")
    is_mongodb = engine.startswith("mongo")

    if is_postgres and target_schema:
        # Self-contained if the script is saved and run outside this tool,
        # matching generate_schema_ddl's own leading search_path statement.
        parts.append(f"SET search_path TO {quote_double(target_schema)};")

    qualifying_schema = target_schema if (is_sqlserver or is_db2 or is_oracle) else None

    source_engine = getattr(schema, "source_engine", "Oracle") or "Oracle"
    constraint_statements = [
        generate_deferred_ddl(table, target_engine, qualifying_schema, source_engine)
        for table in schema.tables
    ]
    constraint_statements = [s for s in constraint_statements if s]
    if constraint_statements:
        parts.append(
            f"-- Constraints & indexes ({len(constraint_statements)} table(s), applied after data load)")
        parts.append("\n".join(constraint_statements))

    # Foreign keys stay a separate pass after the PK/UNIQUE constraints
    # above, for the same reason they're a separate pass in
    # generate_schema_ddl: an FK can reference a table (and a unique key)
    # that only exists once every other table has been processed.
    if is_postgres:
        fk_statements = [generate_foreign_key_ddl_postgres(t) for t in schema.tables]
    elif is_sqlserver:
        fk_statements = [generate_foreign_key_ddl_sqlserver(t, qualifying_schema) for t in schema.tables]
    elif is_db2:
        fk_statements = [generate_foreign_key_ddl_db2(t, qualifying_schema) for t in schema.tables]
    elif is_oracle:
        fk_statements = [generate_foreign_key_ddl_oracle(t, qualifying_schema) for t in schema.tables]
    elif is_mongodb:
        fk_statements = [generate_foreign_key_ddl_mongodb(t) for t in schema.tables]
    else:
        fk_statements = [generate_foreign_key_ddl_mysql(t) for t in schema.tables]
    fk_statements = [fk for fk in fk_statements if fk]
    if fk_statements:
        parts.append(f"\n-- Foreign Keys ({len(fk_statements)} table(s))")
        parts.append("\n".join(fk_statements))

    triggers = [r for r in schema.routines if r.kind == "TRIGGER"]
    if triggers:
        from tgdatabridge.core.plsql_converter import convert_routine
        parts.append(f"\n-- Triggers ({len(triggers)}, created after data load so they never fire on migrated rows)")
        for routine in triggers:
            convert_routine(routine, target_engine)
            header = f"-- {routine.kind} {routine.name} ({routine.status.value})"
            body = routine.converted_source or f"-- MANUAL CONVERSION REQUIRED for {routine.kind} {routine.name}"
            block = f"{header}\n{_replace_prefix(routine, target_engine, qualifying_schema)}{body}"
            # Same terminator handling as generate_schema_ddl's routine
            # loop -- see its comment for why the ';' must go on its own
            # line.
            if not block.rstrip().endswith(";"):
                block += "\n;"
            parts.append(block)
            all_issues.extend(routine.issues)

    if not parts or (len(parts) == 1 and is_postgres and target_schema):
        return "-- Nothing to apply after the data load for this schema.", all_issues

    return "\n\n".join(parts), all_issues


def generate_schema_ddl_phased(
    schema: Schema, target_engine: str, target_schema: Optional[str] = None,
    progress_cb: Optional[Callable[[int, int], None]] = None,
) -> Tuple[str, str, List[ConversionIssue]]:
    """Split the generated DDL into the two scripts a large migration
    actually wants (SCALE.md section 1.3):

      - **pre-load**: sequences, tables as bare columns (types, NOT NULL,
        DEFAULT, IDENTITY, comments), views, and procedures/functions.
      - **post-load**: primary keys, unique and check constraints,
        indexes, foreign keys, and triggers.

    Loading into a table with no indexes and no constraints is
    substantially faster -- nothing is maintained per row -- and building
    each index once over the finished table produces a denser index than
    incremental maintenance does. For a large table this can more than
    halve the load time.

    Returns `(pre_load_sql, post_load_sql, issues)`. The union of the two
    scripts creates the same objects the single-script
    `generate_schema_ddl()` does; nothing is dropped, only reordered.

    The cost, stated plainly: a constraint violation that would have
    failed on the first bad row now surfaces at post-load time, after
    everything has been copied. Run the dry-run/plan step and rehearse
    against representative data.
    """
    pre_sql, pre_issues = generate_schema_ddl(
        schema, target_engine, target_schema, progress_cb,
        defer_constraints=True, include_deferred=False,
    )
    post_sql, post_issues = generate_post_load_ddl(schema, target_engine, target_schema)
    # post_issues comes from re-converting the triggers that the pre-load
    # pass skipped, so the combined list matches what the single-script
    # path would have reported.
    return pre_sql, post_sql, pre_issues + post_issues


# ------------------------------------------------------------- rollback script


def _drop_table_ddl(table: Table, target_engine: str, schema: Optional[str] = None) -> str:
    engine_key = target_engine.lower().replace(" ", "")
    if engine_key.startswith("postgres"):
        return f'DROP TABLE IF EXISTS {_quote_pg(table.name)} CASCADE;'
    if engine_key.startswith("sqlserver"):
        qualified = _quote_sqlserver(table.name, schema)
        ref = f"{schema}.{table.name}" if schema else table.name
        return f"IF OBJECT_ID('{ref}', 'U') IS NOT NULL\n  DROP TABLE {qualified};"
    if engine_key.startswith("db2"):
        qualified = _quote_db2(table.name, schema)
        return _db2_idempotent(f"DROP TABLE {qualified}", "SYSCAT.TABLES", "TABNAME", table.name, "TABSCHEMA", schema)
    if engine_key.startswith("oracle"):
        qualified = _quote_oracle(table.name, schema)
        return _oracle_idempotent(f"DROP TABLE {qualified} CASCADE CONSTRAINTS", ignore_codes=(-942,))
    if engine_key.startswith("mongo"):
        # Recognized by MongoConnector.execute_ddl's "drop" branch -- see
        # that module's docstring.
        return f"db[{json.dumps(table.name)}].drop();"
    # mysql is implicitly "none of the above", same convention generate_schema_ddl uses.
    return f'DROP TABLE IF EXISTS {_quote_mysql(table.name)};'


def _drop_view_ddl(view: View, target_engine: str, schema: Optional[str] = None) -> str:
    engine_key = target_engine.lower().replace(" ", "")
    if engine_key.startswith("postgres"):
        if view.is_materialized:
            # Confirmed directly against a live PostgreSQL 16 server:
            # `DROP VIEW IF EXISTS` on a materialized view errors outright
            # -- "\"x\" is not a view" -- IF EXISTS does not save it,
            # because the object it finds under that name is the wrong
            # *kind*, not merely present-or-absent. DROP MATERIALIZED VIEW
            # is the only spelling that works for one.
            return f'DROP MATERIALIZED VIEW IF EXISTS {_quote_pg(view.name)} CASCADE;'
        return f'DROP VIEW IF EXISTS {_quote_pg(view.name)} CASCADE;'
    if engine_key.startswith("sqlserver"):
        qualified = _quote_sqlserver(view.name, schema)
        ref = f"{schema}.{view.name}" if schema else view.name
        return f"IF OBJECT_ID('{ref}', 'V') IS NOT NULL\n  DROP VIEW {qualified};"
    if engine_key.startswith("db2"):
        qualified = _quote_db2(view.name, schema)
        return _db2_idempotent(f"DROP VIEW {qualified}", "SYSCAT.TABLES", "TABNAME", view.name, "TABSCHEMA", schema)
    if engine_key.startswith("oracle"):
        qualified = _quote_oracle(view.name, schema)
        return _oracle_idempotent(f"DROP VIEW {qualified}", ignore_codes=(-942,))
    if engine_key.startswith("mongo"):
        return (
            f'-- NOTE: VIEW {view.name} is always a MANUAL CONVERSION REQUIRED placeholder on a '
            f"MongoDB target (see generate_view_ddl's MongoDB branch) -- nothing real was ever "
            f"created for it, so there is nothing to drop."
        )
    return f'DROP VIEW IF EXISTS {_quote_mysql(view.name)};'


def _drop_sequence_ddl(seq: Sequence, target_engine: str, schema: Optional[str] = None) -> str:
    engine_key = target_engine.lower().replace(" ", "")
    if engine_key.startswith("postgres"):
        return f'DROP SEQUENCE IF EXISTS {_quote_pg(seq.name)};'
    if engine_key.startswith("sqlserver"):
        qualified = _quote_sqlserver(seq.name, schema)
        return f"IF EXISTS (SELECT 1 FROM sys.sequences WHERE name = {quote_literal(seq.name)})\n  DROP SEQUENCE {qualified};"
    if engine_key.startswith("db2"):
        qualified = _quote_db2(seq.name, schema)
        return _db2_idempotent(f"DROP SEQUENCE {qualified}", "SYSCAT.SEQUENCES", "SEQNAME", seq.name, "SEQSCHEMA", schema)
    if engine_key.startswith("oracle"):
        qualified = _quote_oracle(seq.name, schema)
        return _oracle_idempotent(f"DROP SEQUENCE {qualified}", ignore_codes=(-2289,))
    if engine_key.startswith("mongo"):
        return (
            f'-- NOTE: sequence {seq.name} is emulated via a "counters" helper collection document '
            f"(see generate_sequence_ddl_mongodb) -- it is dropped along with the collection(s) above "
            f'that used it, or remove just this counter: db["counters"].deleteOne({{"_id": {json.dumps(seq.name)}}});'
        )
    # mysql: sequences are emulated via a "<name>_SEQ" helper table (see generate_sequence_ddl_mysql).
    helper_table = f"{seq.name}_SEQ"
    return f'DROP TABLE IF EXISTS {_quote_mysql(helper_table)};'


_REPLACE_CAPABLE_RE = re.compile(
    r"^\s*CREATE\s+OR\s+(?:REPLACE|ALTER)\b", re.IGNORECASE)


def _replace_prefix(routine: Routine, target_engine: str,
                    schema: Optional[str] = None) -> str:
    """A `DROP ... IF EXISTS` to put in front of a routine's CREATE, so
    the script can be applied more than once.

    Applying converted DDL is an apply/adjust/apply-again loop in
    practice, and most of the script already survives that: tables use
    `CREATE TABLE IF NOT EXISTS`, views `CREATE OR REPLACE VIEW`. A
    routine does not. MySQL answers a second run with

        1304 (HY000): PROCEDURE usp_GetEmployees already exists
        1359 (HY000): Trigger already exists

    and even if the apply path treats that as "already there" and carries
    on (see tgdatabridge.utils.ddl_errors), the *edited* body would then never
    reach the target -- the worst outcome of the three, because it looks
    like it worked.

    Emitted only where it is both needed and safe:

    * nothing when the converted CREATE already replaces in place
      (`CREATE OR REPLACE` on PostgreSQL/Oracle/Db2, `CREATE OR ALTER` on
      SQL Server) -- notably keeping PostgreSQL's `DROP FUNCTION ...
      CASCADE`, which would take dependent views with it, out of the
      forward script entirely;
    * nothing for a routine with no converted body (a manual-conversion
      placeholder created nothing to drop);
    * nothing when `_drop_routine_ddl` has only an explanatory comment to
      give (packages flattened into individual objects, MongoDB targets).
    """
    body = routine.converted_source or ""
    if not body.strip():
        return ""
    if _REPLACE_CAPABLE_RE.match(body):
        return ""
    if re.match(r"\s*DROP\s+(TABLE|TRIGGER|PROCEDURE|FUNCTION|VIEW)\b",
                body, re.IGNORECASE):
        # The converter already emitted its own DROP. Several of them do,
        # because they have to: a multi-event trigger becomes N objects
        # under names this function cannot know, and a flattened package
        # becomes one object per member. Adding a second DROP on top is
        # harmless but looks like a bug in the generated script, which is
        # something a DBA reads before running it.
        return ""
    drop = _drop_routine_ddl(routine, target_engine, schema)
    from tgdatabridge.utils.sql_split import has_executable_sql
    if not drop or not has_executable_sql(drop):
        return ""
    return drop.rstrip() + "\n"


def _drop_routine_ddl(routine: Routine, target_engine: str, schema: Optional[str] = None) -> str:
    engine_key = target_engine.lower().replace(" ", "")
    kind = routine.kind
    name = routine.name

    if engine_key.startswith("mongo"):
        # Checked first, ahead of the PACKAGE/PACKAGE BODY flattening note
        # below: convert_routine's MongoDB branch flags *every* routine
        # kind (not just packages) as a MANUAL CONVERSION REQUIRED
        # placeholder, unlike Postgres/MySQL/SQL Server/Db2, which do flatten
        # a package body into real individual procedure/function objects --
        # the "flattened into individual objects" reasoning below simply
        # doesn't apply to a MongoDB target at all.
        return (
            f"-- NOTE: {kind} {name} is always a MANUAL CONVERSION REQUIRED placeholder on a MongoDB "
            f"target (see plsql_converter.convert_routine's MongoDB branch) -- nothing real was ever "
            f"created for it, so there is nothing to drop."
        )

    if kind in ("PACKAGE", "PACKAGE BODY") and not engine_key.startswith("oracle"):
        return (
            f"-- NOTE: {kind} {name} has no direct equivalent on a {target_engine} target -- its "
            f"procedures/functions were flattened into individual objects (see plsql_converter's "
            f"package-body flattener). Drop each of those individually by name rather than by this "
            f"package's original name."
        )

    if kind in ("PACKAGE", "PACKAGE BODY"):
        # Oracle target only -- convert_routine's Oracle branch reconstructs
        # both a real "CREATE OR REPLACE PACKAGE ..." (spec) and a real
        # "CREATE OR REPLACE PACKAGE BODY ..." object here (see
        # plsql_converter's Oracle-target branch), unlike every other
        # target, which never gets a genuine PACKAGE/PACKAGE BODY object at
        # all -- so this is the one place a real DROP PACKAGE[ BODY] is
        # actually correct rather than falling through to the generic
        # PROCEDURE/FUNCTION drop below (which would silently emit the
        # wrong DDL, "DROP PROCEDURE" against a PACKAGE BODY object).
        qualified = _quote_oracle(name, schema)
        drop_kw = "PACKAGE BODY" if kind == "PACKAGE BODY" else "PACKAGE"
        return _oracle_idempotent(f"DROP {drop_kw} {qualified}", ignore_codes=(-4043,))

    if kind == "TRIGGER":
        # A multi-event trigger is created as one object per event on the
        # engines that allow only one -- see plsql_mysql_converter and
        # tsql_routine_converter -- so a rollback that drops only the base
        # name leaves every one of them behind on the target.
        event_names = ([f"{name}_{event}" for event in routine.events]
                       if len(routine.events or []) > 1 else [name])
        if engine_key.startswith("postgres"):
            # PostgreSQL keeps a trigger's body in a companion function
            # the converter names `<trigger>_fn`. Dropping the trigger
            # alone orphaned it on the target after every rollback.
            on_table = f" ON {_quote_pg(routine.table_name)}" if routine.table_name else ""
            return (f'DROP TRIGGER IF EXISTS {_quote_pg(name)}{on_table};\n'
                    f'DROP FUNCTION IF EXISTS {_quote_pg(name + "_fn")}();')
        if engine_key.startswith("sqlserver"):
            return "\n".join(
                f"IF OBJECT_ID('{(schema + '.' + each) if schema else each}', 'TR') "
                f"IS NOT NULL\n  DROP TRIGGER {_quote_sqlserver(each, schema)};"
                for each in event_names)
        if engine_key.startswith("db2"):
            return "\n".join(
                _db2_idempotent(f"DROP TRIGGER {_quote_db2(each, schema)}",
                                "SYSCAT.TRIGGERS", "TRIGNAME", each, "TRIGSCHEMA", schema)
                for each in event_names)
        if engine_key.startswith("oracle"):
            qualified = _quote_oracle(name, schema)
            return _oracle_idempotent(f"DROP TRIGGER {qualified}", ignore_codes=(-4080, -4043))
        return "\n".join(f'DROP TRIGGER IF EXISTS {_quote_mysql(each)};'
                         for each in event_names)

    # PROCEDURE / FUNCTION
    object_kw = "FUNCTION" if kind == "FUNCTION" else "PROCEDURE"
    if engine_key.startswith("postgres"):
        return f'DROP {object_kw} IF EXISTS {_quote_pg(name)} CASCADE;'
    if engine_key.startswith("sqlserver"):
        qualified = _quote_sqlserver(name, schema)
        ref = f"{schema}.{name}" if schema else name
        obj_type_code = "FN" if kind == "FUNCTION" else "P"
        return f"IF OBJECT_ID('{ref}', '{obj_type_code}') IS NOT NULL\n  DROP {object_kw} {qualified};"
    if engine_key.startswith("db2"):
        qualified = _quote_db2(name, schema)
        return _db2_idempotent(f"DROP {object_kw} {qualified}", "SYSCAT.ROUTINES", "ROUTINENAME", name, "ROUTINESCHEMA", schema)
    if engine_key.startswith("oracle"):
        qualified = _quote_oracle(name, schema)
        return _oracle_idempotent(f"DROP {object_kw} {qualified}", ignore_codes=(-4043,))
    return f'DROP {object_kw} IF EXISTS {_quote_mysql(name)};'


def generate_rollback_ddl(schema: Schema, target_engine: str, target_schema: Optional[str] = None) -> str:
    """A DROP script that undoes generate_schema_ddl's output for the same
    (schema, target_engine, target_schema) -- every object, in reverse
    dependency order (routines/triggers first, then views, then tables
    child-before-parent, then sequences last), so an aborted or unwanted
    cutover can be cleanly backed out instead of leaving a half-applied
    target schema behind. Every DROP is guarded to be a no-op against an
    object that was never created (or was already dropped) -- the same
    idempotent-DDL tolerance generate_schema_ddl's own CREATE statements
    already give a re-run, applied here in reverse.

    This only undoes *schema* objects (what "Apply DDL to Target"
    creates) -- it does not, and safely can't in general, separately undo
    just the *data* written by a later "Migrate Data" run: DROP TABLE
    takes any rows in it down too, so running this rollback after data has
    already been migrated is a genuine destructive rollback of both, not
    merely a schema-only undo. Meant to be generated and reviewed
    alongside the forward DDL, the same way that script itself is meant to
    be reviewed before being applied."""
    from tgdatabridge.core.migrator import order_tables_by_dependency  # local import: avoids a hard dependency at module load, mirrors generate_schema_ddl's own local plsql_converter import

    engine_key = target_engine.lower().replace(" ", "")
    is_sqlserver = engine_key.startswith("sqlserver")
    is_db2 = engine_key.startswith("db2")
    is_oracle = engine_key.startswith("oracle")
    qualifying_schema = target_schema if (is_sqlserver or is_db2 or is_oracle) else None

    parts: List[str] = [
        "-- Rollback script: drops every object generate_schema_ddl() would create for this schema,\n"
        "-- in reverse dependency order. Review before running -- this is destructive, and (for\n"
        "-- tables) removes any data already migrated into them, not just the schema definitions."
    ]

    parts.append(f"\n-- Routines / Triggers ({len(schema.routines)})")
    for routine in reversed(schema.routines):
        parts.append(_drop_routine_ddl(routine, target_engine, qualifying_schema))

    parts.append(f"\n-- Views ({len(schema.views)})")
    for view in reversed(schema.views):
        parts.append(_drop_view_ddl(view, target_engine, qualifying_schema))

    ordered_tables = order_tables_by_dependency(schema.tables)
    parts.append(f"\n-- Tables ({len(ordered_tables)}, child-before-parent)")
    for table in reversed(ordered_tables):
        parts.append(_drop_table_ddl(table, target_engine, qualifying_schema))

    parts.append(f"\n-- Sequences ({len(schema.sequences)})")
    for seq in reversed(schema.sequences):
        parts.append(_drop_sequence_ddl(seq, target_engine, qualifying_schema))

    return "\n".join(parts)
