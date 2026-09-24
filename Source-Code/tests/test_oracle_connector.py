"""Tests for tgdatabridge.db.oracle_connector.OracleConnector's pure-Python logic
(schema-name fallback, insert_batch's identifier quoting/schema
qualification, fetch_batches' batching) that doesn't require a real
oracledb driver or Oracle instance -- oracledb is only imported lazily
inside connect(), so these run fine without it installed. OracleConnector
was source-only until Oracle became a valid target too; insert_batch is
the one genuinely new method being tested here, mirroring
tests/test_db2_connector.py's fake-cursor approach for the rest.

The "LOB materialization" section covers _materialize_lob/
_materialize_lobs_in_row and fetch_batches' use of them -- see
_materialize_lob's own docstring for the real bug this fixes (python-
oracledb's default LOB-locator-object fetch behavior breaking any other
connector's insert_batch bind parameters, and native-JSON columns
decoding to a bare dict/list with the same problem) and
ENTERPRISE_READINESS.md section 4 for the "LOB streaming" motivation.
A _FakeLOB stands in for a real oracledb.LOB object here -- detection is
duck-typed (has .read/.size/.getchunksize, matching the REAL driver's
actual attribute name, confirmed against an installed python-oracledb
4.0.2's `dir(oracledb.LOB)`), so a fake with that same shape exercises
the real code path without needing oracledb installed. An earlier
version of both this fake and the production code used `chunk_size`
instead -- a name no real LOB object has -- which meant this whole test
file was quietly testing nothing: the fake matched the (wrong) production
code, and a real Oracle connection's LOB values sailed straight through
unconverted into "cannot adapt type 'LOB'" errors at the target. See
test_materialize_lob_ignores_an_object_with_the_wrong_chunk_size_attribute
below, which pins the *old*, wrong attribute name to always be ignored,
specifically so this mismatch can't quietly come back."""
import ssl
import sys
import types

from tgdatabridge.db.base import ConnectionParams
from tgdatabridge.db.oracle_connector import (
    OracleConnector, _materialize_lob, _materialize_lobs_in_row,
)
from tgdatabridge.db.tls_config import TlsConfig


def _params(**overrides):
    base = dict(host="localhost", port=1521, database="orclpdb1", username="hr", password="pw", schema=None)
    base.update(overrides)
    return ConnectionParams(**base)


class _FakeCursor:
    def __init__(self, batches=None, fetchall_result=None, fetchone_result=None):
        self.executed = []
        self.executemany_calls = []
        self._batches = list(batches or [])
        self.description = [("col_a",), ("col_b",)]
        self.arraysize = None
        self._fetchall_result = fetchall_result if fetchall_result is not None else [("row1",), ("row2",)]
        self._fetchone_result = fetchone_result

    def execute(self, sql, args=None):
        self.executed.append((sql, args))

    def executemany(self, sql, rows):
        self.executemany_calls.append((sql, rows))

    def fetchall(self):
        return self._fetchall_result

    def fetchone(self):
        return self._fetchone_result

    def fetchmany(self, size):
        if not self._batches:
            return []
        return self._batches.pop(0)

    def close(self):
        pass


class _FakeConn:
    def __init__(self, batches=None, fetchall_result=None, fetchone_result=None):
        self.cursor_obj = _FakeCursor(batches, fetchall_result=fetchall_result, fetchone_result=fetchone_result)

    def cursor(self):
        return self.cursor_obj


# --------------------------------------------------------------- schema_name


def test_schema_name_defaults_to_username_uppercased():
    conn = OracleConnector(_params(username="hr"))
    assert conn.schema_name == "HR"


def test_schema_name_uses_provided_schema_as_is():
    conn = OracleConnector(_params(schema="SALES"))
    assert conn.schema_name == "SALES"


# ------------------------------------------------------------------ execute


