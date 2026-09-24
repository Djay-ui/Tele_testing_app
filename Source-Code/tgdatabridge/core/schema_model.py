"""
Data model describing a database schema, independent of the source engine.

The introspector fills these dataclasses from Oracle system views, the
ddl_generator reads them to emit target-engine DDL, and the assessment /
report modules use them to produce the migration assessment report.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


class ConversionStatus(str, Enum):
    AUTOMATIC = "Converted automatically"
    AUTOMATIC_WITH_WARNINGS = "Converted with warnings"
    MANUAL = "Requires manual conversion"
    NOT_SUPPORTED = "Not supported on target"


@dataclass
class ConversionIssue:
    severity: str  # "info" | "warning" | "error"
    message: str


@dataclass
class Column:
    name: str
    # Raw type in the shared Oracle-flavored pivot representation this tool
    # uses internally for every source engine, e.g. "NUMBER(10,2)",
    # "VARCHAR2(255)", "CLOB" -- for an Oracle source this is literally what
    # Oracle's data dictionary reports; for any other source engine (MySQL,
    # and eventually PostgreSQL/SQL Server) it's what that engine's own
    # native type was translated *into* by that engine's own
    # type_mapping.from_<engine>() reverse-mapper, specifically so the
    # existing to_postgres/to_mysql/to_sqlserver/to_db2 forward-mappers
    # keep working completely unchanged regardless of source engine.
    data_type: str
    nullable: bool = True
    default: Optional[str] = None
    identity: bool = False  # Oracle IDENTITY / GENERATED ALWAYS AS IDENTITY
    comment: Optional[str] = None

    # Forces this column's generated type, bypassing the normal
    # data_type -> target mapping. Set only by
    # ddl_generator.align_foreign_key_column_types, which has to make a
    # foreign-key column's type identical to the type of the column it
    # references: every engine requires that, and the two can otherwise
    # diverge legitimately -- an identity column widened to BIGINT so the
    # target will accept it as auto-increment leaves a plain column of the
    # same source type still mapping to DECIMAL(19,0), which is a rejected
    # foreign key ("errno: 150 Foreign key constraint is incorrectly
    # formed"). The override records the decision on the column rather
    # than re-deriving it in each of the six generators.
    target_type_override: Optional[str] = None

    # populated during conversion
    target_type: Optional[str] = None
    issues: List[ConversionIssue] = field(default_factory=list)
    # Issues from reverse-mapping a non-Oracle source's native type into the
    # pivot representation above (e.g. "MySQL ENUM has no equivalent, mapped
    # to VARCHAR2(20) with the enum constraint dropped") -- kept separate
    # from `issues` (which DDL generation overwrites fresh on every call, one
    # target engine at a time) so these source-side issues survive being
    # folded back in on every generate_table_ddl_* call rather than getting
    # silently discarded the next time DDL is (re)generated. Always empty
    # for an Oracle source, since there's no reverse-mapping step there.
    source_issues: List[ConversionIssue] = field(default_factory=list)


@dataclass
class Constraint:
    name: str
    kind: str                      # "PRIMARY KEY" | "FOREIGN KEY" | "UNIQUE" | "CHECK"
    columns: List[str] = field(default_factory=list)
    ref_table: Optional[str] = None
    ref_columns: List[str] = field(default_factory=list)
    check_condition: Optional[str] = None


@dataclass
class Index:
    name: str
    columns: List[str]
    unique: bool = False


@dataclass
class Partition:
    """One partition of a RANGE- or LIST-partitioned Oracle table, as
    ALL_TAB_PARTITIONS reports it -- kept as raw Oracle expression text
    (mirrors Constraint.check_condition), translated into target syntax
    only in ddl_generator, the same "raw pivot representation, translate
    at DDL-generation time" split used everywhere else in this model."""
    name: str
    # RANGE: Oracle's HIGH_VALUE text for this partition's upper bound, one
    # comma-separated expression per partition-key column (e.g. a single
    # `TO_DATE(' 2024-02-01 00:00:00', 'SYYYY-MM-DD HH24:MI:SS')`, or the
    # literal `MAXVALUE`). LIST: HIGH_VALUE's comma-separated literal list
    # for this partition (e.g. `'NY', 'NJ', 'CT'`), or the literal
    # `DEFAULT` for Oracle's default list partition. None only if the
    # dictionary reported no HIGH_VALUE at all (HASH has none -- see
    # PartitionScheme.kind).
    high_value: Optional[str] = None
    position: int = 0


