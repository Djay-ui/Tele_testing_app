"""row_checksum's properties, and what happens when its algorithm changes.

The function is on the hot path of every validated migration -- once per
row on the source side as rows stream past, and once per row again on the
target side when validation re-reads the table -- so it was worth making
faster. It is also pure Python, so it holds the GIL, which is why a
parallel migration of many small tables felt slower than it should.

Speed is the easy half. The hard half is that checkpoints *persist*
per-shard checksums, and a resumed run reuses them (see
migrator._migrate_one_pooled's "a finished shard still has to contribute
its checksum"). Change the algorithm carelessly and a migration started
under an older build would XOR two incompatible values together and
report a data-integrity failure that never happened -- the single worst
false alarm this tool could raise. These tests pin down both the
properties the hash must keep and the behaviour that makes changing it
safe.
"""
import datetime
import decimal

from tgdatabridge.core.validation import (
    CHECKSUM_ALGORITHM, row_checksum, table_checksum, validate_table,
)
from tgdatabridge.utils import app_storage


# ------------------------------------------------------------ properties

def test_identical_rows_hash_identically():
    row = (1, "Acme", datetime.datetime(2026, 1, 1), decimal.Decimal("1.50"), True)
    assert row_checksum(row) == row_checksum(tuple(row))


def test_an_int_and_its_string_form_hash_differently():
    """The reason repr() is used rather than str(): a driver silently
    casting 1 to "1" somewhere in the round trip is exactly the corruption
    this check exists to catch, and str(1) == str("1") would hide it."""
    assert row_checksum((1,)) != row_checksum(("1",))
    assert row_checksum((1.0,)) != row_checksum((1,))
    assert row_checksum((None,)) != row_checksum(("",))
    # True/1 is deliberately NOT in that list any more: MySQL's
    # TINYINT(1) becomes a real BOOLEAN on PostgreSQL by design, so the
    # source hands back 1 and the target True for the same value. See
    # validation._canonical.


def test_column_order_changes_the_hash():
    assert row_checksum((1, 2)) != row_checksum((2, 1))


def test_the_hash_fits_in_64_bits():
    value = row_checksum(("some", "row", 12345))
    assert 0 <= value < 2 ** 64


def test_table_checksum_is_order_independent():
    """The property the whole design rests on: a target is free to return
    rows in a different order from the source."""
    rows = [(1, "a"), (2, "b"), (3, "c")]
    assert table_checksum(rows) == table_checksum(list(reversed(rows)))


def test_table_checksum_notices_a_changed_value():
    assert table_checksum([(1, "a"), (2, "b")]) != table_checksum([(1, "a"), (2, "B")])


def test_table_checksum_notices_a_missing_row():
    """The failure this is really guarding against -- a target driver
    silently dropping rows on write."""
    assert table_checksum([(1, "a"), (2, "b")]) != table_checksum([(1, "a")])


def test_the_hash_is_stable_across_processes():
    """Deliberately not Python's built-in hash(), which is randomised per
    process via PYTHONHASHSEED -- two runs, or a resumed run, would
    otherwise disagree about identical data."""
    import subprocess
    import sys

    code = (
        "import sys; sys.path.insert(0, '.');"
        "from tgdatabridge.core.validation import row_checksum;"
        "print(row_checksum((1, 'Acme', None, True)))"
    )
    outputs = {
        subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True, env={"PYTHONHASHSEED": seed, "PATH": ""}).stdout.strip()
        for seed in ("0", "1", "12345")
    }
    assert len(outputs) == 1, f"hash varied with PYTHONHASHSEED: {outputs}"
    assert outputs.pop() == str(row_checksum((1, "Acme", None, True)))


# --------------------------------------------------- algorithm versioning

def test_the_algorithm_is_identified(tmp_path):
    assert CHECKSUM_ALGORITHM


def test_a_saved_checkpoint_records_the_algorithm(tmp_path):
    cp = app_storage.MigrationCheckpoint(
        checkpoint_id="cid", schema_name="HR", source_engine="MySQL", target_engine="PostgreSQL")
    table = app_storage.TableCheckpoint(status="done", rows_copied=10)
    table.shards["0"] = app_storage.ShardCheckpoint(status="done", rows_copied=10, checksum=999)
    cp.tables["t"] = table
    app_storage.save_checkpoint(cp, base_dir=tmp_path)

    loaded = app_storage.load_checkpoint("cid", base_dir=tmp_path)
    assert loaded.checksum_algorithm == CHECKSUM_ALGORITHM
    assert loaded.checksums_stale is False
    assert loaded.tables["t"].shards["0"].checksum == 999   # same algorithm -> reusable


