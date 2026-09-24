"""Tests for tgdatabridge.db.sqlserver_connector.SqlServerConnector's pure-Python
logic (paramstyle translation, identifier quoting, schema default) that
doesn't require a real pyodbc driver or SQL Server instance -- pyodbc is
only imported lazily inside connect(), so these run fine without it
installed."""
from tgdatabridge.db.base import ConnectionParams
from tgdatabridge.db.sqlserver_connector import SqlServerConnector
from tgdatabridge.db.tls_config import TlsConfig


def _params(**overrides):
    base = dict(host="localhost", port=1433, database="testdb", username="sa", password="pw", schema=None)
    base.update(overrides)
    return ConnectionParams(**base)


class _Row(tuple):
    """Stands in for pyodbc.Row -- a tuple-like object that isn't literally
    a tuple, so fetch_batches's explicit tuple(r) conversion actually gets
    exercised the same way it is against a real pyodbc cursor."""


class _FakeCursor:
    def __init__(self, batches=None, fetchall_result=None, fetchone_result=None):
        self.executed = []
        self.executemany_calls = []
        self.fast_executemany = False
        self._batches = list(batches or [])
        self.description = [("col_a",), ("col_b",)]
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


def test_schema_name_defaults_to_dbo():
    conn = SqlServerConnector(_params())
    assert conn.schema_name == "dbo"


def test_schema_name_uses_provided_schema():
    conn = SqlServerConnector(_params(schema="sales"))
    assert conn.schema_name == "sales"


def test_translate_converts_named_params_to_qmark_in_order():
    conn = SqlServerConnector(_params())
    query, args = conn._translate(
        "SELECT * FROM t WHERE a = %(schema)s AND b = %(name)s", {"schema": "dbo", "name": "X"}
    )
    assert query == "SELECT * FROM t WHERE a = ? AND b = ?"
    assert args == ["dbo", "X"]


def test_translate_passes_through_when_no_params():
    conn = SqlServerConnector(_params())
    query, args = conn._translate("SELECT 1", None)
    assert query == "SELECT 1"
    assert args == []


def test_execute_uses_translated_query_and_returns_tuples():
    conn = SqlServerConnector(_params())
    conn._conn = _FakeConn()
    rows = conn.execute("SELECT name FROM t WHERE schema_name = %(schema)s", {"schema": "dbo"})
    assert rows == [("row1",), ("row2",)]
    executed_sql, executed_args = conn._conn.cursor_obj.executed[0]
    assert executed_sql == "SELECT name FROM t WHERE schema_name = ?"
    assert executed_args == ["dbo"]


def test_execute_ddl_runs_sql_as_is():
    conn = SqlServerConnector(_params())
    conn._conn = _FakeConn()
    conn.execute_ddl("CREATE OR ALTER PROCEDURE [P1] AS BEGIN SET NOCOUNT ON; END;")
    executed_sql, _ = conn._conn.cursor_obj.executed[0]
    assert executed_sql.startswith("CREATE OR ALTER PROCEDURE")


def test_insert_batch_uses_bracket_quoted_case_preserved_identifiers():
    conn = SqlServerConnector(_params())
    conn._conn = _FakeConn()
    conn.insert_batch("EMPLOYEES", ["EMP_ID", "NAME"], [(1, "Alice"), (2, "Bob")])
    sql, rows = conn._conn.cursor_obj.executemany_calls[0]
    assert sql == "INSERT INTO [EMPLOYEES] ([EMP_ID], [NAME]) VALUES (?, ?)"
    assert rows == [(1, "Alice"), (2, "Bob")]


def test_insert_batch_schema_qualifies_when_schema_given():
    conn = SqlServerConnector(_params(schema="dbo"))
    conn._conn = _FakeConn()
    conn.insert_batch("EMPLOYEES", ["EMP_ID"], [(1,)])
    sql, _rows = conn._conn.cursor_obj.executemany_calls[0]
    assert sql.startswith("INSERT INTO [dbo].[EMPLOYEES]")


def test_insert_batch_no_op_on_empty_rows():
    conn = SqlServerConnector(_params())
    conn._conn = _FakeConn()
    conn.insert_batch("EMPLOYEES", ["EMP_ID"], [])
    assert conn._conn.cursor_obj.executemany_calls == []


def test_fetch_batches_yields_columns_and_plain_tuples_until_exhausted():
    conn = SqlServerConnector(_params())
    conn._conn = _FakeConn(batches=[[_Row((1, "a")), _Row((2, "b"))], [_Row((3, "c"))]])
    results = list(conn.fetch_batches("SELECT col_a, col_b FROM t", batch_size=2))
    assert len(results) == 2
    columns0, rows0 = results[0]
    assert columns0 == ["col_a", "col_b"]
    assert rows0 == [(1, "a"), (2, "b")]
    assert all(type(r) is tuple for r in rows0)  # pyodbc.Row-likes converted to plain tuples
    columns1, rows1 = results[1]
    assert rows1 == [(3, "c")]