def test_execute_returns_rows_and_passes_named_binds():
    conn = OracleConnector(_params())
    conn._conn = _FakeConn()
    rows = conn.execute("SELECT name FROM t WHERE owner = :owner", {"owner": "HR"})
    assert rows == [("row1",), ("row2",)]
    executed_sql, executed_args = conn._conn.cursor_obj.executed[0]
    assert executed_sql == "SELECT name FROM t WHERE owner = :owner"
    assert executed_args == {"owner": "HR"}


def test_execute_defaults_params_to_empty_dict():
    conn = OracleConnector(_params())
    conn._conn = _FakeConn()
    conn.execute("SELECT 1 FROM dual")
    _sql, executed_args = conn._conn.cursor_obj.executed[0]
    assert executed_args == {}


# -------------------------------------------------------------- execute_ddl


def test_execute_ddl_runs_sql_as_is():
    conn = OracleConnector(_params())
    conn._conn = _FakeConn()
    conn.execute_ddl('CREATE OR REPLACE PROCEDURE "P1" IS BEGIN NULL; END;')
    executed_sql, _ = conn._conn.cursor_obj.executed[0]
    assert executed_sql.startswith("CREATE OR REPLACE PROCEDURE")


# -------------------------------------------------------------- insert_batch


def test_insert_batch_uses_uppercase_quoted_identifiers_and_positional_binds():
    conn = OracleConnector(_params())
    conn._conn = _FakeConn()
    conn.insert_batch("employees", ["emp_id", "name"], [(1, "Alice"), (2, "Bob")])
    sql, rows = conn._conn.cursor_obj.executemany_calls[0]
    assert sql == 'INSERT INTO "EMPLOYEES" ("EMP_ID", "NAME") VALUES (:1, :2)'
    assert rows == [(1, "Alice"), (2, "Bob")]


def test_insert_batch_schema_qualifies_when_schema_given():
    conn = OracleConnector(_params(schema="app"))
    conn._conn = _FakeConn()
    conn.insert_batch("EMPLOYEES", ["EMP_ID"], [(1,)])
    sql, _rows = conn._conn.cursor_obj.executemany_calls[0]
    assert sql.startswith('INSERT INTO "APP"."EMPLOYEES"')
    assert sql == 'INSERT INTO "APP"."EMPLOYEES" ("EMP_ID") VALUES (:1)'


def test_insert_batch_no_schema_qualification_when_schema_blank():
    conn = OracleConnector(_params(schema=None))
    conn._conn = _FakeConn()
    conn.insert_batch("EMPLOYEES", ["EMP_ID"], [(1,)])
    sql, _rows = conn._conn.cursor_obj.executemany_calls[0]
    assert sql == 'INSERT INTO "EMPLOYEES" ("EMP_ID") VALUES (:1)'


def test_insert_batch_no_op_on_empty_rows():
    conn = OracleConnector(_params())
    conn._conn = _FakeConn()
    conn.insert_batch("EMPLOYEES", ["EMP_ID"], [])
    assert conn._conn.cursor_obj.executemany_calls == []


# ------------------------------------------------------------- fetch_batches


def test_fetch_batches_yields_columns_and_rows_until_exhausted():
    conn = OracleConnector(_params())
    conn._conn = _FakeConn(batches=[[(1, "a"), (2, "b")], [(3, "c")]])
    results = list(conn.fetch_batches("SELECT col_a, col_b FROM t", batch_size=2))
    assert len(results) == 2
    columns0, rows0 = results[0]
    assert columns0 == ["col_a", "col_b"]
    assert rows0 == [(1, "a"), (2, "b")]
    columns1, rows1 = results[1]
    assert rows1 == [(3, "c")]


def test_fetch_batches_yields_nothing_for_an_empty_result():
    conn = OracleConnector(_params())
    conn._conn = _FakeConn(batches=[])
    results = list(conn.fetch_batches("SELECT col_a, col_b FROM t"))
    assert results == []


