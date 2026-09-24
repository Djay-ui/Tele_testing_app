"""A minimal blocking connection pool: lazily creates up to `max_size`
connector instances via a factory, and hands them out via acquire()/
release() (or the `with pool.connection() as conn:` context manager).

This exists for tgdatabridge.core.migrator's parallel table migration and
tgdatabridge.core.batch's multi-schema batch runs (see ENTERPRISE_READINESS.md
section 4, "Scale & performance"): sharing a single DB-API connection
object across threads is unsafe for every driver this tool uses
(python-oracledb, psycopg, mysql-connector-python, pyodbc, ibm_db,
PyMongo -- none of them document a bare Connection object as safe for
concurrent use from multiple threads), so N worker threads need up to N
of their own connections, not variations on sharing one.

This is intentionally simple -- no idle-connection health checks, no
reaping of long-idle connections, no async support, no min-size/
warm-up policy. It's a fixed-size pool of connector instances built once
per migration run (sized to however many worker threads that run
actually uses) and torn down at the end via close_all(), not a
general-purpose long-lived pool for a server process. `factory` is
whatever the caller needs to produce one fully-connected connector
instance -- typically `lambda: _make_connector(engine, params)` followed
by `.connect()`, or an equivalent closure -- this module has no idea
what "a connection" even is beyond "something with a `.close()` method",
by design, so it works identically for every engine this tool supports.
"""
from __future__ import annotations

import queue
import threading
from contextlib import contextmanager
from typing import Callable, List, TypeVar

T = TypeVar("T")


class ConnectionPool:
    def __init__(self, factory: Callable[[], T], max_size: int):
        if max_size < 1:
            raise ValueError("max_size must be at least 1")
        self._factory = factory
        self._max_size = max_size
        self._lock = threading.Lock()
        self._created = 0
        self._idle: "queue.Queue[T]" = queue.Queue()
        self._all: List[T] = []

    @property
    def max_size(self) -> int:
        return self._max_size

    @property
    def created(self) -> int:
        """How many connections this pool has actually created so far --
        never more than max_size, and often fewer (e.g. a run with 3
        tables in its widest dependency "wave" never needs a 5th
        connection even if max_size=5)."""
        with self._lock:
            return self._created

    def acquire(self) -> T:
        """Returns an existing idle connection if one is available,
        otherwise creates a new one (up to max_size), otherwise blocks
        until a connection already in use is released back."""
        try:
            return self._idle.get_nowait()
        except queue.Empty:
            pass
        with self._lock:
            if self._created < self._max_size:
                conn = self._factory()
                self._created += 1
                self._all.append(conn)
                return conn
        # At capacity with nothing idle right now -- wait for a release
        # rather than exceeding max_size.
        return self._idle.get()

    def release(self, conn: T) -> None:
        self._idle.put(conn)

    @contextmanager
    def connection(self):
        conn = self.acquire()
        try:
            yield conn
        finally:
            self.release(conn)

    def close_all(self) -> None:
        """Closes every connection this pool has ever created (idle or
        not -- callers are expected to have finished all work before
        calling this) and resets the pool back to empty, so a
        ConnectionPool instance could in principle be reused for a
        second run. Best-effort: one connection's close() raising
        doesn't stop the rest from being closed too."""
        for conn in self._all:
            try:
                conn.close()
            except Exception:  # noqa: BLE001 - best-effort cleanup, don't let one bad close() hide the others
                pass
        self._all = []
        self._created = 0
        self._idle = queue.Queue()
