"""IBM Db2 (LUW) target connector, built on ibm_db_dbi -- the DB-API 2.0
wrapper shipped inside the `ibm_db` package.

Unlike pyodbc (SQL Server), `ibm_db` bundles the Db2 client libraries it
needs itself rather than depending on a separately-installed OS-level
driver, so there's no equivalent "install an ODBC driver first" caveat here.
"""
from __future__ import annotations

import re
from typing import Iterable, Optional

from tgdatabridge.db.base import ConnectionParams, require_connected
from tgdatabridge.utils.identifiers import quote_double

# Connector-agnostic callers (see tgdatabridge.core.target_introspector) write
# queries using %(name)s-style named placeholders, matching psycopg/
# mysql-connector's paramstyle. ibm_db_dbi only supports positional '?'
# placeholders (qmark paramstyle), so named params are translated here
# rather than forcing every caller to special-case Db2's paramstyle --
# mirrors SqlServerConnector._translate exactly.
_NAMED_PARAM_RE = re.compile(r"%\((\w+)\)s")


class Db2Connector:
    def __init__(self, params: ConnectionParams):
        self.params = params
        self._conn = None

    def _conn_str(self) -> str:
        """The Db2 CLI/ODBC connection string, built without touching
        ibm_db_dbi -- testable with no driver installed, mirroring every
        other connector's own `_connect_kwargs`/`_conn_str`.

        TLS/SSL: the Db2 CLI keyword is `Security=SSL`, plus
        `SSLServerCertificate=<path>` to name a specific CA/self-signed
        certificate to trust instead of the OS trust store -- ibm_db has
        no keyword for turning certificate verification off altogether
        (unlike sqlserver_connector's TrustServerCertificate or
        mysql_connector's ssl_verify_cert): `Security=SSL` always
        validates the chain. It also has no separate mutual-TLS
        (client certificate) keyword and no way to name a hostname to
        verify independently of `HOSTNAME=` the way SQL Server's
        HostNameInCertificate does -- so, same limitation as Oracle's and
        MySQL's connectors, a Db2 connection made through an SSH tunnel
        gets encryption and CA-signature checking, but the Db2 client's
        own hostname check (if any) would be against "127.0.0.1" rather
        than the real database address.
        """
        conn_str = (
            f"DATABASE={self.params.database};"
            f"HOSTNAME={self.params.host};"
            f"PORT={self.params.port};"
            f"PROTOCOL=TCPIP;"
            f"UID={self.params.username};"
            f"PWD={self.params.password};"
        )
        tls = self.params.tls
        if tls and tls.enabled:
            conn_str += "Security=SSL;"
            if tls.ca_cert_path:
                conn_str += f"SSLServerCertificate={tls.ca_cert_path};"
        return conn_str

    def connect(self) -> None:
        import ibm_db_dbi  # lazy import so the GUI can start without the driver installed

        self._conn = ibm_db_dbi.connect(self._conn_str(), "", "")

        # Ensure the target schema exists before anything else runs against
        # it -- mirrors PostgresConnector/SqlServerConnector's own
        # CREATE-SCHEMA-if-missing guard. Db2's own default schema (when
        # none is given) is already the connecting user's ID, so there's
        # nothing to create in that case.
        target_schema = (self.params.schema or self.params.username or "").upper()
        if target_schema and target_schema != (self.params.username or "").upper():
            cur = self._conn.cursor()
            cur.execute("SELECT 1 FROM SYSCAT.SCHEMATA WHERE SCHEMANAME = ?", [target_schema])
            exists = cur.fetchone() is not None
            if not exists:
                cur.execute(f"CREATE SCHEMA {quote_double(target_schema)}")
            cur.close()

    @property
    def schema_name(self) -> str:
        # Db2's own default schema (when none is explicitly given) is the
        # connecting user's ID, upper-cased -- unlike Postgres's "public" or
        # SQL Server's "dbo", there's no engine-wide fixed default name.
        return (self.params.schema or self.params.username or "").upper()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def test_connection(self) -> tuple[bool, str]:
        try:
            self.connect()
            cur = self._conn.cursor()
            # SYSIBM.SYSDUMMY1 is Db2's always-available, no-privilege-
            # required dummy table (Oracle's DUAL equivalent) -- reliable
            # for a bare connectivity check regardless of what catalog/admin
            # privileges the connecting user does or doesn't have.
            cur.execute("SELECT 1 FROM SYSIBM.SYSDUMMY1")
            cur.fetchone()
            cur.close()
            return True, "Connected"
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)
        finally:
            self.close()

    def _translate(self, sql: str, params: Optional[dict]):
        if not params:
            return sql, []
        order: list = []

        def _repl(m: re.Match) -> str:
            order.append(m.group(1))
            return "?"

        query = _NAMED_PARAM_RE.sub(_repl, sql)
        args = [params[name] for name in order]
        return query, args

    def execute(self, sql: str, params: Optional[dict] = None) -> Iterable[tuple]:
        query, args = self._translate(sql, params)
        cur = require_connected(self._conn, "Db2").cursor()
        cur.execute(query, args) if args else cur.execute(query)
        rows = cur.fetchall()
        cur.close()
        return [tuple(r) for r in rows]

    def execute_ddl(self, sql: str) -> None:
        cur = require_connected(self._conn, "Db2").cursor()
        cur.execute(sql)
        cur.close()

    def fetch_batches(self, sql: str, batch_size: int = 5000):
        """Yield rows from sql in batches, for streaming data migration --
        same duck-typed interface as OracleConnector/MySQLConnector/
        PostgresConnector/SqlServerConnector's own fetch_batches, used when
        Db2 is the *source* (see migrator.py's module docstring)."""
        cur = require_connected(self._conn, "Db2").cursor()
        cur.execute(sql)
        while True:
            rows = cur.fetchmany(batch_size)
            if not rows:
                break
            yield [d[0] for d in cur.description], [tuple(r) for r in rows]
        cur.close()

    def insert_batch(self, table: str, columns: list[str], rows: list[tuple]) -> None:
        if not rows:
            return
        # Table/column names arrive here in Oracle's original case.
        # ddl_generator creates every Db2 object uppercased and
        # double-quoted (see ddl_generator._quote_db2), so inserts must
        # address them the same way to resolve to the same object.
        col_list = ", ".join(quote_double(c.upper()) for c in columns)
        placeholders = ", ".join(["?"] * len(columns))
        schema_prefix = f"{quote_double(self.params.schema.upper())}." if self.params.schema else ""
        sql = f"INSERT INTO {schema_prefix}{quote_double(table.upper())} ({col_list}) VALUES ({placeholders})"
        cur = require_connected(self._conn, "Db2").cursor()
        cur.executemany(sql, rows)
        cur.close()

    def count_rows(self, table: str, schema: Optional[str] = None) -> int:
        """Independent post-migration row count -- see
        tgdatabridge.core.validation. Mirrors insert_batch's own uppercased,
        double-quoted, schema-qualified identifier convention."""
        target_schema = schema or self.params.schema
        schema_prefix = f"{quote_double(target_schema.upper())}." if target_schema else ""
        cur = require_connected(self._conn, "Db2").cursor()
        cur.execute(f"SELECT COUNT(*) FROM {schema_prefix}{quote_double(table.upper())}")
        (count,) = cur.fetchone()
        cur.close()
        return count

    def checksum_rows(
        self, table: str, columns: list[str], schema: Optional[str] = None, sample_size: Optional[int] = None,
    ) -> int:
        """Order-independent checksum of `table`'s rows -- see
        tgdatabridge.core.validation.table_checksum. Db2 caps a result set with
        FETCH FIRST n ROWS ONLY rather than LIMIT."""
        from tgdatabridge.core.validation import row_checksum

        target_schema = schema or self.params.schema
        schema_prefix = f"{quote_double(target_schema.upper())}." if target_schema else ""
        col_list = ", ".join(quote_double(c.upper()) for c in columns)
        sql = f"SELECT {col_list} FROM {schema_prefix}{quote_double(table.upper())}"
        if sample_size:
            sql += f" FETCH FIRST {int(sample_size)} ROWS ONLY"
        cur = require_connected(self._conn, "Db2").cursor()
        cur.execute(sql)
        total = 0
        for row in cur.fetchall():
            total ^= row_checksum(tuple(row))
        cur.close()
        return total