@dataclass
class PartitionScheme:
    """A table's Oracle partitioning, as ALL_PART_TABLES/ALL_PART_KEY_COLUMNS/
    ALL_TAB_PARTITIONS report it. See ddl_generator._generate_partition_ddl_postgres
    for which of these this tool actually emits native PostgreSQL
    declarative-partitioning DDL for (RANGE/LIST, not composite/HASH -- the
    same scope decision ora2pg itself makes for HASH, per its own docs)."""
    kind: str  # Oracle's PARTITIONING_TYPE: "RANGE" | "LIST" | "HASH"
    columns: List[str] = field(default_factory=list)  # partition-key column(s), in position order
    # Oracle's SUBPARTITIONING_TYPE ("NONE" for a simple, non-composite
    # scheme -- also normalized to None). A non-None/"NONE" value here
    # means this is a composite scheme (e.g. RANGE-HASH); this tool does
    # not attempt subpartition-level translation for any composite scheme.
    subpartitioning_type: Optional[str] = None
    partitions: List[Partition] = field(default_factory=list)  # in PARTITION_POSITION order


@dataclass
class Table:
    name: str
    schema: str
    columns: List[Column] = field(default_factory=list)
    constraints: List[Constraint] = field(default_factory=list)
    indexes: List[Index] = field(default_factory=list)
    row_count_estimate: int = 0
    comment: Optional[str] = None

    # Oracle RANGE/LIST/HASH (and composite) partitioning, populated by the
    # Oracle introspector from ALL_PART_TABLES et al.; None for an ordinary,
    # unpartitioned table (every non-Oracle source, and most Oracle tables).
    partition_scheme: Optional["PartitionScheme"] = None

    # Mongo-source-only metadata, populated exclusively by
    # mongo_source_introspector and otherwise always None/unused (every
    # other source engine's tables map one-to-one onto a real SQL table
    # already addressable as `f"{schema}.{name}"`, so migrator.migrate_table
    # doesn't need either of these to build its SELECT). A MongoDB
    # collection has no such uniform mapping once nested arrays are
    # normalized into synthesized child tables: `source_collection` is the
    # *real* Mongo collection a table's rows come from (same as `name` for
    # a top-level table; the parent's collection name for a synthesized
    # child table, since the child has no collection of its own), and
    # `source_array_path` is the dotted field path to unwind out of that
    # collection to get the child table's rows (None for a top-level
    # table). See migrator.migrate_table and
    # mongo_connector.MongoConnector.fetch_batches_table.
    source_collection: Optional[str] = None
    source_array_path: Optional[str] = None

    # Spreadsheet-source-only, and populated exclusively by
    # spreadsheet_introspector: which input file this table's sheet lives
    # in. Needed only because an Excel/CSV source can now be given up to
    # 50 files at once (SpreadsheetConnector.MAX_SOURCE_FILES), so a sheet
    # name alone no longer identifies a sheet -- two workbooks in the same
    # job may each contain a "Sheet1". `source_collection` still holds the
    # sheet name within that file, exactly as it did for a single-file
    # source, so this is purely additive: None means "the connector's only
    # file", which is what every pre-existing single-file Table has.
    source_file: Optional[str] = None

    status: ConversionStatus = ConversionStatus.AUTOMATIC
    issues: List[ConversionIssue] = field(default_factory=list)


@dataclass
class Sequence:
    name: str
    schema: str
    start_value: int = 1
    increment_by: int = 1
    min_value: Optional[int] = None
    max_value: Optional[int] = None
    cycle: bool = False

    status: ConversionStatus = ConversionStatus.AUTOMATIC
    issues: List[ConversionIssue] = field(default_factory=list)


@dataclass
class View:
    name: str
    schema: str
    definition: str  # raw SELECT text from Oracle -- or, for a MongoDB
    # source, a JSON-serialized aggregation pipeline (see source_engine
    # below); never SQL text in that case.

    # Which engine's native syntax `definition` is actually written in --
    # mirrors Routine.source_engine (same default, same rationale: every
    # caller that constructs a View directly without passing this keeps
    # working unchanged). ddl_generator.generate_view_ddl checks this
    # first and, for "MongoDB", skips straight to a manual-conversion
    # placeholder rather than running SQL-text heuristics (the CONNECT-BY
    # rewrite, the Oracle-marker scan) against what is actually JSON
    # aggregation-pipeline text, not SQL -- those heuristics could
    # otherwise "succeed" by accident against JSON and silently produce
    # wrong output instead of safely flagging the view for manual review.
    source_engine: str = "Oracle"

    # Populated by the Oracle introspector from ALL_MVIEWS for a
    # materialized view (which never appears in ALL_VIEWS itself -- its
    # container table appears in ALL_TABLES instead, which is why the
    # introspector moves it out of schema.tables and into schema.views as
    # one of these rather than leaving it as a plain Table; see
    # introspector.py and ddl_generator.generate_view_ddl's materialized
    # branch). Always False/None for a non-Oracle source or an ordinary view.
    is_materialized: bool = False
    mview_build_mode: Optional[str] = None      # Oracle's BUILD_MODE: "IMMEDIATE" | "DEFERRED"
    mview_refresh_mode: Optional[str] = None    # Oracle's REFRESH_MODE: "DEMAND" | "COMMIT"
    mview_refresh_method: Optional[str] = None  # Oracle's REFRESH_METHOD: "FORCE" | "FAST" | "COMPLETE"

    status: ConversionStatus = ConversionStatus.AUTOMATIC
    issues: List[ConversionIssue] = field(default_factory=list)
    converted_definition: Optional[str] = None


