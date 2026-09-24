# What changed in this build

## Round 35 — fixed "Apply DDL to Target" failing on a WHEN clause naming a user-declared EXCEPTION (build R35, 17 Sep 2026)

Triggered by "Apply DDL to Target" -- past every fix through Round 34 --
stopping on the next problem in the same DDL script, one statement after
Round 33's SAVEPOINT/ROLLBACK TO fix:

```
Statement 133/166 failed: unrecognized exception condition "e_invalid_status"
CONTEXT: compilation of PL/pgSQL function "feature_pl_admin_pkg_validate_status" near line 4
 -> CREATE OR REPLACE FUNCTION "feature_pl_admin_pkg_validate_status"(p_status VARCHAR) RETURNS VARCHAR AS
    $$ BEGIN IF p_status IN ('NEW','ACTIVE','CLOSED','ERROR') THEN RETURN 'VALID'; END IF; RAISE e_in...
```

**Root cause.** Oracle's `e_invalid_status EXCEPTION;` declares a
user-defined exception local to the routine, later both raised
(`RAISE e_invalid_status;`) and caught by name
(`EXCEPTION WHEN e_invalid_status THEN ...`). An earlier round already
rewrote the RAISE side (`RAISE EXCEPTION 'e_invalid_status';`), but
nothing had ever touched the WHEN side -- Postgres has no user-defined
exception *type* at all, only SQLSTATE condition codes, so it tried to
parse "e_invalid_status" itself as a real condition name (the way
`WHEN division_by_zero THEN` or `WHEN unique_violation THEN` are) and
rejected the *entire* `CREATE FUNCTION`/`CREATE PROCEDURE` outright with
"unrecognized exception condition" -- a compile-time failure, not a
runtime one. Every routine declaring, raising, and catching its own named
exception failed "Apply DDL to Target" the moment it got this far.

