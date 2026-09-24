"""Operation-level metrics -- duration, throughput, error rate -- for the
GUI's long-running actions (Load Schema, Convert Schema, Apply DDL, Dry
Run, Migrate Data, Refresh Target Schema). ENTERPRISE_READINESS.md
section 3, item 4: "Metrics (migration duration, rows/sec throughput,
error rate per engine pair) exported somewhere scrapable (Prometheus
textfile, etc.)".

Two files under tgdatabridge.utils.app_storage.app_data_dir()/metrics/:

    operations.jsonl     One JSON line per recorded operation (source of
                          truth), capped to the _MAX_RECORDS most recent
                          so this can never grow without bound.
    tgdatabridge_metrics.prom    Aggregated Prometheus text-exposition-format
                          snapshot, rewritten in full every time a new
                          operation is recorded -- point node_exporter's
                          textfile collector (or any Prometheus-format
                          scraper) at this file's directory.

Every public function takes an optional base_dir=None test seam, matching
app_storage.py's convention. Deliberately simple: this tool runs on one
analyst's desktop, not a fleet, so re-reading and re-aggregating a
capped, few-thousand-line JSON-lines file on every write is nowhere near
enough volume to be worth a real time-series-database approach.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_METRICS_DIR_NAME = "metrics"
_OPERATIONS_FILE_NAME = "operations.jsonl"
_PROM_FILE_NAME = "tgdatabridge_metrics.prom"
_MAX_RECORDS = 5000


def _metrics_dir(base_dir: Optional[Path] = None) -> Path:
    from tgdatabridge.utils import app_storage
    return app_storage.app_data_dir(base_dir) / _METRICS_DIR_NAME


def _operations_path(base_dir: Optional[Path] = None) -> Path:
    return _metrics_dir(base_dir) / _OPERATIONS_FILE_NAME


def prometheus_textfile_path(base_dir: Optional[Path] = None) -> Path:
    return _metrics_dir(base_dir) / _PROM_FILE_NAME


def load_operations(base_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Every recorded operation, oldest first (i.e. file/append order) --
    tolerant of a missing file (nothing recorded yet) or a corrupted
    trailing line (e.g. the app was killed mid-write), which is skipped
    rather than failing the whole load."""
    path = _operations_path(base_dir)
    records: List[Dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    return records


def record_operation(
    operation: str,
    duration_seconds: float,
    success: bool,
    actor: Optional[str] = None,
    base_dir: Optional[Path] = None,
    **extra: Any,
) -> None:
    """Appends one operation record and refreshes the Prometheus textfile.
    Both steps are best-effort (any OSError is swallowed): a
    metrics-persistence problem should never surface as, or be mistaken
    for, a failure of the operation itself.

    `**extra` carries operation-specific dimensions -- e.g. Migrate Data
    passes source_engine/target_engine/rows/failed_tables so the exported
    Prometheus metrics can be broken down by engine pair."""
    record: Dict[str, Any] = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "operation": operation,
        "duration_seconds": round(duration_seconds, 3),
        "success": bool(success),
        "actor": actor or "unknown",
    }
    record.update(extra)

    try:
        records = load_operations(base_dir)
        records.append(record)
        if len(records) > _MAX_RECORDS:
            records = records[-_MAX_RECORDS:]
        path = _operations_path(base_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    except OSError:
        return

    try:
        export_prometheus_textfile(base_dir)
    except OSError:
        pass


def _fmt_label(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def export_prometheus_textfile(base_dir: Optional[Path] = None) -> Path:
    """Aggregates every recorded operation into a Prometheus text
    exposition format file (see
    https://prometheus.io/docs/instrumenting/exposition_formats/).
    Rewritten in full every call -- see module docstring on why that's
    fine at this tool's scale. Returns the path written."""
    records = load_operations(base_dir)

    op_last_duration: Dict[str, float] = {}
    op_counts: Dict[Tuple[str, str], int] = {}  # (operation, outcome) -> count
    total_rows_migrated = 0
    pair_last_duration: Dict[Tuple[str, str], float] = {}
    pair_last_rows: Dict[Tuple[str, str], int] = {}
    pair_success: Dict[Tuple[str, str], int] = {}
    pair_failure: Dict[Tuple[str, str], int] = {}

    for r in records:
        op = r.get("operation", "unknown")
        outcome = "success" if r.get("success") else "failure"
        op_counts[(op, outcome)] = op_counts.get((op, outcome), 0) + 1

        duration = r.get("duration_seconds")
        if isinstance(duration, (int, float)):
            op_last_duration[op] = duration  # records are in run order, so this ends up as "most recent"

        if op == "Migrate Data":
            rows = r.get("rows")
            if isinstance(rows, (int, float)):
                total_rows_migrated += int(rows)

            src, tgt = r.get("source_engine"), r.get("target_engine")
            if src and tgt:
                pair = (src, tgt)
                # A migration that completed (didn't raise) but left one or
                # more tables failed still counts as an error for this
                # engine pair -- "succeeded" at the outer operation level
                # just means it didn't crash, not that every table landed.
                had_failure = bool(r.get("failed_tables")) or not r.get("success", True)
                if had_failure:
                    pair_failure[pair] = pair_failure.get(pair, 0) + 1
                else:
                    pair_success[pair] = pair_success.get(pair, 0) + 1
                if isinstance(duration, (int, float)):
                    pair_last_duration[pair] = duration
                if isinstance(rows, (int, float)):
                    pair_last_rows[pair] = int(rows)

    lines: List[str] = []

    lines.append("# HELP tgdatabridge_operation_duration_seconds Duration in seconds of the most recently run instance of each operation.")
    lines.append("# TYPE tgdatabridge_operation_duration_seconds gauge")
    for op, dur in sorted(op_last_duration.items()):
        lines.append(f'tgdatabridge_operation_duration_seconds{{operation="{_fmt_label(op)}"}} {dur}')

    lines.append("# HELP tgdatabridge_operation_runs_total Total number of times each operation has been run, by outcome.")
    lines.append("# TYPE tgdatabridge_operation_runs_total counter")
    for (op, outcome), count in sorted(op_counts.items()):
        lines.append(f'tgdatabridge_operation_runs_total{{operation="{_fmt_label(op)}",outcome="{outcome}"}} {count}')

    lines.append("# HELP tgdatabridge_migration_rows_total Total rows migrated across all Migrate Data runs.")
    lines.append("# TYPE tgdatabridge_migration_rows_total counter")
    lines.append(f"tgdatabridge_migration_rows_total {total_rows_migrated}")

    lines.append("# HELP tgdatabridge_migration_rows_per_second Rows/sec throughput of the most recent Migrate Data run, by source/target engine pair.")
    lines.append("# TYPE tgdatabridge_migration_rows_per_second gauge")
    for pair, rows in sorted(pair_last_rows.items()):
        dur = pair_last_duration.get(pair)
        rate = round(rows / dur, 2) if dur and dur > 0 else 0
        src, tgt = pair
        lines.append(
            f'tgdatabridge_migration_rows_per_second{{source_engine="{_fmt_label(src)}",'
            f'target_engine="{_fmt_label(tgt)}"}} {rate}'
        )

    lines.append("# HELP tgdatabridge_migration_error_rate Fraction of Migrate Data runs with at least one failed table (or an outright crash), by source/target engine pair.")
    lines.append("# TYPE tgdatabridge_migration_error_rate gauge")
    for pair in sorted(set(pair_success) | set(pair_failure)):
        s, f = pair_success.get(pair, 0), pair_failure.get(pair, 0)
        rate = round(f / (s + f), 4) if (s + f) > 0 else 0
        src, tgt = pair
        lines.append(
            f'tgdatabridge_migration_error_rate{{source_engine="{_fmt_label(src)}",'
            f'target_engine="{_fmt_label(tgt)}"}} {rate}'
        )

    path = prometheus_textfile_path(base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
