"""Tests for the parts of PostgresConnector that don't need a live database:
the SQL string it builds for insert_batch(). Mirrors ddl_generator's fix —
identifiers must be lowercased to match the lowercase-quoted tables/columns
ddl_generator actually creates, or "Migrate Data" hits the same
relation-does-not-exist mismatch that "Apply DDL" did before that fix."""
from tgdatabridge.db.base import ConnectionParams
from tgdatabridge.db.postgres_connector import PostgresConnector
from tgdatabridge.db.tls_config import TlsConfig


class _FakeCopy:
    """Stands in for the context manager psycopg3's `cursor.copy()`
    returns. Records every row written so tests can assert the whole
    batch reached the COPY stream, and whether the block was exited
    properly (psycopg only finalizes/flushes a COPY on clean exit, so a
    connector that forgets the `with` would silently write nothing)."""

    def __init__(self, sink_rows):
        self.sink_rows = sink_rows
        self.entered = False
        self.exited = False
        self.types = None

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, exc_type, exc, tb):
        self.exited = True
        return False

    def set_types(self, types):
        self.types = list(types)

    def write_row(self, row):
        self.sink_rows.append(tuple(row))


class _FakeCursor:
    def __init__(self, sink, batches=None, fetchone_result=None, fetchall_result=None):
        self.sink = sink
        self._batches = list(batches or [])
        self.description = [("col_a",), ("col_b",)]
        self.last_sql = None
        self._fetchone_result = fetchone_result
        self._fetchall_result = fetchall_result if fetchall_result is not None else []
        self.copied_rows: list = []
        self.copy_objects: list = []
        self.closed = False

    # Columns the fake target "has", as (name, pg_type.typname) -- what
    # PostgresConnector._column_types reads to feed COPY's set_types().
    # Tests that need a specific type override this.
    column_types = {"account_id": "int4", "name": "varchar", "col_a": "int4", "col_b": "varchar"}

    def execute(self, sql, params=None):
        self.last_sql = sql
        if "pg_attribute" in sql:
            # The catalogue lookup, not part of the SQL under test.
            self._catalogue_result = [(n, t) for n, t in self.column_types.items()]
            return
        self.sink.append(sql)

    def executemany(self, sql, rows):
        self.sink.append(sql)

    def copy(self, sql):
        self.sink.append(sql)
        copy = _FakeCopy(self.copied_rows)
        self.copy_objects.append(copy)
        return copy

    def fetchmany(self, size):
        if not self._batches:
            return []
        return self._batches.pop(0)

    def fetchone(self):
        return self._fetchone_result

    def fetchall(self):
        catalogue = getattr(self, "_catalogue_result", None)
        if catalogue is not None:
            self._catalogue_result = None
            return catalogue
        return self._fetchall_result

    def close(self):
        self.closed = True


class _FakeConnection:
    def __init__(self, sink=None, batches=None, fetchone_result=None, fetchall_result=None):
        self.sink = sink if sink is not None else []
        self.cursor_obj = _FakeCursor(
            self.sink, batches, fetchone_result=fetchone_result, fetchall_result=fetchall_result)

    def cursor(self):
        return self.cursor_obj


def _connector_with_fake_conn():
    params = ConnectionParams(host="x", port=5432, database="db", username="u", password="p")
    connector = PostgresConnector(params)
    sink: list[str] = []
    connector._conn = _FakeConnection(sink)
    return connector, sink


def test_insert_batch_lowercases_table_and_column_names():
    connector, sink = _connector_with_fake_conn()

    connector.insert_batch("ACCOUNT", ["ACCOUNT_ID", "CUSTOMER_ID"], [(1, 2)])

    assert len(sink) == 1
    sql = sink[0]
    assert '"account"' in sql
    assert '"account_id"' in sql
    assert '"customer_id"' in sql
    assert '"ACCOUNT"' not in sql
    assert '"ACCOUNT_ID"' not in sql


# ------------------------------------------------- COPY-based bulk load
# insert_batch used to build an INSERT ... VALUES and call executemany;
# it now streams the batch through COPY FROM STDIN, which is roughly an
# order of magnitude faster and is the single biggest lever for a large
# migration (see SCALE.md section 1.1). The signature is unchanged, so
# migrator.migrate_table's checkpoint/retry/validation logic is untouched.


def test_insert_batch_uses_copy_not_insert():
    connector, sink = _connector_with_fake_conn()

    connector.insert_batch("ACCOUNT", ["ACCOUNT_ID"], [(1,)])

    sql = sink[0]
    assert sql.startswith("COPY ")
    assert "FROM STDIN" in sql
    assert "INSERT INTO" not in sql


