# Teleglobal Database Migration Tool

A desktop application, in the spirit of AWS Schema Conversion Tool (SCT),
built for Teleglobal to assess and convert database schemas between
Oracle, MySQL, PostgreSQL, SQL Server, Db2 (LUW), and MongoDB — every one
of the six can now be either the source or the target — and to migrate
table data across. Excel workbooks and CSV files can additionally be used
as a *source*, for loading spreadsheet-bound data into a real database
(see "Excel/CSV as a source" below).

Three engines are the odd ones out: MongoDB, on both sides (as a target
it's a relational-to-document conversion instead of SQL-to-SQL, and as a
source it has no fixed schema at all, so its "schema" is inferred by
sampling real documents rather than read from a catalog — see "MongoDB as
a source" below); Excel/CSV, which is source-only and also inference-based
(a spreadsheet isn't a database and this tool doesn't pretend it is — see
below); and Oracle as a *target*, where "conversion" is close to a no-op
rather than a real cross-dialect translation, since this tool's whole
internal type/DDL representation already *is* Oracle syntax — see
"Converts a relational schema back to Oracle" below.

AWS SCT itself is closed-source (distributed only as an installer), so
this was built from scratch based on its documented feature set — connect
to a source database, introspect the schema, convert DDL and stored code
to the target dialect, produce a migration assessment report scoring what
converted automatically vs. what needs manual work, then optionally apply
the DDL and migrate data.

## What it does

- **Connects** to an Oracle, MySQL, PostgreSQL, SQL Server, Db2 (LUW),
  MongoDB, or Excel/CSV source and an Oracle, PostgreSQL, MySQL, SQL
  Server, Db2 (LUW), or MongoDB target — pick the source engine from a
  dropdown next to *Connect Source*.
- **Introspects** the source schema: tables, columns, primary/foreign
  keys, unique/check constraints, indexes, sequences, views, and stored
  procedures/functions/packages/triggers.
- **MySQL as a source** reverse-maps MySQL's native column types (including
  `UNSIGNED`/`ZEROFILL`, `ENUM`, `SET`, `JSON`, `BIT`, spatial types) into
  the same internal Oracle-flavored representation the rest of the tool
  already speaks, so every existing type-mapping and DDL-generation code
  path works unchanged regardless of source engine. MySQL's per-table
  `PRIMARY` constraint/index name is disambiguated to `<table>_PK` so
  same-named constraints from different tables never collide. Stored
  procedures/functions/triggers are read but always flagged for manual
  conversion (see below) — only schema/table/view introspection is
  automatic for a MySQL source.
- **PostgreSQL as a source** does the same reverse-mapping trick for
  PostgreSQL's own native types (`NUMERIC`/`VARCHAR`/`TEXT`/`UUID`/`JSON`/
  `JSONB`/`INET`/`CIDR`/`MACADDR`/`INTERVAL`/`MONEY`/`BIT`/array types,
  and both flavors of `TIMESTAMP`/`TIME` with and without a time zone) into
  the same shared pivot representation, so it costs nothing extra on the
  target-DDL side either. Unlike MySQL, PostgreSQL constraint and index
  names are already unique within a schema, so no renaming is needed
  there; native `SEQUENCE` objects (and `SERIAL`/`GENERATED ... AS
  IDENTITY` columns, represented as `Column.identity` the same way an
  Oracle `IDENTITY` column is) are introspected too. Same scope limit as
  MySQL: stored procedures/functions/triggers are read but always flagged
  for manual conversion, only schema/table/view/sequence introspection is
  automatic.
- **SQL Server as a source** does the same for SQL Server's own native
  types (`UNIQUEIDENTIFIER`, `MONEY`/`SMALLMONEY`, `XML`, `SQL_VARIANT`,
  `HIERARCHYID`, `GEOMETRY`/`GEOGRAPHY`, `ROWVERSION`, and both
  `DATETIME2`/`DATETIMEOFFSET`), read via `sys.columns`/`sys.types` since
  SQL Server's `information_schema` doesn't expose everything needed
  (identity flag, exact byte-vs-character length for `NCHAR`/`NVARCHAR`).
  Constraint/index names are unique within a schema (same as PostgreSQL,
  no MySQL-style renaming needed), native `SEQUENCE` objects and
  `IDENTITY` columns are introspected, and triggers — which SQL Server
  exposes only via `sys.triggers`/`sys.trigger_events`/`sys.sql_modules`,
  not `information_schema.triggers` — correctly distinguish `AFTER` from
  `INSTEAD OF` (SQL Server has no true `BEFORE` trigger). Same scope limit
  as MySQL/PostgreSQL: stored procedures/functions/triggers are read but
  always flagged for manual conversion.
- **Db2 (LUW) as a source** reads `SYSCAT.*` catalog views (Db2 has no ISO
  `information_schema` at all) and reverse-maps Db2's own native types
  (`GRAPHIC`/`VARGRAPHIC`/`DBCLOB` double-byte character types,
  `DECFLOAT`, `ROWID` — mapped straight onto the pivot's own Oracle
  `ROWID` type, since both are equally non-portable opaque row
  identifiers) into the shared pivot. Constraint/index names are unique
  within a schema (same as PostgreSQL/SQL Server), and `SYSCAT.REFERENCES`
  gives a foreign key's referenced table name directly, unlike the
  standard-SQL `referential_constraints` join PostgreSQL/SQL Server need.
  Triggers are, unusually, *simpler* here than for the other three source
  engines: a Db2 trigger fires on exactly one event (never a combined
  `INSERT OR UPDATE`), so there's no "one row per event" fan-out to
  handle, and Db2 genuinely supports `BEFORE` triggers (unlike SQL
  Server). Same scope limit as the others: stored procedures/functions/
  triggers are read but always flagged for manual conversion.
- **MongoDB as a source** works completely differently from the other
  five, because MongoDB has no fixed schema and no catalog view to read
  one from — a collection's "columns" are *inferred* by sampling up to
  1,000 documents per collection (via MongoDB's own server-side `$sample`
  aggregation stage, not a full collection scan):
  - A nested sub-*object* (an embedded document, not inside an array)
    flattens into dotted-prefix columns on the same table (`address.city`,
    `address.geo.lat`). An *array* field (of sub-documents or of plain
    scalars) has no equivalent on the same row at all, so it's normalized
    into a synthesized **child table** instead: a surrogate integer
    primary key, a foreign key back to the parent's `_id`, and either one
    column per item key (array of sub-documents) or a single `value`
    column (array of scalars). An array nested inside another array's
    items (array-of-arrays, or an array inside a child table's own rows)
    is flagged for manual review and dropped rather than chased into a
    second generation of grandchild tables.
  - `_id` is always the table's `PRIMARY KEY`, regardless of its inferred
    type — MongoDB guarantees a unique index on it universally, unlike
    every other field. Where a field's sampled values disagree on type (or
    on object/array/scalar shape), the most common option wins and a
    warning flags it for manual review, the same "convert what's safe,
    flag the rest" pattern used everywhere else in this tool.
  - Indexes are read via `list_indexes()` (skipping the redundant default
    `_id_` index); one that references a field normalized away into a
    child table can't be represented as a single-table index, so it's
    skipped with a warning instead of emitting a broken column reference.
    Views are introspected too, but a MongoDB view's "definition" is a
    JSON aggregation pipeline, not SQL — see "Converts a relational schema
    to a MongoDB document model" below for how that's handled on
    conversion. Sequences and stored procedures/functions/triggers are
    always empty; MongoDB has no native equivalent of either.
  - Data migration for a synthesized child table can't be expressed as the
    `SELECT cols FROM table` string every other source builds (there's no
    real collection backing a child table) — MongoDB's connector instead
    reads real documents off the parent collection directly and unwinds
    the relevant array field per row.
