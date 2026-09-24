"""Fixtures shared by the integration suite.

The shape of every test here is the same: take an engine this run can
reach, give it a scratch namespace nobody else is using, do something
real to it, and drop the namespace afterwards whether the test passed or
not.

Parameterisation is over `engines.available_engines()`, so the same test
body runs once per reachable engine and the test id names the engine
(`test_full_arc[PostgreSQL]`). An engine that isn't configured doesn't
generate a parameter at all -- rather than generating one that skips --
because a suite reporting "5 skipped" on every developer machine trains
people to ignore skips, and the one time it matters they will.
"""
from __future__ import annotations

import os

import pytest

from tests.integration import engines as engine_registry

_AVAILABLE = engine_registry.available_engines()

# One counter per process so two tests never share a namespace, and the
# pid so two concurrent pytest processes (pytest-xdist, or two developers
# against one shared server) never do either.
_counter = {"n": 0}


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "integration: needs a real database server")


def pytest_report_header(config):
    """Say plainly what was and wasn't exercised. A green run that
    silently covered one engine out of six is the failure mode this line
    exists to prevent."""
    reached = ", ".join(e.label for e in _AVAILABLE) or "none"
    missing = ", ".join(engine_registry.missing_engines()) or "none"
    return [
        f"integration engines reachable: {reached}",
        f"integration engines NOT configured (tests skipped): {missing}",
    ]


def pytest_generate_tests(metafunc):
    if "engine" in metafunc.fixturenames:
        if not _AVAILABLE:
            metafunc.parametrize(
                "engine", [pytest.param(None, marks=pytest.mark.skip(
                    reason="no integration engine configured -- see "
                           "tests/integration/engines.py"))])
        else:
            metafunc.parametrize("engine", _AVAILABLE,
                                 ids=[e.label for e in _AVAILABLE])


@pytest.fixture
def namespace(engine):
    """A scratch schema/database, created before the test and dropped
    after it, named so its origin is obvious if one ever does leak."""
    _counter["n"] += 1
    name = f"tgdatabridge_it_{os.getpid()}_{_counter['n']}"
    engine.create_namespace(name)
    try:
        yield name
    finally:
        engine.drop_namespace(name)


@pytest.fixture
def target(engine, namespace):
    """A connected target connector pointed at the scratch namespace."""
    from tgdatabridge.core.connector_factory import make_target_connector

    conn = make_target_connector(engine.label, engine.params_for(namespace))
    conn.connect()
    try:
        yield conn
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001 - closing a broken connection isn't a test failure
            pass


@pytest.fixture
def source_reader(engine, namespace):
    """A *second* connection to the same namespace, used as a source to
    read back what the migration wrote.

    Deliberately separate from `target`: reading through the same
    connection that did the writing can see uncommitted state and would
    hide a missing commit, which is precisely the class of bug an
    integration test is here to catch.
    """
    from tgdatabridge.core.connector_factory import make_source_connector

    conn = make_source_connector(engine.label, engine.params_for(namespace))
    conn.connect()
    try:
        yield conn
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
