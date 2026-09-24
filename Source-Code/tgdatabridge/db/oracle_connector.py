"""
Oracle connector, built on python-oracledb in "thin" mode so no Oracle
Instant Client install is required on the machine running the tool. Was
source-only originally (this tool's whole internal type/DDL pivot is
already Oracle-flavored, so there was nothing left to translate *into*
until Oracle also became a valid target); insert_batch() below is the one
addition needed for Oracle-as-a-target data migration -- execute/
execute_ddl were already generic enough to need no changes. connect()
needs one addition: it sets `oracledb.defaults.fetch_lobs = False` so
CLOB/BLOB/NCLOB columns come back as plain str/bytes on the normal row
fetch, not as separate LOB-locator objects that would need extra
round-trips to read -- see that assignment's own comment. fetch_batches
and checksum_rows both also run every row through _materialize_lob,
which converts anything a target driver can't bind as-is -- a LOB-locator
object still seen despite the setting above, and a native Oracle JSON
column's decoded dict/list -- into plain str/bytes/JSON-text. See that
function's own docstring, including the real driver's attribute name
this previously got wrong (`chunk_size` vs. the actual `getchunksize`).
"""
from __future__ import annotations

from typing import Iterable, Optional

from tgdatabridge.db.base import ConnectionParams, require_connected
from tgdatabridge.utils.identifiers import quote_double


def _materialize_lob(value):
    """connect() now sets `oracledb.defaults.fetch_lobs = False` before
    ever connecting, specifically so CLOB/BLOB/NCLOB columns arrive here
    already as plain str/bytes -- see that assignment's own comment for
    why (it turns several extra network round-trips per LOB value into
    zero). The LOB-locator branch below is therefore mostly a defensive
    fallback now, for whatever this tool's own connect() didn't reach:
    a LOB value produced by a raw SQL*Plus-style path, a future
    python-oracledb version that changes the default, or a test/mock
    connection that never called this module's connect() at all. It is
    kept rather than deleted because "no code path can ever hand this
    function a real LOB locator object" is a stronger claim than this
    tool can make about a driver it doesn't control end-to-end.

    Before that fetch_lobs setting existed, python-oracledb's default
    behavior (no `fetch_lobs = False`, no output type handler configured)
    was to fetch CLOB/BLOB/NCLOB columns as LOB *locator* objects, not
    plain str/bytes -- confirmed against python-oracledb's own "Using
    CLOB and BLOB Data" docs. Passed straight through to another
    connector's insert_batch() bind parameters as-is, none of the other
    drivers this tool uses (psycopg, mysql-connector-python, pyodbc,
    ibm_db) know how to adapt an oracledb.LOB object, so this must run on
    every row before fetch_batches yields it -- this was a real,
    previously-unhandled bug for any Oracle-sourced table with a
    CLOB/BLOB/NCLOB column, not just a theoretical one.

    Detected duck-typed (has .read/.size/.getchunksize, all of which every
    real oracledb.LOB object exposes) rather than via `isinstance(value,
    oracledb.LOB)`, so this module never needs to import oracledb at
    module level -- oracledb itself is only ever imported lazily inside
    connect() (see this module's own docstring), so the GUI can start
    without the driver installed at all.

    IMPORTANT: the real python-oracledb LOB object's chunk-size accessor is
    named `getchunksize()`, NOT `chunk_size()` -- confirmed directly against
    an installed python-oracledb 4.0.2 (`dir(oracledb.LOB)` lists
    `getchunksize`, `read`, `size`, no `chunk_size` at all). An earlier
    version of this function (and the `_FakeLOB` test double standing in
    for the driver in tests/test_oracle_connector.py) used `chunk_size()`,
    which real LOB objects never have -- so `hasattr(value, "chunk_size")`
    was always False against a real connection, this function's LOB branch
    never ran, and the raw, un-adapted `oracledb.LOB` object was handed
    straight to the target driver, which fails with "cannot adapt type
    'LOB' using placeholder '%t'". This was a real, previously-unnoticed
    bug for any Oracle-sourced table with a CLOB/BLOB/NCLOB column -- the
    unit tests never caught it because the fake stood in for the driver
    incorrectly, matching the (also wrong) attribute name this function
    checked for, rather than the driver's real one.

    Also serializes a native Oracle JSON column's value (Oracle 21c+'s
    DB_TYPE_JSON): python-oracledb decodes that straight into a plain
    Python dict/list rather than a string, and none of this tool's target
    drivers know how to adapt a bare dict/list for a text placeholder
    either -- same class of bug as the LOB one above, just for a different
    native type. Serialized back to a JSON string, the same text shape a
    CLOB-stored JSON payload already has (this tool's own to_postgres()/
    to_mysql()/etc. type mapping maps a native JSON column to a plain text
    column on every target, so the target side already expects text here,
    not a native JSON/JSONB bind value).

    Reads LOBs in getchunksize()-multiple increments -- python-oracledb's
    own docs recommend this as the most efficient LOB access pattern, and
    it bounds how much any single internal read call has to buffer for one
    LOB value's worth of data (see ENTERPRISE_READINESS.md section 4 for
    the "LOB streaming" motivation). The *result* is still one fully-
    materialized Python str/bytes object at the end, though -- the
    largest unit insert_batch's executemany-based bind-parameter
    interface can accept on the target side. True zero-buffering,
    server-to-server LOB streaming isn't achievable through the DB-API-
    style drivers this tool builds on without a substantially larger,
    per-target-driver rewrite of the insert path itself; see
    tgdatabridge.core.migrator's own docstring for the complementary "shrink the
    row batch size for LOB-bearing tables" mitigation this pairs with."""
    if hasattr(value, "read") and hasattr(value, "size") and hasattr(value, "getchunksize"):
        size = value.size()
        if not size:
            return value.read()  # empty/null LOB -- nothing to chunk
        chunk = value.getchunksize()
        parts = []
        offset = 1  # LOB.read()'s offset is 1-based, per python-oracledb's own docs
        while offset <= size:
            parts.append(value.read(offset, chunk))
            offset += chunk
        joiner = b"" if isinstance(parts[0], bytes) else ""
        return joiner.join(parts)

    if isinstance(value, (dict, list)):
        import json
        return json.dumps(value)

    return value


