"""Tests for tgdatabridge.cdc.preflight, connect_client and orchestrator --
SCALE.md section 2.1.

Everything here runs against fakes: a fake Oracle catalog that answers
the preflight queries, and a fake HTTP transport for Kafka Connect. That
keeps the suite free of a database and a Kafka cluster, but it also means
these tests verify *this tool's* logic, not the remote APIs' real
behaviour. The Kafka Connect client in particular needs a smoke test
against a real cluster before a production cutover; see its module
docstring.

Plain functions, no pytest -- see tests/test_cli_config.py's note.
"""
import json

from tgdatabridge.cdc import orchestrator
from tgdatabridge.cdc import preflight
from tgdatabridge.cdc.config import DebeziumConnectorConfig
from tgdatabridge.cdc.connect_client import ConnectError, ConnectorStatus, KafkaConnectClient


# ============================================================== preflight

class _FakeOracle:
    """Answers the preflight probes by substring-matching the SQL. Any
    key set to the sentinel _RAISE makes that query blow up, which is how
    the 'this account can't see that view' paths get covered."""

    RAISE = object()

    def __init__(self, **answers):
        self.answers = answers
        self.queries = []

    def execute(self, sql):
        self.queries.append(sql)
        for fragment, value in self.answers.items():
            if fragment.replace("_", " ").lower() in sql.lower() or fragment.lower() in sql.lower():
                if value is _FakeOracle.RAISE:
                    raise RuntimeError("ORA-00942: table or view does not exist")
                return value
        return []


def _healthy_oracle(tables=("EMPLOYEES", "DEPARTMENTS")):
    return _FakeOracle(**{
        "LOG_MODE": [("ARCHIVELOG",)],
        "SUPPLEMENTAL_LOG_DATA_MIN": [("YES",)],
        "ALL_LOG_GROUPS": [(t,) for t in tables],
        "ALL_TAB_PRIVS": [(v,) for v in preflight._REQUIRED_VIEWS],
        "db_flashback_retention_target": [("1440",)],
    })


def test_healthy_database_passes_every_check():
    report = preflight.run_preflight(
        _healthy_oracle(), "HR", ["EMPLOYEES", "DEPARTMENTS"], "CDC_USER")
    assert report.ready is True
    assert report.failures == []


def test_archivelog_off_is_a_blocking_failure():
    source = _healthy_oracle()
    source.answers["LOG_MODE"] = [("NOARCHIVELOG",)]
    report = preflight.run_preflight(source, "HR", ["EMPLOYEES"], "CDC_USER")
    assert report.ready is False
    failure = report.failures[0]
    assert "ARCHIVELOG" in failure.name
    assert "ALTER DATABASE ARCHIVELOG" in failure.remedy


def test_minimal_supplemental_logging_off_is_blocking_and_explains_the_silence():
    # This is the one that produces wrong data with no error at all, so
    # the message has to say so.
    source = _healthy_oracle()
    source.answers["SUPPLEMENTAL_LOG_DATA_MIN"] = [("NO",)]
    report = preflight.run_preflight(source, "HR", ["EMPLOYEES"], "CDC_USER")
    failure = [f for f in report.failures if "Minimal" in f.name][0]
    assert "before" in failure.detail
    assert "ALTER DATABASE ADD SUPPLEMENTAL LOG DATA;" in failure.remedy


def test_implicit_supplemental_logging_counts_as_enabled():
    source = _healthy_oracle()
    source.answers["SUPPLEMENTAL_LOG_DATA_MIN"] = [("IMPLICIT",)]
    report = preflight.run_preflight(source, "HR", ["EMPLOYEES"], "CDC_USER")
    assert report.ready is True


def test_a_table_missing_supplemental_logging_is_named():
    source = _healthy_oracle(tables=("EMPLOYEES",))
    report = preflight.run_preflight(source, "HR", ["EMPLOYEES", "DEPARTMENTS"], "CDC_USER")
    failure = [f for f in report.failures if "Per-table" in f.name][0]
    assert "DEPARTMENTS" in failure.detail
    assert "ALTER TABLE HR.DEPARTMENTS ADD SUPPLEMENTAL LOG DATA (ALL) COLUMNS;" in failure.remedy


def test_many_missing_tables_are_truncated_not_dumped():
    many = [f"T{i}" for i in range(30)]
    source = _healthy_oracle(tables=())
    report = preflight.run_preflight(source, "HR", many, "CDC_USER")
    failure = [f for f in report.failures if "Per-table" in f.name][0]
    assert "+20 more" in failure.detail
    assert "and 20 more" in failure.remedy