def test_fetch_batches_raises_a_clear_error_instead_of_none_has_no_cursor():
    # The exact real-world failure: a source-connection reconnect (see
    # migrator._reconnect_callback / retry.py's own docstring on reconnect
    # failures being swallowed) can leave _conn at None -- close() worked,
    # the following connect() didn't. The next fetch_batches() call used to
    # raise a bare 'NoneType' object has no attribute 'cursor'; it must now
    # raise something that names the driver and is recognized by
    # retry.is_transient() as retriable, not a cryptic crash.
    conn = OracleConnector(_params())
    assert conn._conn is None
    try:
        list(conn.fetch_batches("SELECT col_a FROM t"))
    except RuntimeError as exc:
        assert "not connected to database" in str(exc)
        assert "Oracle" in str(exc)
    else:
        raise AssertionError("expected a RuntimeError, fetch_batches() did not raise")


# ------------------------------------------------------ LOB materialization


class _FakeLOB:
    """Duck-typed stand-in for a real oracledb.LOB locator object -- has
    exactly the .read()/.size()/.getchunksize() surface _materialize_lob
    detects and uses, nothing more. `getchunksize` (not `chunk_size`) is
    the real python-oracledb attribute name -- see this module's own
    docstring for why that distinction matters here specifically."""

    def __init__(self, data, chunk=8):
        self.data = data
        self._chunk = chunk

    def size(self):
        return len(self.data)

    def getchunksize(self):
        return self._chunk

    def read(self, offset=1, amount=None):
        if amount is None:
            return self.data[offset - 1:]
        return self.data[offset - 1:offset - 1 + amount]


class _WrongAttrLOB:
    """Has the OLD, INCORRECT attribute name (`chunk_size`) this codebase's
    LOB detection used before this fix -- a real oracledb.LOB object never
    has this attribute (it has `getchunksize`, no underscore, a verb).
    _materialize_lob must NOT treat this as a LOB: if it did, that would be
    this exact regression creeping back in a different form."""

    def __init__(self, data):
        self.data = data

    def size(self):
        return len(self.data)

    def chunk_size(self):
        return 8

    def read(self, offset=1, amount=None):
        return self.data


def test_materialize_lob_reads_a_str_backed_clob_in_chunks():
    clob = _FakeLOB("Hello, this is a CLOB value longer than one chunk.", chunk=6)
    assert _materialize_lob(clob) == clob.data


def test_materialize_lob_reads_a_bytes_backed_blob_in_chunks():
    blob = _FakeLOB(bytes(range(50)), chunk=7)
    assert _materialize_lob(blob) == blob.data


def test_materialize_lob_handles_empty_lob():
    empty = _FakeLOB("", chunk=10)
    assert _materialize_lob(empty) == ""


def test_materialize_lob_passes_through_non_lob_values_unchanged():
    assert _materialize_lob(42) == 42
    assert _materialize_lob("plain string") == "plain string"
    assert _materialize_lob(None) is None
    assert _materialize_lob(b"plain bytes") == b"plain bytes"


def test_materialize_lob_ignores_an_object_with_the_wrong_chunk_size_attribute():
    """Regression test for the real bug this round: production code (and
    the test fake standing in for the driver) used to check for
    `chunk_size`, an attribute no real oracledb.LOB object has -- so real
    LOB values were never converted at all and reached the target driver
    raw, failing with "cannot adapt type 'LOB'". This object has that
    same wrong attribute and nothing else a real LOB has (no
    `getchunksize`); it must be passed through unchanged, not "detected"
    as a LOB by accident."""
    fake = _WrongAttrLOB("some clob-shaped data")
    assert _materialize_lob(fake) is fake


def test_materialize_lob_serializes_a_native_json_dict_column():
    """Oracle's native JSON column type (DB_TYPE_JSON, 21c+) decodes
    straight into a Python dict via python-oracledb -- same class of bug
    as the LOB one: no target driver this tool inserts through can adapt
    a bare dict for a text bind parameter."""
    import json
    value = {"active": True, "tags": ["a", "b"], "count": 3}
    result = _materialize_lob(value)
    assert isinstance(result, str)
    assert json.loads(result) == value


def test_materialize_lob_serializes_a_native_json_list_column():
    import json
    value = [1, 2, {"nested": True}]
    result = _materialize_lob(value)
    assert isinstance(result, str)
    assert json.loads(result) == value


