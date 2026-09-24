"""Tests for tgdatabridge.core.retry: transient-error detection and the
retry-with-backoff wrapper migrator.py uses around target.insert_batch."""
from tgdatabridge.core.retry import RetryPolicy, is_transient, retry_call


# -------------------------------------------------------------- is_transient


def test_is_transient_matches_known_transient_messages():
    assert is_transient(RuntimeError("connection reset by peer")) is True
    assert is_transient(RuntimeError("Connection Refused")) is True  # case-insensitive
    assert is_transient(TimeoutError("operation timed out")) is True
    assert is_transient(RuntimeError("deadlock detected")) is True
    assert is_transient(RuntimeError("server closed the connection unexpectedly")) is True


def test_is_transient_false_for_non_transient_errors():
    assert is_transient(ValueError("invalid literal for int()")) is False
    assert is_transient(RuntimeError("column \"foo\" does not exist")) is False
    assert is_transient(RuntimeError("duplicate key value violates unique constraint")) is False


def test_is_transient_matches_the_two_oracle_dpy_errors_a_dropped_connection_produces():
    # A real migration hit both of these back to back on the source side:
    # DPY-4011 the moment the network actually dropped mid-fetch, then
    # DPY-1001 on every following call to the same now-dead connection.
    assert is_transient(RuntimeError(
        "DPY-4011: the database or network closed the connection")) is True
    assert is_transient(RuntimeError("DPY-1001: not connected to database")) is True
    # Case-insensitive, and matched by the plain English phrasing too, not
    # just the error code -- in case a driver ever changes its code prefix.
    assert is_transient(RuntimeError("dpy-4011: the database or network closed the connection")) is True


def test_is_transient_matches_psycopgs_the_connection_is_lost_wording():
    # A real production migration hit this verbatim on a LOB table 26
    # minutes into a run: "connection lost" (already matched above) is NOT
    # a substring of "the connection is lost" -- the word "is" sits between
    # "connection" and "lost" -- so this exact, real-world message used to
    # fall through every marker and fail the table outright instead of
    # reconnecting and retrying.
    assert is_transient(RuntimeError("the connection is lost")) is True
    assert is_transient(RuntimeError("SSL error: the connection is lost")) is True


# -------------------------------------------------------------- RetryPolicy


def test_delay_for_grows_exponentially_up_to_max():
    policy = RetryPolicy(base_delay=1.0, multiplier=2.0, max_delay=10.0)
    assert policy.delay_for(1) == 1.0
    assert policy.delay_for(2) == 2.0
    assert policy.delay_for(3) == 4.0
    assert policy.delay_for(4) == 8.0
    assert policy.delay_for(5) == 10.0  # capped


def test_default_policy_has_reasonable_defaults():
    policy = RetryPolicy()
    assert policy.max_attempts == 3
    assert policy.should_retry is is_transient


# ---------------------------------------------------------------- retry_call


def test_retry_call_returns_immediately_on_first_success():
    calls = []

    def fn():
        calls.append(1)
        return "ok"

    result = retry_call(fn, sleep=lambda _d: None)
    assert result == "ok"
    assert len(calls) == 1


def test_retry_call_retries_transient_failures_then_succeeds():
    attempts = {"n": 0}

    def fn():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise RuntimeError("connection reset by peer")
        return "ok"

    sleeps = []
    result = retry_call(fn, policy=RetryPolicy(max_attempts=5), sleep=sleeps.append)
    assert result == "ok"
    assert attempts["n"] == 3
    assert len(sleeps) == 2  # slept before each of the 2 retries


def test_retry_call_raises_immediately_for_non_transient_error():
    attempts = {"n": 0}

    def fn():
        attempts["n"] += 1
        raise ValueError("bad input")

    try:
        retry_call(fn, policy=RetryPolicy(max_attempts=5), sleep=lambda _d: None)
        assert False, "expected ValueError"
    except ValueError:
        pass
    assert attempts["n"] == 1  # never retried


def test_retry_call_gives_up_after_max_attempts():
    attempts = {"n": 0}

    def fn():
        attempts["n"] += 1
        raise RuntimeError("timeout")

    try:
        retry_call(fn, policy=RetryPolicy(max_attempts=3), sleep=lambda _d: None)
        assert False, "expected RuntimeError"
    except RuntimeError:
        pass
    assert attempts["n"] == 3


def test_retry_call_invokes_on_retry_before_each_sleep():
    attempts = {"n": 0}
    calls = []

    def fn():
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise RuntimeError("connection reset")
        return "ok"

    retry_call(
        fn, policy=RetryPolicy(max_attempts=3),
        on_retry=lambda attempt, exc, delay: calls.append((attempt, str(exc))),
        sleep=lambda _d: None,
    )
    assert calls == [(1, "connection reset")]


def test_retry_call_passes_args_and_kwargs_through():
    def fn(a, b, c=None):
        return (a, b, c)

    result = retry_call(fn, 1, 2, c=3, sleep=lambda _d: None)
    assert result == (1, 2, 3)


def test_retry_call_custom_should_retry_overrides_is_transient():
    attempts = {"n": 0}

    def fn():
        attempts["n"] += 1
        raise ValueError("custom retryable")

    try:
        retry_call(
            fn, policy=RetryPolicy(max_attempts=2, should_retry=lambda exc: True),
            sleep=lambda _d: None,
        )
    except ValueError:
        pass
    assert attempts["n"] == 2  # retried despite not matching is_transient's own markers


# --------------------------------------------------- retry_call's reconnect


def test_retry_call_invokes_reconnect_between_a_failed_attempt_and_the_retry():
    # The actual production bug this fixes: retrying target.insert_batch()
    # on a connection the network already tore down ("DPY-4011: the
    # database or network closed the connection") failed identically every
    # time, because nothing about a plain retry re-established the
    # connection object itself.
    attempts = {"n": 0}
    reconnects = []

    def fn():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise RuntimeError("the connection is closed")
        return "ok"

    result = retry_call(
        fn, policy=RetryPolicy(max_attempts=5), sleep=lambda _d: None,
        reconnect=lambda: reconnects.append(attempts["n"]),
    )
    assert result == "ok"
    # Called once per retry (2 retries here), each right after the
    # failure it's recovering from.
    assert reconnects == [1, 2]


def test_retry_call_never_calls_reconnect_when_the_first_attempt_succeeds():
    calls = []
    retry_call(lambda: "ok", sleep=lambda _d: None, reconnect=lambda: calls.append(1))
    assert calls == []


def test_retry_call_swallows_a_reconnect_that_itself_raises():
    # A reconnect attempt that fails (the database is still down, say)
    # must not prevent the retried call from running and surfacing its
    # own, real error -- a broken reconnect is never worse than having
    # none at all.
    attempts = {"n": 0}

    def fn():
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise RuntimeError("connection reset")
        return "ok"

    def broken_reconnect():
        raise OSError("still can't reach the host")

    result = retry_call(
        fn, policy=RetryPolicy(max_attempts=3), sleep=lambda _d: None,
        reconnect=broken_reconnect,
    )
    assert result == "ok"
