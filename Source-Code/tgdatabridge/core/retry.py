"""Retry-with-backoff for the network-facing calls this tool makes during
data migration (target.insert_batch, and validation's count_rows/
checksum_rows) -- none of the six connectors retried anything before this
module existed: a single dropped connection, deadlock, or transient
timeout failed the whole call immediately, taking down an otherwise-fine
migration over a blip.

This deliberately doesn't touch the connectors themselves; it wraps
callables from the outside (migrator.py is the only caller today), the
same way DDL/data are already handled without any connector-specific
knowledge baked into the calling code.

A genuinely dropped connection is a special case worth calling out: a
real production migration hit exactly this on a table it had been
writing to for hours -- Oracle's driver raised "DPY-4011: the database
or network closed the connection", `is_transient()` correctly recognized
it (its message matches "connection is closed" below) and retried, and
the retry failed with the *identical* error, because retrying only
re-called `target.insert_batch()` on the same now-dead connection object
-- nothing about a plain retry re-establishes a TCP connection the OS
already tore down. `reconnect`, if given to retry_call() below, is called
once per retry (after the backoff sleep, before the retried call) for
exactly this: pass the target connector's own `close()` then `connect()`
so a retry after a real disconnection has an actual chance of succeeding
instead of being guaranteed to fail the same way every time. A reconnect
that itself raises is swallowed -- the retried call below will surface
its own failure the same way it always did, so a broken `reconnect` never
makes this worse than not having one.

A note on correctness: retrying target.insert_batch() is only truly safe
when the target's insert either fully commits or fully rolls back a batch
-- if a batch partially lands (a driver-level executemany can behave this
way under autocommit, which PostgresConnector/MySQLConnector both use) and
then the call raises, a naive retry re-sends the whole batch and produces
duplicate rows. This module does not attempt to detect or dedupe that
case: it is offered as a meaningful reliability improvement over "zero
retries", not as an exactly-once delivery guarantee. Environments that
need the latter should pair retries with a target-side unique constraint
(so a duplicate insert fails loudly instead of silently duplicating) or
route through migrator's checkpoint/resume support, which only ever
re-sends a batch that is *known* not to have reached the target yet.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Optional, TypeVar

T = TypeVar("T")

# Every connector's underlying driver raises its own exception hierarchy
# (psycopg.OperationalError, oracledb.DatabaseError, pyodbc.Error, pymongo
# errors, ...) -- rather than importing all six drivers just to catch their
# specific transient-error subclasses (several aren't even installed in
# every environment this tool runs in), is_transient() makes a best-effort
# judgment from the exception's *message text* for the handful of
# well-known transient conditions. Anything that doesn't match is assumed
# non-transient (a bad password, a syntax error, a missing table, a
# constraint violation) and re-raised on the very first attempt rather than
# wasted retrying something that will never succeed.
_TRANSIENT_MARKERS = (
    "connection reset", "connection refused", "connection reset by peer",
    "timeout", "timed out", "deadlock", "lock wait", "try again",
    "temporary failure", "broken pipe", "server closed the connection",
    "connection already closed", "could not connect", "network error",
    "connection is closed", "connection lost", "server has gone away",
    "communication link failure",
    # Oracle-specific (python-oracledb), added after a real production
    # migration hit both of these back to back on the *source* side: the
    # driver raises "DPY-4011: the database or network closed the
    # connection" when the network drops mid-fetch (matched by "closed
    # the connection" below, broadened from the server-only phrasing
    # above so this one matches too), and every subsequent call on that
    # now-dead connection object raises "DPY-1001: not connected to
    # database" instead -- a different message for the same underlying
    # cause, so it needs its own marker rather than relying on the first
    # one to cover it. The literal error codes are matched too, as a
    # belt-and-suspenders measure independent of the English wording.
    "closed the connection", "not connected to database",
    "dpy-4011", "dpy-1001",
    # "the connection is lost" (psycopg's own wording for a dropped libpq
    # connection, e.g. after a long-running batch outlives an RDS/pgbouncer
    # idle or keepalive timeout) does NOT contain "connection lost" as a
    # substring -- the word "is" sits between them -- so a real production
    # migration hit this verbatim on a LOB table 26 minutes into a run and
    # is_transient() returned False, failing the whole table outright
    # instead of reconnecting and retrying it.
    "connection is lost",
)


def is_transient(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(marker in message for marker in _TRANSIENT_MARKERS)


@dataclass
class RetryPolicy:
    max_attempts: int = 3
    base_delay: float = 0.5
    max_delay: float = 8.0
    multiplier: float = 2.0
    should_retry: Callable[[Exception], bool] = field(default=is_transient)

    def delay_for(self, attempt: int) -> float:
        """attempt is 1-based -- the delay to wait *before* retry attempt
        `attempt + 1`. Capped at max_delay so a high attempt count can't
        produce an unreasonably long wait."""
        return min(self.base_delay * (self.multiplier ** (attempt - 1)), self.max_delay)


def retry_call(
    fn: Callable[..., T],
    *args,
    policy: Optional[RetryPolicy] = None,
    on_retry: Optional[Callable[[int, Exception, float], None]] = None,
    sleep: Callable[[float], None] = time.sleep,
    reconnect: Optional[Callable[[], None]] = None,
    **kwargs,
) -> T:
    """Call fn(*args, **kwargs), retrying up to policy.max_attempts times
    total whenever policy.should_retry(exc) is True for the exception
    raised. The final failing exception (or the first non-retryable one)
    propagates to the caller unchanged -- this never swallows an error, it
    only delays reporting it.

    `sleep` is injectable purely so tests can assert on computed delay
    values without a real test run taking several seconds waiting on
    time.sleep. `on_retry(attempt, exc, delay)` -- if given -- is invoked
    right before each sleep (e.g. to log "retrying table X (attempt 2/3)
    after connection reset..." to the existing log console).

    `reconnect`, if given, is called once after each sleep, right before
    `fn` is retried -- see this module's own docstring for why a plain
    retry can never recover from a connection the OS has actually torn
    down, only from something transient *within* an otherwise-live
    connection (a lock wait, a brief server-side hiccup). Any exception
    `reconnect` itself raises is caught and discarded here: `fn`'s own
    retried call is what surfaces a real failure, exactly as it did
    before this parameter existed, so a `reconnect` that doesn't work
    can't make a retry any worse than not attempting one at all."""
    policy = policy or RetryPolicy()
    attempt = 1
    while True:
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - re-raised below when not retryable/exhausted
            if attempt >= policy.max_attempts or not policy.should_retry(exc):
                raise
            delay = policy.delay_for(attempt)
            if on_retry:
                on_retry(attempt, exc, delay)
            sleep(delay)
            if reconnect is not None:
                try:
                    reconnect()
                except Exception:  # noqa: BLE001 - see this function's own docstring
                    pass
            attempt += 1