def test_materialize_lobs_in_row_converts_only_lob_columns():
    clob = _FakeLOB("clob value", chunk=4)
    row = (1, "Alice", clob)
    result = _materialize_lobs_in_row(row)
    assert result == (1, "Alice", "clob value")


def test_materialize_lobs_in_row_fast_path_when_no_lobs_present():
    row = (1, "Bob", "text", 3.14)
    result = _materialize_lobs_in_row(row)
    assert result is row  # same object -- fast path, no allocation


def test_materialize_lobs_in_row_converts_a_native_json_column_too():
    import json
    row = (1, "Alice", {"role": "admin"})
    result = _materialize_lobs_in_row(row)
    assert result[0] == 1 and result[1] == "Alice"
    assert json.loads(result[2]) == {"role": "admin"}


def test_fetch_batches_materializes_lob_columns_before_yielding():
    clob = _FakeLOB("a clob body", chunk=4)
    conn = OracleConnector(_params())
    conn._conn = _FakeConn(batches=[[(1, clob)]])
    columns, rows = next(conn.fetch_batches("SELECT id, body FROM t"))
    assert rows == [(1, "a clob body")]
    # The real LOB object itself must never leak out to a caller (e.g.
    # migrator.migrate_table, which would hand it straight to another
    # connector's insert_batch bind parameters).
    assert not any(isinstance(v, _FakeLOB) for row in rows for v in row)


# --------------------------------------------------------------- count_rows


def test_count_rows_uses_uppercase_quoted_schema_qualified_name():
    conn = OracleConnector(_params(schema="app"))
    conn._conn = _FakeConn(fetchone_result=(3,))
    count = conn.count_rows("employees")
    assert count == 3
    sql, _args = conn._conn.cursor_obj.executed[0]
    assert sql == 'SELECT COUNT(*) FROM "APP"."EMPLOYEES"'


def test_count_rows_no_schema_qualification_when_schema_blank():
    conn = OracleConnector(_params(schema=None))
    conn._conn = _FakeConn(fetchone_result=(0,))
    conn.count_rows("EMPLOYEES")
    sql, _args = conn._conn.cursor_obj.executed[0]
    assert sql == 'SELECT COUNT(*) FROM "EMPLOYEES"'


# --------------------------------------------------------------- TLS / SSL


def test_dsn_uses_plain_tcp_when_tls_is_not_configured():
    conn = OracleConnector(_params(host="ora.internal", port=1521, database="ORCLPDB1"))
    assert conn._dsn() == "tcp://ora.internal:1521/ORCLPDB1"


def test_dsn_uses_tcps_when_tls_is_enabled():
    conn = OracleConnector(_params(
        host="ora.internal", port=1521, database="ORCLPDB1", tls=TlsConfig(enabled=True)))
    assert conn._dsn() == "tcps://ora.internal:1521/ORCLPDB1"


def test_connect_kwargs_carry_no_ssl_context_when_tls_is_off():
    conn = OracleConnector(_params())
    kwargs = conn._connect_kwargs()
    assert "ssl_context" not in kwargs
    assert "ssl_server_dn_match" not in kwargs


def test_connect_kwargs_build_a_real_ssl_context_when_tls_is_on():
    conn = OracleConnector(_params(tls=TlsConfig(enabled=True, verify_hostname=True)))
    kwargs = conn._connect_kwargs()
    assert isinstance(kwargs["ssl_context"], ssl.SSLContext)
    assert kwargs["ssl_server_dn_match"] is True


def test_connect_kwargs_disable_dn_match_when_hostname_verification_is_off():
    conn = OracleConnector(_params(
        tls=TlsConfig(enabled=True, verify_cert=True, verify_hostname=False)))
    kwargs = conn._connect_kwargs()
    assert kwargs["ssl_server_dn_match"] is False


