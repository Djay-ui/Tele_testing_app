"""Post-migration validation: independent row-count and checksum checks
comparing what migrator.py *believes* it copied against what the source/
target actually contain, run right after a table's data migration
completes.

migrate_table() already counts the rows it *sent* to the target
(MigrationResult.rows_copied) -- that alone doesn't catch a target driver
that silently drops/truncates rows on write (a partial autocommit, a
target-side trigger/constraint eating rows without raising, a truncated
batch on a flaky connection that still returned success, etc.), so this
module makes an independent, after-the-fact query against the target to
check what actually landed there.

Two checks, escalating in cost:
  - row count: always attempted, cheap (one COUNT(*)-equivalent query per
    table), one integer to compare against what migrate_table sent.
  - checksum: an XOR of every copied row's SHA-256 hash, computed once
    while migrate_table streams rows to the target (free -- it's already
    iterating every row) and compared against the *same* computation run
    against the rows actually stored in the target afterward. XOR makes
    this order-independent on purpose: source read order and target
    write/read order are not guaranteed to match, so a simple concatenated
    hash would produce false mismatches. Only attempted for tables at or
    under `checksum_max_rows` -- re-reading every row of a very large
    table purely to validate it is expensive, and a correct row count
    already rules out the most common failure modes for those.

Every connector-specific quirk (Mongo has no SQL/no schema concept, SQL
Server/Db2/Oracle/PostgreSQL each quote and case identifiers differently)
is handled by the connector's own count_rows()/checksum_rows() methods --
this module is engine-agnostic, the same way migrator.py itself is.
"""
from __future__ import annotations

import datetime
import hashlib
from dataclasses import dataclass
from typing import List, Optional


# Identifies the algorithm row_checksum implements, so a persisted
# checksum can be recognised as having come from a different one. Bump
# this whenever row_checksum's output changes for any input; see
# app_storage.MigrationCheckpoint.checksum_algorithm for what a mismatch
# then does.
CHECKSUM_ALGORITHM = "blake2b64-v4"


def _canonical(value):
    """Fold the one difference between a source value and its target
    counterpart that this tool *deliberately* introduces.

    Oracle's DATE carries a time component, so the shared pivot treats
    every source engine's date-only column as an Oracle-style DATE and
    every target maps it to a timestamp (see type_mapping: MySQL,
    PostgreSQL, SQL Server and Db2 each attach an explicit warning saying
    so). The consequence for validation is that a source driver hands
    back `datetime.date(2024, 1, 1)` and the target hands back
    `datetime.datetime(2024, 1, 1, 0, 0)` for the very same value --
    different reprs, different hashes, and a checksum mismatch on a table
    where nothing whatsoever went wrong.

    That is not a hypothetical. A real MySQL -> PostgreSQL run reported
    every table containing a DATE column as "Unvalidated", which reads
    exactly like data loss and sends someone diffing row counts by hand.
    The integration suite reproduces it against both engines
    (tests/integration/test_known_regressions.py).

    Only midnight-widening is folded, and only for a bare `date`. A
    `datetime` is left alone, so a target that truncated 09:30 to 00:00
    still mismatches -- that one really is data loss. Nothing else is
    normalised: str/int, Decimal/float and NULL/'' all still hash
    differently, because those differences are corruption rather than the
    documented mapping.
    """
    if type(value) is datetime.date:      # not isinstance: datetime subclasses date
        return datetime.datetime(value.year, value.month, value.day)

    # A CHAR(n) column is blank-padded to n by PostgreSQL, Oracle and Db2
    # when it is read back, while MySQL strips the padding on retrieval.
    # The same stored value therefore comes back as 'abc' from the source
    # and 'abc' + 33 spaces from the target -- different hashes, and a
    # checksum mismatch on a table where every row arrived intact.
    #
    # Observed on a real MySQL -> PostgreSQL run: a 692-row table whose
    # primary key is CHAR(36) reported "validation issue" with the row
    # counts matching exactly, which reads like data loss and is not.
    #
    # Only *trailing spaces* are folded, and only on a str. A leading
    # space, a tab, and any interior whitespace all still hash
    # differently, so real corruption is still caught. Trailing spaces on
    # a VARCHAR are the price: no engine agrees on whether they survive a
    # round trip anyway, so a check that insists on them reports the
    # engines' disagreement rather than the migration's correctness.
    if type(value) is str:
        return value.rstrip(" ")

    # MySQL's TINYINT(1) and BIT(1) become a real BOOLEAN on PostgreSQL,
    # by design (see type_mapping) -- so the source hands back 1 and the
    # target hands back True for the same value. Folded for exactly the
    # same reason as the date above: it is a difference this tool chose,
    # not one the data has. `bool` is checked before `int` deliberately,
    # since bool is a subclass of int.
    if type(value) is bool:
        return 1 if value else 0
    return value