def test_insert_batch_copies_in_binary_format():
    # Binary avoids a text encode here and a text parse on the server for
    # every numeric/timestamp/boolean in the batch.
    connector, sink = _connector_with_fake_conn()

    connector.insert_batch("ACCOUNT", ["ACCOUNT_ID"], [(1,)])

    assert "(FORMAT BINARY)" in sink[0]


def test_insert_batch_writes_every_row_to_the_copy_stream():
    connector, _ = _connector_with_fake_conn()
    rows = [(1, "a"), (2, "b"), (3, "c")]

    connector.insert_batch("ACCOUNT", ["ACCOUNT_ID", "NAME"], rows)

    assert connector._conn.cursor_obj.copied_rows == rows


def test_insert_batch_row_order_is_preserved():
    connector, _ = _connector_with_fake_conn()
    rows = [(i,) for i in range(50)]

    connector.insert_batch("T", ["ID"], rows)

    assert connector._conn.cursor_obj.copied_rows == rows


def test_insert_batch_enters_and_exits_the_copy_block():
    # psycopg only finalizes and flushes a COPY on clean exit of the
    # context manager -- writing rows without it would silently drop the
    # whole batch while appearing to succeed.
    connector, _ = _connector_with_fake_conn()

    connector.insert_batch("ACCOUNT", ["ACCOUNT_ID"], [(1,)])

    copy = connector._conn.cursor_obj.copy_objects[0]
    assert copy.entered is True
    assert copy.exited is True


def test_insert_batch_empty_rows_is_a_no_op():
    connector, sink = _connector_with_fake_conn()

    connector.insert_batch("ACCOUNT", ["ACCOUNT_ID"], [])

    assert sink == []
    assert connector._conn.cursor_obj.copy_objects == []


def test_insert_batch_closes_the_cursor_even_when_copy_raises():
    # A failing batch is normal (migrate_table retries it) -- leaking a
    # cursor per failure would exhaust the connection over a long run.
    connector, _ = _connector_with_fake_conn()

    def exploding_copy(sql):
        raise RuntimeError("COPY failed")

    connector._conn.cursor_obj.copy = exploding_copy
    try:
        connector.insert_batch("ACCOUNT", ["ACCOUNT_ID"], [(1,)])
        assert False, "expected the COPY failure to propagate"
    except RuntimeError as exc:
        assert "COPY failed" in str(exc)
    assert connector._conn.cursor_obj.closed is True


def test_schema_name_defaults_to_public():
    params = ConnectionParams(host="x", port=5432, database="db", username="u", password="p", schema=None)
    connector = PostgresConnector(params)
    assert connector.schema_name == "public"


def test_schema_name_uses_configured_schema():
    params = ConnectionParams(host="x", port=5432, database="db", username="u", password="p", schema="app")
    connector = PostgresConnector(params)
    assert connector.schema_name == "app"


def test_fetch_batches_yields_columns_and_rows_until_exhausted():
    params = ConnectionParams(host="x", port=5432, database="db", username="u", password="p")
    connector = PostgresConnector(params)
    connector._conn = _FakeConnection(batches=[[(1, "a"), (2, "b")], [(3, "c")]])
    results = list(connector.fetch_batches("SELECT col_a, col_b FROM t", batch_size=2))
    assert len(results) == 2
    columns0, rows0 = results[0]
    assert columns0 == ["col_a", "col_b"]
    assert rows0 == [(1, "a"), (2, "b")]
    columns1, rows1 = results[1]
    assert rows1 == [(3, "c")]


def test_fetch_batches_yields_nothing_for_an_empty_result():
    params = ConnectionParams(host="x", port=5432, database="db", username="u", password="p")
    connector = PostgresConnector(params)
    connector._conn = _FakeConnection(batches=[])
    results = list(connector.fetch_batches("SELECT col_a, col_b FROM t"))
    assert results == []


# --------------------------------------------------------------- count_rows


def test_count_rows_lowercases_table_name_and_returns_count():
    params = ConnectionParams(host="x", port=5432, database="db", username="u", password="p")
    connector = PostgresConnector(params)
    connector._conn = _FakeConnection(fetchone_result=(42,))
    count = connector.count_rows("ACCOUNT")
    assert count == 42
    assert 'SELECT COUNT(*) FROM "account"' == connector._conn.cursor_obj.last_sql


def test_checksum_rows_matches_validation_table_checksum():
    from tgdatabridge.core.validation import table_checksum

    params = ConnectionParams(host="x", port=5432, database="db", username="u", password="p")
    connector = PostgresConnector(params)
    rows = [(1, "a"), (2, "b")]
    connector._conn = _FakeConnection(fetchall_result=rows)
    checksum = connector.checksum_rows("ACCOUNT", ["ACCOUNT_ID", "NAME"])
    assert checksum == table_checksum(rows)
    sql = connector._conn.cursor_obj.last_sql
    assert 'FROM "account"' in sql
    assert '"account_id"' in sql and '"name"' in sql
    assert "LIMIT" not in sql