def _needs_materializing(value) -> bool:
    """True for anything _materialize_lob actually transforms -- a real
    LOB-locator object (duck-typed the same way _materialize_lob detects
    one) or a native-JSON dict/list. Kept in sync with _materialize_lob's
    own detection on purpose: this is only the fast-path skip-check, never
    the conversion itself."""
    if isinstance(value, (dict, list)):
        return True
    return hasattr(value, "read") and hasattr(value, "size") and hasattr(value, "getchunksize")


def _materialize_lobs_in_row(row):
    if not any(_needs_materializing(v) for v in row):
        return row  # fast path: nothing in this row needs converting
    return tuple(_materialize_lob(v) for v in row)


class OracleConnector:
    def __init__(self, params: ConnectionParams):
        self.params = params
        self._conn = None

    def _dsn(self) -> str:
        """An Easy Connect string rather than oracledb.makedsn(): makedsn
        has no way to ask for the TCPS (TLS) protocol, and Easy Connect's
        `tcps://host:port/service_name` form is what python-oracledb's own
        docs give for a TLS-encrypted thin-mode connection."""
        protocol = "tcps" if (self.params.tls and self.params.tls.enabled) else "tcp"
        return f"{protocol}://{self.params.host}:{self.params.port}/{self.params.database}"

    def _connect_kwargs(self) -> dict:
        """Everything oracledb.connect() needs, built without touching the
        driver -- so this is testable with no oracledb installed (see
        this module's own docstring for why the driver is only ever
        imported lazily, inside connect() itself).

        TLS/SSL: python-oracledb's thin mode takes a ready `ssl.SSLContext`
        (see tgdatabridge/db/tls_config.py's TlsConfig.build_ssl_context)
        rather than separate CA/cert/key paths of its own, plus its own
        `ssl_server_dn_match` flag for whether the certificate's name is
        checked against the address being connected to -- oracledb has no
        way to check a name *other than* the DSN's own host (unlike
        PostgreSQL's host/hostaddr split or SQL Server's
        HostNameInCertificate keyword), so a TLS connection made through
        an SSH tunnel cannot verify the real database's hostname this way:
        the DSN host is 127.0.0.1 by the time this runs. Encryption and CA
        trust still apply either way; only the hostname check is affected,
        and only when tunneled -- see TlsConfig.effective_hostname's own
        docstring for the general shape of this limitation.
        """
        kwargs = dict(user=self.params.username, password=self.params.password, dsn=self._dsn())
        tls = self.params.tls
        if tls and tls.enabled:
            kwargs["ssl_context"] = tls.build_ssl_context()
            kwargs["ssl_server_dn_match"] = tls.verify_hostname
        # Dead-connection detection -- see PostgresConnector._connect_kwargs's
        # keepalives comment for the full failure mode this closes (a real
        # migration that sat "running" on a LOB-heavy table forever, no error,
        # no log line, GUI otherwise fine, until the app was killed by hand).
        # `expire_time` (minutes) makes oracledb send a lightweight probe
        # packet on an otherwise-quiet connection so a silently-dropped TCP
        # session (a NAT/firewall/load-balancer idle timeout, easy to hit
        # during the long gaps between packets a big CLOB/BLOB fetch can
        # have) surfaces as a real error -- typically DPY-4011 -- instead of
        # a `recv()` that never returns. That error is exactly what
        # migrate_table's own DPY-4011 handling (see migrator.py's module
        # docstring) already reconnects the source and retries for; without
        # expire_time, that handling was never actually reachable for this
        # specific failure mode, only for a drop the *server* announces.
        # `tcp_connect_timeout` (seconds) is the same idea for the initial
        # connect: fail fast with a clear timeout instead of hanging on a
        # SYN that a firewall silently drops rather than rejects.
        kwargs["expire_time"] = 2
        kwargs["tcp_connect_timeout"] = 30
        return kwargs

    def connect(self) -> None:
        import oracledb  # imported lazily so the GUI can start without the driver installed

        # Ask the driver to hand back CLOB/BLOB/NCLOB columns as plain
        # str/bytes on the same fetch as the rest of the row, instead of
        # LOB *locator* objects that this connector then has to read back
        # from the database with one or more extra round-trips each (see
        # _materialize_lob's docstring for why those extra round-trips
        # were never buying anything: this connector always fully
        # materializes the value into memory anyway, so streaming it in
        # getchunksize()-sized pieces cost extra network calls for zero
        # memory benefit). This is a module-level python-oracledb setting
        # (oracledb.defaults), not a per-connection one, so it is set here,
        # right before the one connect() call this whole process makes to
        # this database -- setting it any earlier would be setting a
        # global for a driver that might not even end up being used.
        # A table with 60,000+ rows and a CLOB column went from one extra
        # DB round-trip per LOB chunk (potentially several per row) to
        # zero: the LOB's text/bytes arrive already inline in the row.
        oracledb.defaults.fetch_lobs = False

        self._conn = oracledb.connect(**self._connect_kwargs())

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def test_connection(self) -> tuple[bool, str]:
        try:
            self.connect()
            cur = self._conn.cursor()
            cur.execute("SELECT banner FROM v$version WHERE ROWNUM = 1")
            row = cur.fetchone()
            cur.close()
            return True, (row[0] if row else "Connected")
        except Exception as exc:  # noqa: BLE001 - surface any driver error to the UI
            return False, str(exc)
        finally:
            self.close()

    def execute(self, sql: str, params: Optional[dict] = None) -> Iterable[tuple]:
        cur = require_connected(self._conn, "Oracle").cursor()
        cur.execute(sql, params or {})
        rows = cur.fetchall()
        cur.close()
        return rows

    def execute_ddl(self, sql: str) -> None:
        cur = require_connected(self._conn, "Oracle").cursor()
        cur.execute(sql)
        cur.close()

    def fetch_batches(self, sql: str, batch_size: int = 5000):
        """Yield rows from sql in batches, for streaming data migration.
        Every row is passed through _materialize_lobs_in_row first -- see
        that function's own docstring for why a CLOB/BLOB/NCLOB column's
        raw fetched value can't be handed to another connector's
        insert_batch() as-is."""
        cur = require_connected(self._conn, "Oracle").cursor()
        cur.arraysize = batch_size
        cur.execute(sql)
        while True:
            rows = cur.fetchmany(batch_size)
            if not rows:
                break
            yield [d[0] for d in cur.description], [_materialize_lobs_in_row(r) for r in rows]
        cur.close()

    def insert_batch(self, table: str, columns: list[str], rows: list[tuple]) -> None:
        """Used when Oracle is the *target* (a lateral or reverse
        migration) -- table/column names arrive here in whatever case the
        source schema used; ddl_generator._quote_oracle always uppercases
        and double-quotes every object it creates (see that function's own
        comment), so inserts must address them the same way here or this
        fails with "ORA-00942: table or view does not exist" the moment
        the case doesn't match. Schema-qualifies the same way
        Db2Connector.insert_batch does, for the same reason: the
        connecting user and the schema DDL was generated for
        (self.params.schema, threaded through generate_table_ddl_oracle
        as its own `schema` argument) aren't necessarily the same
        account. python-oracledb (like cx_Oracle before it) binds by
        position with ":1", ":2", ... rather than "%s"/"?"."""
        if not rows:
            return
        col_list = ", ".join(quote_double(c.upper()) for c in columns)
        placeholders = ", ".join(f":{i + 1}" for i in range(len(columns)))
        schema_prefix = f"{quote_double(self.params.schema.upper())}." if self.params.schema else ""
        sql = f"INSERT INTO {schema_prefix}{quote_double(table.upper())} ({col_list}) VALUES ({placeholders})"
        cur = require_connected(self._conn, "Oracle").cursor()
        cur.executemany(sql, rows)
        cur.close()

    def count_rows(self, table: str, schema: Optional[str] = None) -> int:
        """Independent post-migration row count -- see
        tgdatabridge.core.validation. Mirrors insert_batch's own uppercased,
        double-quoted, schema-qualified identifier convention."""
        target_schema = schema or self.params.schema
        schema_prefix = f"{quote_double(target_schema.upper())}." if target_schema else ""
        cur = require_connected(self._conn, "Oracle").cursor()
        cur.execute(f"SELECT COUNT(*) FROM {schema_prefix}{quote_double(table.upper())}")
        (count,) = cur.fetchone()
        cur.close()
        return count

    def checksum_rows(
        self, table: str, columns: list[str], schema: Optional[str] = None, sample_size: Optional[int] = None,
    ) -> int:
        """Order-independent checksum of `table`'s rows -- see
        tgdatabridge.core.validation.table_checksum. Oracle caps a result set
        with FETCH FIRST n ROWS ONLY (12c+; this connector already
        requires a reasonably current Oracle for python-oracledb thin
        mode's own feature set, so no ROWNUM fallback is needed).

        Rows are run through _materialize_lobs_in_row before checksumming,
        same as fetch_batches -- without it, a table with a native JSON
        column would checksum a raw Python dict here on the source side
        against the plain JSON text migrate_table actually wrote to the
        target (see _materialize_lob's own docstring: every target maps
        Oracle JSON to a text column), and row_checksum's repr()-based
        hash of a dict never equals its hash of the equivalent string --
        so the table would show as "Unvalidated" forever, on every run,
        even though the migrated data is correct. CLOB/BLOB/NCLOB columns
        no longer need this at all now that connect() sets
        `oracledb.defaults.fetch_lobs = False` (they already arrive as
        plain str/bytes), but _materialize_lobs_in_row's own fast-path
        skip check means calling it unconditionally here costs nothing
        extra for a table with neither."""
        from tgdatabridge.core.validation import row_checksum

        target_schema = schema or self.params.schema
        schema_prefix = f"{quote_double(target_schema.upper())}." if target_schema else ""
        col_list = ", ".join(quote_double(c.upper()) for c in columns)
        sql = f"SELECT {col_list} FROM {schema_prefix}{quote_double(table.upper())}"
        if sample_size:
            sql += f" FETCH FIRST {int(sample_size)} ROWS ONLY"
        cur = require_connected(self._conn, "Oracle").cursor()
        cur.execute(sql)
        total = 0
        for row in cur.fetchall():
            total ^= row_checksum(_materialize_lobs_in_row(tuple(row)))
        cur.close()
        return total

    @property
    def schema_name(self) -> str:
        return self.params.schema or self.params.username.upper()
