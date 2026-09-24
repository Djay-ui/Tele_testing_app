"""Tests for tgdatabridge.db.pool.ConnectionPool -- see that module's own
docstring for why this exists (no DB-API driver this tool uses documents
a bare Connection object as safe to share across threads, so parallel
table migration needs its own connection per worker thread, drawn from a
small bounded pool instead)."""
import threading
import time

from tgdatabridge.db.pool import ConnectionPool


class _FakeConn:
    _next_id = [0]

    def __init__(self):
        self.id = _FakeConn._next_id[0]
        _FakeConn._next_id[0] += 1
        self.closed = False

    def close(self):
        self.closed = True


def _reset_ids():
    _FakeConn._next_id[0] = 0


def test_max_size_must_be_at_least_one():
    try:
        ConnectionPool(_FakeConn, max_size=0)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_acquire_creates_up_to_max_size_then_reuses():
    _reset_ids()
    pool = ConnectionPool(_FakeConn, max_size=2)
    c1 = pool.acquire()
    c2 = pool.acquire()
    assert {c1.id, c2.id} == {0, 1}
    assert pool.created == 2
    pool.release(c1)
    c3 = pool.acquire()
    # Reused c1 (now idle) rather than creating a third connection --
    # already at max_size, and one was available.
    assert c3 is c1
    assert pool.created == 2


def test_created_never_exceeds_max_size_under_concurrent_acquire():
    _reset_ids()
    pool = ConnectionPool(_FakeConn, max_size=3)
    results = []
    lock = threading.Lock()

    def worker():
        conn = pool.acquire()
        with lock:
            results.append(conn.id)
        time.sleep(0.01)
        pool.release(conn)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert pool.created <= 3
    assert len(results) == 10


def test_connection_context_manager_releases_on_success_and_on_exception():
    _reset_ids()
    pool = ConnectionPool(_FakeConn, max_size=1)
    with pool.connection() as conn:
        first_id = conn.id
    # Released back -- a second use gets the same connection, not a new one.
    with pool.connection() as conn2:
        assert conn2.id == first_id

    try:
        with pool.connection() as conn3:
            assert conn3.id == first_id
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    # Still released even though the body raised.
    with pool.connection() as conn4:
        assert conn4.id == first_id
    assert pool.created == 1


def test_close_all_closes_every_created_connection_and_resets_pool():
    _reset_ids()
    pool = ConnectionPool(_FakeConn, max_size=3)
    conns = [pool.acquire() for _ in range(3)]
    for c in conns:
        pool.release(c)
    pool.close_all()
    assert all(c.closed for c in conns)
    assert pool.created == 0

    # Reusable after close_all -- a fresh acquire creates a brand new
    # connection rather than erroring or returning a closed one.
    new_conn = pool.acquire()
    assert new_conn.closed is False
    assert pool.created == 1


def test_close_all_is_best_effort_one_bad_close_does_not_stop_the_rest():
    class _FlakyConn:
        def __init__(self, should_raise):
            self.should_raise = should_raise
            self.closed = False

        def close(self):
            if self.should_raise:
                raise RuntimeError("network blip")
            self.closed = True

    calls = [0]

    def factory():
        calls[0] += 1
        return _FlakyConn(should_raise=(calls[0] == 1))

    pool = ConnectionPool(factory, max_size=2)
    c1 = pool.acquire()
    c2 = pool.acquire()
    pool.release(c1)
    pool.release(c2)
    pool.close_all()  # c1 raises on close(); must not prevent c2 from being closed
    assert c2.closed is True