def test_checksum_rows_applies_sample_size_as_limit():
    params = ConnectionParams(host="x", port=5432, database="db", username="u", password="p")
    connector = PostgresConnector(params)
    connector._conn = _FakeConnection(fetchall_result=[])
    connector.checksum_rows("ACCOUNT", ["ACCOUNT_ID"], sample_size=100)
    assert "LIMIT 100" in connector._conn.cursor_obj.last_sql


# --------------------------------------------------------------- TLS / SSL


def _params(**overrides):
    base = dict(host="pg.internal", port=5432, database="app", username="u", password="p")
    base.update(overrides)
    return ConnectionParams(**base)


def test_connect_kwargs_have_no_sslmode_when_tls_is_off():
    conn = PostgresConnector(_params())
    kwargs = conn._connect_kwargs()
    assert "sslmode" not in kwargs
    assert kwargs["host"] == "pg.internal"
    assert "hostaddr" not in kwargs


def test_connect_kwargs_use_verify_full_by_default_when_tls_is_on():
    conn = PostgresConnector(_params(tls=TlsConfig(enabled=True)))
    kwargs = conn._connect_kwargs()
    assert kwargs["sslmode"] == "verify-full"


def test_connect_kwargs_use_require_when_certificate_verification_is_off():
    conn = PostgresConnector(_params(tls=TlsConfig(enabled=True, verify_cert=False)))
    kwargs = conn._connect_kwargs()
    assert kwargs["sslmode"] == "require"


def test_connect_kwargs_use_verify_ca_when_only_hostname_check_is_off():
    conn = PostgresConnector(_params(
        tls=TlsConfig(enabled=True, verify_cert=True, verify_hostname=False)))
    kwargs = conn._connect_kwargs()
    assert kwargs["sslmode"] == "verify-ca"


def test_connect_kwargs_carry_the_cert_paths():
    conn = PostgresConnector(_params(tls=TlsConfig(
        enabled=True, ca_cert_path="/ca.pem",
        client_cert_path="/client.crt", client_key_path="/client.key")))
    kwargs = conn._connect_kwargs()
    assert kwargs["sslrootcert"] == "/ca.pem"
    assert kwargs["sslcert"] == "/client.crt"
    assert kwargs["sslkey"] == "/client.key"


def test_connect_kwargs_split_host_and_hostaddr_when_tunneled():
    """Unlike MySQL/Oracle/DB2, libpq can verify the real hostname even
    through a tunnel: `host` carries the name to check the certificate
    against, `hostaddr` carries the actual address to dial -- see this
    connector's own _connect_kwargs docstring."""
    conn = PostgresConnector(_params(
        host="127.0.0.1",
        tls=TlsConfig(enabled=True, verify_hostname=True, server_host_override="pg.internal"),
    ))
    kwargs = conn._connect_kwargs()
    assert kwargs["host"] == "pg.internal"
    assert kwargs["hostaddr"] == "127.0.0.1"
    assert kwargs["sslmode"] == "verify-full"


def test_connect_kwargs_no_hostaddr_split_when_not_tunneled():
    conn = PostgresConnector(_params(tls=TlsConfig(enabled=True, verify_hostname=True)))
    kwargs = conn._connect_kwargs()
    assert kwargs["host"] == "pg.internal"
    assert "hostaddr" not in kwargs


def test_connect_kwargs_enable_dead_connection_detection():
    """A migration writing a LOB-heavy table for a long time must not be
    able to hang forever on a silently-dropped connection (see this
    module's _connect_kwargs docstring) -- libpq's own keepalive knobs turn
    a dead connection into a real, already-recognized-as-transient timeout
    error instead of a write() that never returns."""
    conn = PostgresConnector(_params())
    kwargs = conn._connect_kwargs()
    assert kwargs["keepalives"] == 1
    assert kwargs["keepalives_idle"] == 30
    assert kwargs["keepalives_interval"] == 10
    assert kwargs["keepalives_count"] == 3


def test_connect_kwargs_relax_synchronous_commit_for_bulk_loading():
    """This connector is bulk-migration-only, never a transaction someone
    else is depending on being durable the instant it commits -- see this
    module's _connect_kwargs docstring for the throughput/durability
    trade-off and why it's safe here specifically."""
    conn = PostgresConnector(_params())
    kwargs = conn._connect_kwargs()
    assert kwargs["options"] == "-c synchronous_commit=off"
