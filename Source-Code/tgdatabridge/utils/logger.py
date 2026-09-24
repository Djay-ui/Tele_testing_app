"""Structured, actor-stamped log bus for the GUI's console panel, with
best-effort disk persistence (JSON lines) and support for a pluggable
subscriber -- ENTERPRISE_READINESS.md
section 3, "Observability & audit", items 1 and 2.

Backward-compatible with the original in-memory pub/sub: subscribe(cb)
still receives (level, formatted_line) exactly as before, so the GUI's
LogConsole (tgdatabridge/gui/log_console.py) needs no changes at all. Internally,
every record is now kept structured -- timestamp, level, actor, message --
so it can be written to disk as JSON without ever re-parsing the
formatted display string back apart.

Disk logs land under app_storage.app_data_dir()/logs/tgdatabridge-YYYY-MM-DD.jsonl
(one file per calendar day, never rotated/deleted automatically -- these
are audit/diagnostic records, not something this tool should silently
throw away). A disk-write failure (full disk, locked-down AppData, etc.)
is swallowed, never raised -- logging is a diagnostic convenience and must
never be able to crash or block whatever operation triggered the log
line.
"""
from __future__ import annotations

import datetime
import getpass
import json
import os
from pathlib import Path
from typing import Callable, Dict, List, Optional

_listeners: List[Callable[[str, str], None]] = []
_history: List[Dict[str, str]] = []

# Test/advanced-use seam, mirroring app_storage.py's base_dir=None
# convention -- real application startup never needs to call configure().
_base_dir: Optional[Path] = None
_disk_enabled: bool = True

_LOG_DIR_NAME = "logs"


def configure(base_dir: Optional[Path] = None, disk_enabled: bool = True) -> None:
    """Redirect where log files are written, or disable disk writes
    entirely. Meant for tests (point this at a throwaway temp directory)
    and any future headless/CLI mode (section 5, item 1) that might want a
    different log location -- not something the GUI needs to call."""
    global _base_dir, _disk_enabled
    _base_dir = base_dir
    _disk_enabled = disk_enabled


def reset_for_tests() -> None:
    """Clears in-memory history/listeners and restores default disk
    settings. Only ever meant to be called between tests, so one test's
    subscribers/history can't leak into the next."""
    global _base_dir, _disk_enabled
    _listeners.clear()
    _history.clear()
    _base_dir = None
    _disk_enabled = True


def current_actor() -> str:
    """Best-effort OS/AD username for "who ran this" attribution
    (roadmap section 3, item 2: "Capture who ran what"). Never raises --
    falls back to "unknown" rather than letting a stripped-down service
    account or unusual OS context turn a log call into a crash."""
    for fn in (getpass.getuser, os.getlogin):
        try:
            actor = fn()
        except Exception:
            continue
        if actor:
            return actor
    return "unknown"


def subscribe(callback: Callable[[str, str], None]) -> None:
    _listeners.append(callback)


def unsubscribe(callback: Callable[[str, str], None]) -> None:
    """A no-op if callback was never subscribed (or was already removed) --
    callers don't need to track subscription state themselves. Used by the
    GUI's Settings dialog to swap out the log-shipping subscriber live when
    the user changes the shipping endpoint, without needing an app restart."""
    try:
        _listeners.remove(callback)
    except ValueError:
        pass


def log_dir(base_dir: Optional[Path] = None) -> Path:
    """Where this machine's logs live.

    Deliberately `local_app_data_dir`, not `app_data_dir`: a configured
    shared-storage folder redirects profiles, history, checkpoints and
    metrics, but not this. A diagnostic record belongs on the machine
    that produced it -- pointing several analysts' logs at one shared
    file is how you end up unable to tell whose run failed, and how a
    briefly-unreachable network share turns into missing evidence for
    the run you most need to explain.
    """
    from tgdatabridge.utils import app_storage
    return app_storage.local_app_data_dir(base_dir if base_dir is not None else _base_dir) / _LOG_DIR_NAME


def _log_file_path() -> Path:
    day = datetime.datetime.now().strftime("%Y-%m-%d")
    return log_dir() / f"tgdatabridge-{day}.jsonl"


def _persist(record: Dict[str, str]) -> None:
    if not _disk_enabled:
        return
    try:
        path = _log_file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        # Disk logging is a diagnostic convenience, never something that
        # should crash or block the operation that triggered the log line.
        pass


def log(level: str, message: str) -> None:
    now = datetime.datetime.now()
    record = {
        "timestamp": now.isoformat(timespec="seconds"),
        "level": level,
        "actor": current_actor(),
        "message": message,
    }
    _history.append(record)
    _persist(record)

    line = f"[{now.strftime('%H:%M:%S')}] {message}"
    for cb in _listeners:
        try:
            cb(level, line)
        except Exception:  # noqa: BLE001
            # A subscriber is a display concern. If one raises -- a widget
            # torn down during shutdown, say -- it must not propagate into
            # the operation that happened to emit the message, which would
            # turn a cosmetic problem into a failed migration. It must also
            # not stop the remaining subscribers from seeing the line.
            pass


def info(message: str) -> None:
    log("info", message)


def warning(message: str) -> None:
    log("warning", message)


def error(message: str) -> None:
    log("error", message)


def history() -> List[Dict[str, str]]:
    """Structured, in-process-session-only history (most recent last) --
    each entry is a {timestamp, level, actor, message} dict. For a
    persisted, cross-restart record, read the JSON-lines files under
    app_storage.app_data_dir()/logs/ instead."""
    return list(_history)
