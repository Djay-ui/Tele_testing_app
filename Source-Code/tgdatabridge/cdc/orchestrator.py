"""The bulk-load / change-capture handoff -- SCALE.md section 2.1.

This module exists for one reason: the ordering below is load-bearing,
and getting it wrong loses data *silently*.

    1. Preflight the Oracle side.
    2. Register the Debezium connector (snapshot.mode=no_data) and wait
       for it to actually reach RUNNING. It records its start SCN here
       and begins buffering every subsequent change into Kafka.
    3. Only now run the bulk load (this tool's parallel, sharded, COPY
       path).
    4. Let the sink drain the buffered changes. Watch lag fall.
    5. Cut over when lag is small: stop writes to the source, wait for
       the last few seconds of delta, validate, switch.

The failure mode if you load first and register second is that changes
committed between the load's read-consistent point and the connector's
start SCN are captured by *neither*. No error is raised. Row counts
match, because the rows exist on both sides -- they're just stale on the
target. That's why this sequence lives in code with a guard rather than
in a runbook: `start_capture_then_load` refuses to run the load if the
connector isn't confirmed RUNNING first.

Measuring lag
-------------
Debezium's richest lag metric (`MilliSecondsBehindSource`) is JMX-only,
and assuming a JMX bridge exists would make this unusable on plenty of
clusters. Instead this reads the connector's committed source offset via
`GET /connectors/{name}/offsets` (Kafka Connect 3.6+) to get the SCN
Debezium has processed to, then asks Oracle to turn that SCN and the
current SCN into timestamps. The difference is real, wall-clock lag.

Both halves can legitimately fail -- an older Connect has no offsets
endpoint, and `SCN_TO_TIMESTAMP` raises ORA-08181 for an SCN older than
the undo retention window. Either way this reports lag as *unknown*
rather than guessing, and `wait_for_drain` treats unknown as "not
drained" rather than quietly proceeding: at cutover, "I can't tell how
far behind we are" must never read the same as "we're caught up".
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, List, Optional

from tgdatabridge.cdc import preflight as cdc_preflight
from tgdatabridge.cdc.config import DebeziumConnectorConfig, ordering_notes, render_config
from tgdatabridge.cdc.connect_client import ConnectError, ConnectorStatus, KafkaConnectClient

DEFAULT_READY_TIMEOUT_SECONDS = 120.0
DEFAULT_DRAIN_TIMEOUT_SECONDS = 3600.0
DEFAULT_POLL_SECONDS = 5.0
# Under a minute of lag is a sensible default "safe to cut over" bar: it's
# the point where the remaining delta applies faster than the human steps
# of a cutover take anyway.
DEFAULT_DRAIN_TARGET_SECONDS = 60.0


class CdcOrchestrationError(Exception):
    """A step in the cutover sequence could not be completed safely."""


@dataclass
class LagReading:
    seconds: Optional[float]
    detail: str

    @property
    def known(self) -> bool:
        return self.seconds is not None


def _scalar(source, sql: str):
    try:
        rows = list(source.execute(sql))
    except Exception:  # noqa: BLE001 - lag is diagnostic; an unreadable source reports unknown
        return None
    if not rows or not rows[0]:
        return None
    return rows[0][0]


def connector_committed_scn(client: KafkaConnectClient, connector_name: str) -> Optional[int]:
    """The SCN Debezium has committed through, from Kafka Connect's
    offsets endpoint. None when the endpoint isn't available (Connect
    older than 3.6) or the offset isn't in the documented shape."""
    try:
        raw = client._request("GET", f"/connectors/{connector_name}/offsets")  # noqa: SLF001 - deliberate: this endpoint is only used here
    except ConnectError:
        return None
    if not isinstance(raw, dict):
        return None
    for entry in raw.get("offsets") or []:
        offset = entry.get("offset") if isinstance(entry, dict) else None
        if not isinstance(offset, dict):
            continue
        # Debezium's Oracle offset uses commit_scn when it has one and
        # scn otherwise; commit_scn is the more conservative of the two
        # (it's what has definitely been committed downstream).
        for key in ("commit_scn", "scn"):
            value = offset.get(key)
            if value is None:
                continue
            try:
                # commit_scn can be a compound "scn:txid:..." string.
                return int(str(value).split(":", 1)[0])
            except (TypeError, ValueError):
                continue
    return None


def measure_lag(source, client: KafkaConnectClient, connector_name: str) -> LagReading:
    """Wall-clock seconds the connector is behind the source."""
    scn = connector_committed_scn(client, connector_name)
    if scn is None:
        return LagReading(
            None,
            "could not read the connector's committed SCN -- Kafka Connect's /offsets endpoint "
            "needs Connect 3.6 or newer. Fall back to Debezium's JMX MilliSecondsBehindSource.",
        )

    lag = _scalar(
        source,
        "SELECT (CAST(SYSTIMESTAMP AS DATE) - CAST(SCN_TO_TIMESTAMP({}) AS DATE)) * 86400 "
        "FROM DUAL".format(scn),
    )
    if lag is None:
        return LagReading(
            None,
            f"SCN {scn} could not be converted to a timestamp (ORA-08181 if it predates the undo "
            "retention window). The connector may be far enough behind that Oracle can no longer "
            "date the SCN -- treat that as significant lag, not as caught up.",
        )
    try:
        seconds = float(lag)
    except (TypeError, ValueError):
        return LagReading(None, f"unrecognized lag value {lag!r} for SCN {scn}")
    return LagReading(max(0.0, seconds), f"committed through SCN {scn}")


