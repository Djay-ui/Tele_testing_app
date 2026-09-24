"""A thin Kafka Connect REST client -- SCALE.md section 2.1.

Only the endpoints this tool's cutover sequence actually needs: register
or update a connector, read its status and its tasks' states, pause and
resume it, and delete it. Built on `urllib.request` rather than
`requests`, matching this project's existing preference for not adding a
runtime dependency for one convenience (see `tgdatabridge/cli/config.py`,
which makes the same call for the same reason).

Every method goes through `_request`, which is injectable via the
`transport` constructor argument -- so the whole client is testable
against a fake, including the error paths, without a Kafka cluster
anywhere near the test suite. That matters more than usual here: this is
the one module in the CDC package whose correctness depends on a remote
API's exact shapes, and it cannot be validated end to end in this
environment. Treat it as needing a smoke test against a real Connect
cluster before a production cutover.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

DEFAULT_TIMEOUT_SECONDS = 15.0

# Kafka Connect's own connector/task state vocabulary.
STATE_RUNNING = "RUNNING"
STATE_PAUSED = "PAUSED"
STATE_FAILED = "FAILED"
STATE_UNASSIGNED = "UNASSIGNED"
STATE_RESTARTING = "RESTARTING"


class ConnectError(Exception):
    """Any problem talking to Kafka Connect -- unreachable, non-2xx, or a
    response that isn't the JSON shape the API documents. The CLI catches
    this and prints one line rather than a traceback, since an
    unreachable Connect cluster is an everyday operational condition, not
    a bug."""


@dataclass
class ConnectorStatus:
    name: str
    connector_state: str
    task_states: List[str]
    trace: str = ""

    @property
    def running(self) -> bool:
        """Running means the connector *and* every task. A connector in
        RUNNING whose single task has FAILED is the common Debezium
        failure shape, and reporting that as healthy would be actively
        misleading -- it's exactly the state where capture has silently
        stopped."""
        return (
            self.connector_state == STATE_RUNNING
            and bool(self.task_states)
            and all(state == STATE_RUNNING for state in self.task_states)
        )

    @property
    def failed(self) -> bool:
        return self.connector_state == STATE_FAILED or STATE_FAILED in self.task_states


def _default_transport(method: str, url: str, body: Optional[bytes], timeout: float):
    request = urllib.request.Request(url, data=body, method=method)
    request.add_header("Content-Type", "application/json")
    request.add_header("Accept", "application/json")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, response.read()


class KafkaConnectClient:
    def __init__(self, base_url: str, timeout: float = DEFAULT_TIMEOUT_SECONDS, transport=None):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._transport = transport if transport is not None else _default_transport

    # ------------------------------------------------------------ plumbing

    def _request(self, method: str, path: str, payload: Optional[Dict[str, Any]] = None):
        url = f"{self.base_url}{path}"
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        try:
            status, raw = self._transport(method, url, body, self.timeout)
        except urllib.error.HTTPError as exc:  # noqa: PERF203 - distinct handling per error type
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001 - the status code is the useful part either way
                pass
            raise ConnectError(f"{method} {url} failed with HTTP {exc.code}: {detail[:400]}") from exc
        except urllib.error.URLError as exc:
            raise ConnectError(f"Could not reach Kafka Connect at {url}: {exc.reason}") from exc
        except OSError as exc:
            raise ConnectError(f"Could not reach Kafka Connect at {url}: {exc}") from exc

        if status is not None and not (200 <= int(status) < 300):
            text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
            raise ConnectError(f"{method} {url} failed with HTTP {status}: {text[:400]}")

        if not raw:
            return None
        text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ConnectError(f"{method} {url} returned a non-JSON body: {text[:200]}") from exc

    # -------------------------------------------------------------- calls

    def list_connectors(self) -> List[str]:
        result = self._request("GET", "/connectors")
        return list(result) if isinstance(result, list) else []

    def connector_exists(self, name: str) -> bool:
        return name in self.list_connectors()

    def create_or_update(self, name: str, config: Dict[str, Any]) -> Dict[str, Any]:
        """PUT /connectors/{name}/config -- idempotent by design, unlike
        POST /connectors which 409s if the connector already exists.
        Re-running a pipeline that registers the same connector is
        routine, so the idempotent verb is the right default."""
        return self._request("PUT", f"/connectors/{name}/config", config) or {}

    def delete(self, name: str) -> None:
        self._request("DELETE", f"/connectors/{name}")

    def status(self, name: str) -> ConnectorStatus:
        raw = self._request("GET", f"/connectors/{name}/status") or {}
        connector = raw.get("connector") or {}
        tasks = raw.get("tasks") or []
        traces = [t.get("trace", "") for t in tasks if t.get("trace")]
        if connector.get("trace"):
            traces.insert(0, connector["trace"])
        return ConnectorStatus(
            name=raw.get("name", name),
            connector_state=connector.get("state", STATE_UNASSIGNED),
            task_states=[t.get("state", STATE_UNASSIGNED) for t in tasks],
            trace="\n".join(traces),
        )

    def pause(self, name: str) -> None:
        self._request("PUT", f"/connectors/{name}/pause")

    def resume(self, name: str) -> None:
        self._request("PUT", f"/connectors/{name}/resume")

    def restart_failed_tasks(self, name: str) -> List[int]:
        """Restart every task currently in FAILED. Returns the task ids
        restarted, so a caller can report "restarted 1 task" rather than
        guessing whether anything happened."""
        raw = self._request("GET", f"/connectors/{name}/status") or {}
        restarted = []
        for task in raw.get("tasks") or []:
            if task.get("state") == STATE_FAILED and "id" in task:
                task_id = task["id"]
                self._request("POST", f"/connectors/{name}/tasks/{task_id}/restart")
                restarted.append(task_id)
        return restarted