def test_missing_logminer_grants_warn_rather_than_fail():
    # The grant may have come via a role, which ALL_TAB_PRIVS doesn't
    # show -- failing hard would block correctly-configured databases.
    source = _healthy_oracle()
    source.answers["ALL_TAB_PRIVS"] = [("V_$DATABASE",)]
    report = preflight.run_preflight(source, "HR", ["EMPLOYEES"], "CDC_USER")
    assert report.ready is True
    warning = [w for w in report.warnings if "LogMiner" in w.name][0]
    assert "role" in warning.detail
    assert "GRANT LOGMINING TO CDC_USER;" in warning.remedy


def test_short_retention_warns_about_outrunning_the_load():
    source = _healthy_oracle()
    source.answers["db_flashback_retention_target"] = [("60",)]
    report = preflight.run_preflight(source, "HR", ["EMPLOYEES"], "CDC_USER")
    warning = [w for w in report.warnings if "retention" in w.name.lower()][0]
    assert "unable to resume" in warning.detail


def test_zero_retention_warns_rather_than_failing():
    # 0 is Oracle's default and can be perfectly fine depending on the
    # backup regime -- this tool can't tell which.
    source = _healthy_oracle()
    source.answers["db_flashback_retention_target"] = [("0",)]
    report = preflight.run_preflight(source, "HR", ["EMPLOYEES"], "CDC_USER")
    assert report.ready is True
    assert any("retention" in w.name.lower() for w in report.warnings)


def test_unreadable_views_report_unknown_rather_than_crashing():
    # A preflight tool that itself dies on a permissions problem is not
    # much use.
    source = _FakeOracle(**{
        "LOG_MODE": _FakeOracle.RAISE,
        "SUPPLEMENTAL_LOG_DATA_MIN": _FakeOracle.RAISE,
        "ALL_LOG_GROUPS": _FakeOracle.RAISE,
        "ALL_TAB_PRIVS": _FakeOracle.RAISE,
        "db_flashback_retention_target": _FakeOracle.RAISE,
    })
    report = preflight.run_preflight(source, "HR", ["EMPLOYEES"], "CDC_USER")
    assert report.ready is True          # unknown doesn't block
    assert len(report.unknowns) >= 3


def test_report_renders_remedies_under_their_check():
    source = _healthy_oracle()
    source.answers["LOG_MODE"] = [("NOARCHIVELOG",)]
    rendered = preflight.run_preflight(source, "HR", ["EMPLOYEES"], "CDC_USER").render()
    assert "[FAIL]" in rendered
    assert "ALTER DATABASE ARCHIVELOG" in rendered


def test_preflight_never_writes_to_the_source():
    # Enabling supplemental logging on a production Oracle has a real
    # performance cost and is a DBA's decision, not this tool's.
    source = _healthy_oracle()
    preflight.run_preflight(source, "HR", ["EMPLOYEES"], "CDC_USER")
    for sql in source.queries:
        assert sql.strip().upper().startswith("SELECT"), sql


# ========================================================= connect client

class _FakeTransport:
    def __init__(self, responses=None, error=None):
        self.responses = responses or {}
        self.error = error
        self.calls = []

    def __call__(self, method, url, body, timeout):
        self.calls.append((method, url, json.loads(body) if body else None))
        if self.error is not None:
            raise self.error
        for fragment, (status, payload) in self.responses.items():
            if fragment in url:
                raw = json.dumps(payload).encode() if payload is not None else b""
                return status, raw
        return 200, b""


def _client(**kwargs):
    transport = _FakeTransport(**kwargs)
    return KafkaConnectClient("http://connect:8083", transport=transport), transport


def test_create_or_update_uses_the_idempotent_verb():
    # PUT /config, not POST /connectors -- re-registering the same
    # connector in a pipeline re-run is routine and must not 409.
    client, transport = _client()
    client.create_or_update("c", {"connector.class": "x"})
    method, url, body = transport.calls[0]
    assert method == "PUT"
    assert url.endswith("/connectors/c/config")
    assert body == {"connector.class": "x"}


def test_status_parses_connector_and_task_states():
    client, _ = _client(responses={"/status": (200, {
        "name": "c",
        "connector": {"state": "RUNNING"},
        "tasks": [{"id": 0, "state": "RUNNING"}],
    })})
    status = client.status("c")
    assert status.running is True
    assert status.failed is False


def test_a_running_connector_with_a_failed_task_is_not_healthy():
    # The common Debezium failure shape: capture has silently stopped
    # while the connector still reports RUNNING.
    client, _ = _client(responses={"/status": (200, {
        "connector": {"state": "RUNNING"},
        "tasks": [{"id": 0, "state": "FAILED", "trace": "ORA-01031"}],
    })})
    status = client.status("c")
    assert status.running is False
    assert status.failed is True
    assert "ORA-01031" in status.trace


def test_a_connector_with_no_tasks_is_not_running():
    client, _ = _client(responses={"/status": (200, {
        "connector": {"state": "RUNNING"}, "tasks": []})})
    assert client.status("c").running is False


