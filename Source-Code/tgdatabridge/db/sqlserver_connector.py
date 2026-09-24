"""SQL Server target connector, built on pyodbc.

Requires a Microsoft ODBC Driver for SQL Server (17 or 18) to be installed
on the machine running the tool -- pyodbc itself is a thin wrapper over the
platform's ODBC driver manager and doesn't bundle one. ODBC Driver 18 is
tried first (the current release), falling back to 17 for machines that
haven't upgraded yet.
"""
from __future__ import annotations

import re
from typing import Iterable, Optional

from tgdatabridge.db.base import ConnectionParams, require_connected
from tgdatabridge.utils.identifiers import quote_bracket

_CANDIDATE_DRIVERS = ["ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server"]

# Connector-agnostic callers (see tgdatabridge.core.target_introspector) write
# queries using %(name)s-style named placeholders, matching psycopg/
# mysql-connector's paramstyle. pyodbc only supports positional '?'
# placeholders, so named params are translated here rather than forcing
# every caller to special-case SQL Server's paramstyle.
_NAMED_PARAM_RE = re.compile(r"%\((\w+)\)s")


class SqlServerConnector:
    def __init__(self, params: ConnectionParams):
        self.params = params
        self._conn = None

    def _conn_str(self, driver: str) -> str:
        """The ODBC connection string, built without touching pyodbc --
        testable with no driver installed, mirroring every other
        connector's own `_connect_kwargs`.

        TLS/SSL: the Microsoft ODBC Driver for SQL Server always
        negotiates encryption when `Encrypt=yes` is set; what
        `TrustServerCertificate` controls is whether the certificate is
        actually *verified* against a CA at all. The old, unconditional
        `TrustServerCertificate=yes;` this connector used to send
        disabled verification for every single connection, which is
        exactly the "encrypted but not authenticated" gap enterprise TLS
        review flags -- it defends against a passive eavesdropper but not
        a machine-in-the-middle presenting any certificate at all.

        That default is preserved when TLS is not explicitly turned on in
        the connection dialog (`self.params.tls` is None or disabled) --
        changing it unconditionally would break every existing saved
        connection to a server whose certificate isn't in a CA store this
        machine trusts, which describes most self-managed SQL Server
        instances. Turning TLS on in the dialog is what opts a connection
        into `TrustServerCertificate=no` plus real verification.

        `Certificate=` (ODBC Driver 18) points verification at a specific
        CA file instead of the OS trust store. `HostNameInCertificate=`
        (ODBC Driver 17+) is Microsoft's own answer to the hostname-
        verification-through-a-tunnel problem TlsConfig's own docstring
        describes -- it lets the certificate's expected name be given
        independently of `SERVER=`, so unlike Oracle's and MySQL's
        drivers, a tunneled SQL Server connection *can* verify the real
        hostname here.

        Mutual TLS (a client certificate) is deliberately not wired up:
        the Microsoft ODBC driver has no client-certificate connection-
        string keyword for a plain TLS handshake the way psycopg's
        sslcert/sslkey do -- SQL Server's usual strong-auth alternative to
        a database password is Windows/AD integrated authentication, a
        different feature this tool does not otherwise touch.
        """
        conn_str = (
            f"DRIVER={{{driver}}};"
            f"SERVER={self.params.host},{self.params.port};"
            f"DATABASE={self.params.database};"
            f"UID={self.params.username};"
            f"PWD={self.params.password};"
        )
        tls = self.params.tls
        if tls and tls.enabled:
            conn_str += "Encrypt=yes;"
            conn_str += f"TrustServerCertificate={'no' if tls.verify_cert else 'yes'};"
            if tls.ca_cert_path:
                conn_str += f"Certificate={tls.ca_cert_path};"
            if tls.verify_hostname:
                effective_host = tls.effective_hostname(self.params.host)
                conn_str += f"HostNameInCertificate={effective_host};"
        else:
            # Unchanged from every earlier build: encrypted, unverified.
            conn_str += "TrustServerCertificate=yes;"
        return conn_str

    def connect(self) -> None:
        import pyodbc  # lazy import so the GUI can start without the driver installed

        available = set(pyodbc.drivers())
        driver = next((d for d in _CANDIDATE_DRIVERS if d in available), _CANDIDATE_DRIVERS[0])

        self._conn = pyodbc.connect(self._conn_str(driver), autocommit=True)

        # Ensure the target schema exists before anything else runs against
        # it -- mirrors PostgresConnector.connect()'s CREATE SCHEMA IF NOT
        # EXISTS, adapted to T-SQL (CREATE SCHEMA must be the only statement
        # in its batch, hence the EXEC(...) wrapper).
        target_schema = self.params.schema or "dbo"
        if target_schema.lower() != "dbo":
            cur = self._conn.cursor()
            cur.execute(
                "IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = ?) "
                "EXEC('CREATE SCHEMA ' + QUOTENAME(?))",
                target_schema, target_schema,
            )
            cur.close()

    @property
    def schema_name(self) -> str:
        return self.params.schema or "dbo"

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def test_connection(self) -> tuple[bool, str]:
        try:
            self.connect()
            cur = self._conn.cursor()
            cur.execute("SELECT @@VERSION")
            row = cur.fetchone()
            cur.close()
            return True, (row[0] if row else "Connected")
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
        cur = require_connected(self._conn, "SQL Server").cursor()
        cur.execute(query, args) if args else cur.execute(query)
        rows = cur.fetchall()
        cur.close()
        return [tuple(r) for r in rows]

    def execute_ddl(self, sql: str) -> None:
        cur = require_connected(self._conn, "SQL Server").cursor()
        cur.execute(sql)
        cur.close()

    def fetch_batches(self, sql: str, batch_size: int = 5000):
        """Yield rows from sql in batches, for streaming data migration --
        same duck-typed interface as OracleConnector/MySQLConnector/
        PostgresConnector's own fetch_batches, used when SQL Server is the
        *source* (see migrator.py's module docstring). pyodbc rows are
        pyodbc.Row objects, not plain tuples -- converted the same way
        execute() already does, or downstream code that expects to unpack
        plain tuples breaks."""
        cur = require_connected(self._conn, "SQL Server").cursor()
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
        # ddl_generator creates every SQL Server object case-preserved and
        # bracket-quoted (see ddl_generator._quote_sqlserver), so inserts
        # must address them the same way to resolve to the same object.
        col_list = ", ".join(quote_bracket(c) for c in columns)
        placeholders = ", ".join(["?"] * len(columns))
        schema_prefix = f"{quote_bracket(self.params.schema)}." if self.params.schema else ""
        sql = f"INSERT INTO {schema_prefix}{quote_bracket(table)} ({col_list}) VALUES ({placeholders})"
        cur = require_connected(self._conn, "SQL Server").cursor()
        cur.fast_executemany = True
        cur.executemany(sql, rows)
        cur.close()

    def count_rows(self, table: str, schema: Optional[str] = None) -> int:
        """Independent post-migration row count -- see
        tgdatabridge.core.validation. Mirrors insert_batch's own case-preserved,
        bracket-quoted, schema-qualified identifier convention."""
        schema_prefix = f"{quote_bracket(schema)}." if schema else (f"{quote_bracket(self.params.schema)}." if self.params.schema else "")
        cur = require_connected(self._conn, "SQL Server").cursor()
        cur.execute(f"SELECT COUNT(*) FROM {schema_prefix}{quote_bracket(table)}")
        (count,) = cur.fetchone()
        cur.close()
        return count

    def checksum_rows(
        self, table: str, columns: list[str], schema: Optional[str] = None, sample_size: Optional[int] = None,
    ) -> int:
        """Order-independent checksum of `table`'s rows -- see
        tgdatabridge.core.validation.table_checksum. T-SQL has no LIMIT clause;
        TOP n is the equivalent way to cap how many rows are read back."""
        from tgdatabridge.core.validation import row_checksum

        schema_prefix = f"{quote_bracket(schema)}." if schema else (f"{quote_bracket(self.params.schema)}." if self.params.schema else "")
        col_list = ", ".join(quote_bracket(c) for c in columns)
        top_clause = f"TOP {int(sample_size)} " if sample_size else ""
        sql = f"SELECT {top_clause}{col_list} FROM {schema_prefix}{quote_bracket(table)}"
        cur = require_connected(self._conn, "SQL Server").cursor()
        cur.execute(sql)
        total = 0
        for row in cur.fetchall():
            total ^= row_checksum(tuple(row))
        cur.close()
        return total
