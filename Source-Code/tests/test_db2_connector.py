"""Tests for tgdatabridge.db.db2_connector.Db2Connector's pure-Python logic
(paramstyle translation, identifier quoting, schema default) that doesn't
require a real ibm_db driver or Db2 instance -- ibm_db_dbi is only imported
lazily inside connect(), so these run fine without it installed."""
from tgdatabridge.db.base import ConnectionParams
from tgdatabridge.db.db2_connector import Db2Connector
from tgdatabridge.db.tls_config import TlsConfig


def _params(**overrides):
    base = dict(host="localhost", port=50000, database="testdb", username="db2inst1", password="pw", schema=None)
    base.update(overrides)
    return ConnectionParams(**base)


class _FakeCursor:
    def __init__(self, fetchone_result=None, batches=None, fetchall_result=None):
        self.executed = []
        self.executemany_calls = []
        self._fetchone_result = fetchone_result
        self._batches = list(batches or [])
        self.description = [("col_a",), ("col_b",)]
        self._fetchall_result = fetchall_result if fetchall_result is not None else [("row1",), ("row2",)]

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
    def __init__(self, fetchone_result=None, batches=None, fetchall_result=None):
        self.cursor_obj = _FakeCursor(fetchone_result, batches, fetchall_result=fetchall_result)

    def cursor(self):
        return self.cursor_obj


def test_schema_name_defaults_to_username_uppercased():
    conn = Db2Connector(_params(username="db2inst1"))
    assert conn.schema_name == "DB2INST1"


def test_schema_name_uses_provided_schema_uppercased():
    conn = Db2Connector(_params(schema="sales"))
    assert conn.schema_name == "SALES"


def test_translate_converts_named_params_to_qmark_in_order():
    conn = Db2Connector(_params())
    query, args = conn._translate(
        "SELECT * FROM t WHERE a = %(schema)s AND b = %(name)s", {"schema": "APP", "name": "X"}
    )
    assert query == "SELECT * FROM t WHERE a = ? AND b = ?"
    assert args == ["APP", "X"]


def test_translate_passes_through_when_no_params():
    conn = Db2Connector(_params())
    query, args = conn._translate("SELECT 1", None)
    assert query == "SELECT 1"
    assert args == []


def test_execute_uses_translated_query_and_returns_tuples():
    conn = Db2Connector(_params())
    conn._conn = _FakeConn()
    rows = conn.execute("SELECT name FROM t WHERE schema_name = %(schema)s", {"schema": "APP"})
    assert rows == [("row1",), ("row2",)]
    executed_sql, executed_args = conn._conn.cursor_obj.executed[0]
    assert executed_sql == "SELECT name FROM t WHERE schema_name = ?"
    assert executed_args == ["APP"]


def test_execute_ddl_runs_sql_as_is():
    conn = Db2Connector(_params())
    conn._conn = _FakeConn()
    conn.execute_ddl('CREATE OR REPLACE PROCEDURE "P1"() LANGUAGE SQL BEGIN SET X = 1; END;')
    executed_sql, _ = conn._conn.cursor_obj.executed[0]
    assert executed_sql.startswith("CREATE OR REPLACE PROCEDURE")


def test_insert_batch_uses_uppercase_quoted_identifiers():
    conn = Db2Connector(_params())
    conn._conn = _FakeConn()
    conn.insert_batch("employees", ["emp_id", "name"], [(1, "Alice"), (2, "Bob")])
    sql, rows = conn._conn.cursor_obj.executemany_calls[0]
    assert sql == 'INSERT INTO "EMPLOYEES" ("EMP_ID", "NAME") VALUES (?, ?)'
    assert rows == [(1, "Alice"), (2, "Bob")]


def test_insert_batch_schema_qualifies_when_schema_given():
    conn = Db2Connector(_params(schema="app"))
    conn._conn = _FakeConn()
    conn.insert_batch("EMPLOYEES", ["EMP_ID"], [(1,)])
    sql, _rows = conn._conn.cursor_obj.executemany_calls[0]
    assert sql.startswith('INSERT INTO "APP"."EMPLOYEES"')


def test_insert_batch_no_op_on_empty_rows():
    conn = Db2Connector(_params())
    conn._conn = _FakeConn()
    conn.insert_batch("EMPLOYEES", ["EMP_ID"], [])
    assert conn._conn.cursor_obj.executemany_calls == []


def test_fetch_batches_yields_columns_and_rows_until_exhausted():
    conn = Db2Connector(_params())
    conn._conn = _FakeConn(batches=[[(1, "a"), (2, "b")], [(3, "c")]])
    results = list(conn.fetch_batches("SELECT col_a, col_b FROM t", batch_size=2))
    assert len(results) == 2
    columns0, rows0 = results[0]
    assert columns0 == ["col_a", "col_b"]
    assert rows0 == [(1, "a"), (2, "b")]
    columns1, rows1 = results[1]
    assert rows1 == [(3, "c")]


def test_fetch_batches_yields_nothing_for_an_empty_result():
    conn = Db2Connector(_params())
    conn._conn = _FakeConn(batches=[])
    results = list(conn.fetch_batches("SELECT col_a, col_b FROM t"))
    assert results == []


def test_count_rows_uses_uppercase_quoted_schema_qualified_name():
    conn = Db2Connector(_params(schema="app"))
    conn._conn = _FakeConn(fetchone_result=(5,))
    count = conn.count_rows("employees")
    assert count == 5
    sql, _args = conn._conn.cursor_obj.executed[0]
    assert sql == 'SELECT COUNT(*) FROM "APP"."EMPLOYEES"'


def test_checksum_rows_matches_validation_table_checksum_and_applies_fetch_first():
    from tgdatabridge.core.validation import table_checksum

    rows = [(1, "Alice"), (2, "Bob")]
    conn = Db2Connector(_params())
    conn._conn = _FakeConn(fetchall_result=rows)
    checksum = conn.checksum_rows("EMPLOYEES", ["EMP_ID", "NAME"], sample_size=10)
    assert checksum == table_checksum(rows)
    sql, _args = conn._conn.cursor_obj.executed[0]
    assert sql == 'SELECT "EMP_ID", "NAME" FROM "EMPLOYEES" FETCH FIRST 10 ROWS ONLY'


# --------------------------------------------------------------- TLS / SSL


def test_conn_str_has_no_security_keyword_when_tls_is_off():
    conn = Db2Connector(_params())
    conn_str = conn._conn_str()
    assert "Security=SSL;" not in conn_str


def test_conn_str_adds_security_ssl_when_tls_is_on():
    conn = Db2Connector(_params(tls=TlsConfig(enabled=True)))
    conn_str = conn._conn_str()
    assert "Security=SSL;" in conn_str


def test_conn_str_names_the_ca_certificate():
    conn = Db2Connector(_params(tls=TlsConfig(enabled=True, ca_cert_path="/ca.pem")))
    conn_str = conn._conn_str()
    assert "SSLServerCertificate=/ca.pem;" in conn_str


def test_conn_str_no_ca_keyword_when_no_ca_path_given():
    conn = Db2Connector(_params(tls=TlsConfig(enabled=True)))
    conn_str = conn._conn_str()
    assert "SSLServerCertificate=" not in conn_str