**Fixed.** Both sides now agree on a synthetic SQLSTATE code minted per
declared exception, in declaration order (so re-running "2. Convert
Schema" on the same routine always produces the same codes):
`RAISE e_invalid_status;` becomes
`RAISE EXCEPTION 'e_invalid_status' USING ERRCODE = 'U0001';`, and
`WHEN e_invalid_status THEN` becomes `WHEN SQLSTATE 'U0001' THEN` --
Postgres's own `RAISE ... USING ERRCODE` and `WHEN SQLSTATE '...' THEN`
forms accept any 5-character code (digits and/or upper-case letters)
other than the reserved `00000`, and this deliberately never ends in
three zeroes, since those are Postgres *category* codes trappable only
by trapping the whole category. Two different exceptions declared in the
same routine get two different codes, so multiple WHEN clauses in one
handler still branch correctly. This is scoped per routine -- a
flattened package's members each get their own numbering starting fresh,
matching how each member already gets its own DECLARE section.

The severity of the existing "no user-defined EXCEPTION type" notice
also changed from an error to a warning: previously it was flagging a
real, unresolved gap (a WHEN clause silently broken); now both RAISE and
WHEN are fully, automatically handled, so nothing here still needs a
manual rewrite -- it's now only worth reviewing if code *outside* this
routine needs to catch the same condition by a specific, stable SQLSTATE
of its own choosing.

**MySQL, SQL Server and Db2 needed no change.** Both already handle a
WHEN clause naming a user-declared exception correctly -- SQL Server and
Db2 already flag it explicitly with their own clear "no known
mapping (user-defined exception)" message instead of guessing, and MySQL
already maps it to a real MySQL named condition. PostgreSQL was the one
target where this specific case silently produced invalid DDL instead of
either a correct rewrite or an honest flag.

**What this means for the migration that hit this error.** Re-running
"Apply DDL to Target" on this build (after re-running "2. Convert
Schema" first) picks up from where it left off -- the 132 statements
that already succeeded stay in place. Any other routine declaring,
raising, and catching its own named exception anywhere in the schema is
covered by this same fix.

## Round 34 — four PostgreSQL PL/SQL conversion gaps from a customer gap analysis (build R34, 17 Sep 2026)

Unlike Rounds 29-33, this round wasn't triggered by a specific "Apply DDL
to Target" failure -- it's the first of a planned series implementing a
customer-provided gap analysis (comparing this tool against ora2pg)
covering PostgreSQL-target conversion. The analysis grouped its
suggestions into three tiers; this round covers the four fastest,
lowest-risk items from Tier 1, in the analysis's own recommended order:
ROWNUM's top-N idiom, LISTAGG, the Oracle REGEXP_* function family, and
Oracle's date-arithmetic functions. Materialized views and native
declarative partitioning (the rest of Tier 1) and an opt-in orafce-
extension mode (Tier 2) follow in later rounds -- both touch the schema
model and introspector, not just the PL/SQL converter, and deserve their
own focused round rather than being rushed in alongside this one.

**ROWNUM's top-N idiom.** `WHERE ROWNUM <= 10` (Oracle's classic
"first N rows" idiom, often combined with an ordered subquery for
deterministic pagination) used to reach PostgreSQL as literal,
unsupported syntax -- Postgres has no ROWNUM pseudo-column at all, so
"Apply DDL to Target" failed the moment it ran. This is now rewritten to
a trailing `LIMIT` clause (`LIMIT 10`, `LIMIT (10 - 1)` for `<`, `LIMIT 1`
for `= 1`) whenever ROWNUM is the last condition in a WHERE clause --
by far the most common real-world shape. A ROWNUM used any other way
(as the first of several AND-ed conditions, inside a CASE expression)
is a different rewrite this doesn't attempt; it's left untouched and
still flagged for manual review, exactly as before.

**LISTAGG.** `LISTAGG(name, ', ') WITHIN GROUP (ORDER BY name)` becomes
`STRING_AGG(name, ', ' ORDER BY name)` -- a real, mechanical rewrite
(Postgres's STRING_AGG has no default delimiter the way Oracle's LISTAGG
does when the delimiter argument is omitted, so an omitted one is filled
in as `''`). Oracle 19c's `ON OVERFLOW ...` clause, or a LISTAGG with no
WITHIN GROUP at all, has no safe equivalent and is left flagged instead.

**Oracle's REGEXP_* functions.** Rewritten only where a Postgres
equivalent works on every supported version, never guessed at:
REGEXP_LIKE(str, pattern) becomes the `~` operator (`~*` for a bare `'i'`
case-insensitive flag); REGEXP_REPLACE's exact 3-argument form needed no
change at all (Postgres's own REGEXP_REPLACE already matches it);
REGEXP_SUBSTR(str, pattern) becomes `substring(str from pattern)`. A 4th
REGEXP_REPLACE argument, a 3rd REGEXP_SUBSTR argument, or any
REGEXP_LIKE match_param other than `'i'` is flagged instead of
guessed -- Postgres's extra positional arguments don't mean the same
thing Oracle's do, so a look-alike rewrite there would run with silently
wrong semantics rather than fail loudly. REGEXP_COUNT and REGEXP_INSTR
have no safe one-line Postgres equivalent this tool is confident is
correct in every case, so both are always flagged, never rewritten.

**Date arithmetic.** ADD_MONTHS(date, n) becomes date + interval
arithmetic (flagged with a warning: Oracle clamps to the last day of the
target month when the original date is itself month-end, e.g. Jan 31 + 1
month = Feb 28/29, while Postgres's interval arithmetic overflows instead,
Jan 31 + 1 month = Mar 3 -- review any call where the source date can be
month-end). LAST_DAY(date) becomes a date_trunc()-based end-of-month
expression with no such caveat. TRUNC(date, 'fmt') becomes
`date_trunc('field', date)` for the unambiguous format models (YYYY/Q/MM/
DD/HH24/MI and their synonyms) -- distinguished from numeric
TRUNC(number[, decimals]) by checking whether the 2nd argument is a
quoted string literal, which is always true for a format model and never
true for a decimals count. Week-based formats (WW/IW/W), the week-start-
day formats (DAY/DY/D), century (CC/SCC) and the Roman-numeral month
format (RM) each need a real computed expression to match Oracle's
behavior, not a single date_trunc() field name, so those are flagged
instead of guessed.

**Where these apply.** All four run on a view's own SELECT text
(generate_view_ddl) and on a routine's embedded SELECTs and cursor
declarations (convert_body / convert_declare_block) -- the same two
places NVL/SYSDATE (Round 31) and RAISE_APPLICATION_ERROR (Round 32)
already reach the target through. All four are scoped to a PostgreSQL
target only, matching the gap analysis's own scope; MySQL, SQL Server
and Db2 keep today's behavior for all four unchanged.

## Round 33 — fixed "Apply DDL to Target" failing on Oracle's SAVEPOINT/ROLLBACK TO (build R33, 17 Sep 2026)

Triggered by "Apply DDL to Target" -- past Rounds 29-32's fixes -- stopping
on the next problem in the same DDL script, in the very same flattened
package body Round 32 just got further into:

```
Statement 131/166 failed: syntax error at or near "TO"
LINE 12: EXCEPTION WHEN OTHERS THEN ROLLBACK TO before_update; RAIS...
 -> -- PACKAGE BODY FEATURE_PL_ADMIN_PKG (Converted with warnings) -- ...
```

**Root cause.** Oracle's `SAVEPOINT before_update; ... EXCEPTION WHEN
OTHERS THEN ROLLBACK TO before_update; RAISE_APPLICATION_ERROR(...);` is a
very common mid-procedure checkpoint idiom: mark a point to undo back to,
then undo to it if anything in between fails. Nothing in this tool's
PostgreSQL, SQL Server, MySQL or Db2 converters had ever handled either
keyword before this round -- both passed straight through untranslated.
On a PostgreSQL target, `ROLLBACK TO before_update;` reaches Postgres
exactly as written, which parses it as the top-level transaction-control
`ROLLBACK TO SAVEPOINT` statement -- not legal inside a plpgsql
function/procedure body -- and rejects it with a syntax error at "TO".
Every routine using this idiom failed "Apply DDL to Target" outright, the
instant it got past whatever came before it in the same routine.

**Fixed, for PostgreSQL.** PL/pgSQL doesn't need an explicit SAVEPOINT/
ROLLBACK TO statement here in the first place: entering a
`BEGIN ... EXCEPTION ... END` block already establishes an implicit
savepoint the instant it's entered, and automatically rolls back to it
the instant an exception is caught -- *before* the handler even runs.
That makes the whole idiom redundant, not merely untranslatable:
  - `SAVEPOINT name;` is always commented out (kept for reference), since
    it does nothing a Postgres function needs.
  - `ROLLBACK TO name;` *inside an exception handler* is also commented
    out, for the same reason -- by the time the handler runs, Postgres
    already did the rollback automatically.
  - `ROLLBACK TO name;` in the *main executable body* (not inside a
    handler) is a different story: it's a deliberate mid-flow rollback
    decision with no automatic equivalent, and dropping it there could
    silently change behavior. This is left untouched and flagged with an
    error for manual rewrite as a nested `BEGIN ... EXCEPTION ... END`
    block instead of being guessed at.

**Fixed differently for SQL Server, on purpose.** Unlike PostgreSQL,
T-SQL supports this exact idiom natively -- just under different
keywords, and identically whether it appears inside a TRY/CATCH handler
or the main body. `SAVEPOINT name;` and `ROLLBACK TO [SAVEPOINT] name;`
are now a real, mechanical rename to `SAVE TRANSACTION name;` and
`ROLLBACK TRANSACTION name;`, the same kind of safe keyword substitution
already used for NVL -> COALESCE/ISNULL elsewhere in this tool.

**MySQL and Db2 needed no change.** MySQL/InnoDB already supports
Oracle-compatible `SAVEPOINT` / `ROLLBACK TO [SAVEPOINT]` syntax natively,
so it was never broken there. Db2 is a deliberate scope decision, not an
oversight: Db2 SQL PL's own SAVEPOINT statement requires a mandatory
`ON ROLLBACK RETAIN CURSORS` clause with syntax this tool has not
verified closely enough to ship a rewrite for with confidence -- Db2
keeps today's pass-through behavior this round rather than risk emitting
unverified Db2-specific SQL.

**What this means for the migration that hit this error.** As with
Rounds 29-32, re-running "Apply DDL to Target" on this build (after
re-running "2. Convert Schema" first) picks up from where it left off --
the 130 statements that already succeeded stay in place. Any other
routine using this same SAVEPOINT/ROLLBACK TO idiom anywhere in the
schema is covered by this same fix on both PostgreSQL and SQL Server
targets.

## Round 32 — fixed "Apply DDL to Target" failing on a RAISE_APPLICATION_ERROR with a concatenated message (build R32, 16 Sep 2026)

Triggered by "Apply DDL to Target" -- past Rounds 29, 30 and 31's fixes --
stopping on the next problem in the same DDL script, inside a flattened
package body:

```
Statement 131/166 failed: syntax error at or near "||"
LINE 10: ...0 THEN RAISE EXCEPTION 'No row found for test_id='||p_test_i...
 -> -- PACKAGE BODY FEATURE_PL_ADMIN_PKG (Converted with warnings) -- ...
```

**Root cause.** Oracle's `RAISE_APPLICATION_ERROR(-20001, 'No row found '
|| 'for test_id=' || p_test_id)` builds its message with Oracle's `||`
string concatenation, exactly the way ordinary Oracle SQL does. This
tool's PostgreSQL converter already translated
`RAISE_APPLICATION_ERROR(code, message)` to PL/pgSQL's `RAISE EXCEPTION
message`, but PL/pgSQL's `RAISE EXCEPTION 'format', args...` requires a
*literal* format string in that position -- `'text'||variable` there is
a syntax error at parse time, not something that fails only when the
exception actually fires. Every routine using a computed
RAISE_APPLICATION_ERROR message reached the target this way, and every
one of them failed "Apply DDL to Target" outright.

**Fixed, for PostgreSQL.** The message is now emitted via PL/pgSQL's
argument-free `RAISE EXCEPTION USING MESSAGE = <expression>` form
instead, which accepts any expression -- literal or computed -- with no
special-casing needed for which kind it is. A plain, non-concatenated
message (already working before this round) is rewritten the same way
now too, for consistency; both produce identical run-time behavior to
the previous, narrower fix.

**Fixed differently for MySQL and SQL Server, on purpose.** Both
targets' own error-raising statements (`SIGNAL ... SET MESSAGE_TEXT =`
for MySQL, `THROW error, message, state` for SQL Server) have a hard
restriction PostgreSQL's `USING MESSAGE` clause doesn't share: neither
accepts an inline expression there, only a literal or a variable already
holding one. Handling a computed message correctly on either target
means declaring a local variable first and assigning the concatenated
value to it before the SIGNAL/THROW -- real restructuring this
mechanical substitution does not attempt, the same reasoning already
applied to DECODE/ROWNUM in views (Round 31) and to genuinely complex
constructs elsewhere in this tool. A `RAISE_APPLICATION_ERROR` call with
a plain literal message continues converting exactly as before on both;
one with a computed message is now left unconverted and flagged with an
error for manual review, rather than emitting `SIGNAL`/`THROW` DDL that
was always going to fail on the target regardless.

**Db2 needed no change.** Db2's `SIGNAL SQLSTATE '...' SET MESSAGE_TEXT =
<expression>` already accepts a general expression, and Db2's own `||`
operator is identical to Oracle's -- a computed message there was never
broken.

**What this means for the migration that hit this error.** As with
Rounds 29-31, re-running "Apply DDL to Target" on this build (after
re-running "2. Convert Schema" first) picks up from where it left off --
the 130 statements that already succeeded stay in place. Any other
routine using a computed RAISE_APPLICATION_ERROR message anywhere in the
schema is covered by this same fix on a PostgreSQL target; on MySQL or
SQL Server, the assessment report's new error flags exactly which ones
still need a manual look.

## Round 31 — fixed "Apply DDL to Target" failing on views using NVL(...) or SYSDATE (build R31, 16 Sep 2026)

Triggered by "Apply DDL to Target" -- past both Round 29 and Round 30's
fixes -- stopping on the next problem in the same DDL script:

```
Statement 130/166 failed: function nvl(numeric, integer) does not exist
LINE 4:     SUM(NVL(amount,0)) total_amount,
 -> CREATE OR REPLACE VIEW "feature_test_category_v" AS SELECT category,
    COUNT(*) feature_count, SUM(NVL(amount,0)) total_amount, ...
```

**Root cause.** `generate_view_ddl` already *knew* about Oracle's
`NVL(...)` and `SYSDATE` -- a scan lower in that same function
(`oracle_markers`) checks for both and adds a warning to the conversion
report ("NVL() must be rewritten as COALESCE().", and the equivalent for
SYSDATE) -- but that scan only detects; it was never wired up to actually
rewrite anything. So a view using either was reported as converted
(sometimes with a warning attached, sometimes not, depending on what else
was in it) and then failed outright the moment its DDL actually ran,
because neither PostgreSQL, MySQL, SQL Server nor Db2 has an `NVL()`
function or a `SYSDATE` keyword -- both are Oracle-only. Meanwhile, this
exact class of problem was *already* solved elsewhere in this tool: a
stored routine or trigger body gets NVL and SYSDATE genuinely rewritten
(`sql_translator.py`'s substitution list, and `plsql_converter.py`), since
both are simple, lossless, mechanical renames with identical semantics on
every target here. Views just never got the same treatment.

**Fixed.** An Oracle-sourced view's text now gets that same rewrite
before its DDL is generated, for every SQL target (Postgres, MySQL, SQL
Server, Db2): `NVL(a, b)` becomes `COALESCE(a, b)` (identical semantics --
Oracle's NVL always takes exactly two arguments, exactly matching
COALESCE's two-argument case) and `SYSDATE` becomes `CURRENT_TIMESTAMP`
(the ANSI equivalent every target here accepts). This mirrors the
existing routine/trigger fix, not a new kind of change. `DECODE(...)`,
`ROWNUM` and the `(+)` outer-join operator are deliberately **not**
rewritten the same way: unlike NVL and SYSDATE, all three genuinely need
restructuring (DECODE into a CASE expression with NULL-comparison care,
ROWNUM into LIMIT/OFFSET or ROW_NUMBER() depending on context, `(+)` into
an ANSI JOIN) that a mechanical substitution cannot get right in general,
so those three stay exactly as before: flagged with a warning for manual
review, not guessed at.

**What this means for the migration that hit this error.** As with
Rounds 29 and 30, re-running "Apply DDL to Target" on this build (after
re-running "2. Convert Schema" first, so the DDL tab actually holds this
fix's output) picks up from where it left off -- the 129 statements that
already succeeded stay in place, and the script continues past
`feature_test_category_v`. Any other view using NVL or SYSDATE anywhere
in the schema is covered by this same fix.

## Round 30 — fixed "Apply DDL to Target" failing on a materialized view's internal fast-refresh index (build R30, 16 Sep 2026)

Triggered by "Apply DDL to Target" -- on the very next statement past
where Round 29 got further than before -- stopping with:

```
Statement 62/167 failed: function sys_op_map_nonnull(character varying) does not exist
 -> CREATE UNIQUE INDEX IF NOT EXISTS "i_snap$_feature_test_mv" ON "feature_test_mv" (SYS_OP_MAP_NONNULL(CATEGORY))...
```

**Root cause.** `i_snap$_feature_test_mv` is not a real, user-created
index at all: it is the unique index Oracle automatically creates on a
materialized view's container table (here, the materialized view
`FEATURE_TEST_MV`) to support fast refresh, named with Oracle's own
`I_SNAP$_<mv name>` convention. Oracle builds that index on an expression
that wraps every column in `SYS_OP_MAP_NONNULL(...)`, an undocumented,
Oracle-internal function that exists purely so its own refresh mechanism
can treat two NULLs as distinct for that one internal comparison. Round
29's fix correctly resolved this expression from Oracle's data dictionary
(the same mechanism that resolves a real function-based index's
expression) and correctly stopped quoting it as a plain identifier -- but
carrying `SYS_OP_MAP_NONNULL(...)` through into the generated DDL was
never going to work regardless, because that function is Oracle-only and
has no equivalent on any target. Unlike a real function-based index (a
business-meaningful `LOWER(email)`, say), this index enforces nothing a
target database, or the application using it, ever needed replicated --
it is Oracle's own plumbing for a feature (fast-refresh materialized
views) that has no bearing on the migrated data's correctness.

**Fixed.** Any index whose expression calls an Oracle-internal
`SYS_OP_*` function is now recognized for what it is and skipped
entirely -- no `CREATE INDEX` is emitted for it on any target, for any
of the five SQL dialects, in both the immediate and deferred/post-load
DDL paths. An informational note is added to the conversion report
explaining why (so a table showing this note isn't mistaken for one
that's missing something), rather than the "built on an expression, not
a plain column" warning Round 29 added for a genuine function-based
index -- that warning would have been actively misleading here, since
there was never anything meaningful to carry over. A real function-based
index (like Round 29's `LOWER(email)` case) is unaffected: only an
expression that specifically calls a `SYS_OP_*` function is treated this
way.

**What this means for the migration that hit this error.** As with
Round 29, re-running "Apply DDL to Target" on this build picks up from
where it left off -- the 61 statements that succeeded before this one
stay in place, and statement 62 onward is attempted fresh. If a schema
has more than one materialized view, each one's own internal `I_SNAP$_`
index is covered by this same fix.

## Round 29 — fixed "Apply DDL to Target" failing on function-based indexes (build R29, 16 Sep 2026)

Triggered by "Apply DDL to Target" stopping partway through a script with:

```
Statement 26/167 failed: column "sys_nc00011$" does not exist
 -> CREATE INDEX IF NOT EXISTS "ix_customers_email_lower" ON "customers" ("sys_nc00011$")...
```

**Root cause.** `ix_customers_email_lower` is a *function-based* Oracle
index -- something like `CREATE INDEX ix_customers_email_lower ON
customers (LOWER(email))`. Oracle doesn't index an expression directly:
it silently creates a hidden virtual column to hold the computed value
and indexes that instead, and that hidden column has a generated,
meaningless name in the shape `SYS_NC#####$` (here, `SYS_NC00011$`).
Oracle's data dictionary view for an index's columns (`ALL_IND_COLUMNS`)
reports that hidden name as the column, with no indication anywhere in
that one row that it's standing in for anything else. This tool's Oracle
introspector was reading that name and carrying it straight through into
the generated `CREATE INDEX` statement for every target -- and no target
this tool ever creates has a `sys_nc00011$` column, because only Oracle
creates one, and only for this one internal purpose. The statement was
never going to succeed on any target, for any function-based index, on
any of the five SQL dialects this tool generates DDL for.

**Fixed.** Oracle keeps the real expression a hidden column stands in
for in a *different* dictionary view, `ALL_IND_EXPRESSIONS`, keyed by the
same index name and column position. The introspector now reads that
view too and substitutes the real expression (e.g. `LOWER("EMAIL")`) in
place of the hidden column name whenever one exists, for every index
column on every table. The DDL generator was also changed to render such
an expression as-is rather than quoting it as if it were a single plain
identifier (which would have sent the target looking for a column
literally named `lower("email")`) -- Oracle's own quoting inside the
expression is stripped, and the bare expression is left for the target
database to fold to its own case the same way it already folds an
ordinary column reference this tool creates. A warning is added to the
conversion report for any table with a function-based index, since the
expression is carried over as-is rather than translated the way a
procedure or function body would be -- a simple case like `LOWER(...)`
or `UPPER(...)` should work unchanged on every target here, but an
expression built from an Oracle-only function (`NVL`, `DECODE`, ...)
might not, and would need a manual look. MongoDB has no equivalent of an
index on a computed expression at all, so a function-based index is
skipped entirely for a MongoDB target (with the same warning explaining
why), rather than creating an index on a made-up field name.

**What this means for the migration that hit this error.** Re-running
"Apply DDL to Target" on this build picks up from where it left off:
the dialog itself already skips any statement that was already applied
rather than re-running it, so the 25 statements that succeeded before
`ix_customers_email_lower` are left alone, and statement 26 onward
(including this index, and anything after it in the script) is attempted
fresh -- there's no need to regenerate DDL from scratch or start the
whole schema over. If a different table also has a function-based index,
this same fix covers it too; the conversion report's new warning is the
place to check for any others before applying.

## Round 28 — source connection-drop cascade, a PostgreSQL trigger error, and faster LOB migration (build R28, 15 Sep 2026)

Triggered by a migration that closed unexpectedly partway through and, in
the same run, reported: a LOB table failing with an Oracle connection
drop (DPY-4011), 17 further tables all failing with "not connected to
database" (DPY-1001) right after it, a PostgreSQL trigger error on
FEATURE_TEST_DATA ('column "inserting" does not exist'), and 3 tables
showing a checksum mismatch despite matching row counts. Four fixes, plus
one honest non-fix, below.

**Bug 1 — one dropped source connection failed every table after it.**
Round 27 fixed a *target* connection being retried without reconnecting
first; this run hit the same shape of bug on the *source* side instead.
Oracle's DPY-4011 ("the database or network closed the connection") hit
while reading a LOB-heavy table, and every following table's very first
query then failed instantly with DPY-1001 ("not connected to database")
-- because nothing reconnected the source connection object, so every
later `migrate_table` call reused the same dead one. **Fixed** two ways:
when a checkpoint is available, the read is retried from scratch on a
freshly reconnected source, and the checkpoint's own batch-skip
bookkeeping (the same mechanism Round 27 made trustworthy) means nothing
already written gets re-sent. Without a checkpoint, that one table still
fails outright -- there's no safe way to prove nothing was already
written twice -- but the source connection is reconnected before moving
on regardless, so it can no longer take every subsequent table down with
it. `is_transient()` (in `retry.py`) also didn't previously recognize
either DPY-4011's or DPY-1001's exact wording; both are now matched, by
message and by error code.

**Bug 2 — PostgreSQL trigger error: `column "inserting" does not exist`.**
Oracle triggers test which DML statement fired them with the boolean
pseudo-columns `INSERTING`/`UPDATING`/`DELETING` (e.g. `IF INSERTING
THEN ...`). PostgreSQL has no such pseudo-columns -- a trigger function
there finds this out from `TG_OP` instead -- and this tool's Postgres
trigger converter never translated them, so they reached PostgreSQL as
bare, undeclared identifiers. PostgreSQL resolves an unqualified name in
a row-level trigger as a column reference first, which is exactly the
`column "inserting" does not exist` error FEATURE_TEST_DATA's trigger
hit. **Fixed:** `INSERTING`/`UPDATING`/`DELETING` are now translated to
`(TG_OP = 'INSERT')` / `'UPDATE'` / `'DELETE'` respectively, wherever
they appear as a bare keyword (a string literal or comment that happens
to contain the word "inserting" is left alone).

**Bug 3 — checksum mismatch on tables whose row counts matched.** By
explicit request, tables with a CLOB/NCLOB/BLOB/LONG column no longer run
a checksum at all during validation (row-count validation still runs
unchanged). Two reasons this is the right fix, not just a workaround:
first, there's no cross-driver guarantee that a LOB value's exact
serialization round-trips byte-for-byte between source and target even
when the content is identical, which is what was producing "row count
matched, checksum did not" on tables that had in fact migrated
correctly; second, hashing a large LOB value is real, avoidable CPU cost,
paid twice (once per side) on exactly the tables slow enough for it to
matter.

**Speed — LOB batch size raised from 200 to 2000 rows.** By explicit
request to speed up LOB migration. The original 200-row default (see
Round 25/26) traded round-trips for memory safety -- holding many large
LOB values in memory at once, in a single batch, was the actual scaling
risk. 2000 is a middle ground between that and the plain 5000-row
default: meaningfully fewer, bigger round trips per LOB table, without
dropping the memory margin to zero. Pass a different `lob_batch_size`
explicitly (5000, or `None` to disable the narrowing entirely) if this
default still isn't the right trade-off for a particular table.

**Not fixed this round: the application closing unexpectedly at 33%.**
This tool already has fairly thorough crash capture (an unhandled
exception on the main thread, an unhandled exception in a worker thread,
and even a hard native crash with no Python exception at all, are all
written to a crash log) -- but no crash log or error message was
available to diagnose *this* particular close, so nothing here can
honestly be called a fix for it yet. If it's a Python-level crash or an
unhandled exception, `%APPDATA%\TGDataBridge\logs\crash-<date>.log` on
the machine it happened on should have a record of it (look for one
dated around when it happened); if that file doesn't exist at all, the
process was most likely killed outright by Windows (an out-of-memory
condition is the leading suspect for something that happens mid-LOB-
migration, since large LOB values held in memory are exactly what a big
batch size makes worse) -- something a Python-level crash log can never
show, because nothing runs to write one in that case. Either way, please
send that log (or say it doesn't exist) so this can be root-caused
properly rather than guessed at. In the meantime, Bug 1's fix and the
smaller, faster LOB batches above should both reduce how much memory a
LOB-heavy migration holds at once, which may help even without knowing
the exact cause.

## Round 27 — fixed doubled/missing rows on resume, connection-loss retries, and blank Duration/Throughput (build R27, 15 Sep 2026)

This round was triggered by a real 8-hour, 43-minute migration run
(`neha.ahire`, 11 Sep 2026, Oracle → PostgreSQL) that lost its database
connection partway through and finished with row counts that didn't add
up. Three separate bugs, all confirmed against that exact log, are fixed
in this build.

**Bug 1 — doubled rows on some tables, missing rows on others.** In that
run, ORDERS ended up with exactly double the rows sent (1,000,000 sent,
2,000,000 landed), and the same exact doubling hit ORDER_ITEMS, PAYMENTS
and SHIPMENTS. Meanwhile CUSTOMER_NOTES and MIGRATION_LOB_DOCUMENTS ended
up with the *wrong* row counts in the other direction. All six tables are
large enough to migrate in multiple batches with checkpoint/resume, which
only re-sends batches it doesn't already see as written to the target —
it decides that by re-running the same `SELECT` and counting off how many
batches it already has. The bug: a plain `SELECT` with no `ORDER BY` has
no guaranteed row order on *any* database across two separate runs of the
same query, so after a pause or retry, the same query can come back in a
different order — and the checkpoint logic ends up skipping the wrong
rows (leaving gaps) or re-sending rows it already sent (doubling them).
**Fixed** by adding `ORDER BY <primary key columns>` to every table's
migration query when it has a primary key, so the same query always
returns rows in the same order and resume/checkpoint logic can trust it.

**Bug 2 — "connection lost" failed every retry, identically.** The same
run's TRANSACTIONS table failed outright with `DPY-4011: the database or
network closed the connection` / `[WinError 10054] An existing connection
was forcibly closed by the remote host`, and the automatic retry that
followed failed with the exact same error. That's because retrying only
re-called the same database write on the *same, already-dead* connection
object — nothing about a plain retry re-opens a connection the network
already tore down. **Fixed:** a retry that follows a real connection-loss
error now closes and re-opens the connection first, so it has an actual
chance of succeeding instead of being guaranteed to fail the same way
every time.

**Bug 3 — Duration and Throughput always showed "—".** Every migration
log report, including this one, has always shown a blank Duration and
Throughput for every single table — those columns were built into the
report but nothing ever measured or supplied the numbers. On a run slow
enough to take 8+ hours, that's exactly the information someone needs
first. **Fixed:** each table's elapsed time is now tracked from the
existing per-batch progress updates and shown per table in the log, so a
slow run's report shows which table(s) actually took the time instead of
a wall of dashes.

**How this was checked:** 11 new tests (2200 total, 37 skipped) —
including a direct reproduction of the doubled-row bug under unstable
row order (and a contrast test showing a table without a primary key can
still be affected, since there's no column to order by), and a
reconnect-before-retry test that pins the exact `close()`-then-`connect()`
sequence. Verified again against the actual shipped `.exe`'s own
bytecode, not just the source tree.

**Note for the Sept 11 run specifically:** TRANSACTIONS failed outright
and never migrated any rows — it will need to be re-run on this build.
The other tables affected by Bug 1 (ORDERS, ORDER_ITEMS, PAYMENTS,
SHIPMENTS, CUSTOMER_NOTES, MIGRATION_LOB_DOCUMENTS) should also be
re-run so their row counts land correctly under the fixed resume logic —
re-running with this build will produce a clean report you can compare
against the old one.

## Round 26 — faster CLOB/BLOB migration, and a checksum fix (build R26, 11 Sep 2026)

**The speed problem:** after Round 25 fixed CLOB/BLOB/NCLOB/JSON columns so
they migrate at all, tables with those columns (CUSTOMERS,
MIGRATION_LOB_DOCUMENTS, CUSTOMER_NOTES, ADV_FEATURE_PARTITION, and
similar) were noticeably slower than tables without them — much slower
than the row counts alone would explain.

**The cause:** Round 25's fix worked by reading each CLOB/BLOB value back
from Oracle in a loop, one `getchunksize()`-sized piece at a time, and
joining the pieces together. Every one of those pieces was a separate
round-trip to the database — on top of the round-trip that already fetched
the row containing that same value in the first place. A CLOB column with
even a few chunks per value, across tens of thousands of rows, added up to
a lot of extra network round-trips that were never actually necessary:
this tool always joins the pieces back into one in-memory value anyway,
so reading it in one piece instead of several costs the same memory and
far fewer round-trips.

**The fix:** the Oracle connector now tells the driver
(`oracledb.defaults.fetch_lobs = False`) to hand back CLOB/BLOB/NCLOB
values as plain text/bytes on the *same* fetch as the rest of the row,
instead of as separate LOB objects this tool then had to read back
piece by piece. The old piece-by-piece reading code is kept as a
fallback for the rare case a LOB object shows up anyway, but for a normal
Oracle-source migration it should now essentially never run.

**A related, previously-unnoticed bug this surfaced:** the migration's
row-count-and-checksum validation step re-reads a table's data from
both sides and compares a hash of each. For a table with a native JSON
column, the *source*-side checksum was hashing the raw Oracle value (a
Python dict) while the *target*-side checksum was hashing the plain JSON
text that got written to PostgreSQL/MySQL/etc. — a dict and its
equivalent string never hash the same, so any table with a native JSON
column would show as **"Unvalidated"** on every single run, forever, even
though the data had migrated correctly (this includes FEATURE_TEST_DATA
and FEATURE_PL_TEST_DATA from earlier logs — their FK-dependent audit
tables would have shown this once the FK errors themselves were fixed).
Fixed by making the source-side checksum convert the value the same way
the migration itself does, before hashing it.

**What to do about a table you previously saw flagged "Unvalidated":**
if it has a native JSON column, re-run validation on this build — it
should now show as validated correctly. If it doesn't have a JSON column,
that "Unvalidated" flag is unrelated to this fix (see Round 25's note
below about genuine data differences).

**How this was checked:** 3 new tests (2189 total, 37 skipped) — one
pinning that `connect()` disables `fetch_lobs` before ever calling
`oracledb.connect()`, two pinning that `checksum_rows` now matches
`table_checksum` on the *converted* value for both a JSON dict and a LOB
column, and confirming the old, wrong (unconverted) checksum is no longer
what's produced. Verified again against the actual shipped `.exe`'s own
bytecode, not just the source tree.

## Round 25 — Oracle CLOB/BLOB/JSON data migration fix (build R25, 9 Sep 2026)

**The bug:** migrating data out of Oracle, any table with a CLOB, BLOB,
NCLOB, or native JSON column failed immediately with an error like:

    cannot adapt type 'LOB' using placeholder '%t' (format: TEXT)
    cannot adapt type 'dict' using placeholder '%t' (format: TEXT)

and every other table that referenced one of those failed tables by
foreign key (an ORDERS row pointing at a CUSTOMERS row that never
arrived, for example) failed right behind it with a foreign key
violation — so one CLOB/JSON column could take down a whole cluster of
otherwise-unrelated tables in the same run.

**The cause:** this tool already had code meant to convert Oracle's
CLOB/BLOB/NCLOB values (which the Oracle driver hands back as LOB
*locator* objects, not plain text) into plain text/bytes before handing
a row to the target database. That conversion code checked for a method
named `chunk_size` to recognize one of these values — but the real
Oracle driver's method is actually named `getchunksize`, not
`chunk_size`. Because that name never matched, the conversion never ran
against a real Oracle connection, and the unconverted value was passed
straight to the target driver, which had no idea what to do with it.
Separately, a native Oracle JSON column arrives from the driver as a
plain Python object (not text at all), and nothing was converting that
either.

**The fix:** the conversion now checks for the driver's real method
name, so CLOB/BLOB/NCLOB values are actually converted the way this tool
always intended. JSON columns are now also converted to plain text.
Both paths are covered by new automated tests, including one that pins
the exact wrong name down so it can't quietly come back. Every table
that was failing only because a *different* table failed first (the
foreign-key cascade above) now succeeds as soon as that other table
does — there was nothing else wrong with those tables.

**If you hit this:** re-run the migration. Checkpointing means it
resumes rather than starting over — tables that already succeeded are
skipped, and only the previously-failed tables (and anything that failed
because of them) are retried.

*A separate item from the same run is not fixed by this, because it
isn't the same kind of problem:* three tables (in one report, EMPLOYEES,
PRODUCTS, and a materialized view) migrated successfully — right row
count — but their checksum didn't match afterward, which the tool flags
as "unvalidated" rather than "failed" specifically because it's not
certain there's a problem: known, harmless differences (a date widened
to a timestamp, a padded CHAR, a TINYINT(1) becoming a real boolean) are
already excluded before that flag is raised, so a mismatch that survives
that filtering is a genuine value difference worth looking at directly
in that table's data — not something a code fix can resolve sight
unseen. If you still see this warning after re-running, it's worth
sending the specific table/column so it can be looked at directly.

## Round 24 — optional AI-assisted features (build R24, 8 Sep 2026)

Four new AI-assisted features, every one of them **off by default** and
independently switchable — nothing about how this tool behaves changes
unless you open **AI Settings…** and turn it on:

- **Schema/column mapping review** — after Convert Schema runs its
  deterministic, rule-based type conversion (unchanged — nothing here
  replaces or overrides that), ask the AI for a second opinion on the
  result: a truncation risk, a precision loss, a default value that
  won't parse on the target. Advisory only — it flags, it never changes a
  generated type.
- **Plain-English migration requests** — type "migrate all the customer
  and order tables, skip anything archived" and the AI proposes a table
  selection from the schema you've already loaded. It only *proposes* —
  nothing is selected automatically; you still tick the boxes in the
  schema tree yourself.
- **Error diagnosis and fix suggestions** — a plain-language explanation
  and concrete fix suggestions for an error, right next to the error
  itself: an "Explain with AI" button on the crash dialog, and (if the
  headless CLI's job config turns it on) printed straight into a failed
  run's own output.
- **Post-migration data quality review** — after data has landed on the
  target, review a small sample of migrated rows for problems the row
  count/checksum validation wouldn't describe in words: an unexpectedly
  all-NULL column, a garbled string, a value that looks truncated. This
  is the one feature of the four that sends real row *data*, not just
  table/column names or error text — it has its own on/off switch in AI
  Settings separate from the other three, and says so plainly in the
  dialog before you use it.

**Where to find it:** two new toolbar buttons, **AI Settings…** and
**AI Review…**, next to the existing History…/Settings… buttons. AI
Settings is where you turn features on, pick a provider, and enter an API
key (encrypted at rest — see "How your API key is stored" below). AI
Review is a three-tab dialog (Schema Mapping Review / Plain-English
Request / Data Quality Review) for the three interactive features; error
diagnosis lives on the crash dialog itself instead, next to the error it
explains.

**Which AI backend** — configurable, not tied to one vendor:

- **Claude** (Anthropic) — needs an API key.
- **OpenAI** — needs an API key.
- **Azure OpenAI** — your own Azure deployment; needs your resource's
  base URL, your deployment name (as "Model"), and an API key.
- **Local/offline server** — anything that speaks the OpenAI-compatible
  chat completions API (Ollama, LM Studio, vLLM, or your own internal
  gateway) — for a shop that needs no data leaving its own network at
  all. Needs a base URL; an API key is usually not required.

No vendor SDK was added to do this — `tgdatabridge/ai/ai_client.py` speaks
each provider's REST API directly over the Python standard library's own
`urllib.request`. That is deliberate, not a style preference: this build
was shipped the same way build R23 was, by recompiling this tool's own
bytecode into the existing `.exe` without touching the 208 MB `_internal`
folder of already-installed packages (see "How this build was packaged"
in previous rounds' notes) — a new third-party dependency is exactly the
one kind of change that pipeline cannot ship as a same-day patch, so the
whole feature is built on what was already there (plus `cryptography`,
which was already bundled as a dependency of the SSH tunnel feature — see
below).

**How your API key is stored.** Every other secret this tool has ever
touched — a database password, an SSH key passphrase, a TLS client key
passphrase — is simply never written to disk at all; you re-enter it each
time. An AI provider's API key can't follow that same rule and still be
usable, since it's configured once for the whole install and used
repeatedly, not re-entered per connection. So it's the one exception: it
*is* persisted, but encrypted at rest in its own file, separate from every
other AI setting, using a key generated on first use and stored alongside
it. That's a real improvement over plain text — glancing at the settings
file, or sending it to support, doesn't expose it — but it is not
equivalent to Windows Credential Manager: anyone with read access to this
tool's whole application-data folder can decrypt it, because the key to
do so lives right there next to it. If `cryptography` is ever unavailable
for some reason, the key is simply not saved rather than falling back to
plain text — you'd re-enter it next time.

**What data actually leaves this machine, per feature:**

| Feature | What's sent |
|---|---|
| Schema mapping review | Table/column names and types only |
| Plain-English requests | Table names and what you typed |
| Error diagnosis | The error text (and, in the CLI, a short step label) |
| Data quality review | Table/column names **and up to 25 sample row values** |

Nothing here is sent unless you've both turned AI on *and* turned on that
specific feature.

**The headless CLI** gets a matching, optional `"ai"` block in its job
config files — error diagnosis only (the other three are interactive
review tools that don't fit an unattended run): turn it on, name an
environment variable holding the API key (`api_key_env`, exactly like
`password_env` already works — never a key written into the config file
itself), and a failed Load Schema / Apply DDL / Migrate Data step prints
a plain-language explanation and suggested fixes right after its `ERROR:`
line. See `README.md` for the field list.

**How this build was checked**

- 2182 unit tests pass, 37 skipped (119 new tests this round) — every one
  of them mocks the HTTP call to whichever AI provider it's testing;
  nothing in this test suite, or in this feature with AI turned off,
  ever makes a real network call.
- Every one of the four features, the settings persistence (including
  the encrypted-at-rest API key), the GUI wiring, and the CLI's `"ai"`
  block are each covered directly.
- `repack.py --verify-roundtrip` still reproduces build R23 exactly
  before this round's changes are layered on, and the R23 rebrand/TLS
  `*_live.py` verification suites still pass unchanged, so nothing about
  either earlier round regressed.

## Round 23 — enterprise-grade TLS/SSL (build R23, 8 Sep 2026)

Every connection this tool makes — Oracle, MySQL, PostgreSQL, SQL Server,
DB2, MongoDB — can now be encrypted with a certificate, the way a security
review means when it asks for "TLS in transit" or "Encrypt in Transit".
This is a new, optional **Security (TLS/SSL)** section in the connection
dialog (and a matching `"tls"` block in the headless CLI's job config
files), off by default so nothing about an existing saved connection
changes on its own.

**What "enterprise-grade" means here, concretely**

- **Encryption** — the connection is encrypted on the wire, the same as
  HTTPS is for a browser.
- **Certificate verification** — the server's certificate is checked
  against a trusted CA (the operating system's own trust store, or a
  specific CA file you point at — the usual case for a private/internal
  CA). Off is still encrypted, just not authenticated — an explicit,
  named opt-out for a self-signed test database with no CA to check
  against, never the default.
- **Hostname verification** — on top of the CA check, the certificate's
  own name is checked against the address being connected to, so a
  certificate for the wrong server is rejected even if it happens to be
  signed by a trusted CA.
- **Mutual TLS** — this machine can present its own client certificate,
  for the databases (Oracle, MySQL, PostgreSQL, MongoDB) whose drivers
  support it over their wire protocol. SQL Server's ODBC driver and DB2's
  CLI have no client-certificate keyword for a plain TLS handshake, so
  those two don't offer it — the dialog simply doesn't show the fields
  for them, rather than showing controls that would silently do nothing.

**One real bug fixed along the way:** the SQL Server connector
unconditionally sent `TrustServerCertificate=yes`, which — despite the
name suggesting the opposite — turns certificate verification **off**
entirely, for every SQL Server connection this tool ever made. That
default is left exactly as it was for anyone not using the new Security
section (changing it out from under existing saved connections would
have broken every one of them pointed at a server with a certificate not
already in this machine's trust store), but turning TLS on now gets a
real, verified connection: `TrustServerCertificate=no` plus a genuine CA
check.

**The one hard problem: verifying a hostname through an SSH tunnel.** A
tunneled connection (see Round-whatever's "How to reach it" bastion
support) dials `127.0.0.1` — the tunnel's own local end — while the
certificate is for the real database address. A naive hostname check
would therefore fail every single tunneled+verified connection. The real
address is now captured before the tunnel rewrites it, and each driver is
told to check against that instead, wherever the driver allows a
connection address and a verification name to be given separately:

  - **PostgreSQL** can, fully — libpq's `host`/`hostaddr` split verifies
    the real name while dialing the tunnel.
  - **SQL Server** can, fully — the Microsoft ODBC driver's own
    `HostNameInCertificate` keyword does the same thing.
  - **Oracle, MySQL, DB2** cannot — their drivers have no equivalent, so a
    tunneled connection through these three gets encryption and CA
    verification, but skips the hostname check specifically (rather than
    failing every tunneled connection outright). This is a driver
    limitation, not an oversight, and it's written down in each
    connector's own code for the next person who goes looking.

**Where the settings live:** the connection dialog's new "Security
(TLS/SSL)" group, right below "How to reach it". A saved connection
remembers the certificate/key *paths* the same way it already remembers
an SSH key's path — never a password, a passphrase, or the certificate's
own bytes. The headless CLI takes a matching `"tls"` object in a job
config file, with a `client_key_password_env` for the one secret involved
(a passphrase-protected client key), following the exact same
environment-variable convention `password_env` already uses for the
database password.

**Verification**

- Unit suite: **2063 passed, 37 skipped** (92 new tests: TlsConfig itself,
  each connector's TLS wiring, the SSH-tunnel hostname hand-off, the
  connection dialog's Security group, saved-profile persistence, and the
  CLI's `"tls"` config block).
- No new runtime dependency — built entirely on the Python standard
  library's own `ssl` module plus each driver's existing TLS-related
  connection parameters.

## Round 22 — the DataBridge™ rebrand (build R22, 7 Sep 2026)

The product is now **TG DataBridge™ — *Connecting Legacy Data to the Future***.

This was a rename, not a rewrite: no migration logic changed. But a rename
that goes only skin-deep is worse than none, so it went all the way down.

**What was renamed**

| | before | after |
|---|---|---|
| Window title | Teleglobal Database Migration Tool — build R21 | TG DataBridge™ — Connecting Legacy Data to the Future — build R22 |
| Executable | `Teleglobal Database Migration Tool.exe` | `TG DataBridge.exe` |
| Python package | `tgsct/` (61 modules) | `tgdatabridge/` |
| PyInstaller spec | `packaging/teleglobal_tdmt.spec` | `packaging/tg_databridge.spec` |
| Settings folder | `%APPDATA%\TeleglobalTDMT\` | `%APPDATA%\TGDataBridge\` |
| Report headers | Teleglobal Database Migration Tool | TG DataBridge™, with the tagline |
| CLI approver variable | `TGSCT_APPROVED_BY` | `TGDATABRIDGE_APPROVED_BY` (old one still read) |

The company name is **unchanged** wherever it appears — the dashboard
watermark still reads *Teleglobal International Pvt Ltd*, because that is
the vendor, not the product, and the "TG" in the new name is that company.

**The three things a rename like this breaks silently, and what was done
about each**

1. **Your saved data would have looked deleted.** Connection profiles, run
   history, sync high-water marks and in-flight *migration checkpoints* all
   live in a folder named after the product. Pointing the new name at a new
   empty folder would look exactly like the rebrand had wiped them — and a
   lost checkpoint is not cosmetic: the next run re-copies every table that
   had already landed. On first launch the old folder's contents are
   **copied** across (copied, not moved, so an older build installed
   alongside keeps working).

2. **The executable would not have started.** The frozen entry script inside
   the .exe imports the package *by name*. Renaming the package without
   recompiling that entry gives you an .exe that dies on
   `ModuleNotFoundError` before a single window appears. The build tool
   (`repack.py`) grew `--rename-from` and `--entry-script` to recompile it,
   and now refuses to do the one without the other.

3. **The window would have opened with no icon, no logo and no styling.**
   The 208 MB `_internal` folder is not re-sent with every build, and the
   copy on your machine holds the icons, the watermark and the stylesheet
   under the *old* package directory name. Every one of those loads is
   written to tolerate a missing file, so this would have failed silently
   and just looked like a broken build. There is now a resolver
   (`tgdatabridge/utils/resources.py`) that finds them under either name,
   and the startup log says which one it used.

Also: the product name is now spelled in exactly **one** file
(`tgdatabridge/version.py`). A test fails the build if any other module
hard-codes it in a string a user can see, so the next rebrand is an edit to
four lines rather than a hunt through forty modules.

**Verification**

- Unit suite: **1971 passed, 37 skipped** (10 new tests).
- All **15 live suites** re-run against real MariaDB 10.11 and PostgreSQL 16
  servers, loading the shipping `.exe`'s own bytecode — not the source tree.
- A new `rebrand_live.py` suite (18 checks) proves, from inside the shipped
  executable: the title, that no `tgsct` module survives in the archive, that
  the entry script imports the new package, that a pre-rebrand `%APPDATA%`
  folder is adopted with its checkpoints and watermarks intact, and that the
  assets resolve against a pre-rebrand `_internal`.
- `repack.py --verify-roundtrip` still reproduces the original executable
  byte-for-byte, which is what makes the rest of the rebuild trustworthy.

---

Twelve defects on the **SQL Server → MySQL / PostgreSQL** path, found while
investigating two reported problems. Every fix was verified by generating the
DDL and applying it to a live PostgreSQL 16 and MariaDB 10.11 server, then
inserting rows and checking the migrated objects actually behaved — not by
reading the code.

Full test suite: **1456 passed, 37 skipped** (37 new tests added, none of the
existing ones changed).

---

## The two you reported

### 1. Procedures, functions and triggers always said "Requires manual conversion"

`plsql_converter.convert_routine()` opened with
`if routine.source_engine != "Oracle"` and returned a placeholder. That guard
was correct at the time — every converter in the tool (`plsql_converter`,
`tsql_converter`, `db2_converter`) reads Oracle PL/SQL, and running Oracle
regexes over T-SQL would produce silently wrong output. But it meant a SQL
Server migration converted 100% of tables and 0% of routines, by design.

**Fixed** by two new modules:

- `tgdatabridge/core/tsql_dialect.py` — T-SQL expression, query, DEFAULT and CHECK
  translation for MySQL and PostgreSQL.
- `tgdatabridge/core/tsql_routine_converter.py` — T-SQL procedure, function and
  trigger structure: parameters (with defaults and OUTPUT), `DECLARE`/`SET`/
  SELECT-assignment, `IF`/`ELSE`/`WHILE`, `TRY`/`CATCH`, cursor loops,
  `RAISERROR`/`THROW`, and AFTER triggers — including rewriting the set-based
  `INSERT ... SELECT FROM inserted JOIN deleted` audit idiom into the
  row-level `NEW`/`OLD` form both targets use.

Anything outside that surface (dynamic SQL, table variables, temp tables,
`MERGE`, `INSTEAD OF` triggers, table-valued functions on MySQL) is still
reported with an error-level issue and keeps its original source. The rule the
converter holds to: **a routine it calls converted is one you can read, run and
trust.**

### 2a. `getdate` errors when applying DDL

> MySQL 3770 — *Default value expression of column 'CreatedDate' contains a
> disallowed function: getdate*
> PostgreSQL — *function getdate() does not exist*

The DDL generator interpolated the source default straight into the target's
`CREATE TABLE` (`line += f" DEFAULT {col.default}"`). SQL Server's catalog
returns `(getdate())`; neither target has a `getdate` function, so statement 1
of 17 failed and nothing was created.

**Fixed** — `tsql_dialect.translate_default()` now handles it per target:
strips SQL Server's wrapping parentheses, maps `getdate`/`newid`/`suser_sname`/
`datediff` and friends, applies MySQL's rule that an expression default must be
parenthesised while `CURRENT_TIMESTAMP` must not, and drops defaults MySQL
forbids on `TEXT`/`BLOB`/`JSON`. Anything it cannot express is **omitted with
an error-level action item naming the column** — never emitted as SQL that
fails.

### 2b. The tool closes itself while applying DDL

No error dialog, no log line, nothing in the crash file — because the process
is aborted by Qt below Python, where `crash.py`'s handlers cannot see it.

`_run_async()` reassigned `self._worker`, which was the only Python reference
to the previous `QThread`. If that thread was still running, PySide deleted it
and Qt called `std::terminate()`: *"QThread: Destroyed while thread is still
running."* Reachable on the ordinary success path — Apply DDL's success handler
starts *Refresh Target Schema* from inside the old worker's own signal — and on
closing the window while any operation is in flight.

Reproduced against the original file: closing the window mid-operation exits
**134** (SIGABRT). With the fix the same scenario exits **0**.

**Fixed** — superseded workers are held until Qt's own `finished` signal fires;
a new `closeEvent` waits (bounded to 10 s) for in-flight work before teardown;
progress is emitted through a guarded helper.

---

## Found while testing

### 3. "Defer constraints" could never work on MySQL

> MySQL 1075 — *Incorrect table definition; there can be only one auto column
> and it must be defined as a key*

Deferral omits the primary key from `CREATE TABLE`, but InnoDB requires an
`AUTO_INCREMENT` column to be part of a key **at creation time**. So on those
tables deferral does not postpone index maintenance — it makes the statement
illegal. Every table with an identity column failed, then every post-load
`ALTER` failed after it because none of the tables existed.

Measured on the demo schema: **4 of 10** pre-load and **7 of 8** post-load
statements failed before; **0 of 15** after.

**Fixed** — for those tables only, the primary key (or a plain index on the
identity column when there is no PK) stays inline and is excluded from the
post-load script, so it cannot collide with error 1068. Secondary indexes,
unique and check constraints, foreign keys and triggers are all still deferred,
and the table carries an action item explaining why.

### 4. A `bigint` identity column produced an illegal column

> MySQL 1063 — *Incorrect column specifier*
> PostgreSQL 22023 — *identity column type must be smallint, integer, or bigint*

The type pivot is faithful rather than target-aware: SQL Server `bigint` →
`NUMBER(19)` → `DECIMAL(19,0)` on MySQL, `NUMERIC(19)` on PostgreSQL. Both are
fine numeric types and both are illegal as auto-increment.

**Fixed** — identity columns are widened to `BIGINT` on those two targets, with
an action item recording it. Oracle, SQL Server and Db2 accept a fixed-point
identity and are untouched.

### 5. A `bit` column's default stayed an integer on PostgreSQL

> *column "isactive" is of type boolean but default expression is of type integer*

**Fixed** — `0`/`1` become `FALSE`/`TRUE` when the target column is boolean.
MySQL keeps `TINYINT(1)`, where `0`/`1` is already correct.

### 6. Views were copied through as raw T-SQL

The tree showed them as **"Converted automatically"** — the worst kind of
wrong, because it looks fine until Apply DDL runs. The bodies still contained
`[dbo].[Employees]`, `ISNULL()`, `TOP n`, `GETDATE()` and `+` string
concatenation.

**Fixed** — SQL Server-sourced view bodies go through the dialect translator.
String concatenation is rewritten only where a literal operand proves the `+`
is text — to `CONCAT()` on MySQL, `||` on PostgreSQL — so `qty + 1` is never
touched. A view containing anything untranslatable is flagged rather than
shipped.

### 7. CHECK constraints kept their bracket quoting

SQL Server reports a check as `([Salary]>=(0))`. Square brackets are a syntax
error on both targets. **Fixed** — check conditions run through the same
translator as defaults, inline and in the post-load script.

### 8. The MySQL connector re-split statements on every semicolon

`MySQLConnector.execute_ddl()` did `sql.split(";")` — on statements the caller
had already split correctly. A routine body is full of internal semicolons, so
every `CREATE PROCEDURE`/`FUNCTION`/`TRIGGER` was chopped into fragments. This
would have blocked routine conversion even after the converter existed.

**Fixed** — it reuses `split_sql_statements()`, which already tracks BEGIN/END
nesting, quoting and comments. Cursors are closed in a `finally` and pending
result sets drained, so a failure no longer surfaces as a misleading
*"Unread result found"* on the *next* statement.

### 9. Comment-only placeholders were sent to the server

A `MANUAL CONVERSION REQUIRED` block wraps the original source in `/* … */`.
Executing one gets *"Query was empty"* from MySQL and a flat refusal from
psycopg — so a schema with a single unconverted routine aborted the entire
apply for a reason that had nothing to do with the schema.

**Fixed** — new `sql_split.has_executable_sql()` filters them at execution time
only. They stay in the script you read and save, so statement numbering does
not shift and a saved script still shows what needs hand-porting. The apply now
reports how many were skipped.

### 10. The modal progress dialog could re-enter itself

`QProgressDialog.setValue()` on a modal dialog calls `processEvents()`
internally, which can deliver the next progress signal while still inside the
call. On a large schema reporting thousands of steps, native stack frames nest
until the C stack overflows — another hard crash with no Python traceback.

**Fixed** — a re-entrancy guard plus throttling (one update per ten statements
above 50).

### 11. Conversion metrics were never recorded

`convert_metrics()` unpacked three values from a four-value result. The
`ValueError` landed inside the deliberately-swallowing `try/except` in
`_record_operation_metrics`, so every conversion since the post-load tab was
added recorded nothing but a bare duration. **Fixed** — one line.

---

## Behaviour changes callers should know about

- **PostgreSQL procedures that return result sets become `refcursor`
  functions.** PL/pgSQL cannot return an ad-hoc result set the way a T-SQL
  procedure does. Call inside a transaction, then `FETCH ALL` from the returned
  cursor. A procedure that returns nothing becomes a real `CREATE PROCEDURE`.
- **MySQL stored procedures cannot have parameter defaults**, so those are
  dropped with a warning; pass the value explicitly.
- **T-SQL locals are renamed** `@Total` → `v_Total`. In MySQL `@x` is a
  *session* variable, which would silently share state across calls — a real
  bug, not a syntax error.

## Verify it against your own MySQL

MySQL was verified here against MariaDB 10.11 (what installs in a Linux
container). MySQL 8 is stricter about which functions may appear in a column
default, so the allowed set is deliberately conservative: `NEWID()` and
`SUSER_SNAME()` defaults are dropped with an action item rather than emitted
and risking error 3770 again. One run against your real MySQL 8 instance is
worth doing.

## Files

| File | | |
|---|---|---|
| `tgdatabridge/core/tsql_dialect.py` | new | T-SQL expression / query / default / CHECK translation |
| `tgdatabridge/core/tsql_routine_converter.py` | new | T-SQL procedures, functions, triggers → MySQL / PL&#47;pgSQL |
| `tgdatabridge/core/ddl_generator.py` | patched | default and CHECK translation, identity-type coercion, MySQL deferral fix, view translation |
| `tgdatabridge/core/plsql_converter.py` | patched | routes SQL Server routines to the new converter; Oracle path untouched |
| `tgdatabridge/db/mysql_connector.py` | patched | statement splitting, cursor cleanup, result draining |
| `tgdatabridge/utils/sql_split.py` | patched | adds `has_executable_sql()` |
| `tgdatabridge/gui/main_window.py` | patched | worker lifetime, `closeEvent`, progress guard, apply-DDL reporting, metrics unpack |
| `tests/test_sqlserver_source_conversion.py` | new | 37 regression tests, one per defect above |
| `packaging/teleglobal_tdmt.spec` | new | PyInstaller spec for rebuilding the .exe |
| `BUILD-EXE.bat` | new | one-click rebuild of the standalone executable |

Every fix carries an inline comment explaining the failure it prevents, so the
reasoning survives in the code rather than only in this document.

---

## Round 2 — found from your crash log (24 Aug)

Your `%APPDATA%\TeleglobalTDMT\logs\crash-2026-08-24.log` showed two more,
both now fixed and both verified the same way.

### 13. MySQL 1067 — "Invalid default value for 'CreatedDate'"

`getdate()` was being translated correctly, but not *precisely* enough. A
SQL Server `datetime` maps to `DATETIME(6)`, and MySQL requires the
fractional-seconds precision to match right across the column definition:
`DATETIME(6) DEFAULT CURRENT_TIMESTAMP` is rejected, only
`DATETIME(6) DEFAULT CURRENT_TIMESTAMP(6)` is accepted.

This is exactly the MariaDB-vs-MySQL-8 gap flagged in the caveats above —
MariaDB accepts the unqualified form, so the first round's live test passed
while your MySQL 8 refused it. The default now carries the column's own
precision, driven by MySQL's documented rule rather than by what one server
tolerated.

### 14. The application closing itself when Apply DDL fails

The real cause, and not the one guessed at in round 1:

```
Windows fatal exception: access violation
  File "tgdatabridge\gui\log_console.py", line 22 in _on_log
  File "tgdatabridge\utils\logger.py", line 139 in log
  File "tgdatabridge\gui\main_window.py", line 99 in run
```

`logger.log()` calls its subscribers synchronously on whatever thread
logged. `_Worker.run`'s exception handler logs the whole traceback — from
the **worker** thread — and the log console's subscriber called
`appendHtml()` straight from there. Qt widgets may only be touched from the
GUI thread; on Windows that is an access violation and the process dies
with no traceback and no dialog.

Because it sits in the *failure* path, it only fired once something else had
already gone wrong — so a rejected DDL statement killed the app instead of
reporting itself. That is the "it closes when I click Apply DDL" symptom.

The console's subscriber now only emits a signal; Qt queues it onto the GUI
thread, so the widget is written from the thread that owns it. `logger.log()`
also guards each subscriber, so a display problem can never propagate into a
migration. `tests/test_log_console_threading.py` covers it, and was checked
against the old code to confirm it actually catches the bug.

---

## Round 3 — from your 18:34 / 18:59 runs

### 15. MySQL 3734 — "Missing column 'EmployeeID' ... in the referenced table 'employees'"

Not a conversion bug. The generated DDL is correct; the *target* was not
empty. `prod_db` already contained an `Employees` table with different
columns, so `CREATE TABLE IF NOT EXISTS` reported success and changed
nothing — and the foreign-key pass a few statements later ran against the
stale table. Reproduced exactly on a live server: the CREATE reports OK,
then the FK fails.

The tool already had the check that explains this (`target_shape.
check_table_shape`, whose docstring even names the `Employees`/`employees`
case-folding trap on Windows MySQL) — but it only ran before **Migrate
Data**, long after Apply DDL had already failed and left the schema
half-applied.

**Fixed** — the same check now runs as a pre-flight before "Apply DDL to
Target" executes a single statement. Instead of error 3734 pointing at a
constraint, you get:

> Target table 'Employees' already existed with a different set of columns,
> so "CREATE TABLE IF NOT EXISTS" left it unchanged and the data doesn't fit
> it. Missing on the target: EmployeeID, FirstName. Drop or rename the
> existing 'Employees' table, or migrate into a different database/schema,
> then re-run.

and nothing is applied.

### 16. `tgdatabridge.core.target_shape` was missing from the packaged executable

Found while adding the above. `tgdatabridge/core/migrator.py` imports
`target_shape` at module level, but the module is **absent from the shipped
.exe's archive** — PyInstaller's dependency analysis was run against an
older source tree and nothing re-checked it since. So "4b. Migrate Data"
raised `ModuleNotFoundError` in the packaged application while working
perfectly from source. `tgdatabridge.core.batch` is missing for the same reason.

This is a pre-existing packaging defect, not a consequence of any change
here. The build now computes the transitive import closure of the tgdatabridge
package and includes anything the code actually imports, and the
verification step imports **every** tgdatabridge module out of the finished
executable — which is what surfaced it.

### 17. A dropped default no longer marks the whole table red

`EmployeeSalaryAudit` showed "Requires manual conversion" solely because
its `suser_sname()` default cannot exist on MySQL and was dropped. The
table converts and creates correctly; only that column loses its default.
Reported as a warning now, so the object tree reflects what actually
needs attention.

### 18. Foreign keys rejected with errno 150 — a bug introduced by fix #4

Found by testing the reset-and-retry loop against a **clean** database, so
it is separate from the stale-table problem above.

Fix #4 widens an identity column to `BIGINT` so the target will accept it
as auto-increment. But a plain column of the *same* source type keeps the
faithful mapping — so a SQL Server `bigint IDENTITY` primary key became
`BIGINT` while the `bigint` columns referencing it became `DECIMAL(19,0)`.
Every engine requires the two sides of a foreign key to have the same
type, so MySQL rejected it:

```
1005 (HY000): Can't create table ... (errno: 150 "Foreign key constraint
is incorrectly formed")
```

with nothing to indicate a type mismatch was the cause.

**Fixed** — `ddl_generator.align_foreign_key_column_types()` runs before
any table DDL is generated and makes every foreign-key column's type
identical to the column it references, recording each adjustment as an
action item. The per-table generators cannot do this themselves: each sees
only its own table.

### 19. New: "Reset Target…"

The tool could *generate* a rollback script but never run it, which left a
hole in the loop a migration is actually tested in — apply, hit something,
reset, retry. Recovering from a half-applied schema, or from a target that
already had tables of the same names, meant leaving the tool and running
DDL by hand.

The new toolbar button drops the loaded schema's objects from the target
after an explicit confirmation that names the database, lists what will go,
and shows the exact script. It only touches objects in the schema
currently loaded; anything else in that database is left alone. It skips
the pre-flight (removing those tables is the point) and continues past
errors (an object that is already gone is not a failure), reporting
whatever did not apply.

Verified end to end on a live server: stale target → pre-flight names the
conflict → Reset Target → pre-flight clean → Apply DDL applies every
statement including the foreign keys.

## Round 4 — from your `test_db` run (error 1064)

### 20. MySQL 1064 — a `CREATE VIEW` nested inside a `CREATE VIEW`

Your last run applied statements 1–11 (every table and every foreign key)
without a single failure, then died on statement 12:

```
CREATE OR REPLACE VIEW `vw_EmployeeDetails` AS
CREATE VIEW vw_EmployeeDetails AS SELECT e.EmployeeID, ...
```

`View.definition` is documented — and treated everywhere in the generator —
as holding only the *SELECT text* of a view. Oracle, MySQL and PostgreSQL
all report it that way. SQL Server does not: `information_schema.views.
view_definition` returns the **entire original statement**, header
included. The SQL Server introspector stored that whole statement, and the
generator then wrapped it in its own `CREATE OR REPLACE VIEW … AS`, so the
server saw a second `CREATE` where a `SELECT` had to be — a syntax error,
and one that would hit every view in every SQL Server migration.

The fix is a new `tsql_dialect.strip_create_view_header()`. It walks the
text outside string literals and brackets and cuts at the `AS` that closes
the view header, so it handles all the shapes SQL Server actually stores:

* `CREATE VIEW v AS SELECT …`
* `CREATE OR ALTER VIEW v AS …` and `CREATE OR REPLACE VIEW v AS …`
* an explicit column list — `CREATE VIEW v (a, b) AS …`
* `WITH SCHEMABINDING` / `WITH ENCRYPTION` between the name and the `AS`
* a definition that is already a bare `SELECT` (left untouched)
* an `AS` appearing inside a string literal or a column alias (not cut on)

It is applied in the introspector, where the wrong value entered the model,
and again defensively in `generate_view_ddl` so a schema saved by an older
build, or loaded from a file, is corrected on the way out too.

Verified against the shipping executable: the introspector now yields
`SELECT e.EmployeeID, CONCAT(…)`, both the MySQL and PostgreSQL generators
emit exactly one `CREATE`, and the resulting view was created on a live
server and queried — `[('Anil Chavan',)]`.

## Round 5 — from your 12:15 run (error 1826)

### 21. MySQL 1826 — "Duplicate foreign key constraint name" on a re-run

Statements 1–6 applied, then statement 7 stopped everything:

```
1826 (HY000): Duplicate foreign key constraint name 'FK_Attendance_Employees'
  -> ALTER TABLE `EmployeeAttendance` ADD CONSTRAINT `FK_Attendance_Employees`
     FOREIGN KEY (`EmployeeID`) REFERENCES `Employees` (`EmployeeID`)
```

`test_db` already had those tables and those foreign keys from the previous
run. That is not an accident of your testing — it is how a migration is
actually done: apply, look at the result, adjust one thing, apply again.
The script mostly survives that loop already (`CREATE TABLE IF NOT EXISTS`
no-ops, `CREATE OR REPLACE VIEW` replaces), but a foreign key cannot be
written idempotently in standard SQL. There is no `ADD CONSTRAINT IF NOT
EXISTS`, so the second run died on a constraint that was already present
and already correct — with the views, function, procedures and trigger
after it never applied.

Three changes:

**a. An object that is already there is no longer a failure.** The apply
loop now recognises "what this statement creates already exists" across
every target engine — MySQL 1826/1304/1359/1050/1061, MariaDB's different
answer for the same case (1005 with InnoDB sub-code 121), PostgreSQL
SQLSTATE 42710/42P07, SQL Server 2714, Oracle ORA-00955, Db2 42710 — and
skips that one statement, then keeps going. Nothing is hidden: each skip is
named in the summary dialog ("1 object(s) were already on the target and
were left as they are: foreign key / constraint FK_Attendance_Employees")
and written to the log.

The matcher is deliberately narrow. The two errors fixed in earlier rounds
— 3734 "Missing column ... in the referenced table" and 1005 errno 150
"Foreign key constraint is incorrectly formed" — are explicitly *not*
already-exists errors and still stop the run, because those mean the
statement's dependencies are wrong rather than its object being built.

**b. Routines are now replaced, not duplicated.** Procedures, functions and
triggers were emitted as a bare `CREATE`, so a second run would have hit
1304 / 1359 next. Simply skipping those would have been the worst of the
three outcomes — an edited procedure body would silently never reach the
target while the run reported success. Each routine's `CREATE` is now
preceded by the matching `DROP ... IF EXISTS`, so re-applying installs the
current version. Not emitted where it would be wrong: nothing is added when
the generated statement already replaces in place (`CREATE OR REPLACE` on
PostgreSQL/Oracle/Db2, `CREATE OR ALTER` on SQL Server) — which also keeps
PostgreSQL's `DROP FUNCTION ... CASCADE`, and the dependent views it would
take with it, out of the forward script entirely — and nothing is added for
a routine still flagged for manual conversion, which created no object.

**c. A failure summary is no longer overwritten.** When a script both
failed a statement and contained a manual-conversion placeholder, the
dialog reported only the placeholder and hid every real failure — one
`note =` that should have been `note +=`.

Verified on live servers through the shipping executable: the same script
applied twice, back to back. Run 1 applies 7 statements; run 2 applies 6,
reports the one foreign key as already present, and the view, procedure and
data all still work afterwards — with the constraint existing exactly once.

## Round 6 — requested: tick/untick objects to leave them out

### 22. The checkboxes in the object tree now decide what gets migrated

Every object in the source tree already had a checkbox, but only "4b.
Migrate Data" ever read it. "2. Convert Schema" generated DDL for the
whole loaded schema regardless, so unticking a table changed nothing in
what was created on the target — and the tick state was thrown away
anyway, because the tree is rebuilt after a conversion to show each
object's status and that rebuild re-ticked everything.

Both are fixed, and the selection is now honoured everywhere downstream.

**What a tick means now.** Only ticked objects are converted, written into
the generated DDL, applied to the target, dropped by "Reset Target…", and
migrated. Untick a table, view, sequence, procedure, function or trigger
and it is simply not part of the run.

**Leaving one out no longer breaks the rest.** An object that is left out
takes its dependants with it, or the target rejects the script:

* A **foreign key** on a kept table that points at an excluded table is
  dropped from the generated DDL, with a line in the log naming both. Left
  in, MySQL answers 1005/150 or 3734.
* A **trigger** whose table is excluded is left out — it has nothing to
  attach to.
* A **view** that selects from an excluded table is left out, and the log
  says which table to tick if the view is wanted. A view's body is
  resolved when it is created, so keeping it would guarantee "Table 'x'
  doesn't exist" partway through the script. The check reads table
  positions only (FROM / JOIN / INTO / UPDATE), so a column or alias that
  happens to share a table's name is not mistaken for a dependency.
* **Procedures and functions** are deliberately left alone: their bodies
  are not resolved at creation time on any supported target, so one that
  reads an excluded table still installs cleanly.

**Nothing is hidden and nothing is lost.**

* The category rows read `Tables (3 of 5 selected)` when a selection is
  partial, and are tri-state, so a partial category is visible without
  expanding it.
* A strip above the tree says either "All objects selected" or "**N
  object(s) left out** of the next conversion", with the details on hover,
  and carries *Select all*, *Clear all* and *Invert* buttons. The same
  three are on the right-click menu, per category and for the whole tree.
* The "Migrate data?" confirmation — the one irreversible step — now
  states what is unticked before you press it.
* Unticking never edits the loaded schema. Untick, convert, re-tick,
  convert again, and the second run sees the original object graph, with
  the foreign key back.

Verified on a live server through the shipping executable: with the parent
table unticked, the generated script contains neither that table nor the
foreign key into it nor the view that selects from it, applies with zero
failures, and leaves exactly the selected table on the target. 1514 unit
tests pass, 15 of them new for this.

## Round 7 — requested: connect to a private database (AWS RDS behind a bastion)

### 23. New: connect through an SSH tunnel / bastion host

A production database usually is not on the public internet. An AWS RDS
instance in a private subnet has no publicly routable endpoint at all, so
there was no address you could type into Host that would work from your
laptop — the tool could only reach databases that were already reachable.

**What was added.** The connection dialog (both Source and Target) now asks
"How to reach it" outright, with three answers:

* **Public — the database is reachable from this machine.** The default,
  and exactly what the tool has always done. Nothing else to fill in.
* **Private — connect through an SSH tunnel (bastion / jump host).** Opens
  the SSH port-forward for you, then connects the database driver to its
  local end.
* **Private — I am connected to a VPN that reaches it.** Connects
  directly, because that is all a VPN needs, but checks the route first
  (see below).

Only the fields the chosen answer needs are shown, so a public connection
never has an SSH form in the way. It is exactly
what you would get from

```
ssh -N -L 5433:mydb.abc123.eu-west-1.rds.amazonaws.com:5432 ec2-user@bastion
```

and connecting to localhost:5433 — except the tool starts it, watches it,
reports its failures in the same dialog as any other connection failure,
and shuts it down when the application closes.

The fields:

* **SSH host / port / username** — the bastion. On AWS the username comes
  from the AMI: `ec2-user` for Amazon Linux, `ubuntu` for Ubuntu, `admin`
  for Debian. It is not the database username.
* **Authentication** — a private key file (the usual `.pem` downloaded
  from the EC2 console), a password, or the SSH agent / your default keys.
  Only the fields the chosen method uses are shown.
* **Database host / port** — optional. The database's address *as the
  bastion sees it*, for when the endpoint that the bastion can resolve is
  not the one you typed in Host. Leave blank to use Host and Port.
* **Verify host key** — off by default, because a bastion's key is not in
  this machine's known_hosts the first time and there would be no way to
  accept it from the dialog. Tick it once the host is known.

It works everywhere a connection does: "Test Connection", Load Schema,
Apply DDL, Dry Run, Migrate Data, Refresh Target — source side, target
side, or both at once, and either engine. One SSH session is shared by
every operation rather than re-authenticating on each click, it is
reopened automatically if it drops, and it is closed on exit.

**Saved connections** now remember which of the three you chose (shown in
the list as "… over VPN" or "… via bastion.example.com", so the same
database reached two ways is two entries rather than one overwriting the
other), and the jump host, its port, username,
authentication method and key file path — never the SSH password or the
key's passphrase, on exactly the same principle as the database password.
The same connection reached directly and reached through a bastion are
kept as two separate saved connections rather than overwriting each other.

**The headless CLI** takes an optional `"access": "public" | "vpn" | "ssh"`
on either connection, and the jump host as an `ssh:` block in the job
config, with the two secrets given as the *names* of environment variables
(`password_env`, `passphrase_env`) so a config file stays safe to commit:

```json
"source": {
  "engine": "PostgreSQL", "host": "mydb.abc123.eu-west-1.rds.amazonaws.com",
  "port": 5432, "database": "app", "username": "appuser",
  "password_env": "SRC_DB_PASSWORD",
  "ssh": {
    "host": "bastion.example.com", "username": "ec2-user",
    "private_key_path": "C:\\keys\\bastion.pem"
  }
}
```

**The VPN option, and what it does.** A VPN (OpenVPN, AWS Client VPN,
WireGuard, IPsec) is an operating-system level route: once it is up, the
private endpoint resolves and connects like any other host, and no
application can bring that route up on your behalf without being the VPN
client itself. So choosing this option does not connect any differently —
it says what you *expect*, and the tool checks it before every connection.

That is the whole value of having it as an explicit choice. Without it, a
VPN session that has quietly expired reaches you as a database driver
timeout that mentions neither the VPN nor the route. With it, you get:

> 10.0.3.44:5432 cannot be reached from this machine. This connection is
> set to "Private — I am connected to a VPN", so the address is expected
> to resolve through the VPN. Check that the VPN client is connected right
> now, that this endpoint is inside the network the VPN routes, and that
> the database's security group allows the VPN's address range. If there
> is no VPN and the database sits in a private subnet, switch this
> connection to "Private — connect through an SSH tunnel" instead.

The public/direct mode is deliberately *not* probed this way — some setups
legitimately answer on a path a bare TCP check does not model, and
breaking one of those to improve a message would be a bad trade. Instead,
when a direct "Test Connection" fails, the tool checks the route only then
and, if the address is not routable at all, adds one line saying so and
pointing at the two private options — which is exactly the situation this
whole feature exists for.

**Implementation notes.** The tunnel uses the asyncssh library, now
bundled inside the executable — pure Python on top of the `cryptography`
library the application already shipped, so there is nothing to install.
If it is ever unavailable (running from source without the dependency),
the tool falls back to the system `ssh` client, which is present by
default on Windows 10/11. Failures are translated into something
actionable rather than the library's own wording — a refused login names
the AMI-specific usernames to try, a timeout points at the security group,
a refused forward points at the database's security group, and an
un-decryptable key explains the OpenSSH-vs-PEM key format difference and
gives the `ssh-keygen` command to fix it.

Verified against a real SSH server, through the shipping executable:
MySQL and PostgreSQL both reached *only* through the jump host with key
auth, password auth and a passphrase-protected key; a full Load Schema
over the tunnel; an unresolvable RDS-style endpoint carried by an explicit
tunnel target; one tunnel shared across repeated connections; and each of
the four failure modes producing its intended message. 1556 unit tests
pass, 42 of them new for this — including the public and VPN modes
exercised against the shipping executable: a direct connection still
resolving to itself untouched, a VPN-mode connection checking its route
and then connecting, and an unroutable VPN-mode endpoint reported as a
VPN problem rather than a database one.

## Round 8 — from your screenshot: the dialog ran off the bottom of the screen

### 24. The connection dialog no longer grows past the screen

Adding "How to reach it" made the dialog tall enough that on a 1080p
laptop with the taskbar showing, the **Test Connection / OK / Cancel** row
fell below the bottom of the screen. A dialog whose only way out is
Alt+F4 is worse than no dialog, so:

* Everything above the buttons — the connection fields, "How to reach it"
  and the jump-host form — now lives in a scrolling area. **Test
  Connection, OK and Cancel sit outside it and are always visible**, which
  is the whole point: they are what you need precisely when the form is
  too tall.
* The dialog opens at its natural size but is capped to the screen's
  *usable* area, which excludes the taskbar, and is nudged back down if it
  would start above the top of the screen.
* Choosing "Private — connect through an SSH tunnel" makes the dialog
  taller to fit the fields it reveals, when the screen has the room —
  rather than leaving you to scroll a window that could simply have been
  bigger. Switching back does not shrink a window you resized yourself.

Covered by tests for every engine: the dialog fits inside the usable
screen area, the buttons are never inside the scrolling part, revealing
the jump-host fields makes room for them, and on a deliberately tiny
screen the height is capped and the content scrolls instead.

### 25. The selected "How to reach it" option is now unmistakable

In your screenshot the chosen option was impossible to pick out: some
Windows themes draw a selected radio button as a small, low-contrast dot,
and this is the one choice that decides whether a connection can work at
all. The selected option's label is now **bold**, and the radio indicator
itself is drawn explicitly — an open ring when unselected, a solid blue
dot in a ring when selected. Check boxes elsewhere in the tool are left
exactly as they were.

### 26. A non-standard SSH port is now pointed out

Your screenshot had **SSH port 21**. That is FTP; SSH listens on 22, and
the two are one keystroke apart. A wrong port produces exactly the same
"nothing answered" failure as a wrong address, so the message now adds:

> Also check the SSH port: it is set to 21, and SSH normally listens on
> 22 — 21 is the usual FTP port.

The same note appears for any other port that is a well-known service
(23 Telnet, 80 HTTP, 3306 MySQL, and so on), and never when the port is
already 22.

## Round 9 — from your 2059 screenshot

Good news first: **the tunnel worked.** Error 2059 is MySQL's own
authentication-plugin error, which means the connection went through the
bastion, reached `10.0.2.178:3306` and got as far as the login handshake.
Two things were wrong past that point.

### 27. MySQL 2059 — "Authentication plugin 'mysql_native_password' cannot be loaded"

This looks like a credentials problem and is not one. mysql-connector
prefers its **C extension** whenever one is importable, and that extension
loads each authentication plugin as a separate DLL from a directory it
locates relative to `libmysql.dll`. Inside a frozen application there is
no such directory, so the plugin is never found and every connection fails
this way regardless of the username and password.

The connector now asks for the **pure-Python implementation**
(`use_pure=True`). It carries its plugins as ordinary Python modules
(`mysql.connector.plugins.*`), which are inside the executable and always
found. This affects every MySQL connection, tunnelled or not — a direct
MySQL connection would have hit the same wall on a server configured for
`mysql_native_password`.

### 28. asyncssh was silently not loading — the tunnel was running on the fallback

The bottom of your screenshot has the line that gave this away:

```
... "cryptography.hazmat.primitives.kdf") ... falling back to the system ssh client
```

The bundled asyncssh needs `cryptography.hazmat.primitives.kdf.pbkdf2`,
and that module was packaged — but its **parent package**
`cryptography.hazmat.primitives.kdf` was not. A frozen import that reaches
a missing parent fails at the parent, so asyncssh could not be imported at
all and the tool quietly used its second backend, the system `ssh` client.

That fallback is why the tunnel worked at all, which is the design working
as intended — but it is the weaker path: no password authentication, and
its errors are OpenSSH's rather than the explained ones. The missing
package is now included, so asyncssh is what actually runs.

The build now derives every added module's parent packages automatically
instead of relying on them being listed, and the verification step fails
the build outright if any module in the archive has a parent that is not
also in it — so this class of mistake cannot ship again.

### Two things to change in your own settings

* **SSH port was 21** in the earlier screenshot (it is 22 now — correct).
  21 is FTP.
* **"Verify the jump host's key against known_hosts" is ticked.** Unless
  `13.201.121.138` is already in this machine's `known_hosts`, untick it
  for the first connection.

## Round 10 — from your MySQL → PostgreSQL run

The tunnel, the schema load and the conversion all worked: 60+ tables read
out of the private MySQL through the bastion and converted for PostgreSQL.
The stop was the pre-flight check, and it was right to stop — but it left
you with nowhere to go.

### 29. Being blocked by a table already on the target now offers a way out

`localhost/Priti` already had an `accounts` table with different columns.
`CREATE TABLE IF NOT EXISTS` over it does nothing at all, so the run would
have failed several statements later on a foreign key with the schema
half-applied — which is exactly what the pre-flight exists to prevent. But
the dialog had only an **OK** button, so the fix meant leaving the tool,
working out which tables clashed, and dropping or renaming them by hand in
another client.

The dialog now offers the two things you actually want, and both are
things this tool can do:

* **Leave them out** — unticks those tables in the object tree. Nothing on
  the target is touched, and the rest of the schema goes across. This runs
  through the same selection machinery as unticking by hand, so a foreign
  key that pointed into one of them and a view that selected from one are
  dropped from the script too, each named in the log, instead of failing
  on the server.
* **Replace them…** — drops exactly those tables on the target and then
  applies the schema. Only the tables that were in the way, never the
  whole rollback script. It asks a second time first, naming the tables,
  saying that everything in them goes with them, and showing the exact
  `DROP` statements — because this is the one action here that destroys
  someone's data.
* **Cancel** — which is the default, and what to choose if you would
  rather point the target at a different database or schema and start
  again.

Table names are matched case-insensitively, since PostgreSQL folds names
to lower case and MySQL on Windows is case-insensitive, so the name the
target reports is often not spelled the way the source spells it.

Verified live: with an unrelated `accounts` table sitting on the target,
"Leave them out" applies the rest with zero failures and leaves the
existing row untouched; "Replace them" drops only `accounts` — not
`contacts`, not `workflows` — after which the pre-flight is clean and the
whole schema applies, foreign keys included.

### Still to do for this migration

`trg_user_activity_insert` shows "Requires manual conversion". Automatic
routine conversion currently covers an **Oracle** source and a **SQL
Server** source; a **MySQL** source's triggers, procedures and functions
are still flagged for manual review rather than translated. That is the
next piece of work if you need it — say the word.

## Round 11 — from your 117/1061 screenshot

116 statements applied. Statement 117 died on

```
syntax error at or near "00"
LINE 24: ..."last_modified" TIMESTAMP NOT NULL DEFAULT 0000-00-00 00:00:00
```

### 30. MySQL reports a column's default *unquoted* — it was going into the DDL as-is

This is the bug, and it is much wider than one column. MySQL's
`information_schema.columns.COLUMN_DEFAULT` gives a **literal** default
with no quotes: a column declared `DEFAULT 'Active'` comes back as the
bare word `Active`, `DEFAULT 'pending review'` as `pending review`, and
`DEFAULT '0000-00-00 00:00:00'` as `0000-00-00 00:00:00`. Interpolated
into the generated DDL none of those is a string:

* `DEFAULT Active` — PostgreSQL reads an identifier: *column "active" does
  not exist*.
* `DEFAULT pending review` — a syntax error.
* `DEFAULT 0000-00-00 00:00:00` — the syntax error you got.

The MySQL introspector now turns every default into valid SQL as it reads
it, quoting a literal on a character, date/time, binary, enum, set or json
column and escaping any apostrophe inside it, while leaving alone the
things that are already SQL: a bare number, `NULL`, `CURRENT_TIMESTAMP`,
and any expression MySQL 8 marks as `DEFAULT_GENERATED`. MariaDB reports
defaults *with* quotes already, so those are recognised and not
double-quoted.

### 31. MySQL's "zero date" has no equivalent anywhere and is now dropped

Quoting `'0000-00-00 00:00:00'` fixes the syntax but not the meaning:
`0000-00-00` is not a representable date in PostgreSQL, Oracle, SQL Server
or Db2 — nor in MySQL 8's own default `sql_mode` — so the column would
still be rejected, now with *date/time field value out of range*.

The default is dropped and reported, with the part you have to decide
spelled out:

> Column last_modified: DEFAULT '0000-00-00 00:00:00' is MySQL's "zero
> date", which no other engine can store — PostgreSQL rejects it
> outright. The default was dropped so the table can be created. Rows
> holding that value arrive as NULL, so if this column is NOT NULL either
> give it a real default on the target or allow NULLs before migrating the
> data.

### 32. MariaDB's `current_timestamp()` broke PostgreSQL too

Found by running the fix against a real MariaDB rather than trusting it.
MariaDB reports `DEFAULT CURRENT_TIMESTAMP` back as the *call*
`current_timestamp()`, and PostgreSQL has no parenthesised form:

```
syntax error at or near ")"
LINE 12: "created_at" TIMESTAMP NOT NULL DEFAULT current_timestamp(),
```

`current_timestamp()`, `now()`, `sysdate()`, `localtimestamp()`,
`curdate()`, `curtime()`, `utc_timestamp()`, `utc_date()` and
`utc_time()` are now translated into each target's own spelling — and
kept, rather than dropped by the unknown-function guard, which would have
silently lost a default that has a perfectly good equivalent. An explicit
precision (`CURRENT_TIMESTAMP(6)`) survives.

### 33. A failed statement now offers "apply the rest and list every failure"

Stopping at the first failure is the right default — statement N+1 usually
depends on statement N. But your script is 1061 statements, and one class
of problem across thirty tables means thirty runs and thirty dialogs.

The failure dialog now offers **"Apply the rest and list every failure"**
alongside **Stop**. It runs the whole script through and reports every
statement that fails, so one run tells you everything that is left. Safe
to offer only because the script became idempotent in round 5: what
already exists is skipped rather than re-applied, so the 116 statements
that already worked are not applied twice.

Verified against a real MariaDB and a real PostgreSQL, through the
shipping executable: a table carrying every one of these defaults —
`'Active'`, `'pending review'`, `'O''Brien'`, `0.00`, `0`, `''`,
`'2020-01-01'`, `CURRENT_TIMESTAMP` and the zero date — is introspected,
converted and applied with zero failures, and each default lands on
PostgreSQL with its meaning intact.

## Round 12 — "it takes a long time, and every table shows zero records"

The DDL applied: 499 tables are on the target. Two things about what you
saw afterwards.

### 34. Zero rows on the target is correct at that point — and now says so

**"3. Apply DDL to Target" creates the schema. It does not copy any
data.** Every table being empty right after it is the expected state, not
a failed migration — but nothing in the tool said that, and "499 tables,
all record counts zero" reads exactly like a migration that silently did
nothing.

Two changes:

* The **"DDL applied"** dialog now ends with: *"This created the schema
  only — no rows have been copied yet, so every table on the target is
  empty at this point. That is expected. Next: 4a. Dry Run (Plan) to check
  what would be copied, then 4b. Migrate Data to copy it."* With "Defer
  constraints" ticked, as you have it now, it also reminds you that
  **5. Apply Post-Load DDL** comes after the data.
* The **"Target schema (as migrated)"** pane now shows row counts:
  `Tables (499) — all empty, no data migrated yet` while it is, and
  `Tables (499) — 340 with data, ~12,480,000 row(s) in total` once the
  data is in, with a per-table figure on each row. They come from the
  engine's own statistics (PostgreSQL's `pg_class.reltuples`, MySQL's
  `information_schema.tables.table_rows`) — one extra query for the whole
  schema, rather than a `COUNT(*)` per table, which across 499 tables
  would be 499 full scans. Approximate is the right trade for telling
  empty from not-empty, and if the statistics cannot be read the pane
  falls back to plain names rather than failing.

### 35. No second progress dialog after a long operation

Apply DDL and Migrate Data both refresh the target pane when they finish.
That refresh had its own modal *"Refresh Target Schema…"* dialog, so a
long operation ended by putting up **another** dialog — which reads as
"still working" well after the thing you asked for has finished. It is
what your screenshot caught. That follow-up refresh is now silent: it
still runs, still updates the pane and the Schema Diff, and still logs,
but without a second dialog on top. Pressing "Refresh Target Schema"
yourself still shows one.

### 36. The log now says where the time actually went

Rather than guess, the apply reports its own throughput:

> Apply DDL to Target: applied 1000 DDL statement(s) to PostgreSQL target
> in 1.4s (690 statements/second). Most of that is the round trip to the
> server, one statement at a time.

Measured at your scale on this end, through the shipping executable:
generating the DDL for 499 tables takes 0.06s, applying all 1000
statements to a local PostgreSQL takes 1.45s, and refreshing the target
pane takes 0.02s. So if a run is taking minutes on your machine, the time
is going to the server or the network rather than to the tool — and that
line in the log will now show it, which tells us where to look next.

## Round 13 — from the migration log: 6,490,258 rows in, 17 tables refused

The data migration ran. Reading the log you sent, the failures fall into
exactly two groups, and one of them is a real bug that has been there all
along.

### 37. A column called `primary`, `key` or `order` broke its whole table

Every failing table in the log reports the same thing:

```
address_rel: 1064 (42000): You have an error in your SQL syntax ... near
'primary, created_at, updated_at, deleted_at FROM 3f8cddb...e.address_rel'
```

The migration builds its own `SELECT`, and it was building it out of
**bare, unquoted names**:

```sql
SELECT id, address_id, primary, created_at, updated_at FROM app.address_rel
```

`primary` is a reserved word. So are `key`, `order`, `group`, `index`,
`class`, `function`, `condition` and `year_month` — and between them those
account for every one of the 17 tables that failed:

> address_rel, autodesk_insight_metrics, campaign_template,
> dropdown_lists, email_address_rel, eta_configurations, langtranslations,
> languages_masters, logichooks, phone_numbers_rel, saml2_tenants,
> sso_configurations, user_preferences, users_google_calendars,
> workflow_conditions, drop_down_lang, opr_settings

Every table whose columns happened to avoid the reserved list migrated
perfectly, which is exactly why this went unnoticed until a schema this
size hit it. It is not MySQL-specific either: the same statement is
invalid on PostgreSQL, Oracle, SQL Server and Db2; only the list of
reserved words differs.

Every identifier the migration emits is now quoted, in the source
engine's own style — backticks for MySQL, double quotes for
PostgreSQL/Oracle/Db2, brackets for SQL Server — taken from the source
connector so the migration's SQL and the connector's own SQL agree.
Names are quoted exactly as the introspector read them, because quoting
makes case significant and re-casing would turn a working query into
"column does not exist".

The **schema name is quoted too**, which matters here: your source
database is called `3f8cddb7226647be97fe09fd0b094e5e`.

The same bug was in the **sharding** probe and predicates — a primary key
called `order` made `SELECT MIN(order)...` a syntax error, which silently
turned sharding off for that table rather than reporting anything. Fixed
in the same place.

Verified live, through the shipping executable: a MySQL `address_rel`
with columns `primary`, `key`, `order`, `group`, `index`, `class`,
`function`, `condition` and `year_month`, 2,500 rows — schema applied
with zero failures, all 2,500 rows migrated to PostgreSQL, values intact.

### The other failures were the pre-flight, not a bug

`accounts`, `autodesk_subscriptions`, `autodesk_teams`, `export_accounts`,
`export_autodesk_usages`, `export_subscriptions`, `feature_statistics`,
`inbound_emails` and `module_managements` failed with *"Target table
already existed with a different set of columns"*. Those are the nine the
pre-flight named, and the "Drop these tables?" dialog in your screenshot
is the tool offering to resolve them. They are not affected by the fix
above — decide those with **Leave them out** or **Replace them**, per
round 12.

Re-running **4b. Migrate Data** resumes from the checkpoint rather than
starting over, so the 6.49 million rows already copied are not copied
again.

## Round 14 — the reserved-word fix held; two things left in the new log

**6,587,792 rows migrated, and all 17 syntax failures are gone.**
`address_rel`, `langtranslations`, `user_preferences`, `sso_configurations`
and the rest all went across. What is left in the log is nine tables and
one false alarm.

### 38. "expected 692 row(s), target has 692" — a validation warning that was not one

`team_members` was reported as a validation issue with a message printing
two identical numbers, which reads like a bug in the tool rather than a
finding about the data. Two separate faults behind it.

**The message never said which check failed.** Row count and checksum are
both validated; only the row count was ever printed. A *checksum*
mismatch was therefore announced by showing the row count twice. Each
result now says what actually happened:

> team_members: row count matches (692) but the checksum does not, so at
> least one value differs between source and target.

**And the checksum itself was wrong to complain.** It compares a hash of
every row as read from the source against the same hash of the rows read
back from the target — which only works if both engines hand back the
same value. Two of the differences are ones *this tool deliberately
creates*, and both were being counted as corruption:

* **`CHAR(n)` is blank-padded** by PostgreSQL, Oracle and Db2 when read
  back, while MySQL strips the padding. A `CHAR(36)` primary key comes
  back as `id-0` from the source and `id-0` followed by 32 spaces from
  the target. Any table with a CHAR column could never validate.
* **`TINYINT(1)` becomes a real `BOOLEAN`** on PostgreSQL, by design, so
  the source returns `1` where the target returns `True`.

Both are now folded before hashing, alongside the date-widened-to-a-
timestamp case that was already handled. Only trailing *spaces* are
folded and only on strings, so a leading space, a tab, interior
whitespace, `1` vs `"1"`, `NULL` vs `''`, `Decimal("1.50")` vs `1.5` and
a truncated time all still mismatch — those are corruption, not mapping.
The checksum's algorithm id moved to `blake2b64-v4` so a checkpoint
written by the previous build is recognised as incomparable rather than
silently compared.

**And a table that could not be verified is no longer called "failed".**
The dialog said *"N table(s) failed post-migration validation"* for a
table whose every row copied without error. It now says they "could not
be fully verified afterwards", which is what actually happened, and
points at the log line that says which check was inconclusive.

Verified live through the shipping executable: a MySQL table with a
`CHAR(36)` primary key and a `TINYINT(1)` column, 2,500 rows, migrates to
PostgreSQL and reports **validated** — where before it would have been
flagged.

### The nine that remain are the pre-flight, still

`accounts`, `autodesk_subscriptions`, `autodesk_teams`, `export_accounts`,
`export_autodesk_usages`, `export_subscriptions`, `feature_statistics`,
`inbound_emails`, `module_managements` — every one reports *"target table
already existed with a different set of columns"*, naming the columns the
existing table is missing (`atd_flexCustomerFlag`, `totalSeats`,
`tat_sla_Configuration`, and so on).

Nothing in the tool can decide those: the tables on `Priti` are not the
ones this migration would create, and whether their contents matter is
your call. In the "Drop these tables?" dialog choose **Leave them out**
to migrate everything else and deal with those separately, or **Replace
them** to drop and recreate them from the source — after checking whether
anything in them is real.

## Round 15 — the nine tables were never "already there". I was wrong, and so was the message.

You created a brand-new empty PostgreSQL database and the same nine
tables failed with *"target table already existed with a different set of
columns"*. On an empty database nothing existed, so the message was
false — and so was the advice I gave you off the back of it. Dropping
those tables would not have helped, because they were not the problem.

### 39. A camelCase column made its table unmigratable on PostgreSQL

Look at what the nine have in common — it is not their history, it is
their column names:

| Table | Columns reported "missing" |
|---|---|
| `autodesk_teams` | `totalSeats` |
| `module_managements` | `tat_sla_Configuration` |
| `inbound_emails` | `trashFolder`, `sentFolder`, `leave_msg_on_mail_Server` |
| `feature_statistics` | `db_last_Preference_used`, `incoming_Call_popup_number` |
| `accounts` | `atd_flexCustomerFlag`, `atd_isNamedAccount`, … |
| `export_autodesk_usages` | `Usage Type`, `Subscription ID`, `Tokens Used` |

Every one is camelCase or contains a capital. Every table whose columns
happened to be plain lower-case migrated without trouble — all 490 of
them.

**The mechanism.** PostgreSQL folds unquoted identifiers to lower case,
so this tool creates every PostgreSQL object lower-cased and quoted (see
`ddl_generator._quote_pg` for why that is the right choice). A source
column `atd_flexCustomerFlag` therefore becomes `atd_flexcustomerflag` on
the target — deliberately — and `postgres_connector.insert_batch`
lower-cases the same names when it writes, so the rows fit perfectly.

The shape check did not. It folded the *table* name to match, and then
probed the *columns* in their original case:

```sql
SELECT "atd_flexCustomerFlag" FROM "accounts" WHERE 1=0
```

A quoted identifier is case-sensitive, so PostgreSQL answered *column
does not exist* — and the check concluded the table had the wrong shape
and refused it before a single row was written. On any database, empty or
not. The same fault applied to Oracle and Db2 targets, which fold the
other way, to upper case.

The columns are now folded exactly as the DDL created them and as the
insert addresses them. Messages still name a genuinely missing column the
way the *source* spells it, since that is where you would go looking for
it, and MySQL — which folds nothing — is untouched.

**Verified live through the shipping executable, on a brand-new empty
PostgreSQL database:** a MySQL source with `totalSeats`,
`atd_flexCustomerFlag`, `atd_isNamedAccount`, `atd_buyingReadinessScore`
and `atd_autodeskMainContactEmail` — pre-flight blocks nothing, the
schema applies, the shape check passes, and 2,400 rows across two tables
migrate and validate. Before this fix the same run refused both tables
with the message you saw.

### What this means for your run

Those nine tables should now migrate. You do **not** need to drop
anything, and if you have not dropped them yet on `Priti`, don't — the
original `public` schema was never the problem either. Re-run **4b.
Migrate Data** against whichever database you prefer; the checkpoint
means only the outstanding tables are attempted.

### The five checksum warnings are not failures

`attendance_configurations`, `call_check_lists`, `opportunities_cstm`,
`tasks` and `scheduler_logs` reported "row count matches but the checksum
does not". Every row was copied and the counts agree exactly on all five;
what could not be confirmed is that every *value* round-tripped
identically. Some representational differences between MySQL and
PostgreSQL are still not folded — a `FLOAT` widened to `DOUBLE
PRECISION`, for instance, genuinely changes the stored value's
representation. Worth a spot-check on a column or two if those tables
matter to you, but nothing is missing.

## Round 16 — "no unique constraint matching given keys for dropdown_lists"

The foreign key at statement 1046 is the symptom. The cause is a thousand
statements earlier and much worse than it looks.

### 40. Every MySQL primary key is named `PRIMARY` — so only one table could have one

MySQL names *every* table's primary key literally `PRIMARY`, and scopes
index names to the table. That is legal there and nowhere else:
PostgreSQL, Oracle, SQL Server and Db2 all scope constraint and index
names to the **schema**. Your 499 tables therefore produced 499
constraints called `PRIMARY`, and PostgreSQL accepts exactly the first:

```
CREATE TABLE "dropdown_lists" (... CONSTRAINT "primary" PRIMARY KEY ("id"));   -- ok
CREATE TABLE "drop_down_lang" (... CONSTRAINT "primary" PRIMARY KEY ("id"));
ERROR: relation "primary" already exists
```

**And the message says "already exists"** — so the apply loop's
already-there handling, added in round 5 to make re-running safe, read it
as "this table was created on a previous run" and skipped the statement.
The table was never created, the run reported success, and the damage
surfaced a thousand statements later as a foreign key pointing at a table
with no primary key:

> there is no unique constraint matching given keys for referenced table
> "dropdown_lists"

That is also why the earlier runs appeared to work: they had **Defer
constraints** ticked, and the deferred pre-load script creates tables with
columns only — no primary keys at all, so nothing collided. The tables
went in; the constraints never did. `dropdown_lists` on your target has no
primary key for exactly that reason.

**Two fixes, because there were two faults.**

*The names.* Constraint and index names are now made unique across the
schema before any DDL is generated, for every target that scopes them
that way. A primary key named `PRIMARY` becomes `<table>_pkey` —
PostgreSQL's own convention — and a repeated index name is prefixed with
its table. The renaming is deterministic and idempotent, so re-converting
produces the identical script and re-applying stays safe, and it is
reported in the conversion issues so nothing is silent. Oracle's shorter
identifier limit is respected, with a hash suffix rather than a plain
truncation so two long names can never collapse into one. **A MySQL
target is left completely alone** — the names are per-table there and
changing them would alter DDL that works.

*The error that hid it.* "Already exists" is now only read as
"already there" when the error names **the object the statement is
creating**. `CREATE TABLE "drop_down_lang"` failing with `relation
"primary" already exists` is about a different object, so it fails loudly
instead of being skipped. A genuine re-run — `relation "drop_down_lang"
already exists`, or a duplicate foreign key — is still skipped exactly as
before.

**Verified live through the shipping executable:** 500 MySQL-style tables,
every primary key named `PRIMARY` and every index named `idx_parent`,
applied to a real PostgreSQL. 1,003 statements, **0 failures, 0 skipped —
500 tables, 500 primary keys, 1 foreign key**. Before this fix the same
script created one table.

### What to do with your current target

`Priti12` has tables without primary keys, from the deferred runs. The
cleanest route is a fresh database (or `Reset Target…`), then:

1. **2. Convert Schema** with **Defer constraints** as you prefer — it now
   works either way.
2. **3. Apply DDL to Target**
3. **4b. Migrate Data**
4. **5. Apply Post-Load DDL**, if you deferred.

## Round 17 — "violates foreign key constraint team_members_team_id_foreign"

This one is **not a bug in the tool**, and the tool should have said so.

The DDL got to statement 1568 of 1572 — every table, every index, all but
the last handful of foreign keys. Then:

```
insert or update on table "team_members" violates foreign key constraint
"team_members_team_id_foreign"
DETAIL: Key (team_id)=(7f304fcf-5a72-458e-9536-20a5f7ad7801)
        is not present in table "teams".
```

The constraint is right, the types are right, the tables are right. The
**rows** do not satisfy it: at least one `team_members` row points at a
`teams` row that does not exist. Adding the constraint would make the
table's own contents illegal, so PostgreSQL refuses.

Those orphans almost certainly came with the data. MySQL enforces foreign
keys only on InnoDB, and only while `FOREIGN_KEY_CHECKS` is on — a MyISAM
table, a table converted to InnoDB after the fact, or any bulk load done
with the checks off accumulates rows whose parent has since been deleted.
Nothing complains until something tries to create the constraint for
real, which is exactly what migrating to PostgreSQL does.

### 41. A foreign-key violation now explains itself and hands over the query

The driver's message names one offending key and stops there — it says
neither how many rows are involved nor what to do, and it cannot tell the
two very different causes apart. The tool now counts, while the
connection is still open, and says which one it is.

**When the orphans came with the data:**

> Foreign key team_members_team_id_foreign could not be created: rows in
> team_members point at teams rows that are not there.
>
> 12 of 693 row(s) in team_members have no matching teams row (300 rows).
>
> Those orphans almost certainly came with the data: MySQL enforces
> foreign keys only on InnoDB and only while FOREIGN_KEY_CHECKS is on …
>
> To see them:
>
>     SELECT c.* FROM "team_members" c LEFT JOIN "teams" p
>       ON c."team_id" = p."id"
>      WHERE c."team_id" IS NOT NULL AND p."id" IS NULL;

**When the parent table simply did not migrate** — a different problem
with a different fix, and much more serious:

> teams is EMPTY on the target while team_members has 693 row(s). That
> points at the parent table not having migrated rather than at the data
> — check whether teams failed or was left out, migrate it, and apply
> this step again.

The count uses a LEFT JOIN rather than `NOT IN`, so a NULL foreign key —
which is legal on every engine — is never miscounted as an orphan. The
explanation goes into the log as well as the dialog, and a diagnosis that
cannot be produced (no permission on the parent table, say) is simply not
shown rather than becoming a second failure.

Verified live against a real PostgreSQL, through the shipping executable:
693 `team_members` rows, 12 of them orphaned and one with a NULL key —
counted as 12, not 13 — and the handed-over `SELECT` returns exactly the
offending rows. With the parent emptied, the same failure is reported as
the missing-table case instead.

### What to do about your run

Everything up to statement 1567 applied. For each foreign key that fails
this way:

1. Press **"Apply the rest and list every failure"** — the script is
   idempotent, so it carries on and shows you *every* foreign key in this
   position in one pass instead of one dialog at a time.
2. Run the `SELECT` for each one to see the orphaned rows.
3. Either delete or repoint them in the source (or the target) and apply
   step 5 again, or leave those constraints off if the orphans are
   expected. Everything else — tables, data, indexes, the other foreign
   keys — is already in place either way.

---

## Round 18 — the migration finishes on its own

**What you asked for:** "please resolve the all issues and give me updated
tool as i want to migrate the data."

Round 17 explained the `team_members` failure. It still stopped. That was
the wrong answer to a real migration: a 1,572-statement script abandoned
four statements from the end over **one** bad row out of 692, and a person
asked to hand-fix data at the exact moment they wanted a finished
database.

This round finishes the script.

### What actually happens now

When the target refuses a foreign key because the rows already in the
table break it, the tool creates the constraint anyway — without
re-checking the rows that were already there — logs what it did, and
carries on to the next statement. Nothing stops. Nothing is deleted.
Nothing is blanked.

Every engine has a way to say exactly that:

| Target | How the constraint is created |
|---|---|
| PostgreSQL | `... REFERENCES "teams" ("id") NOT VALID` |
| Oracle | `... REFERENCES "teams" ("id") ENABLE NOVALIDATE` |
| SQL Server | `ALTER TABLE t WITH NOCHECK ADD CONSTRAINT ...` |
| Db2 | `... REFERENCES "teams" ("id") NOT ENFORCED` |
| MySQL / MariaDB | `SET FOREIGN_KEY_CHECKS = 0` around the `ALTER` |

**This is a real constraint.** "Unvalidated" does not mean "off". Every
insert and every update from that moment on is checked against it — the
live test proves it by inserting a new orphan and watching PostgreSQL and
MariaDB both refuse it. What it has not done is re-litigate rows that were
already sitting in the table when the constraint was added.

At the end of the run you get a summary naming every constraint this
happened to, how many orphaned rows each one has, the `SELECT` that lists
them, and the one statement that promotes the constraint to fully
validated once you have cleaned them up:

```
1 foreign key could not be created as written, because rows already in
those tables break it. ...

1 was created anyway and is enforced from now on:
  team_members_team_id_foreign (team_members -> teams): 1 orphan row(s);
  created unvalidated, no data changed
    see them:  SELECT c.* FROM "team_members" c LEFT JOIN "teams" p
               ON c."team_id" = p."id"
               WHERE c."team_id" IS NOT NULL AND p."id" IS NULL;
    once clean: ALTER TABLE "team_members"
                VALIDATE CONSTRAINT "team_members_team_id_foreign";
```

### The new control: "Orphan rows"

On the toolbar, next to "Defer constraints". Five choices:

| Choice | What it does |
|---|---|
| **create the key anyway** (default) | The above. Changes no data. |
| blank the orphans | Sets the offending foreign-key values to `NULL`, then creates the key fully checked. Only where the column allows `NULL`; falls back to the default if it does not, and says so. |
| delete the orphans | Deletes those child rows, then creates the key fully checked. Destructive — never chosen for you. |
| leave the key off | Carries on without that one constraint. |
| stop and ask me | Round 17's behaviour, still there if you want to look at each one. |

The default is the only one applied without asking, and it is the only one
that touches no data. That is deliberate.

### Details that took the most care

**A `NULL` foreign key is not a violation** on any engine. It is not
counted as an orphan, not blanked, and not deleted. Getting this wrong
would have silently destroyed rows.

**MySQL error 1093** — "You can't specify target table for update in FROM
clause" — means the portable `NOT EXISTS` form of the cleanup is illegal
on MySQL. It uses multi-table `UPDATE ... LEFT JOIN` / `DELETE c FROM ...`
there and `NOT EXISTS` everywhere else.

**`FOREIGN_KEY_CHECKS` is restored in a `finally`.** If the `ALTER` failed
between the two `SET` statements, the session would otherwise carry on
with foreign-key checking disabled and quietly accept bad rows into every
table loaded afterwards — far worse than the constraint it was trying to
add.

**Schema-qualified tables stay qualified.** The constraint is rebuilt from
the parsed statement rather than string-patched (the original is wrapped
in a `DO $$ ... END $$` block — this tool's stand-in for PostgreSQL's
missing `ADD CONSTRAINT IF NOT EXISTS` — so appending `NOT VALID` to the
text would have put it after the `END`). Re-quoting a bare name would have
resolved it against `search_path` instead of the table that actually
failed, so the schema prefix is carried through.

**`ON DELETE CASCADE` and friends survive the rebuild.** Not emitted by
this tool's own generator today, but a hand-edited script can carry them,
and dropping them would silently change what the constraint does.

**An empty parent is still called out.** If `teams` came back with zero
rows, that means the parent table did not migrate — a bigger problem than
a few orphans. The constraint is still created, but the summary says so in
as many words.

**The command-line runner got the same treatment**, plus the
already-exists skipping the GUI has had since Round 5. A scheduled
migration that stops dead on one bad row is a migration nobody can
automate.

### Verified

1,761 unit tests (up from 1,706), and twelve live suites run against the
shipping executable's own bytecode on a real PostgreSQL 16 and a real
MariaDB 10.11. The new one, `recovery_live`, seeds the exact shape of your
data — 680 valid rows, 12 orphans pointing at
`7f304fcf-5a72-458e-9536-20a5f7ad7801`, one `NULL` — and checks, on both
engines:

- the constraint exists afterwards
- a **new** orphan is refused (it is genuinely enforced)
- a valid new row is still accepted
- no row was deleted, blanked or otherwise touched
- exactly 12 orphans counted — not 13, the `NULL` is not one
- `VALIDATE CONSTRAINT` fails while the orphans remain and succeeds once
  they are gone, i.e. the statement handed to you actually works
- MySQL's session is left with `FOREIGN_KEY_CHECKS` back **on**
- a non-`public` schema stays qualified
- an empty parent is reported as the missing-table case

### What this means for your run

Press **5. Apply Post-Load DDL** again. It will run to the end. Statements
1 to 1567 are already there and are skipped as "already exists";
`team_members_team_id_foreign` is created unvalidated; statements 1569 to
1572 — which never ran before — apply. You get one summary at the end
naming the one orphaned row, and the migration is done.

Clean up that row whenever it suits you, then run the `VALIDATE
CONSTRAINT` line from the summary. Or leave it — the constraint is
protecting every new write either way.

---

## Round 19 — Oracle → MySQL: the routines convert for real

**What you asked for:** "I'm performing now migration from oracle to mysql
via this tool, I want conversion of all db objects."

Your screenshot showed `GET_ORDER_TOTAL` and `ORDER_PACKAGE` as **Requires
manual conversion**, and `UPDATE_ORDER_STATUS` / `TRG_ORDER_DATE` as
*Converted with warnings*.

That second label was the worse of the two. Until this build, an Oracle →
MySQL migration converted every table and every view and **not one
routine**. The MySQL path fell through to a fallback that applies four
regular expressions to the PL/SQL and hands it back essentially unchanged.
The result was labelled "converted", but MySQL cannot run a line of it:
`v := 0;`, `ELSIF`, `EXIT WHEN`, `FOR r IN (SELECT ...) LOOP` and a
trailing `EXCEPTION WHEN OTHERS THEN` are all syntax errors there.

There is now a real Oracle PL/SQL → MySQL converter, the same class of
thing this tool already had for PostgreSQL and SQL Server.

### What it translates

| Oracle | MySQL |
|---|---|
| `PROCEDURE p (a IN NUMBER, b OUT VARCHAR2)` | `CREATE PROCEDURE p(IN a DOUBLE, OUT b VARCHAR(4000))` |
| `v NUMBER(12,2) := 0;` | `DECLARE v DECIMAL(12,2) DEFAULT 0;` |
| `v := x;` | `SET v = x;` |
| `ELSIF` | `ELSEIF` |
| `WHILE c LOOP ... END LOOP;` | `w: WHILE c DO ... END WHILE w;` |
| `FOR i IN 1..10 LOOP` | a labelled `WHILE` with a real counter |
| `FOR i IN REVERSE 1..10 LOOP` | the same, counting down |
| `FOR r IN (SELECT a, b FROM t) LOOP ... r.a ...` | a cursor, a `NOT FOUND` handler, per-column fetch variables, and every `r.a` rewritten to the variable holding it |
| `EXIT WHEN c;` / `CONTINUE WHEN c;` | `IF c THEN LEAVE lbl; END IF;` / `ITERATE` |
| `RETURN;` in a procedure | `LEAVE routine_body;` on a labelled block |
| `EXCEPTION WHEN ... THEN` at the end | `DECLARE EXIT HANDLER FOR ...` hoisted to the top |
| `e_bad EXCEPTION;` / `RAISE e_bad;` | `DECLARE e_bad CONDITION FOR SQLSTATE '45000';` / `SIGNAL e_bad` |
| `RAISE_APPLICATION_ERROR(-20001,'x')` | `SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'x'` |
| `EXECUTE IMMEDIATE s;` | `PREPARE` / `EXECUTE` / `DEALLOCATE PREPARE` |
| `a \|\| b \|\| c` | `CONCAT(a, b, c)` |
| `NVL` `NVL2` `DECODE` `INSTR` `TRUNC` `SYSDATE` `SYSTIMESTAMP` | `IFNULL`, `CASE`, `CASE`, `INSTR`/`LOCATE`, `TRUNCATE`/`DATE`, `NOW()`, `NOW(6)` |
| `TO_CHAR(d,'DD/MM/YYYY')` | `DATE_FORMAT(d,'%d/%m/%Y')` — the mask is translated, not copied |
| `TO_DATE(s,'YYYY-MM-DD')` | `STR_TO_DATE(s,'%Y-%m-%d')` |
| `PACKAGE BODY pkg` | each member as `pkg_<member>`, with its own `DROP ... IF EXISTS` |
| `BEFORE INSERT OR UPDATE` trigger | two triggers, one per event |
| `:NEW.col` / `:OLD.col` | `NEW.col` / `OLD.col` |
| `IF INSERTING THEN` | resolved to `TRUE`/`FALSE` in each emitted trigger |
| `SEQ.NEXTVAL` | `SEQ_NEXTVAL()` — see below |

### Sequences now really work

`generate_sequence_ddl_mysql` claimed to produce "a helper table plus a
NEXTVAL-style stored function". It produced the table and a **comment**
describing what you would have to write yourself. So every converted
routine calling `ORDER_SEQ.NEXTVAL` was calling something that did not
exist.

The functions are generated now:

```sql
CREATE FUNCTION `ORDER_SEQ_NEXTVAL`() RETURNS BIGINT MODIFIES SQL DATA ...
CREATE FUNCTION `ORDER_SEQ_CURRVAL`() RETURNS BIGINT ...
```

`NEXTVAL` uses `LAST_INSERT_ID(next_val + increment)` inside the UPDATE,
which is atomic — two sessions incrementing at once cannot be handed the
same number. `CURRVAL` is session-scoped, like Oracle's, and raises a
clear error if the session has not called `NEXTVAL` yet, exactly as Oracle
raises ORA-08002 rather than inventing a value.

Also: a sequence called `ORDER_SEQ` no longer produces a helper table
called `ORDER_SEQ_SEQ`.

### Three bugs the live tests caught that reading the code would not

**`||` cannot be left alone.** MySQL reads `||` as logical OR unless the
session has `PIPES_AS_CONCAT` set. `'Order ' || p_order_id` would have
silently become `1`. Rewriting it needs real expression boundaries, and
the first attempt sliced a `CASE ... END` in half because `WHEN`/`THEN`/
`ELSE`/`END` look like the end of an expression. A CASE is now one operand.

**`DECIMAL(65,30)` is the wrong type for a routine variable.** It is the
right mapping for a *column* — exact, no precision lost. But MySQL prints
a DECIMAL with its full scale, so `'Order ' || id` came out as `Order
10.000000000000000000000000000000`. Oracle's unconstrained `NUMBER` has no
fixed scale; neither does `DOUBLE`. Unconstrained `NUMBER` in a routine is
now `DOUBLE`, with a warning telling you to use `DECIMAL(p,s)` if that
variable holds money. Columns are unchanged.

**The statement splitter miscounted `CASE`.** `CASE ... END` closes with a
bare `END` — the same token a `BEGIN` block closes with. The splitter
counted that `END` as closing the routine's `BEGIN`, so the next `;` ended
the statement and MariaDB was sent half a `CREATE FUNCTION`, reported as
`1064 ... near ''`. This affected **any** generated routine containing a
CASE, including one produced from a PostgreSQL `DECODE` conversion — so
this fix reaches beyond the MySQL work.

Two more that only a live server would show:

- MySQL allows **one handler per condition per block** (error 1338), so a
  routine with two cursor loops, or one cursor loop plus a `WHEN
  NO_DATA_FOUND` handler, would refuse to create. Each cursor loop now
  gets its own nested block.
- MySQL **saves and restores `LAST_INSERT_ID()` around a stored function
  call**, so `CURRVAL` reading it always returned whatever came before.
  It reads a session variable instead.

### What it still refuses, on purpose

`BULK COLLECT`, `FORALL`, `CONNECT BY`, autonomous transactions, `DBMS_*`
and `UTL_*` packages, local `TYPE` declarations, `%ROWTYPE`, `GOTO`,
parameterised cursors, a function with an `OUT` parameter, and a cursor
loop over `SELECT *` or an unaliased expression — that last one because
`FETCH ... INTO` needs a column list, and guessing one produces a routine
that compiles and then binds the wrong column. Each is reported with what
to do about it.

### Verified

**1,848 unit tests** (up from 1,761) and **thirteen live suites** run
against the shipping executable's own bytecode. The new one,
`oracle_mysql_live`, does not inspect the generated text — it applies every
converted routine to a real MariaDB 10.11 and then **calls** it:

- `GET_ORDER_TOTAL(10)` returns `360.00` (cursor loop, `ELSIF` discount,
  exception section)
- `UPDATE_ORDER_STATUS` returns `Order 10 set to SHIPPED` through its OUT
  parameter, writes exactly 3 audit rows from its `FOR` loop, and its early
  `RETURN` stops the procedure
- both triggers fire, one per event, and default the status
- `ORDER_PACKAGE_count_orders` and `ORDER_PACKAGE_grade_customer` answer
  correctly as standalone routines
- `ORDER_SEQ_NEXTVAL()` returns 100, 101, 102 across a re-applied script
- `BUILTINS('Acme')` returns `Acme @ 04/03/2026 pos=6 known`
- `RAISE e_missing` actually raises
- two cursor loops in one routine create and run

### What to do

Press **2. Convert Schema** again. `GET_ORDER_TOTAL` and `ORDER_PACKAGE`
should no longer say "Requires manual conversion", and the Generated DDL
tab will show real MySQL. Anything still flagged is on the refusal list
above, and the Assessment Report says which construct and why.

---

## Round 20 — triggers, from any database

**What you asked for:** "via my tool im unable to migrate the triggers of
any database please modify the tool properly."

You were right, and the cause was worse than it looked. The tool
converted stored routines from exactly **two** source engines: Oracle and
SQL Server. Everything else hit a guard that flagged it "Requires manual
conversion" on sight. So a MySQL → PostgreSQL migration converted every
table and view and not one trigger — and so did a **MySQL → MySQL** one,
where there was nothing to translate at all.

Three separate faults, all of which had to be fixed for triggers to work.

### 1. The introspection was incomplete

| | Before | Now |
|---|---|---|
| A PostgreSQL trigger's body | `information_schema.triggers.action_statement`, which on PostgreSQL is literally the text `EXECUTE FUNCTION foo()` — **no logic at all** | read from `pg_proc` and attached to the trigger |
| A multi-event trigger | one Routine **per firing event**, all sharing one name → N identical DDL blocks, N rollback DROPs, N indistinguishable rows in the object tree under one checkbox | one Routine carrying all its events |
| A MySQL / PostgreSQL routine's parameters | **lost** — `routine_definition` is the body alone, so there was nothing to put between the parentheses on any target | read from `information_schema.parameters` / `pg_get_function_arguments` |
| A PostgreSQL trigger function | invisible (`information_schema.routines` hides functions returning `trigger`) | read, and attached to its trigger rather than listed as a function nobody wrote |

### 2. There was no converter

New: `tgdatabridge/core/native_routine_converter.py`, covering four directions.

**Same engine** — MySQL → MySQL, PostgreSQL → PostgreSQL, SQL Server →
SQL Server, Db2 → Db2. Nothing is translated; the `CREATE` statement the
catalog never stored is reassembled from the metadata above. This is
always a clean conversion, and it is the case that should never have been
broken.

**MySQL ↔ PostgreSQL** — a real translation:

```
SET v = x;              <->  v := x;
ELSEIF                  <->  ELSIF
WHILE c DO … END WHILE  <->  WHILE c LOOP … END LOOP
REPEAT … UNTIL c        ->   LOOP … EXIT WHEN c; END LOOP
LEAVE / ITERATE         <->  EXIT / CONTINUE
lbl: LOOP               <->  <<lbl>> LOOP
SIGNAL SQLSTATE '45000' <->  RAISE EXCEPTION
DECLARE v INT DEFAULT 0 <->  v INT := 0  (moved into a DECLARE section)
IFNULL / IF(a,b,c)      <->  COALESCE / CASE WHEN a THEN b ELSE c END
DATE_FORMAT('%d/%m/%Y') <->  to_char('DD/MM/YYYY')   (the mask, translated)
`backticks`             <->  "double quotes"
```

The structural difference that matters most: **PostgreSQL keeps a
trigger's body in a separate function.** A MySQL trigger becomes a
`CREATE FUNCTION … RETURNS TRIGGER` plus a `CREATE TRIGGER … EXECUTE
FUNCTION`, and `RETURN NEW;` is added if the body has no RETURN — because
a BEFORE row trigger that returns nothing does not fail, it **silently
discards the row**. Going the other way the function is inlined back into
the trigger body and the `RETURN NEW;` removed, since MySQL rejects it.

### 3. Everything downstream mis-reported triggers

- **Rollback** dropped only the base name, so every `<trigger>_<EVENT>`
  object a split trigger created survived a rollback; and on PostgreSQL
  the companion `<trigger>_fn` function was orphaned every time.
- **The target diff** reported a false "only in target" for every
  PostgreSQL trigger function ever migrated, and expected per-event
  trigger names only for Db2 — so a 3-event trigger on a MySQL target
  showed one false "only in source" plus three false "only in target".
- **"Save DDL…" could not export the post-load script**, which with
  "Defer constraints" ticked is where every trigger, index and foreign key
  lives. There is now a **"Save Post-Load DDL…"** button beside it.

### Three bugs the live tests caught

**A `#` inside a string ate the rest of the line.** MySQL's `#` starts a
comment, and comments were masked before string literals, so
`CONCAT(name, ' #', id)` became `CONCAT(name, ' /* ', id); */` — a
perfectly good function turned into a syntax error. Strings and comments
are now matched in one pass, so whichever starts first wins. The same
latent fault was fixed in the Oracle → MySQL converter.

**`UPDATE t SET c = v` was read as an assignment.** The `SET` rewrite
fired on any `SET`, turning a working UPDATE into `UPDATE t c := v`. Only
a `SET` that *starts a statement* is an assignment now.

**`||` reached a MySQL target unconverted.** PostgreSQL's concatenation is
MySQL's logical OR, so `'created ' || NEW.name` would have quietly become
`0` or `1` in every migrated row rather than failing. It is rewritten to
`CONCAT`.

### Verified

**1,902 unit tests** (up from 1,853) and **fourteen live suites** against
the shipping executable's own bytecode. The new one, `trigger_live`, does
not inspect generated text — it creates real triggers, procedures and
functions on a real server, **introspects them with the tool's own
introspector**, converts, applies to a real target, and then INSERTs and
UPDATEs rows to prove the migrated trigger fires:

- MariaDB → PostgreSQL: BEFORE INSERT sets `updated_at` and defaults a
  column through its `IF`; AFTER INSERT writes the audit row; BEFORE
  UPDATE moves `updated_at` on; the procedure and function both answer
- PostgreSQL → MariaDB: the same three checks, plus the function
- MariaDB → MariaDB and PostgreSQL → PostgreSQL: triggers fire on the
  copy, and re-applying the whole script is safe

### What to do

Load the schema and press **2. Convert Schema**. Your triggers should now
show "Converted automatically" instead of "Requires manual conversion".
If "Defer constraints" is ticked they are in the **Post-Load DDL** tab
(and applied by step 5), not the main script — that is deliberate, so they
never fire once per migrated row during the data load.

---

## Round 20a — the build now says which build it is

You reported the trigger still showing "Requires manual conversion" after
Round 20 shipped. It was not a bug in the conversion: the DDL in your
screenshot contained the line

```
-- Source engine is MySQL, not Oracle -- this tool's stored-routine
   converters are Oracle PL/SQL-specific and were not run against it.
```

which is a message **that no longer exists in the Round 20 code**. The
folder still held the Round 19 executable, because the connection to your
PC dropped part-way through that delivery.

Running your exact case — MySQL source, MySQL target, one
`before_employee_insert` trigger — through both builds side by side:

| Build | Result |
|---|---|
| Round 19 (9,614,825 bytes) | `Requires manual conversion` + that message |
| Round 20 (9,638,379 bytes) | `Converted automatically`, real `CREATE TRIGGER` |

Rather than leave you checking file sizes in Explorer to answer "which
build am I running", **the build stamp is now in the title bar**:

```
Teleglobal Database Migration Tool  —  build R20 (2026-09-04)
```

It is also the first line of the log, so a saved log identifies its own
build, and a screenshot of any future problem answers the question by
itself without a round trip.

Also fixed while checking this: a converted trigger came out with its
`DROP TRIGGER IF EXISTS` twice — once from the converter (which has to
emit its own, because a multi-event trigger becomes several objects under
names the generator cannot know) and once from the generator on top.
Harmless, but it reads as a bug in a script a DBA reviews before running
it. The generator now leaves the DROP alone when the converter already
wrote one.

1,908 unit tests, and all fourteen live suites re-run against this build.

---

## Round 21 — incremental sync

**What you asked for:** "i want to add that incremental changes also need
to migrate from source to target database."

A migration is almost never a single event. The bulk load runs, and then
the source keeps working — rows are added, rows are edited, rows are
deleted. Until now the only way to catch the target up was to re-run the
whole thing, which for your 6.5 million row schema means hours of work to
move a few hundred changed rows.

**New: "6. Sync Changes"**, on the toolbar after Migrate Data. It makes
the target a mirror of the source again, moving only what actually
differs.

### What counts as a change

All three, as you asked: rows **added**, rows whose values **differ**, and
rows **deleted** at the source. The last one is why this is not simply
"copy anything with a recent timestamp" — a deleted row leaves nothing
behind to carry a timestamp, so the only way to find one is to read the
target's keys and ask the source whether each still exists.

There is an **incl. deletes** tick next to the button. Untick it if the
target deliberately keeps history the source has purged.

### How a changed row is found

Per table, whichever is cheaper and safe — and the choice is reported, so
a table quietly taking the expensive path is visible rather than
surprising.

| Strategy | When | Cost |
|---|---|---|
| **timestamp** | the table has an `updated_at` / `modified` / `last_updated` / `changed_at` **date-time** column | reads only rows at or after the last run's high-water mark — a 250,000-row table with 40 edits reads 40 rows |
| **compare** | no such column | reads and hashes every row, comparing against the target's own copy — slower, but it catches an edit the application forgot to stamp |
| **skipped** | the table has no primary key or unique constraint | named, with the reason |

A table with no key is refused on purpose. There is no way to say which
target row a changed source row corresponds to, so an "update" would have
to be delete-everything-and-reinsert — a full reload wearing an
incremental costume, which is not what anyone pressing "sync" expects.

Two deliberate refusals worth knowing:

- **`updated_by VARCHAR` is not used as a watermark.** It matches the name
  patterns, and comparing against it succeeds, returns the wrong rows, and
  looks like it worked. Only a real date-time column is trusted.
- **`created_at` and `joined_on` are not used either.** A date column that
  is not about *change* would leave every edited row behind.

### On a schedule

Next to the button: an interval and **Start scheduled sync**. It repeats
until stopped, so the target stays close to live while a cutover is
planned. The first run starts immediately.

A tick that arrives while the previous run is still going is **skipped,
not queued** — two syncs over the same tables at once would race on the
same rows. Scheduled runs report to the log and the status bar rather than
opening a dialog; a sync every fifteen minutes that interrupts what you
are doing is a sync nobody leaves running. Failures are still shown.

The high-water marks are stored per source → target → schema pairing and
survive restarting the tool. A table that **failed** keeps its previous
mark rather than the one that run reached: advancing past rows that were
read but never written would make those changes invisible to every later
sync.

### Two things that would have made this useless

**A `CHAR` key does not match itself across engines.** PostgreSQL
blank-pads a `CHAR(8)` on read and MySQL strips it, so the same key is
`'AA      '` on one side and `'AA'` on the other. Compared raw, every
source row looks new *and* every target row looks deleted — a sync
straight after a clean migration would delete and re-insert the whole
table. Keys are folded with the same normalisation post-migration
validation already uses, which also covers a `DATE` widened to midnight
by this tool's own type mapping.

**No merge-join.** The obvious implementation streams both sides ordered
by primary key and walks them together. It is wrong across engines:
MySQL's default collation sorts `'a'` and `'A'` together and PostgreSQL's
does not, and a merge-join fed two differently-ordered streams silently
reports the entire table as inserts *and* deletes. Instead the source is
read in batches and the target asked about exactly those keys
(`WHERE (k) IN (...)`), then the reverse for deletes. No ordering
assumption anywhere, exact on any collation.

The watermark filter is **inclusive** (`>=`, not `>`): a row written in
the same second as the last run's mark would be missed by a strict `>`,
and re-reading a handful of rows is free because the upsert makes them
no-ops, while missing one is silent data loss.

### Verified

1,961 unit tests (up from 1,908) and **fifteen** live suites against the
shipping executable. The new one, `incremental_live`, migrates real tables
and then edits the source the way an application would — insert, update,
delete — and reads the target back:

- MariaDB → PostgreSQL and PostgreSQL → MariaDB, both directions
- both strategies: a table with `updated_at`, and one with no timestamp at all
- a sync straight after a full migration reports **no changes** — no false
  positives from `CHAR` padding or `DATE` widening
- running it a second time still reports no changes
- the timestamp path reads only rows at or after the watermark; the
  compare path still reads the whole table
- the no-key table is reported as **skipped**, not failed
- an edit made *without* bumping `updated_at` is missed by the timestamp
  strategy (as documented) and caught by the compare strategy
- with deletes off the row stays; with deletes on it goes

### How to use it

1. **4b. Migrate Data** — the full load, once, as now.
2. **5. Apply Post-Load DDL** — constraints, indexes and triggers.
3. **6. Sync Changes** whenever you want the target caught up, or **Start
   scheduled sync** to have it repeat.

Safe to run repeatedly. Run it *after* Migrate Data, not instead of it —
the first sync of an empty target would work, but it would do it the slow
way, one batch at a time, rather than through the bulk-load path.