def wait_until_running(
    client: KafkaConnectClient,
    connector_name: str,
    timeout: float = DEFAULT_READY_TIMEOUT_SECONDS,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    on_poll: Optional[Callable[[ConnectorStatus], None]] = None,
    sleep_fn=time.sleep,
    time_fn=time.monotonic,
) -> ConnectorStatus:
    """Block until the connector and all of its tasks are RUNNING.

    A connector in RUNNING whose task has FAILED is the common Debezium
    failure shape and is *not* accepted here -- see ConnectorStatus.running.
    """
    deadline = time_fn() + timeout
    last: Optional[ConnectorStatus] = None
    while True:
        status = client.status(connector_name)
        last = status
        if on_poll:
            on_poll(status)
        if status.running:
            return status
        if status.failed:
            raise CdcOrchestrationError(
                f"Connector '{connector_name}' failed to start: {status.trace[:800] or 'no trace reported'}")
        if time_fn() >= deadline:
            raise CdcOrchestrationError(
                f"Connector '{connector_name}' did not reach RUNNING within {timeout:.0f}s "
                f"(connector={last.connector_state}, tasks={last.task_states or 'none assigned'})."
            )
        sleep_fn(poll_seconds)


def start_capture(
    client: KafkaConnectClient,
    config: DebeziumConnectorConfig,
    source=None,
    run_preflight: bool = True,
    require_preflight_pass: bool = True,
    print_fn: Callable[[str], None] = print,
    ready_timeout: float = DEFAULT_READY_TIMEOUT_SECONDS,
    sleep_fn=time.sleep,
    time_fn=time.monotonic,
) -> ConnectorStatus:
    """Preflight, register the connector, and wait for it to be RUNNING.

    Must complete **before** the bulk load starts -- see this module's
    docstring for what happens if it doesn't.
    """
    if run_preflight:
        if source is None:
            raise CdcOrchestrationError(
                "run_preflight=True needs a connected source connector to query. Pass source=..., "
                "or set run_preflight=False if the Oracle side has already been verified.")
        report = cdc_preflight.run_preflight(
            source, config.schema_name, config.table_names, config.database_user)
        print_fn(report.render())
        if not report.ready and require_preflight_pass:
            raise CdcOrchestrationError(
                f"CDC preflight found {len(report.failures)} blocking problem(s) on the Oracle "
                "source. Fix them (the remedial SQL is above) and re-run. Starting capture anyway "
                "would produce change events with missing before-images, which corrupt the target "
                "without raising any error."
            )

    for note in ordering_notes(config):
        print_fn(f"NOTE: {note}")

    client.create_or_update(config.connector_name, render_config(config))
    print_fn(f"Registered Debezium connector '{config.connector_name}'; waiting for it to start...")

    status = wait_until_running(
        client, config.connector_name, timeout=ready_timeout,
        sleep_fn=sleep_fn, time_fn=time_fn,
    )
    print_fn(
        f"Connector '{config.connector_name}' is RUNNING. It is now buffering changes -- "
        "start the bulk load."
    )
    return status


def wait_for_drain(
    source,
    client: KafkaConnectClient,
    connector_name: str,
    target_seconds: float = DEFAULT_DRAIN_TARGET_SECONDS,
    timeout: float = DEFAULT_DRAIN_TIMEOUT_SECONDS,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    print_fn: Callable[[str], None] = print,
    sleep_fn=time.sleep,
    time_fn=time.monotonic,
) -> LagReading:
    """Poll until the connector is within `target_seconds` of the source.

    Unknown lag never counts as drained. At cutover, "I can't tell how far
    behind we are" and "we're caught up" must not be the same outcome.
    """
    deadline = time_fn() + timeout
    readings: List[LagReading] = []
    while True:
        reading = measure_lag(source, client, connector_name)
        readings.append(reading)
        if reading.known:
            print_fn(f"CDC lag: {reading.seconds:.1f}s ({reading.detail})")
            if reading.seconds <= target_seconds:
                return reading
        else:
            print_fn(f"CDC lag: unknown -- {reading.detail}")

        status = client.status(connector_name)
        if status.failed:
            raise CdcOrchestrationError(
                f"Connector '{connector_name}' failed while draining: "
                f"{status.trace[:800] or 'no trace reported'}")

        if time_fn() >= deadline:
            last = readings[-1]
            current = f"{last.seconds:.1f}s" if last.known else "unknown"
            raise CdcOrchestrationError(
                f"CDC lag did not fall to {target_seconds:.0f}s within {timeout:.0f}s "
                f"(last reading: {current}). Do not cut over on this: the target is still "
                "behind the source by an unverified amount."
            )
        sleep_fn(poll_seconds)


def stop_capture(client: KafkaConnectClient, connector_name: str, delete: bool = False,
                 print_fn: Callable[[str], None] = print) -> None:
    """Pause (default) or delete the connector once cutover is complete.

    Pause is the default deliberately: it leaves the offsets in place, so
    if the cutover is rolled back the connector resumes from where it
    stopped instead of needing a fresh snapshot of a 1 TB source.
    """
    if delete:
        client.delete(connector_name)
        print_fn(
            f"Deleted connector '{connector_name}'. Its offsets are gone -- restarting capture "
            "would require a fresh snapshot.")
        return
    client.pause(connector_name)
    print_fn(
        f"Paused connector '{connector_name}'. Offsets are retained, so it can resume from this "
        "point if the cutover is rolled back.")
