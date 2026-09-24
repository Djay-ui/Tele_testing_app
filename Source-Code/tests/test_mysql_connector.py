"""Tests for tgdatabridge.db.mysql_connector.MySQLConnector's pure-Python logic
(schema_name, batched fetch, insert building) that doesn't require a real
mysql-connector-python driver or MySQL instance -- mysql.connector is only
imported lazily inside connect(), so these run fine without it installed."""
from tgdatabridge.db.base import ConnectionParams
from tgdatabridge.db.mysql_connector import MySQLConnector
from tgdatabridge.db.tls_config import TlsConfig


def _params(**overrides):
    base = dict(host="localhost", port=3306, database="testdb", username="root", password="pw", schema=None)
    base.update(overrides)
    return ConnectionParams(**base)


class _FakeCursor:
    def __init__(self, batches=None, fetchall_result=None, fetchone_result=None):
        self.executed = []
        self.executemany_calls = []
        self._batches = list(batches or [])
        self.description = [("col_a",), ("col_b",)]
        self._fetchall_result = fetchall_result if fetchall_result is not None else [("row1",), ("row2",)]
        self._fetchone_result = fetchone_result

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

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


def test_schema_name_is_the_database_name():
    conn = MySQLConnector(_params(database="mydb"))
    assert conn.schema_name == "mydb"


def test_execute_returns_rows():
    conn = MySQLConnector(_params())
    conn._conn = _FakeConn()
    rows = conn.execute("SELECT 1", {"x": 1})
    assert rows == [("row1",), ("row2",)]


def test_insert_batch_uses_backtick_quoted_identifiers():
    conn = MySQLConnector(_params())
    conn._conn = _FakeConn()
    conn.insert_batch("employees", ["id", "name"], [(1, "Alice")])
    sql, rows = conn._conn.cursor_obj.executemany_calls[0]
    assert sql == "INSERT INTO `employees` (`id`, `name`) VALUES (%s, %s)"
    assert rows == [(1, "Alice")]


def test_insert_batch_no_op_on_empty_rows():
    conn = MySQLConnector(_params())
    conn._conn = _FakeConn()
    conn.insert_batch("employees", ["id"], [])
    assert conn._conn.cursor_obj.executemany_calls == []


def test_fetch_batches_yields_columns_and_rows_until_exhausted():
    conn = MySQLConnector(_params())
    conn._conn = _FakeConn(batches=[[(1, "a"), (2, "b")], [(3, "c")]])
    results = list(conn.fetch_batches("SELECT col_a, col_b FROM t", batch_size=2))
    assert len(results) == 2
    columns0, rows0 = results[0]
    assert columns0 == ["col_a", "col_b"]
    assert rows0 == [(1, "a"), (2, "b")]
    columns1, rows1 = results[1]
    assert rows1 == [(3, "c")]


def test_fetch_batches_yields_nothing_for_an_empty_result():
    conn = MySQLConnector(_params())
    conn._conn = _FakeConn(batches=[])
    results = list(conn.fetch_batches("SELECT col_a, col_b FROM t"))
    assert results == []


def test_count_rows_uses_backtick_quoted_table_name():
    conn = MySQLConnector(_params())
    conn._conn = _FakeConn(fetchone_result=(7,))
    count = conn.count_rows("employees")
    assert count == 7
    sql, _params_ = conn._conn.cursor_obj.executed[0]
    assert sql == "SELECT COUNT(*) FROM `employees`"


def test_checksum_rows_matches_validation_table_checksum_and_applies_limit():
    from tgdatabridge.core.validation import table_checksum

    rows = [(1, "Alice"), (2, "Bob")]
    conn = MySQLConnector(_params())
    conn._conn = _FakeConn(fetchall_result=rows)
    checksum = conn.checksum_rows("employees", ["id", "name"], sample_size=50)
    assert checksum == table_checksum(rows)
    sql, _params_ = conn._conn.cursor_obj.executed[0]
    assert sql == "SELECT `id`, `name` FROM `employees` LIMIT 50"


# --------------------------------------------------------------- TLS / SSL


def test_connect_kwargs_carry_no_ssl_options_when_tls_is_off():
    conn = MySQLConnector(_params())
    kwargs = conn._connect_kwargs()
    assert not any(k.startswith("ssl_") for k in kwargs)
    assert kwargs["use_pure"] is True


def test_connect_kwargs_include_ssl_paths_when_tls_is_on():
    conn = MySQLConnector(_params(
        host="mysql.internal",
        tls=TlsConfig(enabled=True, ca_cert_path="/ca.pem",
                      client_cert_path="/client.crt", client_key_path="/client.key"),
    ))
    kwargs = conn._connect_kwargs()
    assert kwargs["ssl_ca"] == "/ca.pem"
    assert kwargs["ssl_cert"] == "/client.crt"
    assert kwargs["ssl_key"] == "/client.key"
    assert kwargs["ssl_verify_cert"] is True
    # Not tunneled -- the effective hostname equals the dialed host, so a
    # real hostname check is possible and turned on.
    assert kwargs["ssl_verify_identity"] is True


def test_connect_kwargs_do_not_verify_identity_when_hostname_verification_is_off():
    conn = MySQLConnector(_params(tls=TlsConfig(enabled=True, verify_hostname=False)))
    kwargs = conn._connect_kwargs()
    assert kwargs["ssl_verify_cert"] is True
    assert kwargs["ssl_verify_identity"] is False


def test_connect_kwargs_do_not_verify_identity_when_tunneled():
    """The driver would be checking the certificate's name against
    "127.0.0.1" -- the tunnel's local end -- which can never match a real
    certificate, so identity verification is skipped rather than made to
    fail every tunneled connection. Encryption and CA verification still
    apply -- see this connector's own _connect_kwargs docstring."""
    conn = MySQLConnector(_params(
        host="127.0.0.1",
        tls=TlsConfig(enabled=True, verify_hostname=True,
                      server_host_override="mysql.internal"),
    ))
    kwargs = conn._connect_kwargs()
    assert kwargs["ssl_verify_cert"] is True
    assert kwargs["ssl_verify_identity"] is False