- **Excel/CSV as a source** loads local `.xlsx`/`.xlsm`/`.csv`/`.tsv`
  files into any of the six target engines. Pick *Excel/CSV* as the source
  engine and *Connect Source* becomes a file picker instead of a
  host/port/credentials form.

  **Several files at once.** The picker is multi-select and accepts up to
  **50 files** in one migration; every sheet in every file becomes a table
  in a single schema, so one Convert -> Apply DDL -> Migrate Data pass
  covers the lot instead of one run per file. The headless CLI takes the
  same thing as a `files` list in place of `database` (see "Headless /
  CI-CD usage" below). Three details worth knowing:
    - **Table names collide across files** -- two workbooks exported by
      the same system will both have a `Sheet1`, and monthly CSVs are
      often identical but for their folder. The first sheet to claim a
      name keeps it; a later one gets `<file stem>_<sheet>`, then a
      numeric suffix if even that collides. Every rename is recorded as a
      conversion issue, the same as a sanitized name. Single-file jobs are
      unaffected: names are never prefixed when there's nothing to collide
      with, so existing output doesn't change.
    - **The schema name** still defaults to the file's own stem for a
      single file. For several it defaults to `spreadsheet` rather than
      borrowing whichever file happened to be first -- name it yourself in
      the *Schema to create* box if you want something else.
    - **Duplicate paths are read once**, and the whole set is validated
      before anything is read, so a wrong file in a batch of fifty is
      reported by name up front rather than 40 minutes in.

  This is source-only — writing a converted schema back out to a
  spreadsheet isn't something this tool does — and it's worth being clear
  about what it is: a spreadsheet is not a database, so the value here is
  in the *data migration* half of the tool, not the schema-conversion
  half. The assessment report for a spreadsheet source is sparse because
  there is genuinely nothing to assess.
  - **One sheet = one table**, row 1 is the header, data starts at row 2.
    Sheet names and headers are sanitized into legal SQL identifiers
    (`Order Items (2024)` → `Order_Items_2024`), with the original name
    recorded as a conversion issue so nothing changes silently. Blank
    header cells become `column_N`; duplicate headers get a numeric
    suffix (case-insensitively, since most targets fold identifiers).
  - **Nothing relational is invented.** No primary keys, foreign keys,
    indexes, views, sequences or routines are inferred, because none of
    them exist in a worksheet. A column that looks like an ID carries no
    uniqueness guarantee whatsoever, and synthesizing a `PRIMARY KEY`
    from that guess would make *Apply DDL* fail on the target the moment
    a duplicate showed up. (This is the deliberate difference from the
    MongoDB source, which *does* synthesize a PK — MongoDB genuinely
    guarantees a unique `_id`.)
  - **Types are inferred by sampling cells** (the first 10,000 rows per
    sheet), since CSV has no types at all and Excel's are per-cell rather
    than per-column. Three rules are worth knowing:
    - Text that looks numeric becomes numeric (`"42"` → an integer
      column) — "numbers stored as text" is the single most common thing
      wrong with a real spreadsheet.
    - **...except with a leading zero.** `"01234"` and `"007"` stay text,
      because that's virtually always a zip code, account number or
      product code where the zero is meaningful data.
    - **Only unambiguous ISO-8601 dates are recognized.** `2024-03-07` is
      a date; `03/07/2024` stays text, because nothing in the file says
      whether that's March 7th or July 3rd, and a tool that silently
      picks one produces a silently wrong migration for half the world.
  - **A mixed column becomes text rather than its majority type.** A
    numeric column with a few `N/A` cells in it is text with numbers in
    it; picking the majority type would make the migration *fail* on the
    minority rows, possibly 40,000 rows into a long-running job. A pure
    numeric widening (a money column of `100.50`, `0`, `-42.75`) is
    resolved silently, without a warning — a warning that fires on nearly
    every numeric column is a warning nobody reads.
  - **Every column is nullable**, even one with no blanks anywhere in the
    sample. There's no declared `NOT NULL` here to preserve, only a guess
    from a sample, and that guess fails in the worst possible way. Add
    `NOT NULL` on the target afterwards, where a violation is an
    immediate, cheap error instead of a lost migration run.
  - Integer columns are sized to `NUMBER(9)`/`NUMBER(18)`, the two
    precisions that map to a native `INTEGER`/`BIGINT` on every target.
  - Data migration gets the same reliability features as any other
    source — checkpoint/resume, retry, post-migration row-count and
    checksum validation — since it goes through the same migrator.
  - `.xlsx`/`.xlsm` needs `openpyxl` (in `requirements.txt`); `.csv`/
    `.tsv` needs nothing beyond the standard library. Legacy `.xls`
    files aren't readable — the error says so and tells you to re-save
    as `.xlsx`. Merged cells, multi-row headers and leading junk rows
    are deliberately *not* handled: detecting them heuristically fails
    silently and confusingly when it guesses wrong, and the honest fix
    is 30 seconds of cleanup in Excel first.
- **Converts** data types (e.g. `NUMBER(10,2)` → `NUMERIC(10,2)` /
  `DECIMAL(10,2)`, `VARCHAR2` → `VARCHAR`, `CLOB` → `TEXT`/`LONGTEXT`,
  `DATE` → `TIMESTAMP`/`DATETIME`, etc.) and generates target DDL for
  tables, constraints, indexes, sequences, and views.
- **Converts a relational schema back to Oracle**, for an Oracle target —
  the one target where "conversion" isn't really a cross-dialect
  translation at all, since this tool's whole internal type/DDL
  representation *already is* Oracle's own native syntax (`VARCHAR2(100)`,
  `NUMBER(10,2)`, `CLOB`, `TIMESTAMP(6) WITH TIME ZONE`, ...) — that's the
  entire reason it was chosen as the shared pivot representation every
  other target's own forward-mapper translates *out of*. Useful for a
  lateral migration between two Oracle databases (e.g. consolidating
  schemas, standing up a lower environment) or a reverse migration back
  onto Oracle from any other supported source:
  - Column types pass straight through unchanged (`to_oracle()` mostly
    just validates and re-normalizes whitespace/casing) — the one
    exception is `BOOLEAN` (only reachable via a non-Oracle source's own
    reverse type mapping, e.g. MySQL's `TINYINT(1)` or MongoDB's `bool`),
    which gets an informational note that native `BOOLEAN` table columns
    require Oracle Database 23c or later.
  - Oracle has no `CREATE ... IF NOT EXISTS` for any object type (tables,
    sequences, indexes, or a plain `CREATE VIEW` without `OR REPLACE`),
    unlike Postgres/MySQL — idempotent re-runs are instead handled the
    standard Oracle way: `EXECUTE IMMEDIATE` wrapped in a PL/SQL block
    that catches exactly the "already exists"-flavored `ORA-00955`/
    `ORA-02264`/`ORA-02275` error(s) for the object kind in question.
  - Sequences are native, with no numeric-range clamping concern at all
    (unlike the bigint-backed sequences Postgres/SQL Server/Db2 above
    have to clamp against) — Oracle sequences support up to 28-digit
    precision, so even the huge default `MAXVALUE` an Oracle source's own
    unbounded sequences get passes straight through.
  - Stored procedures/functions/packages/triggers need no rule-based
    syntax rewriting for a genuinely Oracle-sourced routine (the
    non-Oracle-source guard below still applies in full — a MySQL/
    PostgreSQL/SQL Server/Db2/MongoDB-sourced routine is still always
    flagged manual, exactly as for every other target, since this tool
    has no dialect-to-dialect PL/SQL translator). What *is* needed is
    reconstructing the "`CREATE [OR REPLACE] ...`" header Oracle's own
    catalog never stores in the first place: `ALL_SOURCE` (procedures/
    functions/packages) omits the leading `CREATE [OR REPLACE]` keyword,
    and `ALL_TRIGGERS.TRIGGER_BODY` omits the *entire* `CREATE TRIGGER
    ... {BEFORE|AFTER} {event} ON table [FOR EACH ROW]` header, starting
    straight at `DECLARE`/`BEGIN` — both are rebuilt from the Routine's
    own metadata (the same table/timing/events/row_level fields every
    other target's own trigger converter already reads).
  - Views work the same way, keyed off the same `View.source_engine`
    field MongoDB-as-a-source introduced: a genuinely Oracle-sourced
    view's SQL text (including Oracle-only constructs like `CONNECT BY`,
    `ROWNUM`, `(+)`, `DECODE()`, that every *other* target's own
    Oracle-marker scan would flag for rewrite) passes straight through
    unrewritten, since it's already exactly what an Oracle target needs;
    a non-Oracle-sourced view is flagged manual instead of being guessed
    at, the same reasoning as routines above.
  - No `CREATE SCHEMA` statement is ever emitted for an Oracle target,
    unlike Postgres/SQL Server/Db2 above — Oracle has no such statement (a
    "schema" *is* a user account, created via `CREATE USER` plus separate
    grant/quota administration this tool has no business performing on
    someone's behalf) — a given target schema is only ever used to
    schema-qualify each generated object reference, and must already
    exist as a real user for those references to resolve.
- **Converts PL/SQL to PL/pgSQL** for a PostgreSQL target — this is real
  rule-based syntax translation, not just flagging:
  - Procedure/function signatures, parameter modes (`IN`/`OUT`/`IN OUT` →
    `IN`/`OUT`/`INOUT`), `RETURN` types, and variable/constant declarations
    (types run through the same Oracle → PostgreSQL type mapping used for
    tables; `%TYPE`/`%ROWTYPE` are left as-is since PostgreSQL supports
    them natively).
  - Builtins: `NVL` → `COALESCE`, `NVL2` → `CASE`, `DECODE` → `CASE` (with
    a warning about DECODE's NULL-matching semantics), `INSTR` (2-arg) →
    `POSITION`, `DBMS_OUTPUT.PUT_LINE` → `RAISE NOTICE`, `SYSDATE`/
    `SYSTIMESTAMP` → `CURRENT_TIMESTAMP`, `seq.NEXTVAL`/`.CURRVAL` →
    `nextval()`/`currval()`, `EXECUTE IMMEDIATE` → `EXECUTE`,
    `RAISE_APPLICATION_ERROR` → `RAISE EXCEPTION`, `FROM DUAL` removed,
    Oracle cursor syntax (`CURSOR c IS SELECT...`) → PostgreSQL's
    (`c CURSOR FOR SELECT...`).
  - Named exceptions: `DUP_VAL_ON_INDEX`/`ZERO_DIVIDE` are remapped to
    PostgreSQL's `unique_violation`/`division_by_zero`; Oracle
    user-defined `EXCEPTION` declarations and exceptions with no
    PostgreSQL equivalent (`VALUE_ERROR`, etc.) are flagged for manual
    rewrite rather than guessed at.
  - **Triggers** are split into a `RETURNS TRIGGER` function (with
    `:NEW`/`:OLD` → `NEW`/`OLD`, and a `RETURN NEW/OLD;` added if the
    original body didn't have one) plus a matching `CREATE TRIGGER`,
    using the timing/event/row-level metadata read from Oracle.
  - **Packages** have no PostgreSQL equivalent, so a `PACKAGE BODY` is
    best-effort flattened into standalone `pkgname_procname` functions;
    the spec is left as an explanatory comment.
  - A nested `PROCEDURE`/`FUNCTION` declared inside another routine's own
    `DECLARE` section (or a local `TYPE ... IS TABLE OF`/`RECORD`/`REF
    CURSOR` declaration) is detected as one intact unit — via
    `tgdatabridge/core/nested_subprogram.py`, shared by all three routine
    converters — and cut out whole into a **MANUAL CONVERSION REQUIRED**
    comment block with the original source preserved, rather than being
    shredded statement-by-statement by the ordinary declaration parser
    (which used to silently corrupt the routine into garbage output).
  - `BULK COLLECT` gets a real rewrite for its single most common shape —
    `SELECT cols BULK COLLECT INTO vars FROM ...` (one Oracle collection
    per selected column) becomes `SELECT array_agg(c1), array_agg(c2) ...
    INTO vars FROM ...`. A `SELECT *` form, or a column/variable count
    mismatch, is left unrewritten and flagged with the specific reason
    instead of guessed at. `FORALL` and a cursor's `FETCH ... BULK COLLECT
    INTO` still have no safe generic rewrite (no target-table schema
    access to build one from) but now get a diagnostic naming the specific
    DML statement (`FORALL ... INSERT/UPDATE/DELETE/MERGE`) or construct
    (`FETCH ... BULK COLLECT INTO`) instead of one generic marker for both.
  - `DBMS_LOB.GETLENGTH`/`DBMS_LOB.SUBSTR` (argument order swapped to match
    each target's own substring function) and `DBMS_RANDOM.VALUE`'s no-arg
    form are now mapped to a real target equivalent (`LENGTH`/`SUBSTR`/
    `random()` for Postgres; `LEN` — with a trailing-space caveat noted —
    /`SUBSTRING`/`RAND()` for SQL Server; `LENGTH`/`SUBSTR`/`RAND()` for
    Db2) across all three targets; the 2-argument ranged form of
    `DBMS_RANDOM.VALUE` is out of scope and still flagged.
  - Everything else with no safe mechanical translation — `DBMS_*`/`UTL_*`
    packages beyond the ones above, autonomous transactions — is left
    in place and flagged rather than mistranslated, the same "convert
    what's safe, flag the rest" approach used for tables. MySQL
    stored-procedure syntax is different enough that MySQL-target
    routines are still flag-only (see below). The same is true in the
    other direction: a routine read from a MySQL, PostgreSQL, SQL
    Server, or Db2 *source* is always flagged for manual conversion too,
    regardless of target — the Oracle-PL/SQL-specific converters below
    are never run against non-Oracle source syntax, since some of their
    patterns are generic enough to "succeed" by accident and silently
    produce wrong SQL.
  - `CONNECT BY` hierarchical queries get one further step than a flag:
    the common "single table, single `PRIOR` equality" shape (`SELECT
    ... FROM t START WITH ... CONNECT BY [NOCYCLE] PRIOR a = b`,
    optionally with a `LEVEL` column and a trailing `ORDER BY`) is
    automatically rewritten into an equivalent recursive CTE —
    `WITH RECURSIVE` for PostgreSQL/MySQL, plain `WITH` for SQL Server/Db2
    (both infer the recursion themselves). This runs wherever such a
    query can appear — a view definition, a cursor declaration, a
    row-FOR-loop header, a plain `SELECT ... INTO` — before anything else
    looks at that SQL. Anything outside that shape (an extra `WHERE`,
    multiple/joined source tables, `ORDER SIBLINGS BY`,
    `CONNECT_BY_ROOT`/`SYS_CONNECT_BY_PATH`, more than one join condition)
    is left completely untouched and still flagged for manual rewrite,
    exactly as before. See `tgdatabridge/core/connect_by_rewriter.py`.
- **Converts PL/SQL to T-SQL** for a SQL Server target — a real,
  structural rewrite (not just flagging), since T-SQL differs from
  PL/pgSQL enough that this needed its own converter rather than a
  reuse of the PostgreSQL one:
  - `IF...THEN...ELSIF...END IF` → `IF...BEGIN...END ELSE IF...BEGIN...END`
    (T-SQL has no `THEN`/`END IF`), `LOOP`/`WHILE...LOOP` → `WHILE`,
    numeric-range `FOR i IN [REVERSE] a..b LOOP` → an explicit counter +
    `WHILE` loop, `EXIT`/`EXIT WHEN`/`CONTINUE`/`CONTINUE WHEN` →
    `BREAK`/`IF ... BREAK`/`CONTINUE`/`IF ... CONTINUE`. A genuine CASE
    *statement* (`CASE ... END CASE`, as opposed to a CASE *expression*)
    has no T-SQL statement equivalent and is flagged for manual rewrite
    as `IF`/`ELSE IF` rather than guessed at.
  - Implicit-cursor/row `FOR rec IN (SELECT ...) LOOP` (and named-cursor
    `FOR rec IN cursor_name LOOP`) become an explicit
    `CURSOR .../OPEN/FETCH NEXT/WHILE @@FETCH_STATUS/CLOSE/DEALLOCATE`
    loop — only when the SELECT's column list is simple and explicit
    enough to name every fetched value (a `SELECT *` or an unaliased
    expression column is flagged **MANUAL CONVERSION REQUIRED** instead
    of guessed at).
  - Local variables/parameters are referenced bare in Oracle but must be
    `@`-prefixed in T-SQL — every declared name's references throughout
    the body are rewritten, along with Oracle's `:=` assignment operator
    (which isn't valid T-SQL at all) becoming a `SET @var = ...;`
    statement, and `SELECT col INTO var FROM ...` becoming
    `SELECT @var = col FROM ...` (plain `SELECT ... INTO` means something
    entirely different in T-SQL — creating a new table from the result).
  - Oracle's native `EXCEPTION WHEN ... THEN` has no T-SQL equivalent, so
    it's restructured into `BEGIN TRY ... END TRY BEGIN CATCH IF
    ERROR_NUMBER() = ... ... END CATCH`, with `DUP_VAL_ON_INDEX`/
    `ZERO_DIVIDE` mapped to their real SQL Server error numbers (2627/
    8134) and an unmatched `OTHERS`-less CATCH block re-throwing rather
    than silently swallowing the error.
  - Builtins: `NVL` → `ISNULL`, `DECODE`/`NVL2` → `CASE`, `INSTR` →
    `CHARINDEX` (with the argument order swapped to match), `SYSDATE`/
    `SYSTIMESTAMP` → `GETDATE()`/`SYSDATETIME()`, `DBMS_OUTPUT.PUT_LINE`
    → `PRINT`, `seq.NEXTVAL` → `NEXT VALUE FOR seq` (`seq.CURRVAL` has no
    T-SQL equivalent and is flagged), `RAISE_APPLICATION_ERROR` →
    `THROW 50000, ...`, `EXECUTE IMMEDIATE` → `EXEC`, `DBMS_LOB.GETLENGTH`/
    `DBMS_LOB.SUBSTR` → `LEN`/`SUBSTRING` (argument order swapped to
    match; `LEN`'s trailing-space-trimming difference from
    `DBMS_LOB.GETLENGTH` is noted as an issue), `DBMS_RANDOM.VALUE`
    (no-arg form) → `RAND()`. A nested `PROCEDURE`/`FUNCTION` in a
    routine's own `DECLARE` section is detected and flagged as one intact
    unit rather than silently corrupted — see the shared
    `nested_subprogram.py` note under the PostgreSQL converter above,
    which applies here too.
  - `RAISE exc_name;` (a locally-declared exception) is converted to
    `THROW 50000, 'exc_name', 1;` wherever it appears — in the main
    try-body, not just inside a `WHEN...THEN` handler's own statements —
    since Oracle allows raising a local exception conditionally in the
    main body and catching it in that same block's own `EXCEPTION`
    section, a completely ordinary pattern.
  - **Triggers**: SQL Server has no `BEFORE` trigger (only `AFTER`/
    `INSTEAD OF`), so a `BEFORE` trigger is flagged for manual rewrite
    rather than silently substituted with `INSTEAD OF` (which replaces
    the triggering statement entirely — not a safe drop-in). Oracle's
    row-level `:NEW`/`:OLD` are rewritten to single-row lookups against
    T-SQL's statement-level `inserted`/`deleted` pseudo-tables, with an
    explicit warning that this assumes single-row DML.
  - **Packages** are flattened the same way as the PostgreSQL converter
    (standalone `pkgname_procname` procedures/functions, spec left as a
    comment). T-SQL scalar functions can't contain `PRINT`/`THROW`/DML or
    `OUTPUT` parameters — both are flagged rather than silently emitted
    as invalid T-SQL. `CREATE OR ALTER` (SQL Server 2016 SP1+) is used
    throughout for idempotent procedure/function/trigger/view creation.
- **Converts PL/SQL to Db2 (LUW) SQL PL** for a Db2 target — Db2's SQL PL
  is structurally much closer to Oracle PL/SQL than T-SQL is (native
  `IF/THEN/ELSIF/END IF`, bare `LOOP`, and `SELECT ... INTO` all carry over
  almost verbatim, and local variables are referenced bare with no `@`-style
  sigil needed), so this converter does less rewriting than the T-SQL one in
  several respects — but Db2 has its own structural rules with no Oracle
  equivalent:
  - Every `DECLARE` (variables, cursors, condition handlers) in a Db2
    compound statement must appear *before* any executable statement in
    that block. Oracle's numeric-range `FOR i IN [REVERSE] a..b LOOP` has
    no Db2 equivalent and is rewritten to a `WHILE` loop with an explicit
    counter, whose `DECLARE` is hoisted to the top of the routine (Db2, un-
    like T-SQL, forbids declaring it inline at the loop's own location).
  - Oracle's `EXCEPTION WHEN ... THEN` has no block-scoped equivalent in
    Db2 — it's restructured into one or more
    `DECLARE EXIT HANDLER FOR <condition-list> ...;` statements hoisted to
    the declare section of whichever `BEGIN...END` block the `EXCEPTION`
    section was attached to (recursively, for a nested anonymous block with
    its own local handlers), with `DUP_VAL_ON_INDEX`/`ZERO_DIVIDE`/
    `TOO_MANY_ROWS`/`INVALID_NUMBER` mapped to their real Db2 SQLSTATEs,
    `NO_DATA_FOUND` mapped to Db2's `NOT FOUND` condition, and `OTHERS` to
    `SQLEXCEPTION`.
  - Db2 has no unlabeled `EXIT`/`CONTINUE` — `LEAVE`/`ITERATE` always
    require a label, and every loop that uses them must itself be labeled.
    A label is synthesized for every loop converted, and
    `EXIT`/`EXIT WHEN`/`CONTINUE`/`CONTINUE WHEN` are rewritten against it
    only after that loop's own body has already been recursively converted
    (so a leftover bare `EXIT`/`CONTINUE` always belongs to *that* loop, not
    an inner one that already consumed its own).
  - `WHILE ... LOOP` → `WHILE ... DO ... END WHILE`. Implicit-cursor/row
    `FOR rec IN (SELECT ...) LOOP` (and named-cursor `FOR rec IN
    cursor_name LOOP`) become Db2's *native*
    `FOR rec AS cursor CURSOR FOR select DO ... END FOR;` construct — unlike
    the T-SQL converter, this needs no column-list extraction or `rec.col`
    rewriting at all, since Db2 keeps row-variable dot access working
    automatically, the same as Oracle.
  - Builtins: `NVL` → `COALESCE`, `DECODE`/`NVL2` → `CASE`, `INSTR` →
    `LOCATE` (argument order swapped to match), `SYSDATE`/`SYSTIMESTAMP` →
    `CURRENT TIMESTAMP`, `FROM DUAL` → `FROM SYSIBM.SYSDUMMY1`,
    `seq.NEXTVAL`/`.CURRVAL` → `NEXT VALUE FOR seq`/`PREVIOUS VALUE FOR seq`
    (Db2, unlike SQL Server, supports both directions natively),
    `RAISE_APPLICATION_ERROR` → `SIGNAL SQLSTATE '70000' SET MESSAGE_TEXT =
    ...`, bare `RAISE;` → `RESIGNAL;` (a true re-signal of the currently-
    handled condition), `RAISE exc_name;` (a locally-declared exception,
    wherever it appears in the routine, not just inside a handler) →
    `SIGNAL SQLSTATE '70000' SET MESSAGE_TEXT = 'exc_name';`,
    `DBMS_LOB.GETLENGTH`/`DBMS_LOB.SUBSTR` → `LENGTH`/`SUBSTR` (argument
    order swapped to match), `DBMS_RANDOM.VALUE` (no-arg form) → `RAND()`
    — `DBMS_OUTPUT.PUT_LINE` remains flag-only, Db2 SQL PL has no
    console-output statement to substitute it with. `SELECT ... INTO ...
    FROM` and `EXECUTE IMMEDIATE` are both native, identical-syntax Db2
    constructs and need no rewriting at all. A genuine CASE *statement* is
    left completely untouched — Db2, unlike T-SQL, supports it natively. A
    nested `PROCEDURE`/`FUNCTION` in a routine's own `DECLARE` section is
    detected and flagged as one intact unit rather than silently
    corrupted — see the shared `nested_subprogram.py` note under the
    PostgreSQL converter above, which applies here too.
  - **Triggers**: Db2 supports `BEFORE` (as `NO CASCADE BEFORE`), `AFTER`,
    and `INSTEAD OF` natively, including row-level `BEFORE` triggers that
    can modify the incoming row — unlike SQL Server, there's no need to
    flag `BEFORE` as unsupported. A `REFERENCING NEW AS NEW_ROW OLD AS
    OLD_ROW` clause is emitted and Oracle's `:NEW.col`/`:OLD.col` rewritten
    to `NEW_ROW.col`/`OLD_ROW.col`. Db2 has no equivalent to firing one
    trigger on multiple combined events (`INSERT OR UPDATE OR DELETE`), so
    a multi-event Oracle trigger is split into one `CREATE TRIGGER` per
    event, each named `triggername_EVENT`.
  - **Packages** are flattened the same way as the other converters
    (standalone `pkgname_procname` procedures/functions, spec left as a
    comment). `CREATE OR REPLACE` (native in Db2 for procedures, functions,
    triggers, and views alike) is used throughout for idempotent creation.
- **Converts a relational schema to a MongoDB document model** for a
  MongoDB target — a fundamentally different kind of conversion than the
  other four (SQL-to-SQL) targets, since MongoDB has no SQL dialect, no
  foreign-key enforcement, and no server-side stored-procedure concept at
  all:
  - Every table becomes a `db.createCollection(name, {...})` call with a
    `$jsonSchema` validator: column types map to BSON types (`bsonType`),
    `NOT NULL` columns become `required`, and the two most common single-
    column `CHECK` shapes — a numeric comparison (`salary > 0`) and an
    `IN` list (`status IN ('ACTIVE','INACTIVE')`) — are automatically
    translated into `minimum`/`maximum`/`exclusiveMinimum`/
    `exclusiveMaximum`/`enum` rules; anything more complex is flagged for
    manual enforcement instead of guessed at, the same "convert what's
    safe, flag the rest" approach used everywhere else in this tool.
  - `PRIMARY KEY`/`UNIQUE` constraints and secondary indexes become real
    `db[collection].createIndex(...)` calls (the two pieces of a
    relational schema MongoDB can actually enforce server-side).
    `FOREIGN KEY` has no MongoDB equivalent at all — no `ALTER TABLE ADD
    CONSTRAINT` exists to emit — so it's rendered as a documentation-only
    `-- NOTE: ...` comment plus a warning, and the relationship must be
    maintained by application code (or a `$lookup`-based check) instead.
  - Oracle `SEQUENCE` objects (MongoDB has no native equivalent, same gap
    as MySQL) are emulated as one document per sequence inside a single
    shared `counters` collection, upserted via `updateOne(...,
    {$setOnInsert: {seq: start_value}}, {upsert: true})`.
  - **Views and stored procedures/functions/triggers are always flagged
    for manual conversion, unconditionally** — even a genuinely
    Oracle-sourced routine that every other target *would* run through
    its real converter. There's no partial-automation path here: a SQL
    `SELECT` has no mechanical translation into a MongoDB aggregation
    pipeline, and there's no server-side procedural-code concept in
    MongoDB for a translated routine body to even target (the original
    source is preserved verbatim in a comment for manual porting to
    application-layer code, or an Atlas Trigger/change stream listener
    for a trigger). The same guard also fires in the *other* direction: a
    view read from a MongoDB *source* has a JSON aggregation pipeline as
    its "definition", not SQL, so it's unconditionally flagged manual
    regardless of target too — see "MongoDB as a source" above — rather
    than running SQL-text heuristics against text that was never SQL.
  - The generated "DDL" is a small, self-controlled subset of
    mongosh-flavored JavaScript (`db.createCollection(...)`,
    `db["collection"].createIndex(...)`, `db["collection"].updateOne(...)`)
    built entirely from `json.dumps(...)` rather than hand-formatted
    strings, so `mongo_connector.py` can parse it back out deterministically
    and re-issue each statement as a real PyMongo call — see that file's
    module docstring for the exact grammar it accepts (and deliberately
    rejects anything else, rather than attempting a general JavaScript
    interpreter). Column/index identifier names are never quoted with the
    double-quote/backtick/bracket conventions the four SQL targets use,
    since MongoDB field/collection names are just JSON string values.
- **Generates a migration assessment report** (HTML) summarizing object
  counts, percent converted automatically, an estimated manual-effort
  figure, and a punch list of action items — one row per object that
  needs review.
- **Applies** the generated DDL to the target database.
- **Migrates data** table-by-table in batches (streaming fetch from
  Oracle, batched insert into the target), with five reliability features
  and (see `ENTERPRISE_READINESS.md` §4) two scale features layered on
  top (see §2 for the reliability features' own motivation, and
  `tgdatabridge/core/migrator.py`/`validation.py`/`retry.py`/`tgdatabridge/db/pool.py`
  for the implementation):
  - **Post-migration validation** — once a table's rows are streamed to
    the target, an independent re-query against the target (a row count,
    plus an order-independent checksum for tables at or under 50,000 rows)
    catches a target driver that silently drops or truncates rows on
    write, something the "rows sent" count alone can't catch. Surfaced in
    the GUI as a distinct "validation warning" outcome, separate from an
    outright failed table.
  - **Checkpoint/resume** — every "Migrate Data" run is checkpointed
    locally (keyed by source database + target database + schema + target
    engine, alongside connection profiles/history under
    `%APPDATA%\TeleglobalTDMT\migration_checkpoints\`). Re-running after a
    partial failure skips any table already fully copied and resumes a
    partially-copied one from its last completed batch, instead of
    starting the whole run over. The checkpoint is deleted automatically
    once a run finishes with nothing failed.
  - **Retry with backoff** — every batch written to the target is retried
    (exponential backoff, 3 attempts by default) on a transient-looking
    error (connection reset, timeout, deadlock) before the table is
    reported failed; a non-transient error (bad data, a constraint
    violation) still fails immediately. See `tgdatabridge/core/retry.py`'s
    docstring for the correctness trade-off this implies under an
    autocommit target.
  - **Dry-run / plan mode** ("4a. Dry Run (Plan)") — checks source row
    counts and target table existence/row counts without writing any
    data, so a migration can be sanity-checked (including against a live
    production source) before "4b. Migrate Data" actually runs.
  - **Auto-generated rollback script** ("Rollback Script" button, and a
    "Rollback Script" tab alongside the Generated DDL) — a DROP script
    that undoes the converted schema's DDL, in reverse dependency order,
    with the same idempotent-DDL guards the forward DDL uses per engine.
  - **Parallel table migration** — a "Parallel workers" spinbox next to the
    "4b. Migrate Data" button (1, today's original single-threaded
    behavior, by default) migrates several tables at once within each
    FK-dependency "wave" (see `order_tables_by_dependency_waves` below), so
    a child table still never starts before every table its own foreign
    keys reference has finished, no matter how high this is set.
  - **LOB-aware batch sizing and streaming** — a table with a CLOB/NCLOB/
    BLOB/LONG/LONG RAW column automatically uses a smaller row batch size,
    and every LOB value fetched from Oracle is read in bounded chunks
    (`OracleConnector.fetch_batches`) instead of one unbounded read — see
    `ENTERPRISE_READINESS.md` §4 for both the correctness bug this fixed
    (Oracle LOB locator objects were never being converted to plain str/
    bytes before being handed to another engine's insert) and what's still
    open (true zero-buffering server-to-server LOB streaming).
- **Remembers connection profiles and past runs**, entirely locally
  (`%APPDATA%\TeleglobalTDMT\` on Windows, `~/.teleglobal_tdmt/` elsewhere —
  see `tgdatabridge/utils/app_storage.py`):
  - Every time a source or target connection dialog is opened, a "Saved
    connection" dropdown offers previously-used host/port/database/
    username/schema combinations for that engine, most-recently-used
    first. Selecting one fills in the rest of the form. **The password
    field is never part of a saved profile and is never written to
    disk** — it's always re-entered by hand. Saving is opt-in per-dialog
    via a "Remember this connection" checkbox (checked by default) and
    silently no-ops if the disk write fails, so it can never block an
    otherwise-successful connection.
  - Every completed "Convert Schema" run is logged (schema name, source/
    target engine and database, object count, automatic-conversion
    percentage, estimated manual hours, action-item count), with an
    auto-saved copy of that run's report and DDL so they can be reopened
    later even if you never explicitly saved them. A new "History…"
    toolbar button opens a dialog listing past runs and reopens their
    report/DDL in your OS's default viewer. Both the profile list (per
    engine) and the run history are capped (20 profiles/engine, 200 runs)
    with oldest-first eviction, so these files can't grow unbounded.
- **Logs to disk, stamps every line with who ran it, and exports
  scrapable metrics** — the log console's history now also persists as
  structured JSON lines under `%APPDATA%\TeleglobalTDMT\logs\
  tgdatabridge-YYYY-MM-DD.jsonl` (one file per day), so a failed overnight or
  unattended run can be diagnosed after the fact instead of being lost
  when the app closes — see `tgdatabridge/utils/logger.py`. Every log line and
  every "Convert Schema" history record is stamped with the OS/AD
  username that produced it (`tgdatabridge.utils.logger.current_actor()`).
  Optionally, a **Settings…** toolbar button lets you forward
  warning/error (or info-and-up) log lines to a central log sink — a
  Splunk HTTP Event Collector URL, an ELK/Logstash HTTP input, or any
  endpoint that accepts a JSON POST body — shipped fire-and-forget on a
  background thread so a slow or unreachable endpoint can never freeze
  the app (`tgdatabridge/utils/log_shipper.py`). Separately, every Load Schema /
  Convert Schema / Apply DDL / Dry Run / Migrate Data / Refresh Target
  Schema run records its duration and outcome to
  `%APPDATA%\TeleglobalTDMT\metrics\operations.jsonl`, aggregated on
  every write into a Prometheus text-exposition-format snapshot at
  `...\metrics\tgdatabridge_metrics.prom` — operation duration, run counts by
  outcome, total rows migrated, rows/sec throughput, and error rate, the
  last two broken down by source/target engine pair — point
  node_exporter's textfile collector (or any Prometheus-format scraper)
  at that file. See `tgdatabridge/utils/metrics.py`.
- **Diffs the source against the target** for iterative/incremental
  conversions: every time "Refresh Target Schema" runs (including
  automatically after "Apply DDL to Target" / "Migrate Data") while a
  source schema is loaded, a new "Schema Diff" tab reports, per category
  (tables/views/sequences/routines), what's only in the loaded source
  (not deployed yet), only on the target (created outside this tool, or
  left over from a previous/different schema), or present in both. Name
  matching is case-insensitive (each target engine folds unquoted names
  to its own fixed case), and package members / Db2's per-event-split
  triggers are matched against their actual flattened/split target names
  rather than the source object's own name, so a clean conversion doesn't
  show up as a false mismatch. See `tgdatabridge/core/schema_diff.py`.

## What it deliberately does not do (v1 scope)

- No ongoing replication / CDC — this is a one-time cutover tool, not a
  DMS-style continuous replication service.
- PL/SQL → PL/pgSQL conversion is still mostly rule-based, not a full
  parser-driven translation — it handles the constructs listed above well,
  but anything it doesn't recognize (or MySQL/PostgreSQL/SQL Server/Db2 as
  a routine target or source) is left flagged for a developer to convert
  by hand rather than guessed at. AWS SCT has the same limitation for
  sufficiently complex routines. One structural piece — finding a
  routine's own top-level `BEGIN` and any nested subprogram declared
  before it — now runs on a real grammar-based parser instead (see
  `tgdatabridge/core/plsql_ast.py` below), with the regex approach kept as an
  automatic fallback; the actual syntax translation elsewhere in the
  converters is not yet migrated onto it.
- Multi-schema / whole-database batch conversion is now built at the core
  level (`tgdatabridge/core/batch.py`'s `run_batch_migration`), but not wired into
  the desktop GUI yet — the GUI's "Migrate Data" flow still operates on one
  already-loaded schema at a time; see "Possible next steps" below and
  `ENTERPRISE_READINESS.md` section 4.
- MongoDB's views/stored-procedure/trigger conversion is always flag-only,
  unconditionally, in both directions — even for a genuinely Oracle-sourced
  routine every other target would run through a real converter. Translating
  an arbitrary SQL `SELECT` into a MongoDB aggregation pipeline (or vice
  versa), or Oracle PL/SQL into application-layer/Atlas Trigger code, is a
  fundamentally different, much harder problem this tool doesn't attempt —
  see "Converts a relational schema to a MongoDB document model" above.
- MongoDB-as-a-source schema inference samples up to 1,000 documents per
  collection rather than scanning the whole collection, so a field that
  never appears in the sample is invisible no matter how common it is in
  the full collection — and only one level of array-to-child-table
  normalization is attempted; an array nested inside another array's items
  is flagged for manual review rather than modeled as a further grandchild
  table. See "MongoDB as a source" above.

## Project layout

```
teleglobal-schema-conversion-tool/
  main.py                        entry point (launches the GUI)
  SCALE.md                       what it takes to run a 1 TB Oracle -> PostgreSQL migration in a few-hour cutover window: measured bottlenecks, prioritized changes, effort estimates
  migrate_cli.py                 headless/CI-CD entry point (see "Headless / CI-CD usage" below) -- no PySide6/GUI dependency
  requirements.txt
  tgdatabridge/
    cli/
      config.py                  config-as-code for migrate_cli.py: CliJobConfig/CliConnectionConfig dataclasses, JSON-always/YAML-if-available loading, password-from-env resolution -- a config file never contains a plaintext password
      runner.py                  headless orchestration: Load Schema -> Convert -> (optionally) Apply DDL / Migrate Data, the production approval gate (check_approval_gate), argparse CLI (build_arg_parser/main)
    core/
      connector_factory.py       engine-name -> connector/introspector dispatch, shared by both the GUI (main_window.py) and the CLI (cli/runner.py) -- no PySide6 dependency, so the CLI works with no Qt installed at all
      schema_model.py            dataclasses: Table, Column, View, Sequence, Routine, Schema (Table has two Mongo-source-only optional fields, source_collection/source_array_path; View has source_engine, mirroring Routine.source_engine)
      type_mapping.py            Oracle -> Oracle / PostgreSQL / MySQL / SQL Server / Db2 / MongoDB type rules (to_oracle is close to an identity mapping -- see its own docstring), plus MySQL/PostgreSQL/SQL Server/Db2/MongoDB -> Oracle-pivot reverse mapping (from_mysql/from_postgres/from_sqlserver/from_db2/from_mongodb) for using those engines as a source, plus from_spreadsheet for inferred Excel/CSV cell types
      introspector.py            reads Oracle's ALL_* data dictionary views
      mysql_introspector.py      reads MySQL's information_schema, reverse-mapping types via type_mapping.from_mysql, for MySQL as a source
      postgres_introspector.py   reads PostgreSQL's information_schema + pg_catalog, reverse-mapping types via type_mapping.from_postgres, for PostgreSQL as a source
      sqlserver_introspector.py  reads SQL Server's information_schema + sys.* catalog views, reverse-mapping types via type_mapping.from_sqlserver, for SQL Server as a source
      db2_introspector.py        reads Db2's SYSCAT.* catalog views, reverse-mapping types via type_mapping.from_db2, for Db2 as a source
      mongo_source_introspector.py  infers a schema by sampling documents (no fixed schema/catalog to read) -- nested objects flatten to dotted columns, arrays normalize into synthesized child tables, reverse-mapping types via type_mapping.from_mongodb, for MongoDB as a source
      spreadsheet_introspector.py   one sheet -> one table, header row -> columns, sampled cells -> inferred types, for Excel/CSV as a source; deliberately synthesizes no keys/indexes/views/routines (a worksheet has none)
      spreadsheet_types.py       cell classification/coercion + identifier sanitization shared by the Excel/CSV introspector and connector (leading-zero codes stay text, only ISO dates are recognized, mixed columns widen to text)
      sharding.py                splits one large table into disjoint PK-range shards so several workers can migrate it at once (SCALE.md section 1.2); falls back to a single whole-table unit whenever a safe partition can't be built
      ddl_generator.py           emits target CREATE TABLE/VIEW/SEQUENCE DDL (or, for MongoDB, db.createCollection/createIndex/updateOne JS calls), calls plsql_converter for routines; the Oracle-target branch reconstructs real Oracle DDL (EXECUTE IMMEDIATE-wrapped idempotent guards, native SEQUENCE, CREATE OR REPLACE VIEW) rather than translating into it; generate_schema_ddl_phased() splits the script into pre-load (bare tables) and post-load (constraints, indexes, FKs, triggers) halves for a large migration -- see SCALE.md section 1.3
      plsql_converter.py         Oracle PL/SQL -> PostgreSQL PL/pgSQL syntax converter (procedures, functions, triggers, packages); also unconditionally flags every routine manual for a MongoDB target; its Oracle-target branch needs no syntax rewriting at all, only reconstructing the CREATE-statement header ALL_SOURCE/ALL_TRIGGERS never stored in the first place; also home to the DBMS_LOB/DBMS_RANDOM mapping helpers tsql_converter.py/db2_converter.py both import and reuse
      nested_subprogram.py       shared nested-PROCEDURE/FUNCTION-in-a-DECLARE-section detection (used by all three routine converters below) -- cuts a nested subprogram out as one intact unit for flagging, and locates a routine/trigger's own top-level BEGIN even when a nested subprogram (with or without its own body) sits before it in the DECLARE section; tries plsql_ast.py's grammar-based parser first, falls back to its own regex/depth-counting implementation whenever that doesn't apply
      plsql_ast.py               clean Python API over the ANTLR-generated Oracle PL/SQL grammar parser (plsql_grammar/generated/) -- find_declare_and_body_structure() for nested_subprogram.py's use above, plus parse_routine_source()/can_parse() for parsing a full routine; never raises for a syntax error or unsupported construct, always returns a value callers can fall back from
      tsql_converter.py          Oracle PL/SQL -> SQL Server T-SQL syntax converter (procedures, functions, triggers, packages)
      db2_converter.py           Oracle PL/SQL -> Db2 (LUW) SQL PL syntax converter (procedures, functions, triggers, packages)
      connect_by_rewriter.py     best-effort CONNECT BY -> recursive CTE rewrite, shared by ddl_generator (views) and all three routine converters
      sql_translator.py          PL/SQL complexity scoring; also the flag-only fallback for MySQL-target routines
      assessment.py              rolls object statuses up into a summary
      migrator.py                batched source -> target data copy, plus post-migration validation, checkpoint/resume, retry-with-backoff wiring, dry-run/plan mode (plan_table/plan_schema), parallel table migration (order_tables_by_dependency_waves + max_workers/source_factory/target_factory), and LOB-aware batch sizing; prefers a source connector's fetch_batches_table(table) over the usual SQL-string fetch_batches(sql) when the source defines one (MongoDB only, so far -- a synthesized child table has no real collection of its own for a SQL string to name)
      batch.py                   multi-schema/whole-database batch migration orchestration -- run_batch_migration() runs migrate_schema() once per SchemaMigrationJob, schemas always one at a time (never concurrently with each other), aggregating a BatchReport; core-only for now, no GUI wiring yet (see README's "What it deliberately does not do")
      validation.py              order-independent row checksum + validate_table(), an independent post-migration row-count/checksum re-query against the target
      retry.py                   RetryPolicy + retry_call(), exponential-backoff retry for transient target errors during data migration
      target_introspector.py     lightweight "what's actually on the target" object-name lookup (reads Oracle's own ALL_* views for an Oracle target; reads collections directly off PyMongo's `db` for a MongoDB target, since there's no information_schema/SYSCAT to query)
      schema_diff.py             case-insensitive source-vs-target name diff (tables/views/sequences/routines)
    plsql_grammar/
      generated/                 ANTLR-generated Python3 lexer/parser (PlSqlLexer.py/PlSqlParser.py) from the antlr/grammars-v4 Oracle PL/SQL grammar, plus the hand-written PlSqlLexerBase.py/PlSqlParserBase.py base classes the grammar's `superClass` option requires
      vendor/antlr4/              vendored copy of the antlr4-python3-runtime package (see requirements.txt) -- offline fallback for environments where `pip install` can't reach PyPI
    db/
      oracle_connector.py        python-oracledb (thin mode, no Instant Client needed); source-only originally, insert_batch() is the one addition needed for Oracle as a target too; count_rows()/checksum_rows() support post-migration validation; fetch_batches() materializes CLOB/BLOB/NCLOB LOB-locator values into plain str/bytes in chunk_size()-multiple reads before yielding a row (see _materialize_lob's docstring for the bug this fixes)
      pool.py                    ConnectionPool -- minimal blocking connection pool (factory + max_size), used by migrator.py's parallel table migration and batch.py's multi-schema jobs, since no DB-API driver this tool uses documents a bare connection as safe to share across threads
      postgres_connector.py      psycopg 3; insert_batch() bulk-loads via COPY ... FROM STDIN (FORMAT BINARY) rather than executemany INSERT -- roughly 10x on the write side, see SCALE.md section 1.1; count_rows()/checksum_rows() support post-migration validation
      mysql_connector.py         mysql-connector-python; count_rows()/checksum_rows() support post-migration validation
      sqlserver_connector.py     pyodbc (requires a Microsoft ODBC Driver 17/18 for SQL Server); count_rows()/checksum_rows() support post-migration validation
      db2_connector.py           ibm_db_dbi (bundles its own Db2 client libraries, no separate OS driver needed); count_rows()/checksum_rows() support post-migration validation
      mongo_connector.py         PyMongo; parses ddl_generator's json.dumps-built JS subset back into real db.create_collection/create_index/update_one/drop calls (target side, the last of these emitted by generate_rollback_ddl); execute() still raises NotImplementedError (no SQL dialect exists), but fetch_batches_table() supports MongoDB as a source's data migration by reading collections/unwinding arrays directly; count_rows()/checksum_rows() support post-migration validation
      spreadsheet_connector.py   reads a local .xlsx/.xlsm/.csv/.tsv file as a source (ConnectionParams.database holds the file path); source-only, so execute()/execute_ddl() both raise; fetch_batches_table() streams coerced rows and count_rows() backs dry-run planning
    reports/
      report_generator.py        builds the HTML assessment report
    gui/
      main_window.py             toolbar + schema tree + DDL/report/rollback/diff tabs + log console; wires dry-run, checkpointed/retried/validated data migration, and rollback-script generation into their own toolbar buttons; a "Parallel workers" spinbox next to Migrate Data controls migrate_schema's max_workers (1 = original single-threaded behavior); a "Settings…" button opens settings_dialog.py
      connection_dialog.py       source/target connection dialogs with Test Connection + saved-connection picker
      history_dialog.py          "History…" dialog listing past conversion runs, reopens their report/DDL
      settings_dialog.py         "Settings…" dialog: optional centralized log-shipping endpoint (section 3) and optional shared storage folder for a team deployment (section 5)
      schema_tree.py             checkbox tree of the introspected *source* schema
      target_schema_tree.py      compact read-only tree of what's actually on the *target*
      status_widgets.py          connection badges + running/success/failure status indicator
      report_view.py             DDL + HTML report + schema diff + rollback-script tabs
      log_console.py             live log panel
    utils/
      sql_split.py               splits a DDL script into individually-executable statements (dollar-quote, T-SQL/Db2 BEGIN/END, and "--"/"/* */" comment aware)
      app_storage.py             on-disk persistence for saved connection profiles, conversion run history (no passwords ever stored, every record actor-stamped), and per-migration checkpoints (migration_checkpoints/) -- local per-machine by default, or a configured shared folder (see settings.py, "Shared storage for a team" below)
      logger.py                  structured, actor-stamped log bus: in-memory pub/sub for the GUI console (unchanged API) plus best-effort JSON-lines disk persistence (logs/tgdatabridge-YYYY-MM-DD.jsonl)
      log_shipper.py             optional forwarding of log lines to a central sink (Splunk HEC / ELK / any JSON-POST endpoint) via urllib, fire-and-forget on a background thread
      settings.py                small persisted app settings: log-shipping endpoint + shared_storage_path (always resolved locally -- see its own docstring for why)
      metrics.py                 per-operation duration/outcome recording (metrics/operations.jsonl) + Prometheus textfile export (metrics/tgdatabridge_metrics.prom): operation duration, run counts, rows/sec throughput and error rate by engine pair
  tests/                         unit tests for type mapping, DDL generation, PL/SQL/T-SQL/Db2 conversion, assessment, local persistence, the five migration-reliability features (validation, checkpoint/resume, retry, dry-run, rollback script), the grammar-based parser wrapper (test_plsql_ast.py), the scale features (test_pool.py, parallel-migration/LOB-batch-sizing tests in test_migrator.py, LOB-materialization tests in test_oracle_connector.py, test_batch.py), observability (test_logger.py, test_log_shipper.py, test_settings.py, test_metrics.py), the headless CLI (test_connector_factory.py, test_cli_config.py, test_cli_runner.py), the Excel/CSV source (test_spreadsheet_types.py, test_spreadsheet_connector.py, test_spreadsheet_introspector.py), intra-table sharding (test_sharding.py, test_migrator_sharded.py), two-phase DDL (test_deferred_ddl.py), throttled checkpointing (test_checkpoint_writer.py), and the Debezium CDC integration (test_cdc_config.py, test_cdc_preflight_and_client.py)
    fixtures/plsql/              golden-file regression suite: realistic Oracle routine .sql fixtures + a manifest.json describing each one's kind/trigger metadata, with saved expected/<postgresql|sqlserver|db2>/<id>.sql conversion snapshots compared against fresh output by test_golden_plsql.py
  vendor/                        source materials for the grammar-based parser -- the ANTLR 4.13.2 tool jar, the Oracle PL/SQL .g4 grammar files (from antlr/grammars-v4), and the antlr4-python3-runtime sdist -- used to (re-)generate tgdatabridge/plsql_grammar/generated/ and tgdatabridge/plsql_grammar/vendor/; not needed at runtime once those are generated
  Launch Teleglobal Database Migration Tool.bat   Windows launcher -- double-click to run instead of `python main.py`
  Build Windows Package.bat     one-click build of a standalone, no-install-needed Windows package (see below)
  packaging/
    teleglobal_tdmt.spec         PyInstaller build spec used by Build Windows Package.bat
    teleglobal_tdmt.iss          Inno Setup script -- builds the optional Teleglobal-TDMT-Setup.exe installer
```

## Setup

Requires Python 3.10+ on Windows (or any OS with a desktop, for testing).

```bash
cd teleglobal-schema-conversion-tool
python -m venv .venv
.venv\Scripts\activate          # on Windows
pip install -r requirements.txt
python main.py
```

On Windows, once dependencies are installed, you can also just double-click
**`Launch Teleglobal Database Migration Tool.bat`** in this folder instead of typing `python
main.py` in a command prompt each time. It picks up a local `.venv` if one
exists, and stays open with the error message if something goes wrong
(rather than a window that flashes and disappears).

No Oracle Instant Client install is required — `python-oracledb` runs in
"thin" mode by default, connecting directly over the network.

The Oracle account used to connect needs `SELECT_CATALOG_ROLE` (or
equivalent read grants) to introspect a schema other than the one it
logs in as.

A SQL Server target needs a Microsoft ODBC Driver for SQL Server (17 or
18) installed separately — `pyodbc` is a thin wrapper over the platform's
ODBC driver manager and doesn't bundle one itself. Download it from
Microsoft's ODBC Driver for SQL Server page for your OS if `Test
Connection` fails with a driver-not-found error.

A Db2 target needs no separate OS-level driver install — `ibm_db` bundles
the Db2 client libraries it needs itself.

A MongoDB target needs no separate OS-level driver install either —
`pymongo` speaks MongoDB's wire protocol directly over the network, the
same as `psycopg`/`mysql-connector-python` do for their respective engines.

## Building a standalone Windows package (no Python needed to run it)

`Launch Teleglobal Database Migration Tool.bat` above still needs Python and the pip
dependencies installed on the machine. For handing the tool to someone
whose machine has neither, **`Build Windows Package.bat`** produces a
fully self-contained package instead — a Windows `.exe` with every DLL and
library it depends on (the Python interpreter, Qt/PySide6, and all six
database drivers) sitting right next to it, plus the complete source code.

Run it once, **on a Windows machine with internet access** (needed to
pip-install the build tools and dependencies — PyInstaller bundles
whatever OS it's run on, so this step can't be done from Linux/macOS or
in a sandbox):

```
Build Windows Package.bat
```

That produces `Teleglobal-TDMT-Windows-Package.zip` in the project root,
containing:

```
Teleglobal-TDMT-Windows-Package/
  START HERE.txt        quick instructions
  App/                  the standalone build -- run the .exe inside this folder
    Teleglobal Database Migration Tool.exe
    _internal/          bundled Python runtime, Qt, and all six DB drivers
  Source Code/           the full, human-readable source this was built from
```

Copy that zip to any other Windows machine, unzip it, and double-click the
`.exe` inside `App\` — no Python, no `pip install`, nothing else to set
up. The whole `App` folder needs to move together (the `.exe` depends on
everything in `_internal`), so distribute/copy it as a unit, not just the
`.exe` file by itself. The SQL Server ODBC driver caveat above still
applies to the standalone build too — it's an OS-level component, not
something PyInstaller can bundle into the `.exe`.

### Optional: a proper installer (Start Menu shortcut, uninstaller)

The portable `.zip` above is a complete deliverable on its own — but if
you'd rather hand someone a double-click installer that adds a Start Menu
shortcut, an optional desktop icon, and a normal entry in Windows' "Add
or Remove Programs" (with a real uninstaller), install
[Inno Setup](https://jrsoftware.org/isdl.php) (free) on the build machine
first, then run `Build Windows Package.bat` as above — it detects Inno
Setup automatically and, after building the portable package, also
produces:

```
Teleglobal-TDMT-Setup.exe
```

in the project root. If Inno Setup isn't installed, that step is skipped
with a message telling you where to get it; the portable `.zip` still
builds normally either way. The installer script itself is
`packaging\teleglobal_tdmt.iss`, and can be compiled by hand (open it in
the Inno Setup Compiler, or run `ISCC packaging\teleglobal_tdmt.iss`) if
you'd rather not re-run the whole `.bat`.

**This installer is not code-signed** — Teleglobal doesn't currently have
a code-signing certificate, and one can't be generated by this tool.
Windows SmartScreen will very likely show an "Unknown publisher" /
"Windows protected your PC" warning the first time `Teleglobal-TDMT-
Setup.exe` (or the installed app itself) runs on another machine; click
"More info" → "Run anyway" to proceed. This is expected for any unsigned
installer and isn't a sign of a corrupted download. If your organization
obtains a code-signing certificate later, sign the built `.exe` files
with `signtool sign` and add a `SignTool=` line to the `[Setup]` section
of `packaging\teleglobal_tdmt.iss` to have Inno Setup sign the installer
itself as part of the build too.

Saved connection profiles and conversion history
(`%APPDATA%\TeleglobalTDMT\`, see "Remembers connection profiles and past
runs" above) live outside the install folder and are left alone by the
uninstaller, the same way most well-behaved Windows apps treat per-user
data.

## Using it

1. **Connect Source** — pick Oracle, MySQL, PostgreSQL, SQL Server, DB2, or
   MongoDB from the source-engine dropdown, enter host/port/service name
   (or database)/credentials, optionally a different schema to introspect
   than the login user, and click *Test Connection* before saving.
   MongoDB (like MySQL) has no separate "schema" field — its database name
   doubles as the schema — and "Load Schema" samples up to 1,000 documents
   per collection rather than reading a catalog, since MongoDB has none.
2. **Connect Target** — pick Oracle, PostgreSQL, MySQL, SQL Server, DB2, or
   MongoDB from the dropdown, then connect the same way. An Oracle target
   needs no separately-generated schema (there's no `CREATE SCHEMA` for
   Oracle at all — a "schema" there is a user account) — the schema field
   defaults to the connecting username, same as an Oracle source.
3. **1. Load Schema** — introspects the source schema and populates the
   object tree on the left.
4. **2. Convert Schema** — generates target DDL, scores stored routines,
   and builds the assessment report (see the two tabs on the right).
5. **3. Apply DDL to Target** — executes the generated DDL against the
   connected target database.
6. **4a. Dry Run (Plan)** — optional: checks source row counts and target
   table readiness without writing anything, so you can sanity-check a
   migration before actually running it.
7. **4b. Migrate Data** — copies rows for the checked tables into the
   newly created target tables, in batches, with live progress in the log
   console, automatic retry on transient errors, post-migration
   validation, and checkpointed resume (a re-run after a partial failure
   picks up where it left off instead of starting over).
8. **Rollback Script** — optional: generates a DROP script undoing the
   converted schema's DDL, shown in its own tab.
9. **Save Report... / Save DDL... / Save Rollback...** — export the
   assessment report as standalone HTML, the DDL as a `.sql` file, or the
   rollback script as a `.sql` file, for change-review / sign-off outside
   the tool.

## Headless / CI-CD usage (`migrate_cli.py`)

ENTERPRISE_READINESS.md section 5 ("Operability & automation"). Everything
above is the GUI; `migrate_cli.py` drives the exact same
Load Schema → Convert → (optionally) Apply DDL / Migrate Data flow from a
script or CI/CD pipeline instead, using a config file in place of clicks —
same conversion logic, same reliability features (validation, checkpoint/
resume, retry-with-backoff), same structured/actor-stamped logs and
Prometheus metrics as a GUI run. Nothing under `tgdatabridge/cli/` imports
PySide6, so this works on a machine with no Qt installed at all.

```bash
python migrate_cli.py --config job.json
python migrate_cli.py --config job.json --approved-by "Jane Doe"   # for production=true jobs
python migrate_cli.py --config job.json --dry-run                  # force plan-only, nothing written
python migrate_cli.py --help                                       # full flag list + exit codes
```

A config file (`job.json`, or `job.yaml`/`.yml` if `pyyaml` is installed —
see `requirements.txt`) never contains a plaintext password, only the name
of an environment variable to read it from at run time:

```json
{
  "source": {
    "engine": "Oracle", "host": "dbhost1", "port": 1521, "database": "HRPROD",
    "username": "hr_admin", "password_env": "SRC_DB_PASSWORD", "schema": "HR"
  },
  "target": {
    "engine": "PostgreSQL", "host": "dbhost2", "port": 5432, "database": "hrdb",
    "username": "postgres", "password_env": "TGT_DB_PASSWORD"
  },
  "target_schema": "public",
  "tables": ["EMPLOYEES", "DEPARTMENTS"],
  "apply_ddl": true,
  "migrate": true,
  "dry_run_migrate": false,
  "max_workers": 4,
  "max_shards_per_table": 0,
  "min_rows_to_shard": 1000000,
  "defer_constraints": true,
  "checkpoint_flush_seconds": 5,
  "cdc": {
    "enabled": true,
    "connect_url": "http://kafka-connect:8083",
    "kafka_bootstrap_servers": "kafka:9092",
    "topic_prefix": "hrmig",
    "snapshot_mode": "no_data",
    "drain_target_seconds": 60
  },
  "production": true,
  "approval_command": "my-org-change-ticket-check.sh HR-MIGRATION-123",
  "output_dir": "./out"
}
```

`source`/`target` accept any of the six engines (`Oracle`, `MySQL`,
`PostgreSQL`, `SQL Server`, `DB2`, `MongoDB`) in any source/target
combination the GUI supports, plus `Excel/CSV` as a source only. A
spreadsheet source has no host, port or credentials, so its block is just
the file path (and optionally a schema name) — supplying any of the
network fields is rejected rather than silently ignored:

```json
"source": { "engine": "Excel/CSV", "database": "./data/sales.xlsx", "schema": "reporting" }
```

To read several files in one job, give `files` (a list) instead of
`database` -- up to 50, the same cap the GUI enforces. Setting both is an
error rather than a silent precedence rule:

```json
"source": {
  "engine": "Excel/CSV",
  "files": ["./data/north.xlsx", "./data/south.xlsx", "./data/refunds.csv"],
  "schema": "reporting"
}
```

`tables`, if omitted, means every table in
the loaded schema (matching the GUI's own "nothing checked = everything"
behavior). Every run always does Load Schema → Convert Schema, writing
`ddl.sql` and `report.html` into `output_dir` regardless of `apply_ddl`/
`migrate` — a config with both left `false` is a pure "assess this schema
and report on it" CI check, useful on its own. `apply_ddl`/`migrate`
opt into the destructive steps; `migrate: true` with `dry_run_migrate:
true` (or the `--dry-run` flag, which forces it regardless of the file)
runs the same plan-only check as the GUI's "4a. Dry Run (Plan)" button —
nothing is written to either side.

**Parallelism and sharding:** `max_workers > 1` migrates several tables at
once within each FK-dependency wave, *and* splits large tables across
workers so one huge table can't pin a single worker while the rest idle
(see SCALE.md section 1.2). `max_shards_per_table` caps the split — `0`
(the default) means "use `max_workers`" — and `min_rows_to_shard`
(default 1,000,000) keeps small tables unsharded, where the coordination
overhead would cost more than it saves. Only a table with a single-column
**integer primary key** is ever split; everything else falls back to a
single whole-table read, with the reason logged. Sharding applies only
when `max_workers > 1`.

**Two-phase DDL** (`defer_constraints: true`, SCALE.md section 1.3): creates tables as bare columns first, migrates the data into them, then applies primary keys, unique/check constraints, indexes, foreign keys and triggers. Loading into an unindexed table is substantially faster, each index is built once over the finished table rather than maintained per row, and triggers never fire on migrated rows — which is both faster and usually more correct, since the source rows already reflect whatever those triggers do. Writes `ddl_preload.sql` and `ddl_postload.sql` instead of a single `ddl.sql`, and applies the second automatically once the data has landed (skipped, with the script left on disk, if any table failed to migrate). The trade-off: a constraint violation surfaces at the end of the run rather than on the first bad row, so rehearse anything large.

**Checkpoint flush interval** (`checkpoint_flush_seconds`, SCALE.md section 1.5): migration progress is written to the resumable checkpoint file at most once every N seconds (default 5) rather than after every batch — at bulk-load speeds across several shards, rewriting that file per batch is itself a bottleneck. Terminal transitions (a table or shard reaching done or failed) always write immediately, since those are what a resume actually reads. Set it to `0` to write after every batch. This value is also the worst case amount of re-copied work if the process dies mid-run.

**AI-assisted error diagnosis** (`ai.enabled`) — optional, off by default. A
config's top-level `"ai"` block turns on a plain-language explanation and
fix suggestions, printed right after the `ERROR:` line whenever Load
Schema, Apply DDL or Migrate Data fails:

```json
"ai": {
  "enabled": true,
  "provider": "claude",
  "api_key_env": "TGDATABRIDGE_AI_API_KEY",
  "error_diagnostics": true
}
```

Like `password_env`, `api_key_env` names an environment variable to read
the key from at run time — never a plaintext key in the file itself.
`provider` is one of `claude`, `openai`, `azure_openai` (also needs
`base_url` and `model`, your deployment name) or `compatible` (a local/
offline OpenAI-compatible server; needs `base_url`, `api_key_env` is
usually not needed). This is deliberately the one AI feature wired into
the headless CLI — the other three (schema mapping review, plain-English
requests, data quality review) are interactive review tools that live in
the GUI's **AI Review…** dialog instead; see `WHATS-FIXED.md`'s "Round 24"
section for what each one does and exactly what data leaves this machine.

**Change data capture** (`cdc.enabled`, SCALE.md section 2.1): for a migration that needs a short cutover window rather than a full-outage copy. This tool does **not** implement CDC — it generates a [Debezium](https://debezium.io) Oracle connector config from the schema it just introspected, checks the Oracle-side prerequisites, registers the connector over the Kafka Connect REST API, and waits for lag to drain before declaring the migration cutover-ready. Run `--cdc-preflight` days ahead: it writes `debezium-connector.json` and reports ARCHIVELOG mode, supplemental logging, LogMiner grants and redo retention with the remedial SQL for anything missing, without touching either database.

The ordering is enforced rather than documented: with `snapshot_mode: no_data` the connector is registered and confirmed RUNNING **before** the bulk load starts, so it buffers every change from that SCN onward. Loading first would leave a gap that no row count would reveal. Applying the captured changes to PostgreSQL is the Debezium JDBC sink connector's job, not this tool's.

**Approval gate** (`production: true`, section 5 item 3): `apply_ddl`/
`migrate` are refused unless an approver name is given
(`--approved-by "Name"` or the `TGSCT_APPROVED_BY` environment variable —
handy for a CI job to inject from whoever triggered the pipeline), and,
if `approval_command` is also set, that shell command must exit `0` — a
generic hook for integrating with whatever change-management tooling your
org already uses (a script that checks a ticketing system, calls an
internal API, etc.), the same way centralized log shipping is a plain
HTTP endpoint rather than one vendor's SDK. `ddl.sql`/`report.html` are
still written even when the gate blocks the run (the assess-only part is
always safe); nothing else touches either database.

**Exit codes:** `0` everything requested succeeded · `1` a step ran but
failed (connection/introspection error, a DDL statement failed, or one or
more tables failed to migrate/validate) · `2` a config/environment problem
meant nothing ever touched a database (bad file, unknown engine, an unset
`password_env` variable) · `3` blocked by the production approval gate.

Other flags: `--state-dir PATH` overrides where conversion history/
checkpoints/logs/metrics are read and written for this run (instead of
this machine's usual local, or configured shared, storage location) —
useful for CI to pin an explicit shared location without depending on a
runner having its own `settings.json`. `--output-dir PATH` overrides the
config file's own `output_dir`. `--quiet` stops echoing log lines to
stdout as they happen (files/history/metrics are still written).

### Shared storage for a team (Settings…, section 5 item 4)

By default, connection profiles, conversion history, migration
checkpoints, logs, and metrics all live per-machine under
`%APPDATA%\TeleglobalTDMT\` (or `~/.teleglobal_tdmt/`). The **Settings…**
toolbar button (or a `shared_storage_path` value in a machine's local
`settings.json`) can point this at a shared folder — a mapped network
drive or UNC path — instead, so a team sees the same profiles/history/
checkpoints regardless of which machine they're running the tool (or the
CLI) from. This is deliberately simple: last-write-wins, no locking or
conflict resolution, matching the tool's existing one-analyst-at-a-time
usage pattern, just with that state now shared. `settings.json` itself
always stays local to each machine — it's the small pointer *to* the
shared location, not itself something that gets shared (see
`tgdatabridge/utils/settings.py`'s docstring for why).

## Running the tests

```bash
pip install pytest
pytest tests/
```

(All 1279 tests in this repo cover the pure logic layer — type mapping
(Oracle -> targets including Oracle itself, close to an identity mapping
for an Oracle target, and MongoDB's BSON `bsonType` mapping;
MySQL/PostgreSQL/SQL Server/Db2 -> the shared Oracle-flavored pivot type;
and MongoDB's own sampled-BSON-type -> pivot reverse mapping),
MySQL/PostgreSQL/SQL Server/Db2 schema introspection, MongoDB's
sample-based schema inference (nested-object flattening, array-to-
child-table normalization including array-of-scalars/array-of-
sub-documents/always-empty-array/array-of-arrays/array-nested-in-array
edge cases, type-conflict resolution, index skip-on-array-promoted-field,
and view/sequence/routine handling), DDL generation (including preserving
reverse-mapping issues across repeated/re-targeted runs, MongoDB's
`$jsonSchema`-validator/CHECK-constraint translation, sequence-as-
counters-collection emulation, the always-manual view placeholder in both
the MongoDB-target and MongoDB-source directions, and Oracle-target DDL's
`EXECUTE IMMEDIATE`-wrapped idempotent guards/native-sequence/no-CREATE-
SCHEMA behavior), PL/SQL -> PL/pgSQL, PL/SQL -> T-SQL, PL/SQL -> Db2 SQL
PL, and PL/SQL -> Oracle conversion (including the non-Oracle-source
manual-conversion guard applying to every target including Oracle,
MongoDB's own always-manual routine guard, and the Oracle-target
CREATE-header reconstruction for both non-trigger and trigger routines),
statement splitting (including "--"/"/* */" comment awareness), the
MongoDB DDL-statement parser/dispatcher, Oracle and MongoDB target
introspection, MongoDB source data migration (`fetch_batches_table`'s
top-level-collection and array-unwinding-into-child-table paths, and
`migrator.migrate_table`'s duck-typed dispatch between it and the
ordinary SQL-string `fetch_batches` with zero behavior change for every
other engine), Oracle-as-a-target data migration (`insert_batch`'s
identifier quoting and schema qualification), assessment/complexity
scoring, local connection-profile/history persistence, the source-vs-
target schema diff, the CONNECT BY -> recursive CTE rewrite, and the five
migration-reliability features (order-independent row checksumming,
`validate_table`'s row-count/checksum comparison and its "unverified vs.
failed" distinction, each connector's `count_rows`/`checksum_rows` SQL
per its own quoting convention, retry-with-backoff's transient-error
detection/exponential delay/non-retryable-fails-fast behavior,
checkpoint/resume's skip-already-done-tables and resume-partial-tables-
without-duplicate-writes behavior and its checksum-still-matches-a-fresh-
run guarantee, `plan_table`/`plan_schema`'s dry-run checks, and
`generate_rollback_ddl`'s per-engine DROP syntax/reverse-dependency-
ordering/idempotent guards across all six target engines), the shared
nested-subprogram/local-TYPE-declaration detection module and its
integration into all three routine converters' declare-block parsers and
trigger-body splitters (including the forward-declaration edge case where
a bodyless `PROCEDURE foo(...);` precedes a real nested subprogram or the
routine's own body), the Postgres `BULK COLLECT` -> `array_agg()` rewrite
and its sharper FORALL/cursor-BULK-COLLECT diagnostics, the
`DBMS_LOB`/`DBMS_RANDOM` mappings across all three targets, a
golden-file regression suite running eight realistic Oracle routines
end-to-end through all three converters and diffing the full output
against a saved snapshot, and the grammar-based parser wrapper
(`tgdatabridge/core/plsql_ast.py`'s error collection, and both source shapes
`find_declare_and_body_structure` recognizes) plus its integration into
`nested_subprogram.py` (parity between the grammar and regex paths on the
same input, the trigger-DECLARE-keyword shape, and the regex fallback
correctly engaging for a construct outside this grammar snapshot's
coverage), and the four scale features (`ConnectionPool`'s bounded
creation/reuse/best-effort `close_all`, `order_tables_by_dependency_waves`
grouping independent tables together while a multi-level FK chain still
gets one wave per level, parallel `migrate_schema` genuinely running
multiple worker threads while still never starting a later wave before
the current one finishes -- checked by both a thread-identity check and a
timing check -- with deterministic result ordering and a connection-
factory failure becoming an ordinary failed result rather than crashing
the pool, LOB-column detection driving batch-size narrowing, Oracle LOB-
locator materialization in bounded chunks, and `run_batch_migration`
running one schema at a time while isolating one bad schema's connection
failure from the rest of the batch) — and were verified to pass without
requiring a live database, a real `pymongo`/`bson`/`oracledb` install, or
the GUI dependencies.)

## Possible next steps

- ~~Package as a signed Windows installer (PyInstaller/Inno Setup) so
  non-technical users don't need a Python environment.~~ Done, as an
  *unsigned* installer (Teleglobal has no code-signing certificate) —
  see "Optional: a proper installer" above.
- ~~Add MySQL as a *source* engine too, for reverse or lateral
  migrations.~~ Done — see "MySQL as a source" above.
- ~~Add PostgreSQL as a *source* engine too, for reverse or lateral
  migrations.~~ Done — see "PostgreSQL as a source" above.
- ~~Add SQL Server as a *source* engine too, for reverse or lateral
  migrations.~~ Done — see "SQL Server as a source" above.
- ~~Add Db2 (LUW) as a *source* engine too, for reverse or lateral
  migrations.~~ Done — see "Db2 (LUW) as a source" above. Every target
  engine (PostgreSQL, MySQL, SQL Server, Db2) can now also be a source,
  alongside Oracle.
- ~~Add MongoDB as a *source* engine too, via sample-based schema
  inference.~~ Done — see "MongoDB as a source" above. Every engine this
  tool supports at all (Oracle, MySQL, PostgreSQL, SQL Server, Db2,
  MongoDB) can now be a source; every non-Oracle engine can also be a
  target. A real second generation of array-in-array grandchild tables
  (currently flagged manual and dropped instead) would be a natural
  further step if that shape turns out to be common in practice.
- ~~Persist connection profiles and past conversion runs to disk.~~ Done
  — see "Remembers connection profiles and past runs" above.
- ~~Expand the PL/SQL translator's automatic-rewrite coverage (e.g. simple
  `DECODE()` → `CASE`, basic `CONNECT BY` → recursive CTE patterns).~~
  Done — `DECODE()` → `CASE` was already implemented across all three
  converters; the common single-table `CONNECT BY` shape now gets a
  best-effort recursive-CTE rewrite too, see "hierarchical queries get
  one further step than a flag" above.
- ~~Add a diff view comparing source and already-existing target schema,
  for iterative/incremental conversions.~~ Done — see "Diffs the source
  against the target" above.
- ~~Add MongoDB as a target engine.~~ Done — see "Converts a relational
  schema to a MongoDB document model" above.
- ~~Add Oracle as a target engine too, for lateral/reverse migrations.~~
  Done — see "Converts a relational schema back to Oracle" above. Every
  engine this tool supports (Oracle, MySQL, PostgreSQL, SQL Server, Db2,
  MongoDB) can now be *either* the source or the target.
- ~~Post-migration validation, checkpoint/resume, retry with backoff,
  dry-run mode, and an auto-generated rollback script~~ Done — see the
  five-part "Migrates data" bullet above, and `ENTERPRISE_READINESS.md`
  §2 ("Reliability & data integrity") for the reasoning behind each.
- ~~PL/SQL conversion robustness: fix the nested-subprogram silent-
  corruption bug, expand construct coverage (`BULK COLLECT`, `DBMS_LOB`/
  `DBMS_RANDOM`), and a golden-file regression suite.~~ Done — see the
  "nested subprogram"/"BULK COLLECT"/"DBMS_LOB.GETLENGTH" mentions across
  the three converter bullets above, and `ENTERPRISE_READINESS.md` §6 for
  the reasoning.
- ~~Move to a grammar-based parser (e.g. an ANTLR Oracle PL/SQL grammar)
  instead of the current regex/marker approach.~~ Done, narrowly scoped:
  a real ANTLR-generated parser (see `tgdatabridge/core/plsql_ast.py` and
  `tgdatabridge/plsql_grammar/` above) now backs the nested-subprogram/
  top-level-BEGIN detection specifically, with the regex approach kept as
  an automatic fallback — not yet a replacement for the actual syntax
  translation elsewhere in the converters; see `ENTERPRISE_READINESS.md`
  §6 for the full scope and reasoning, and for how further conversion
  rules could migrate onto it incrementally.
- ~~Scale & performance: parallelize table migration, LOB streaming for
  very large CLOB/BLOB objects, multi-schema/whole-database batch runs,
  and connection pooling.~~ Done — see the "Parallel table migration" and
  "LOB-aware batch sizing and streaming" bullets under "Migrates data"
  above, `tgdatabridge/core/batch.py`'s `run_batch_migration`, and
  `tgdatabridge/db/pool.py`'s `ConnectionPool`; `ENTERPRISE_READINESS.md` §4 has
  the full scope, including what's still open there (a GUI multi-schema
  picker, and true zero-buffering server-to-server LOB streaming).
- The rest of `ENTERPRISE_READINESS.md`'s roadmap (encryption at rest,
  per-connector TLS enforcement, a headless/CLI mode, structured/
  persisted audit logging, data masking, and a code-signed installer) is
  still open.