def test_connect_kwargs_enable_dead_connection_detection():
    """A migration that streams a LOB-heavy table for a long time must not
    be able to hang forever on a silently-dropped connection (see this
    module's connect()/_connect_kwargs docstrings) -- expire_time makes a
    dead connection surface as a real, already-retried DPY-4011 instead."""
    conn = OracleConnector(_params())
    kwargs = conn._connect_kwargs()
    assert kwargs["expire_time"] == 2
    assert kwargs["tcp_connect_timeout"] == 30


def test_checksum_rows_matches_validation_table_checksum_and_applies_fetch_first():
    from tgdatabridge.core.validation import table_checksum

    rows = [(1, "Alice"), (2, "Bob")]
    conn = OracleConnector(_params())
    conn._conn = _FakeConn(fetchall_result=rows)
    checksum = conn.checksum_rows("EMPLOYEES", ["EMP_ID", "NAME"], sample_size=10)
    assert checksum == table_checksum(rows)
    sql, _args = conn._conn.cursor_obj.executed[0]
    assert sql == 'SELECT "EMP_ID", "NAME" FROM "EMPLOYEES" FETCH FIRST 10 ROWS ONLY'


def test_checksum_rows_materializes_a_native_json_dict_column_before_hashing():
    """Regression test: without running checksum_rows' fetched rows
    through _materialize_lobs_in_row, a table with a native JSON column
    would checksum a raw Python dict here on the source side against the
    plain JSON text migrate_table actually wrote to the target -- and
    those never hash equal, so the table would show "Unvalidated" on
    every single run, forever, even with correctly migrated data. This
    pins the fix: the checksum here must match table_checksum() run over
    the already-serialized (str) equivalent, the same shape the target
    connector's own checksum_rows would see."""
    from tgdatabridge.core.validation import table_checksum

    raw_rows = [(1, {"active": True, "tags": ["a", "b"]}), (2, {"active": False, "tags": []})]
    serialized_rows = [_materialize_lobs_in_row(r) for r in raw_rows]

    conn = OracleConnector(_params())
    conn._conn = _FakeConn(fetchall_result=raw_rows)
    checksum = conn.checksum_rows("FEATURE_TEST_DATA", ["ID", "PAYLOAD"])

    assert checksum == table_checksum(serialized_rows)
    assert checksum != table_checksum(raw_rows)  # would have been the old, wrong result


def test_checksum_rows_materializes_a_lob_column_too():
    clob = _FakeLOB("hello", chunk=2)
    raw_rows = [(1, clob)]

    conn = OracleConnector(_params())
    conn._conn = _FakeConn(fetchall_result=raw_rows)
    checksum = conn.checksum_rows("CUSTOMER_NOTES", ["ID", "NOTE"])

    from tgdatabridge.core.validation import table_checksum
    assert checksum == table_checksum([(1, "hello")])


# ------------------------------------------------------- connect() / speed


def test_connect_disables_fetch_lobs_before_connecting(monkeypatch):
    """The actual fix for "migration takes a long time on tables with
    CLOB/BLOB columns": connect() must set `oracledb.defaults.fetch_lobs
    = False` before it ever calls oracledb.connect(). Without it, every
    CLOB/BLOB/NCLOB value costs one or more *extra* database round-trips
    (one per _materialize_lob's read() call, chunked at the LOB's own
    getchunksize()) on top of the row fetch that already contained it --
    with it, the driver returns the value pre-materialized as part of
    the normal row fetch, and _materialize_lob's LOB branch is only ever
    a fallback (see that function's own docstring)."""
    fake_oracledb = types.ModuleType("oracledb")

    class _Defaults:
        fetch_lobs = True  # the driver's own real default, before connect() runs

    fake_oracledb.defaults = _Defaults()
    connect_calls = []
    fake_oracledb.connect = lambda **kwargs: connect_calls.append(kwargs) or object()
    monkeypatch.setitem(sys.modules, "oracledb", fake_oracledb)

    conn = OracleConnector(_params())
    conn.connect()

    assert fake_oracledb.defaults.fetch_lobs is False
    assert len(connect_calls) == 1
