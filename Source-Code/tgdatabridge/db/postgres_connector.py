"""PostgreSQL target connector, built on psycopg (v3)."""
from __future__ import annotations

from typing import Iterable, Optional

from tgdatabridge.db.base import ConnectionParams, require_connected
from tgdatabridge.utils.identifiers import quote_double


class PostgresConnector:
    def __init__(self, params: ConnectionParams):
        self.params = params
        self._conn = None
        # table -> {column name: pg_type.typname}, filled on first write to
        # each table so binary COPY can declare real types (see
        # _column_types). One query per table, not per batch.
        self._column_type_cache: dict = {}
        # Latched on if a binary COPY ever fails, so the rest of the run
        # goes straight to the text path instead of retrying and failing
        # once per batch.
        self._copy_text_only = False

    def _connect_kwargs(self) -> dict:
        """Everything psycopg.connect() needs, built without touching the
        driver -- testable with no psycopg installed, mirroring
        OracleConnector._connect_kwargs / MySQLConnector._connect_kwargs.

        TLS/SSL: psycopg (built on libpq) takes `sslmode` plus plain file
        paths -- `sslrootcert`/`sslcert`/`sslkey` -- and `sslmode` is
        where TlsConfig's verify_cert/verify_hostname split actually
        lands: "require" encrypts with no verification at all,
        "verify-ca" checks the CA signature only, "verify-full" also
        checks the hostname.

        Unlike every other connector here, libpq has a real answer for
        verifying a tunneled connection's hostname: `host` and `hostaddr`
        can be given *both*, in which case libpq dials `hostaddr` (the
        SSH tunnel's 127.0.0.1) but still uses `host` (the real database
        address, restored via TlsConfig.effective_hostname before the
        tunnel rewrote it -- see connector_factory._pin_tls_hostname) for
        the certificate's SNI and hostname check. So "verify-full" through
        a tunnel genuinely works here, where Oracle's and MySQL's drivers
        cannot manage it.
        """
        kwargs = dict(
            dbname=self.params.database,
            user=self.params.username,
            password=self.params.password,
            autocommit=True,
            # Dead-connection detection (SCALE.md / a real migration that sat
            # "running" on a LOB-heavy table for good, with no error and no
            # log line, until the app was killed and restarted). Without
            # these, libpq's socket sits in a plain blocking recv() with no
            # OS-level probing: if a NAT gateway, load balancer or firewall
            # between here and the target silently drops an idle TCP
            # connection (common on a long COPY of large LOB values, where
            # long gaps between packets look "idle" to a middlebox even
            # though the migration is still very much in progress), no FIN
            # or RST ever arrives and the write simply hangs forever --
            # indistinguishable, from the GUI, from the app being frozen,
            # even though the GUI thread itself is fine. These turn that
            # silent hang into a real, raised error (after roughly
            # keepalives_idle + keepalives_interval * keepalives_count
            # seconds of true silence on a healthy-looking connection) that
            # migrate_table's existing retry/reconnect handling can act on,
            # instead of the run sitting there indefinitely with nothing to
            # retry against. Values are libpq's own keepalive knobs --
            # standard TCP keepalive, not a PostgreSQL-specific feature --
            # and are a no-op on a connection that never goes quiet.
            keepalives=1,
            keepalives_idle=30,
            keepalives_interval=10,
            keepalives_count=3,
            # This connector is only ever used for bulk migration, never
            # for a transaction whose durability another system already
            # promised to someone. `synchronous_commit=off` lets PostgreSQL
            # acknowledge each autocommit'd COPY as soon as it's written to
            # the WAL *buffer*, not once that buffer is fsynced to disk --
            # normally the single biggest fixed cost of a small-to-medium
            # COPY batch, paid once per batch under the default `autocommit`
            # above. The trade-off is exactly the one CheckpointWriter's own
            # docstring already accepts elsewhere in this codebase: on a
            # crash in the tiny window before the next WAL flush, a commit
            # this connection believes finished could be lost -- caught by
            # this same migration's own row-count/checksum validation and
            # by checkpoint/resume, not silent. A `postgresql.conf` that
            # pins `synchronous_commit=on` server-wide still wins if the
            # database administrator wants that guarantee back; this is a
            # per-session request, not an override.
            options="-c synchronous_commit=off",
        )
        tls = self.params.tls
        effective_host = tls.effective_hostname(self.params.host) if tls else self.params.host
        if tls and tls.enabled and effective_host != self.params.host:
            # Tunneled: dial the tunnel's local port, verify the real host.
            kwargs["host"] = effective_host
            kwargs["hostaddr"] = self.params.host
        else:
            kwargs["host"] = self.params.host
        kwargs["port"] = self.params.port
        if tls and tls.enabled:
            if not tls.verify_cert:
                kwargs["sslmode"] = "require"
            elif not tls.verify_hostname:
                kwargs["sslmode"] = "verify-ca"
            else:
                kwargs["sslmode"] = "verify-full"
            if tls.ca_cert_path:
                kwargs["sslrootcert"] = tls.ca_cert_path
            if tls.client_cert_path:
                kwargs["sslcert"] = tls.client_cert_path
            if tls.client_key_path:
                kwargs["sslkey"] = tls.client_key_path
        return kwargs

    def connect(self) -> None:
        import psycopg  # lazy import

        self._conn = psycopg.connect(**self._connect_kwargs())

        # Without an explicit, existing schema on search_path, CREATE TABLE/
        # FUNCTION raises "no schema has been selected to create in" the
        # moment DDL runs (this bit us when the schema field was left blank
        # and fell through to None instead of a real default). Always pin to
        # a concrete schema here — "public" if none was given — rather than
        # trusting the connection's own default search_path to resolve to
        # something writable.
        target_schema = self.params.schema or "public"
        cur = self._conn.cursor()
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {quote_double(target_schema)}")
        cur.execute(f"SET search_path TO {quote_double(target_schema)}")
        cur.close()

    @property
    def schema_name(self) -> str:
        return self.params.schema or "public"

    def fetch_batches(self, sql: str, batch_size: int = 5000):
        """Yield rows from sql in batches, for streaming data migration --
        same duck-typed interface as OracleConnector/MySQLConnector's own
        fetch_batches, used when PostgreSQL is the *source* (see
        migrator.py's module docstring). psycopg 3's cursor has no
        Oracle-style `arraysize` prefetch knob; `fetchmany(batch_size)` in a
        loop is the portable equivalent, same approach already used by
        MySQLConnector.fetch_batches."""
        cur = require_connected(self._conn, "PostgreSQL").cursor()
        cur.execute(sql)
        while True:
            rows = cur.fetchmany(batch_size)
            if not rows:
                break
            yield [d[0] for d in cur.description], rows
        cur.close()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def test_connection(self) -> tuple[bool, str]:
        try:
            self.connect()
            cur = self._conn.cursor()
            cur.execute("SELECT version()")
            row = cur.fetchone()
            cur.close()
            return True, (row[0] if row else "Connected")
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)
        finally:
            self.close()

    def execute(self, sql: str, params: Optional[dict] = None) -> Iterable[tuple]:
        cur = require_connected(self._conn, "PostgreSQL").cursor()
        cur.execute(sql, params or {})
        rows = cur.fetchall()
        cur.close()
        return rows

    def execute_ddl(self, sql: str) -> None:
        cur = require_connected(self._conn, "PostgreSQL").cursor()
        cur.execute(sql)
        cur.close()

    def insert_batch(self, table: str, columns: list[str], rows: list[tuple]) -> None:
        """Bulk-load one batch via COPY -- the fastest ingest path
        PostgreSQL exposes over the wire protocol.

        This used to build an `INSERT INTO t (cols) VALUES (%s, ...)` and
        call `cur.executemany`, which pays per-row statement overhead,
        per-row parse/plan and per-row WAL accounting; COPY pays each of
        those once for the whole batch and is roughly an order of
        magnitude faster on the write side. See SCALE.md section 1.1 --
        for a 1 TB migration this is the single largest lever there is.

        The signature is deliberately unchanged, so migrator.migrate_table's
        checkpointing, retry and post-migration validation all carry over
        untouched: this is a drop-in replacement for the write, not a new
        code path callers have to opt into.

        FORMAT BINARY avoids a text encode on this side and a text parse
        on the server for every numeric, timestamp and boolean. psycopg
        adapts each value using the same machinery `executemany` used, so
        the types that reach the server are the ones that did before.

        Two behavioural notes worth knowing:
          - COPY is all-or-nothing per batch. So was executemany, so
            migrate_table's retry/checkpoint semantics are unaffected --
            but a malformed row still fails its whole batch rather than
            just itself, exactly as before.
          - COPY doesn't fire rules or support ON CONFLICT. Neither did
            the previous INSERT as written (no conflict clause was ever
            emitted), and ddl_generator creates no rules, so nothing that
            worked before stops working.
        """
        if not rows:
            return
        # Table/column names arrive here in Oracle's original (usually
        # uppercase) case. ddl_generator creates every Postgres object with
        # a lowercased, quoted name (see ddl_generator._quote_pg for why),
        # so this must address them the same way or it fails with
        # "relation ... does not exist" the moment the case doesn't match.
        col_list = ", ".join(quote_double(c.lower()) for c in columns)
        target = quote_double(table.lower())

        pg_types = None if self._copy_text_only else self._column_types(table, columns)
        if pg_types is not None:
            try:
                self._copy_binary(target, col_list, pg_types, rows)
                return
            except Exception:  # noqa: BLE001
                # Binary COPY is all-or-nothing, so nothing was written --
                # falling back writes the batch exactly once. Latch the
                # fallback on so the rest of the migration doesn't pay a
                # failed round trip per batch. Deliberately broad: any
                # binary-encoding problem should degrade to a slower write
                # rather than fail somebody's migration, which is the whole
                # lesson of the bug this replaced.
                self._copy_text_only = True
                self._reset_after_failed_copy()
        self._copy_text(target, col_list, rows)

    def _copy_binary(self, target: str, col_list: str, pg_types: list, rows: list[tuple]) -> None:
        """Fast path: declare each column's real type, then write binary.

        set_types is the part that was missing. Without it psycopg infers
        an OID per value, and its default for a Python int is `numeric` --
        which PostgreSQL rejects for an int4/int8/bool column with
        "insufficient data left in message", failing the entire batch.
        """
        cur = require_connected(self._conn, "PostgreSQL").cursor()
        try:
            with cur.copy(
                f"COPY {target} ({col_list}) FROM STDIN (FORMAT BINARY)"
            ) as copy:
                copy.set_types(pg_types)
                for row in rows:
                    copy.write_row(row)
        finally:
            cur.close()

    def _copy_text(self, target: str, col_list: str, rows: list[tuple]) -> None:
        """Fallback: text-format COPY, where the *server* parses each value
        against the column's real type instead of trusting a client-side
        OID guess. Slower than binary (measured ~1.4x) but still several
        times faster than executemany, and it accepts every value shape the
        binary path is fussy about."""
        cur = require_connected(self._conn, "PostgreSQL").cursor()
        try:
            with cur.copy(f"COPY {target} ({col_list}) FROM STDIN") as copy:
                for row in rows:
                    copy.write_row(row)
        finally:
            cur.close()

    def _reset_after_failed_copy(self) -> None:
        """Clear any aborted-transaction state left by a failed COPY, so
        the fallback write starts from a usable connection."""
        try:
            self._conn.rollback()
        except Exception:  # noqa: BLE001
            pass

    def _column_types(self, table: str, columns: list[str]) -> Optional[list]:
        """The target's own `pg_type.typname` for each requested column, in
        the order given -- the names psycopg's set_types() expects
        ("int4", "varchar", "timestamp", "numeric", "bool", ...).

        Read from the catalogue rather than mapped from the source schema
        on purpose: what matters is the type the column *actually* has on
        the target, which may differ from what this tool generated if the
        table was created by hand or altered afterwards. Cached per table,
        so this is one extra query per table, not per batch.

        Returns None if anything is unresolvable -- an unknown table, a
        column the target doesn't have -- which sends the caller down the
        text path instead of guessing.
        """
        key = table.lower()
        cached = self._column_type_cache.get(key)
        if cached is None:
            try:
                cur = require_connected(self._conn, "PostgreSQL").cursor()
                cur.execute(
                    "SELECT a.attname, t.typname "
                    "FROM pg_attribute a "
                    "JOIN pg_type t ON t.oid = a.atttypid "
                    "WHERE a.attrelid = %s::regclass AND a.attnum > 0 AND NOT a.attisdropped",
                    (key,),
                )
                cached = {name: typname for name, typname in cur.fetchall()}
                cur.close()
            except Exception:  # noqa: BLE001
                self._reset_after_failed_copy()
                return None
            self._column_type_cache[key] = cached

        resolved = []
        for col in columns:
            typname = cached.get(col.lower())
            if typname is None:
                return None
            resolved.append(typname)
        return resolved

    def count_rows(self, table: str, schema: Optional[str] = None) -> int:
        """Independent post-migration row count -- see
        tgdatabridge.core.validation. `schema` is accepted (matching every other
        connector's count_rows signature) but unused: PostgresConnector.
        connect() already pins the session to one schema via search_path,
        so an unqualified table reference always resolves there."""
        cur = require_connected(self._conn, "PostgreSQL").cursor()
        cur.execute(f"SELECT COUNT(*) FROM {quote_double(table.lower())}")
        (count,) = cur.fetchone()
        cur.close()
        return count

    def checksum_rows(
        self, table: str, columns: list[str], schema: Optional[str] = None, sample_size: Optional[int] = None,
    ) -> int:
        """Order-independent checksum of `table`'s rows -- see
        tgdatabridge.core.validation.table_checksum. `sample_size`, if given,
        caps how many rows are read back (LIMIT), for tables too large to
        fully re-read just to validate; None reads every row."""
        from tgdatabridge.core.validation import row_checksum

        col_list = ", ".join(quote_double(c.lower()) for c in columns)
        sql = f"SELECT {col_list} FROM {quote_double(table.lower())}"
        if sample_size:
            sql += f" LIMIT {int(sample_size)}"
        cur = require_connected(self._conn, "PostgreSQL").cursor()
        cur.execute(sql)
        total = 0
        for row in cur.fetchall():
            total ^= row_checksum(tuple(row))
        cur.close()
        return total
