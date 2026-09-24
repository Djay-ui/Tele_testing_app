"""Common connection-parameter object and connector interface."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Iterable, List, Optional, Protocol

from tgdatabridge.db.access import ACCESS_DIRECT

if TYPE_CHECKING:  # pragma: no cover - import only for the annotation
    from tgdatabridge.db.ssh_tunnel import SshTunnelConfig
    from tgdatabridge.db.tls_config import TlsConfig


@dataclass
class ConnectionParams:
    host: str
    port: int
    database: str          # service name / SID for Oracle, database name for PG/MySQL
    username: str
    password: str
    schema: Optional[str] = None  # Oracle schema to introspect, defaults to username

    # File-based source engines only (connector_factory.FILE_SOURCE_ENGINES,
    # i.e. "Excel/CSV"), where there is no server and `database` carries a
    # *file path* rather than a database name. An Excel/CSV source may be
    # given several files at once -- up to
    # SpreadsheetConnector.MAX_SOURCE_FILES -- and this holds the full
    # list.
    #
    # `database` stays populated with the first file whenever this is set,
    # deliberately: it is what every engine-agnostic consumer already
    # reads for a human-facing label, a saved connection profile, and the
    # migration checkpoint key (app_storage.checkpoint_id_for). Leaving
    # those to discover a new field would have meant touching each of
    # them; keeping `database` meaningful means a single-file job behaves
    # exactly as it always did, and none of that code needed to change.
    files: Optional[List[str]] = None

    # Excel/CSV only: how a sheet's table name is derived when several
    # files are read at once.
    #
    #   None  -- decide automatically: prefix every table with its file's
    #            name when there is more than one input file, and leave a
    #            single-file job's names exactly as they have always been.
    #   True  -- always prefix, even for one file.
    #   False -- never prefix; fall back to prefixing only when a name
    #            actually collides (the original behaviour).
    #
    # See spreadsheet_introspector._resolve_table_name for what each mode
    # produces and why the automatic default is what it is.
    prefix_tables_with_file: Optional[bool] = None

    # Reach this database through an SSH jump host / bastion instead of
    # connecting to it directly -- the usual and often the only way in to
    # an AWS RDS instance in a private subnet, which has no publicly
    # routable endpoint at all. None (the default) means a direct
    # connection, exactly as before.
    #
    # Nothing outside tgdatabridge.db.ssh_tunnel and the connection dialog reads
    # this: connector_factory resolves it into a plain 127.0.0.1:<port>
    # host/port pair before any connector sees the parameters, so every
    # connector, the introspectors, the migrator and the CLI keep working
    # unchanged. See tgdatabridge/db/ssh_tunnel.py.
    ssh: Optional["SshTunnelConfig"] = None

    # How this machine is meant to reach the database: directly, through
    # an SSH jump host, or over an already-connected VPN. See
    # tgdatabridge/db/access.py. Defaults to the direct/public mode, which is
    # what every existing caller and every saved job already means.
    access_mode: str = ACCESS_DIRECT

    # Certificate-based encryption (TLS/SSL) for the connection itself --
    # a separate concern from `ssh` above, and the two compose freely (a
    # tunneled connection can also be encrypted end-to-end). None (the
    # default) means unencrypted, exactly as before. See
    # tgdatabridge/db/tls_config.py.
    tls: Optional["TlsConfig"] = None


def require_connected(conn: Any, driver_label: str) -> Any:
    """Guard for the top of every connector method that dereferences its
    own `self._conn` (`.cursor()`, `.execute()`, ...): raises a clear,
    *recognized-as-transient* error instead of letting `None.cursor()`
    raise a bare AttributeError.

    The gap this closes: `tgdatabridge.core.retry.retry_call`'s `reconnect`
    parameter (built by `migrator._reconnect_callback` as `close()` then
    `connect()`) is deliberately allowed to fail -- "a reconnect that
    itself raises is caught and discarded", per retry.py's own docstring,
    so a broken `reconnect` never makes a retry worse than not having one.
    But a *partial* failure inside reconnect -- `close()` succeeds (setting
    `self._conn = None`) and the following `connect()` then raises, e.g.
    because the network glitch that caused the original drop hadn't fully
    cleared yet -- leaves the connector with `self._conn = None` for the
    *next* attempt. On a real production migration this surfaced as
    `'NoneType' object has no attribute 'cursor'` on the very next call
    (Oracle-as-source, 20+ minutes into a LOB-heavy table's second retry):
    a confusing, connector-specific crash that is also invisible to
    `is_transient()`, so it aborted the retry loop immediately instead of
    trying again, AND skipped the "reconnect the source before moving to
    the next table" fallback in migrator.migrate_table's own except block
    (guarded by `is_transient(exc)`), reintroducing the exact
    connection-poisons-every-subsequent-table failure mode Round 27 fixed
    for DPY-4011/DPY-1001.

    Raising "not connected to database" here instead -- already one of
    retry.py's recognized transient markers -- means: (1) the message is
    the same regardless of which connector/driver hit it, so a person
    reading the log sees a clear, actionable statement instead of a
    driver-internals AttributeError; (2) if attempts remain, retry_call
    retries again (giving reconnect() another chance to actually
    succeed) instead of giving up on an unrecognized error; and (3) if
    this was the final attempt and the exception propagates all the way
    out, migrate_table's own except block correctly recognizes it as
    transient and still reconnects the source before returning, so the
    *next* table's migrate_table call doesn't inherit a dead connection.
    """
    if conn is None:
        raise RuntimeError(
            f"{driver_label}: not connected to database -- the connection was closed "
            "(or a reconnect after a dropped connection did not succeed) and this call "
            "was made before a new one was established."
        )
    return conn


class DBConnector(Protocol):
    """Minimal interface every engine connector implements."""

    def connect(self) -> None: ...

    def close(self) -> None: ...

    def test_connection(self) -> tuple[bool, str]: ...

    def execute(self, sql: str, params: Optional[dict] = None) -> Iterable[tuple]: ...

    def execute_ddl(self, sql: str) -> None: ...