def test_a_checkpoint_from_a_different_algorithm_drops_its_checksums(tmp_path):
    """Progress is kept -- which batches landed on the target has nothing
    to do with how they were hashed -- but the checksums are discarded, so
    a resume can't compare two algorithms' values against each other."""
    cp = app_storage.MigrationCheckpoint(
        checkpoint_id="cid", schema_name="HR", source_engine="MySQL", target_engine="PostgreSQL")
    table = app_storage.TableCheckpoint(status="done", rows_copied=10, batches_completed=2)
    table.shards["0"] = app_storage.ShardCheckpoint(status="done", rows_copied=10, checksum=777)
    cp.tables["t"] = table
    app_storage.save_checkpoint(cp, base_dir=tmp_path)

    # Rewrite the file as an older build would have left it.
    import json
    path = tmp_path / "migration_checkpoints" / "cid.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["checksum_algorithm"] = "sha256-64/v1"
    path.write_text(json.dumps(raw), encoding="utf-8")

    loaded = app_storage.load_checkpoint("cid", base_dir=tmp_path)
    assert loaded.checksums_stale is True
    assert loaded.checksum_algorithm == CHECKSUM_ALGORITHM
    # Resumable progress survives...
    assert loaded.tables["t"].status == "done"
    assert loaded.tables["t"].rows_copied == 10
    assert loaded.tables["t"].batches_completed == 2
    # ...the incomparable checksum does not.
    assert loaded.tables["t"].shards["0"].checksum == 0


def test_a_checkpoint_predating_the_field_is_trusted(tmp_path):
    """A file with no algorithm recorded at all was written before this
    existed. Treating it as stale would needlessly skip validation on
    every in-flight migration at upgrade time; it is far more likely to
    have come from the build immediately preceding this one."""
    import json
    cp = app_storage.MigrationCheckpoint(
        checkpoint_id="cid", schema_name="HR", source_engine="MySQL", target_engine="PostgreSQL")
    table = app_storage.TableCheckpoint(status="done")
    table.shards["0"] = app_storage.ShardCheckpoint(status="done", checksum=999)
    cp.tables["t"] = table
    app_storage.save_checkpoint(cp, base_dir=tmp_path)
    path = tmp_path / "migration_checkpoints" / "cid.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    del raw["checksum_algorithm"]
    path.write_text(json.dumps(raw), encoding="utf-8")

    loaded = app_storage.load_checkpoint("cid", base_dir=tmp_path)
    assert loaded.checksums_stale is False
    assert loaded.tables["t"].shards["0"].checksum == 999


# ------------------------------------------- an unknown checksum validates

class _FakeTarget:
    def __init__(self, rows):
        self._rows = rows

    def count_rows(self, table, schema=None):
        return len(self._rows)

    def checksum_rows(self, table, columns, schema=None, sample_size=None):
        return table_checksum(self._rows)


def test_expected_checksum_none_skips_the_comparison_but_still_counts_rows():
    """What a stale-checksum resume falls back to: row counts are still
    validated, the checksum half is reported as not checked rather than
    as a mismatch."""
    target = _FakeTarget([(1, "a"), (2, "b")])
    result = validate_table(target, "t", ["id", "name"], expected_rows=2, expected_checksum=None)
    assert result.row_counts_match is True
    assert result.checksum_checked is False
    assert result.ok is True


def test_a_wrong_checksum_is_still_caught():
    target = _FakeTarget([(1, "a"), (2, "B")])
    result = validate_table(
        target, "t", ["id", "name"], expected_rows=2,
        expected_checksum=table_checksum([(1, "a"), (2, "b")]))
    assert result.row_counts_match is True
    assert result.checksums_match is False
    assert result.ok is False


# ---------------------------------------------------------------------
# The DATE round trip.
#
# The pivot is Oracle-flavoured, so a date-only source column becomes an
# Oracle DATE -- which always carries a time component -- and every
# target therefore maps it to a timestamp. type_mapping attaches an
# explicit warning saying so for MySQL, PostgreSQL, SQL Server and Db2
# alike. The source driver then hands back datetime.date and the target
# hands back datetime.datetime at midnight for the same value.
#
# row_checksum hashes repr(), so those two hashed differently and every
# table with a DATE column came back "Unvalidated" -- which reads exactly
# like data loss, on a table where nothing was wrong. Reproduced against
# a real PostgreSQL and a real MariaDB on the first run of the
# integration harness.

def test_a_date_and_the_timestamp_it_maps_to_hash_the_same():
    import datetime
    assert row_checksum((1, datetime.date(2024, 3, 17))) == \
           row_checksum((1, datetime.datetime(2024, 3, 17, 0, 0)))


def test_a_genuinely_truncated_time_still_mismatches():
    """The fix has to stay narrow. Widening a date to midnight is the
    documented mapping; a target that threw away a real 09:30 is data
    loss and must still be caught."""
    import datetime
    assert row_checksum((1, datetime.datetime(2024, 3, 17, 9, 30))) != \
           row_checksum((1, datetime.datetime(2024, 3, 17, 0, 0)))


def test_the_other_kinds_of_corruption_are_still_visible():
    """Nothing else is normalised, on purpose: an int that came back as a
    string, a Decimal that came back as a float, and a NULL that came
    back as an empty string are all corruption rather than mapping."""
    assert row_checksum((1,)) != row_checksum(("1",))
    assert row_checksum((None,)) != row_checksum(("",))


def test_the_algorithm_id_was_bumped_with_the_behaviour():
    """row_checksum's output changed for date-valued rows, so a
    checkpoint written by an older build must not be trusted as
    comparable. That is exactly what the id is for."""
    assert CHECKSUM_ALGORITHM == "blake2b64-v4"