def row_checksum(row: tuple) -> int:
    """A stable 64-bit hash of one row's values, XOR-combined across rows
    by table_checksum below (which is what makes the result independent of
    the order a target returns rows in).

    repr() -- not str() -- is used so `1` and `"1"` hash differently: a
    silent int-to-string cast somewhere in the round trip through a target
    driver is exactly the kind of corruption this check exists to catch,
    and str(1) == str("1") would hide it.

    BLAKE2b with an 8-byte digest, rather than the SHA-256 this used to
    truncate. Same 64 bits kept, but the hash produces exactly those bytes
    instead of computing 32 and discarding 24, which measured ~1.3x faster
    over 200,000 rows. That matters more than it looks: this runs once per
    row on both the source and target side of validation, it is pure
    Python so it holds the GIL, and a parallel migration of many small
    tables therefore serialises on it.

    Deliberately not Python's built-in hash(): that is randomised per
    process (PYTHONHASHSEED), so two runs -- or a resumed run -- would
    disagree about identical data.
    """
    return int.from_bytes(
        hashlib.blake2b(
            repr(tuple(_canonical(v) for v in row)).encode("utf-8", errors="replace"),
            digest_size=8,
        ).digest(),
        "big",
    )


def table_checksum(rows) -> int:
    """XOR every row's row_checksum() together. XOR is commutative and
    self-cancelling, so the result is the same regardless of what order
    `rows` is iterated in -- the source read order (migrate_table, while
    copying) and the target read order (a connector's checksum_rows,
    after the fact) are never guaranteed to match, and this check must
    still agree when nothing is actually wrong."""
    total = 0
    for row in rows:
        total ^= row_checksum(row)
    return total


@dataclass
class ValidationResult:
    table_name: str
    expected_rows: int
    actual_rows: Optional[int] = None
    row_counts_match: Optional[bool] = None
    checksum_checked: bool = False
    checksums_match: Optional[bool] = None
    error: Optional[str] = None

    @property
    def summary(self) -> str:
        """Which check actually failed, in words.

        The GUI used to report every unvalidated table as "expected N
        row(s), target has N" regardless of what went wrong, so a
        *checksum* mismatch was announced by printing two identical
        numbers -- a message that looks like a bug in the tool rather
        than a finding about the data.
        """
        if self.error:
            return f"validation could not run: {self.error}"
        parts = []
        if self.row_counts_match is False:
            parts.append(
                f"row count mismatch: {self.expected_rows} row(s) were sent, the target "
                f"has {self.actual_rows}")
        if self.checksum_checked and self.checksums_match is False:
            parts.append(
                f"row count matches ({self.expected_rows}) but the checksum does not, so "
                f"at least one value differs between source and target. Values that the "
                f"type mapping deliberately changes -- a date widened to a timestamp, a "
                f"CHAR blank-padded by the target, a TINYINT(1) becoming a real boolean -- "
                f"are already allowed for, so this is worth a look at the data itself")
        if not parts:
            return "validated"
        return "; ".join(parts)

    @property
    def ok(self) -> bool:
        """True only when every check that actually ran passed. A check
        that didn't run at all (actual_rows is None because the target has
        no count_rows() method, or the checksum was skipped for being over
        checksum_max_rows) is treated as "unverified", not as a failure --
        callers that care about the distinction should inspect the
        individual fields rather than relying on `ok` alone."""
        if self.error is not None:
            return False
        if self.row_counts_match is False:
            return False
        if self.checksum_checked and self.checksums_match is False:
            return False
        return True


def validate_table(
    target,
    table_name: str,
    columns: List[str],
    expected_rows: int,
    expected_checksum: Optional[int] = None,
    schema: Optional[str] = None,
    checksum_max_rows: int = 50000,
) -> ValidationResult:
    """Independently re-query `target` for `table_name` and compare against
    what migrate_table() believes it sent: `expected_rows` (a plain count)
    and, optionally, `expected_checksum` (migrate_table's own running
    table_checksum() over every row it streamed to insert_batch).

    A target connector without count_rows()/checksum_rows() (there is none
    among the six shipped connectors, but this stays defensive for any
    future one that might not implement them right away) degrades to an
    explicit "not verified" result via `error` rather than raising."""
    result = ValidationResult(table_name=table_name, expected_rows=expected_rows)
    count_rows = getattr(target, "count_rows", None)
    if count_rows is None:
        result.error = f"{type(target).__name__} has no count_rows() -- row-count validation skipped."
        return result

    try:
        result.actual_rows = count_rows(table_name, schema=schema)
        result.row_counts_match = result.actual_rows == expected_rows
    except Exception as exc:  # noqa: BLE001 - a failed validation query is reported, not fatal
        result.error = str(exc)
        return result

    if expected_checksum is not None and expected_rows <= checksum_max_rows:
        checksum_rows = getattr(target, "checksum_rows", None)
        if checksum_rows is not None:
            try:
                actual_checksum = checksum_rows(table_name, columns, schema=schema)
                result.checksum_checked = True
                result.checksums_match = actual_checksum == expected_checksum
            except Exception as exc:  # noqa: BLE001 - same tolerance as the row-count query above
                result.error = str(exc)

    return result