def test_unreachable_connect_raises_a_clean_error():
    import urllib.error
    client, _ = _client(error=urllib.error.URLError("connection refused"))
    try:
        client.list_connectors()
        assert False, "expected ConnectError"
    except ConnectError as exc:
        assert "Could not reach Kafka Connect" in str(exc)


def test_non_2xx_raises_with_the_body():
    client, _ = _client(responses={"/connectors": (500, {"message": "boom"})})
    try:
        client.list_connectors()
        assert False, "expected ConnectError"
    except ConnectError as exc:
        assert "500" in str(exc)


def test_connector_exists():
    client, _ = _client(responses={"/connectors": (200, ["a", "b"])})
    assert client.connector_exists("a") is True
    assert client.connector_exists("z") is False


def test_pause_and_resume_hit_the_right_endpoints():
    client, transport = _client()
    client.pause("c")
    client.resume("c")
    assert transport.calls[0][1].endswith("/connectors/c/pause")
    assert transport.calls[1][1].endswith("/connectors/c/resume")


def test_restart_failed_tasks_only_restarts_failed_ones():
    client, transport = _client(responses={"/status": (200, {
        "connector": {"state": "RUNNING"},
        "tasks": [{"id": 0, "state": "RUNNING"}, {"id": 1, "state": "FAILED"}],
    })})
    assert client.restart_failed_tasks("c") == [1]
    assert any("/tasks/1/restart" in url for _, url, _ in transport.calls)


# ========================================================== orchestrator

class _ScriptedClient:
    """A KafkaConnectClient stand-in driven by a list of statuses."""

    def __init__(self, statuses, offsets=None):
        self.statuses = list(statuses)
        self.offsets = offsets
        self.registered = None
        self.paused = False
        self.deleted = False

    def create_or_update(self, name, config):
        self.registered = (name, config)

    def status(self, name):
        return self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]

    def pause(self, name):
        self.paused = True

    def delete(self, name):
        self.deleted = True

    def _request(self, method, path):
        if self.offsets is None:
            raise ConnectError("no offsets endpoint")
        return self.offsets


def _status(connector="RUNNING", tasks=("RUNNING",), trace=""):
    return ConnectorStatus("c", connector, list(tasks), trace)


def _connector_config():
    return DebeziumConnectorConfig(
        connector_name="c", database_hostname="h", database_port=1521,
        database_user="u", database_dbname="ORCL", topic_prefix="p",
        kafka_bootstrap_servers="k:9092", schema_name="HR",
        table_names=["EMPLOYEES"],
    )


def test_wait_until_running_returns_once_healthy():
    client = _ScriptedClient([_status(connector="UNASSIGNED", tasks=()), _status()])
    status = orchestrator.wait_until_running(client, "c", sleep_fn=lambda s: None)
    assert status.running is True


def test_wait_until_running_raises_on_a_failed_task():
    client = _ScriptedClient([_status(tasks=("FAILED",), trace="ORA-01031: insufficient privileges")])
    try:
        orchestrator.wait_until_running(client, "c", sleep_fn=lambda s: None)
        assert False, "expected CdcOrchestrationError"
    except orchestrator.CdcOrchestrationError as exc:
        assert "ORA-01031" in str(exc)


def test_wait_until_running_times_out():
    client = _ScriptedClient([_status(connector="UNASSIGNED", tasks=())])
    clock = {"t": 0.0}

    def time_fn():
        clock["t"] += 30.0
        return clock["t"]

    try:
        orchestrator.wait_until_running(
            client, "c", timeout=10, sleep_fn=lambda s: None, time_fn=time_fn)
        assert False, "expected a timeout"
    except orchestrator.CdcOrchestrationError as exc:
        assert "did not reach RUNNING" in str(exc)


def test_start_capture_registers_then_waits():
    client = _ScriptedClient([_status()])
    lines = []
    orchestrator.start_capture(
        client, _connector_config(), run_preflight=False,
        print_fn=lines.append, sleep_fn=lambda s: None)
    assert client.registered[0] == "c"
    # The operator is told, in order, what just happened and what to do.
    assert any("start the bulk load" in line for line in lines)


def test_start_capture_prints_the_ordering_warning():
    client = _ScriptedClient([_status()])
    lines = []
    orchestrator.start_capture(
        client, _connector_config(), run_preflight=False,
        print_fn=lines.append, sleep_fn=lambda s: None)
    assert any("BEFORE starting the bulk load" in line for line in lines)