@dataclass
class RoutineParameter:
    """One parameter of a stored procedure or function.

    Oracle, SQL Server and Db2 keep the whole `CREATE ...` header in their
    catalogs, so a converter can re-read the signature from
    `Routine.source`. MySQL and PostgreSQL do not -- their
    `information_schema.routines.routine_definition` is the *body alone* --
    so a routine from either of them arrived with no parameter list at all,
    and nothing downstream could rebuild one. Read separately and kept
    here instead.
    """
    name: str
    data_type: str
    mode: str = "IN"                       # "IN" | "OUT" | "INOUT"
    default: Optional[str] = None


@dataclass
class Routine:
    """Stored procedure, function, package (spec/body), or trigger."""
    name: str
    schema: str
    kind: str  # "PROCEDURE" | "FUNCTION" | "PACKAGE" | "PACKAGE BODY" | "TRIGGER"
    source: str

    # Which engine's native syntax `source` is actually written in --
    # populated by whichever introspector built this Routine (defaults to
    # "Oracle" for the common case and for backward compatibility with
    # every caller that constructs a Routine directly without passing
    # this). plsql_converter.convert_routine checks this first and, for
    # anything other than "Oracle", skips straight to a manual-conversion
    # flag rather than running Oracle-PL/SQL-specific regexes against
    # syntax they were never written to understand (MySQL/PostgreSQL/SQL
    # Server routine bodies look enough like PL/SQL in places that some of
    # those regexes could "succeed" by accident and silently produce wrong
    # SQL instead of safely flagging the routine for manual review).
    source_engine: str = "Oracle"

    # Signature, for the sources whose catalog does not put it in `source`.
    # Empty on Oracle/SQL Server/Db2, where the header is part of the text.
    parameters: List["RoutineParameter"] = field(default_factory=list)
    return_type: Optional[str] = None

    # trigger-only metadata, populated by the introspector from ALL_TRIGGERS
    table_name: Optional[str] = None
    timing: Optional[str] = None          # "BEFORE" | "AFTER" | "INSTEAD OF"
    #: Every event this ONE trigger fires for. MySQL, PostgreSQL and SQL
    #: Server all list a multi-event trigger once per event in their
    #: catalogs; the introspectors collapse those rows into a single
    #: Routine rather than passing duplicates downstream, where they used
    #: to produce N identical DDL blocks, N rollback DROPs, and a schema
    #: tree with N indistinguishable rows sharing one checkbox.
    events: List[str] = field(default_factory=list)  # ["INSERT", "UPDATE", "DELETE"]
    row_level: bool = True                # True = FOR EACH ROW, False = statement-level
    #: For a PostgreSQL trigger, the function it executes. PostgreSQL keeps
    #: a trigger's logic in a separate function and
    #: `information_schema.triggers.action_statement` is only
    #: "EXECUTE FUNCTION foo()", so the body has to be fetched from
    #: pg_proc and carried here or the trigger migrates as an empty shell.
    trigger_function: Optional[str] = None

    status: ConversionStatus = ConversionStatus.MANUAL
    complexity_score: int = 0
    issues: List[ConversionIssue] = field(default_factory=list)
    converted_source: Optional[str] = None


@dataclass
class Schema:
    """Full extracted schema plus conversion results, for one Oracle schema/user."""
    name: str
    source_engine: str = "Oracle"
    target_engine: str = "PostgreSQL"

    tables: List[Table] = field(default_factory=list)
    views: List[View] = field(default_factory=list)
    sequences: List[Sequence] = field(default_factory=list)
    routines: List[Routine] = field(default_factory=list)

    def all_objects(self):
        return [*self.tables, *self.views, *self.sequences, *self.routines]