def test_fetch_batches_yields_nothing_for_an_empty_result():
    conn = SqlServerConnector(_params())
    conn._conn = _FakeConn(batches=[])
    results = list(conn.fetch_batches("SELECT col_a, col_b FROM t"))
    assert results == []


def test_count_rows_uses_bracket_quoted_schema_qualified_name():
    conn = SqlServerConnector(_params(schema="dbo"))
    conn._conn = _FakeConn(fetchone_result=(9,))
    count = conn.count_rows("EMPLOYEES")
    assert count == 9
    sql, _args = conn._conn.cursor_obj.executed[0]
    assert sql == "SELECT COUNT(*) FROM [dbo].[EMPLOYEES]"


def test_count_rows_schema_argument_overrides_connection_schema():
    conn = SqlServerConnector(_params(schema="dbo"))
    conn._conn = _FakeConn(fetchone_result=(1,))
    conn.count_rows("EMPLOYEES", schema="sales")
    sql, _args = conn._conn.cursor_obj.executed[0]
    assert sql == "SELECT COUNT(*) FROM [sales].[EMPLOYEES]"


def test_checksum_rows_matches_validation_table_checksum_and_applies_top():
    from tgdatabridge.core.validation import table_checksum

    rows = [(1, "Alice"), (2, "Bob")]
    conn = SqlServerConnector(_params())
    conn._conn = _FakeConn(fetchall_result=rows)
    checksum = conn.checksum_rows("EMPLOYEES", ["EMP_ID", "NAME"], sample_size=25)
    assert checksum == table_checksum(rows)
    sql, _args = conn._conn.cursor_obj.executed[0]
    assert sql == "SELECT TOP 25 [EMP_ID], [NAME] FROM [EMPLOYEES]"


# --------------------------------------------------------------- TLS / SSL


_DRIVER = "ODBC Driver 18 for SQL Server"


def test_default_conn_str_is_unchanged_encrypted_but_unverified():
    """The pre-existing, insecure-by-default behaviour is preserved when
    TLS is not explicitly turned on in the connection dialog -- flipping
    it unconditionally would break every saved connection to a server
    whose certificate isn't in a CA store this machine trusts. See this
    connector's own _conn_str docstring."""
    conn = SqlServerConnector(_params())
    conn_str = conn._conn_str(_DRIVER)
    assert "TrustServerCertificate=yes;" in conn_str
    assert "Encrypt=yes;" not in conn_str


def test_tls_enabled_verifies_the_certificate_by_default():
    conn = SqlServerConnector(_params(tls=TlsConfig(enabled=True)))
    conn_str = conn._conn_str(_DRIVER)
    assert "Encrypt=yes;" in conn_str
    assert "TrustServerCertificate=no;" in conn_str


def test_tls_enabled_with_verification_off_still_encrypts():
    conn = SqlServerConnector(_params(tls=TlsConfig(enabled=True, verify_cert=False)))
    conn_str = conn._conn_str(_DRIVER)
    assert "Encrypt=yes;" in conn_str
    assert "TrustServerCertificate=yes;" in conn_str


def test_tls_enabled_names_the_ca_certificate():
    conn = SqlServerConnector(_params(tls=TlsConfig(enabled=True, ca_cert_path="/ca.pem")))
    conn_str = conn._conn_str(_DRIVER)
    assert "Certificate=/ca.pem;" in conn_str


def test_tls_enabled_names_the_hostname_to_verify():
    conn = SqlServerConnector(_params(host="sql.internal", tls=TlsConfig(enabled=True, verify_hostname=True)))
    conn_str = conn._conn_str(_DRIVER)
    assert "HostNameInCertificate=sql.internal;" in conn_str


def test_tls_hostname_verification_uses_the_real_host_when_tunneled():
    """Unlike Oracle/MySQL/DB2, the Microsoft ODBC driver's
    HostNameInCertificate keyword lets the certificate's expected name be
    given independently of SERVER=, so a tunneled SQL Server connection
    *can* verify the real hostname."""
    conn = SqlServerConnector(_params(
        host="127.0.0.1",
        tls=TlsConfig(enabled=True, verify_hostname=True, server_host_override="sql.internal"),
    ))
    conn_str = conn._conn_str(_DRIVER)
    assert "SERVER=127.0.0.1,1433;" in conn_str
    assert "HostNameInCertificate=sql.internal;" in conn_str


def test_tls_no_hostname_keyword_when_hostname_verification_is_off():
    conn = SqlServerConnector(_params(tls=TlsConfig(enabled=True, verify_hostname=False)))
    conn_str = conn._conn_str(_DRIVER)
    assert "HostNameInCertificate=" not in conn_str