def test_start_capture_refuses_when_preflight_fails():
    # Starting capture with minimal supplemental logging off produces
    # events with missing before-images -- corrupting the target with no
    # error anywhere.
    source = _healthy_oracle()
    source.answers["SUPPLEMENTAL_LOG_DATA_MIN"] = [("NO",)]
    client = _ScriptedClient([_status()])
    try:
        orchestrator.start_capture(
            client, _connector_config(), source=source,
            print_fn=lambda line: None, sleep_fn=lambda s: None)
        assert False, "expected CdcOrchestrationError"
    except orchestrator.CdcOrchestrationError as exc:
        assert "blocking problem" in str(exc)
    assert client.registered is None, "must not register a connector after a failed preflight"


def test_start_capture_needs_a_source_when_preflighting():
    client = _ScriptedClient([_status()])
    try:
        orchestrator.start_capture(client, _connector_config(), source=None,
                                   print_fn=lambda line: None)
        assert False, "expected CdcOrchestrationError"
    except orchestrator.CdcOrchestrationError as exc:
        assert "needs a connected source" in str(exc)


def _offsets(scn):
    return {"offsets": [{"partition": {}, "offset": {"scn": str(scn)}}]}


def test_committed_scn_is_read_from_the_offsets_endpoint():
    client = _ScriptedClient([_status()], offsets=_offsets(12345))
    assert orchestrator.connector_committed_scn(client, "c") == 12345


def test_commit_scn_is_preferred_and_compound_values_are_parsed():
    client = _ScriptedClient([_status()], offsets={
        "offsets": [{"offset": {"commit_scn": "999:1:0a0b", "scn": "12345"}}]})
    assert orchestrator.connector_committed_scn(client, "c") == 999


def test_missing_offsets_endpoint_yields_unknown_lag():
    client = _ScriptedClient([_status()], offsets=None)
    reading = orchestrator.measure_lag(_healthy_oracle(), client, "c")
    assert reading.known is False
    assert "3.6 or newer" in reading.detail


def test_lag_is_computed_from_the_scn_timestamp():
    client = _ScriptedClient([_status()], offsets=_offsets(500))
    source = _FakeOracle(**{"SCN_TO_TIMESTAMP": [(12.5,)]})
    reading = orchestrator.measure_lag(source, client, "c")
    assert reading.known is True
    assert reading.seconds == 12.5


def test_unconvertible_scn_reports_unknown_not_zero():
    source = _FakeOracle(**{"SCN_TO_TIMESTAMP": _FakeOracle.RAISE})
    client = _ScriptedClient([_status()], offsets=_offsets(500))
    reading = orchestrator.measure_lag(source, client, "c")
    assert reading.known is False
    assert "ORA-08181" in reading.detail


def test_wait_for_drain_returns_once_lag_is_under_target():
    client = _ScriptedClient([_status()], offsets=_offsets(500))
    source = _FakeOracle(**{"SCN_TO_TIMESTAMP": [(5.0,)]})
    reading = orchestrator.wait_for_drain(
        source, client, "c", target_seconds=60, print_fn=lambda line: None,
        sleep_fn=lambda s: None)
    assert reading.seconds == 5.0


def test_wait_for_drain_never_treats_unknown_lag_as_drained():
    # At cutover, "I can't tell how far behind we are" must not read the
    # same as "we're caught up".
    client = _ScriptedClient([_status()], offsets=None)
    clock = {"t": 0.0}

    def time_fn():
        clock["t"] += 100.0
        return clock["t"]

    try:
        orchestrator.wait_for_drain(
            _healthy_oracle(), client, "c", timeout=10, print_fn=lambda line: None,
            sleep_fn=lambda s: None, time_fn=time_fn)
        assert False, "expected a timeout rather than a false 'drained'"
    except orchestrator.CdcOrchestrationError as exc:
        assert "Do not cut over" in str(exc)


def test_wait_for_drain_aborts_if_the_connector_fails():
    client = _ScriptedClient([_status(tasks=("FAILED",), trace="mining error")], offsets=_offsets(1))
    source = _FakeOracle(**{"SCN_TO_TIMESTAMP": [(9999.0,)]})
    try:
        orchestrator.wait_for_drain(
            source, client, "c", target_seconds=1, print_fn=lambda line: None,
            sleep_fn=lambda s: None)
        assert False, "expected CdcOrchestrationError"
    except orchestrator.CdcOrchestrationError as exc:
        assert "failed while draining" in str(exc)


def test_stop_capture_pauses_by_default():
    # Pause retains offsets, so a rolled-back cutover can resume instead
    # of re-snapshotting a 1 TB source.
    client = _ScriptedClient([_status()])
    orchestrator.stop_capture(client, "c", print_fn=lambda line: None)
    assert client.paused is True
    assert client.deleted is False


def test_stop_capture_can_delete_and_says_what_that_costs():
    client = _ScriptedClient([_status()])
    lines = []
    orchestrator.stop_capture(client, "c", delete=True, print_fn=lines.append)
    assert client.deleted is True
    assert any("fresh snapshot" in line for line in lines)
