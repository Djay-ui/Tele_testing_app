"""MySQL target connector, built on mysql-connector-python."""
from __future__ import annotations

from typing import Iterable, Optional

from tgdatabridge.db.base import ConnectionParams, require_connected
from tgdatabridge.utils.identifiers import quote_backtick


class MySQLConnector:
    def __init__(self, params: ConnectionParams):
        self.params = params
        self._conn = None

    def _connect_kwargs(self) -> dict:
        """Everything mysql.connector.connect() needs, built without
        touching the driver -- testable with no mysql-connector-python
        installed, mirroring OracleConnector._connect_kwargs.

        TLS/SSL: mysql-connector-python's pure-Python implementation (see
        `use_pure` below) takes plain file paths rather than a ready
        ssl.SSLContext -- `ssl_ca`/`ssl_cert`/`ssl_key` -- plus two
        separate booleans that map exactly onto TlsConfig's own
        verify_cert/verify_hostname split: `ssl_verify_cert` checks the CA
        signature, `ssl_verify_identity` additionally checks the
        certificate's name against the host being connected to. Unlike
        PostgreSQL's host/hostaddr split, this driver has no way to check
        a name other than the `host` it dials -- so, same limitation as
        Oracle's, a TLS connection made through an SSH tunnel cannot
        verify the real database's hostname (it would be checking against
        "127.0.0.1"); this is why ssl_verify_identity is only turned on
        when the effective hostname MATCHES what's actually being dialed,
        i.e. when the connection isn't tunneled.
        """
        kwargs = dict(
            host=self.params.host,
            port=self.params.port,
            database=self.params.database,
            user=self.params.username,
            password=self.params.password,
            autocommit=True,
            # The pure-Python implementation, not the bundled C extension.
            #
            # mysql-connector-python prefers its C extension whenever one
            # is importable, and that extension loads its authentication
            # plugins as separate DLLs from a directory it locates
            # relative to libmysql.dll. Inside a frozen application there
            # is no such directory, so the first real connection dies on
            #
            #     2059 (HY000): Authentication plugin
            #     'mysql_native_password' cannot be loaded: The specified
            #     module could not be found.
            #
            # -- a failure that has nothing to do with the credentials it
            # appears to be about, and that no amount of correcting them
            # fixes. The pure-Python implementation carries its plugins as
            # ordinary Python modules (mysql.connector.plugins.*, named in
            # the PyInstaller spec's hidden imports for exactly this
            # reason), so they are inside the executable and always found.
            use_pure=True,
        )
        tls = self.params.tls
        if tls and tls.enabled:
            kwargs["ssl_ca"] = tls.ca_cert_path or None
            kwargs["ssl_cert"] = tls.client_cert_path or None
            kwargs["ssl_key"] = tls.client_key_path or None
            kwargs["ssl_verify_cert"] = tls.verify_cert
            can_check_hostname = tls.effective_hostname(self.params.host) == self.params.host
            kwargs["ssl_verify_identity"] = tls.verify_hostname and can_check_hostname
        return kwargs

    def connect(self) -> None:
        import mysql.connector  # lazy import

        self._conn = mysql.connector.connect(**self._connect_kwargs())

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def test_connection(self) -> tuple[bool, str]:
        try:
            self.connect()
            cur = self._conn.cursor()
            cur.execute("SELECT VERSION()")
            row = cur.fetchone()
            cur.close()
            return True, (row[0] if row else "Connected")
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)
        finally:
            self.close()

    def execute(self, sql: str, params: Optional[dict] = None) -> Iterable[tuple]:
        cur = require_connected(self._conn, "MySQL").cursor()
        cur.execute(sql, params or {})
        rows = cur.fetchall()
        cur.close()
        return rows

    def execute_ddl(self, sql: str) -> None:
        """Execute one DDL script.

        This used to be `for statement in sql.split(";")`, a naive split
        that is wrong for exactly the statements that matter most: a
        `CREATE PROCEDURE`/`FUNCTION`/`TRIGGER` body is full of internal
        `;` terminators, so splitting on every `;` chopped each routine
        into fragments and fed the server things like a bare
        `DECLARE v_x INT` with no surrounding block. The caller (the GUI's
        "Apply DDL to Target", and cli/runner) already splits the script
        properly with utils.sql_split.split_sql_statements, which tracks
        BEGIN/END nesting, dollar quoting, string literals and comments --
        so the correct behaviour here is to reuse that same splitter
        rather than to re-split with a worse one.

        The cursor is also closed in a `finally` now. Leaving it open on
        an error left the connection with an unread result, so the *next*
        statement failed with a misleading "Unread result found" instead
        of the real error, and a later `close()` could raise on top of the
        exception being propagated.
        """
        from tgdatabridge.utils.sql_split import has_executable_sql, split_sql_statements

        cur = require_connected(self._conn, "MySQL").cursor()
        try:
            for statement in split_sql_statements(sql):
                # A comment-only block ("MANUAL CONVERSION REQUIRED ..."
                # wrapping the original source) is answered by MySQL with
                # "Query was empty" -- an error about nothing.
                if not has_executable_sql(statement):
                    continue
                statement = statement.strip().rstrip(";").strip()
                if not statement:
                    continue
                cur.execute(statement)
                # mysql-connector raises "Unread result found" on the next
                # execute() if a statement returned rows (a SELECT inside an
                # applied script, or a driver that reports a result set for
                # DDL) and nothing consumed them.
                try:
                    while cur.nextset():
                        pass
                except Exception:  # noqa: BLE001 -- no result set to advance past
                    pass
        finally:
            try:
                cur.close()
            except Exception:  # noqa: BLE001 -- never mask the real error
                pass

    def insert_batch(self, table: str, columns: list[str], rows: list[tuple]) -> None:
        if not rows:
            return
        col_list = ", ".join(quote_backtick(c) for c in columns)
        placeholders = ", ".join(["%s"] * len(columns))
        sql = f"INSERT INTO {quote_backtick(table)} ({col_list}) VALUES ({placeholders})"
        cur = require_connected(self._conn, "MySQL").cursor()
        cur.executemany(sql, rows)
        cur.close()

    def count_rows(self, table: str, schema: Optional[str] = None) -> int:
        """Independent post-migration row count -- see
        tgdatabridge.core.validation. `schema` is accepted (matching every other
        connector's signature) but unused: MySQL has no schema concept
        distinct from the database itself, already connected to."""
        cur = require_connected(self._conn, "MySQL").cursor()
        cur.execute(f"SELECT COUNT(*) FROM {quote_backtick(table)}")
        (count,) = cur.fetchone()
        cur.close()
        return count

    def checksum_rows(
        self, table: str, columns: list[str], schema: Optional[str] = None, sample_size: Optional[int] = None,
    ) -> int:
        """Order-independent checksum of `table`'s rows -- see
        tgdatabridge.core.validation.table_checksum."""
        from tgdatabridge.core.validation import row_checksum

        col_list = ", ".join(quote_backtick(c) for c in columns)
        sql = f"SELECT {col_list} FROM {quote_backtick(table)}"
        if sample_size:
            sql += f" LIMIT {int(sample_size)}"
        cur = require_connected(self._conn, "MySQL").cursor()
        cur.execute(sql)
        total = 0
        for row in cur.fetchall():
            total ^= row_checksum(tuple(row))
        cur.close()
        return total

    def fetch_batches(self, sql: str, batch_size: int = 5000):
        """Yield rows from sql in batches, for streaming data migration --
        used when this connector is the *source* of a migration (MySQL as
        a source engine; see tgdatabridge.core.mysql_introspector). mysql-
        connector-python's cursor has no Oracle-style `arraysize` knob to
        pre-size batches with; `fetchmany(batch_size)` in a loop achieves
        the same streaming effect regardless."""
        cur = require_connected(self._conn, "MySQL").cursor()
        cur.execute(sql)
        while True:
            rows = cur.fetchmany(batch_size)
            if not rows:
                break
            yield [d[0] for d in cur.description], rows
        cur.close()

    @property
    def schema_name(self) -> str:
        # MySQL has no separate "schema" concept distinct from the database
        # itself -- matches how this connector is already treated on the
        # target side (see target_introspector.introspect_target_mysql and
        # main_window._refresh_target_schema's schema_name fallback).
        return self.params.database
