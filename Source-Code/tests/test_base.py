"""Tests for tgdatabridge.db.base.require_connected -- the guard every
connector calls before dereferencing its own `self._conn`, so a call made
after a dropped-then-failed-to-reconnect connection raises a clear,
recognized-as-transient error instead of a bare AttributeError.

See require_connected's own docstring for the real-world failure this
closes: a real production migration hit 'NoneType' object has no attribute
'cursor' on an Oracle source, 20+ minutes into a LOB-heavy table's second
retry, because retry.py's `reconnect` callback is deliberately allowed to
fail silently (close() succeeded, the following connect() didn't), leaving
the connector's `_conn` at None for the next call.
"""
import pytest

from tgdatabridge.core.retry import is_transient
from tgdatabridge.db.base import require_connected


def test_require_connected_returns_the_connection_unchanged_when_present():
    sentinel = object()
    assert require_connected(sentinel, "PostgreSQL") is sentinel


def test_require_connected_raises_a_clear_error_when_conn_is_none():
    with pytest.raises(RuntimeError, match="not connected to database"):
        require_connected(None, "Oracle")


def test_require_connected_names_the_driver_in_the_message():
    with pytest.raises(RuntimeError, match="MySQL"):
        require_connected(None, "MySQL")


def test_require_connected_error_is_recognized_as_transient():
    # The whole point: retry_call (and migrate_table's own except block)
    # must recognize this as retriable/reconnectable, not a hard failure.
    try:
        require_connected(None, "Oracle")
    except RuntimeError as exc:
        assert is_transient(exc) is True
    else:
        pytest.fail("expected RuntimeError")
