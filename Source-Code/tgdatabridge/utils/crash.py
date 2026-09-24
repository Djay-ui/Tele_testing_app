"""Last-resort crash capture -- ENTERPRISE_READINESS.md, C2.

The packaged application is built with ``console=False`` (see
``packaging/tg_databridge.spec``), so it has no stdout or stderr to
write to. Nothing installed a ``sys.excepthook``, which meant an
unhandled exception on the Qt main thread wrote its traceback to a
stream that does not exist: no dialog, no log line, no file. The user
saw the window misbehave or vanish, and the support engineer received a
bug report with nothing in it.

That is the specific gap this module closes. It is deliberately separate
from ``logger.py`` and imports no Qt, so the same handlers work for the
GUI, the headless CLI, and a test process. The GUI supplies a ``notify``
callback to put a dialog in front of the user; everything else gets the
file on disk.

Three distinct failure modes are covered, because they fail in three
different ways:

* **Unhandled exception on the main thread** -- ``sys.excepthook``.
* **Unhandled exception in a worker thread** -- ``threading.excepthook``
  (Python 3.8+). Qt's own worker threads route through
  ``_Worker.run``'s try/except, but a plain ``threading.Thread``
  started anywhere else does not.
* **A hard crash with no Python exception at all** -- ``faulthandler``.
  This one matters more here than in most applications: this tool loads
  five native database drivers (oracledb's thin mode aside, psycopg's C
  extension, pyodbc, ibm_db's Db2 client, pymongo's C bson), and a
  segfault inside any of them kills the process without unwinding
  anything Python could catch. ``faulthandler`` writes the C-level and
  Python stacks to a file descriptor kept open for the process lifetime,
  which is the only trace such a crash leaves behind.

Everything here is best-effort and swallows its own errors. A crash
handler that raises replaces a diagnosable failure with an
undiagnosable one, so each step is independently guarded and the
original exception is always re-raised to the default hook at the end.
"""
from __future__ import annotations

import datetime
import faulthandler
import sys
import threading
import traceback
from pathlib import Path
from typing import Callable, Optional

# Signature: (summary_line, crash_file_path_or_None) -> None
NotifyFn = Callable[[str, Optional[Path]], None]

_installed = False
_notify: Optional[NotifyFn] = None
_base_dir: Optional[Path] = None
_prev_excepthook = None
_prev_threadhook = None
# faulthandler needs its file object to stay open for the life of the
# process -- if this were a local, it would be garbage collected and
# faulthandler would end up writing to a closed descriptor.
_fault_file = None
_last_crash_path: Optional[Path] = None
# Guards against a crash *inside* the crash handler turning into infinite
# recursion, which is the classic way this kind of code makes things worse.
_handling = False

_CRASH_DIR_NAME = "logs"


def crash_log_path(base_dir: Optional[Path] = None) -> Path:
    """Where today's crash file lives -- alongside the ordinary JSONL
    logs, so a support bundle picks both up from one directory."""
    from tgdatabridge.utils import app_storage
    day = datetime.datetime.now().strftime("%Y-%m-%d")
    # local_app_data_dir, matching logger.log_dir -- a crash report belongs
    # on the machine that crashed, not on a team share.
    return app_storage.local_app_data_dir(base_dir) / _CRASH_DIR_NAME / f"crash-{day}.log"


def last_crash_path() -> Optional[Path]:
    """The file the most recent crash was written to, if any -- so a GUI
    dialog can offer to open it."""
    return _last_crash_path


def format_exception(exc_type, exc_value, exc_tb) -> str:
    try:
        return "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    except Exception:  # noqa: BLE001 -- formatting must not itself fail
        return f"{exc_type}: {exc_value}\n(traceback unavailable)"


def _write_crash_file(text: str) -> Optional[Path]:
    """Append a crash report and return where it went, or None if the
    write failed. Deliberately independent of logger.py's own disk path:
    if logging is what broke, this still has to land."""
    global _last_crash_path
    try:
        path = crash_log_path(_base_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now().isoformat(timespec="seconds")
        with path.open("a", encoding="utf-8") as f:
            f.write(f"\n{'=' * 72}\n{stamp}\n{'=' * 72}\n{text}\n")
        _last_crash_path = path
        return path
    except Exception:  # noqa: BLE001
        return None


def handle_exception(exc_type, exc_value, exc_tb, thread_name: Optional[str] = None) -> None:
    """The shared body of both hooks. Public so tests can drive it
    directly rather than having to provoke a real unhandled exception."""
    global _handling

    # KeyboardInterrupt is a user pressing Ctrl-C, not a crash. Reporting
    # it as one would be noise, and swallowing it would break the normal
    # interrupt behaviour of the CLI.
    if issubclass(exc_type, KeyboardInterrupt):
        if _prev_excepthook is not None:
            _prev_excepthook(exc_type, exc_value, exc_tb)
        return

    if _handling:
        # Already reporting a crash; a second one must not recurse.
        return
    _handling = True
    try:
        where = f" in thread {thread_name}" if thread_name else ""
        summary = f"Unhandled {exc_type.__name__}{where}: {exc_value}"
        detail = format_exception(exc_type, exc_value, exc_tb)

        path = _write_crash_file(detail)

        # Route through the ordinary log bus too, so the crash appears in
        # the GUI's own console panel and the day's JSONL file, and ships
        # to a central sink if one is configured.
        try:
            from tgdatabridge.utils import logger
            logger.error(summary)
            for line in detail.rstrip().splitlines():
                logger.error("  " + line)
        except Exception:  # noqa: BLE001
            pass

        if _notify is not None:
            try:
                _notify(summary, path)
            except Exception:  # noqa: BLE001
                pass

        # Always give the default hook its turn -- when a console *does*
        # exist (the CLI, a dev run), the traceback should still appear
        # there exactly as it always has.
        try:
            if _prev_excepthook is not None:
                _prev_excepthook(exc_type, exc_value, exc_tb)
        except Exception:  # noqa: BLE001
            pass
    finally:
        _handling = False


def _excepthook(exc_type, exc_value, exc_tb) -> None:
    handle_exception(exc_type, exc_value, exc_tb)


def _threadhook(args) -> None:
    # threading.excepthook receives a single namedtuple-ish args object.
    handle_exception(
        args.exc_type, args.exc_value, args.exc_traceback,
        thread_name=getattr(args.thread, "name", None),
    )


def install(base_dir: Optional[Path] = None, notify: Optional[NotifyFn] = None) -> bool:
    """Install all three handlers. Idempotent -- calling it twice is a
    no-op rather than chaining a handler onto itself, which would double
    every report. Returns True if faulthandler could be enabled.

    `notify` is how the GUI surfaces a crash to the user; leave it None
    for a headless process, where the file and the log line are enough.
    """
    global _installed, _notify, _base_dir, _prev_excepthook, _prev_threadhook, _fault_file

    _notify = notify
    _base_dir = base_dir

    if _installed:
        return _fault_file is not None

    _prev_excepthook = sys.excepthook
    sys.excepthook = _excepthook

    if hasattr(threading, "excepthook"):
        _prev_threadhook = threading.excepthook
        threading.excepthook = _threadhook

    fault_ok = False
    try:
        path = crash_log_path(_base_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        _fault_file = path.open("a", encoding="utf-8")
        faulthandler.enable(file=_fault_file, all_threads=True)
        fault_ok = True
    except Exception:  # noqa: BLE001
        # An unwritable AppData directory must not stop the application
        # starting -- the Python-level hooks above still work.
        _fault_file = None

    _installed = True
    return fault_ok


def uninstall() -> None:
    """Restore the previous hooks. Only needed by tests -- the
    application installs these once and keeps them for its lifetime."""
    global _installed, _notify, _fault_file, _prev_excepthook, _prev_threadhook, _handling
    global _last_crash_path
    if _prev_excepthook is not None:
        sys.excepthook = _prev_excepthook
    if _prev_threadhook is not None and hasattr(threading, "excepthook"):
        threading.excepthook = _prev_threadhook
    try:
        faulthandler.disable()
    except Exception:  # noqa: BLE001
        pass
    if _fault_file is not None:
        try:
            _fault_file.close()
        except Exception:  # noqa: BLE001
            pass
    _fault_file = None
    _prev_excepthook = None
    _prev_threadhook = None
    _notify = None
    _installed = False
    _handling = False
    # Reset too, so a fresh install() never reports a stale path from a
    # previous one -- caught by test_keyboard_interrupt_is_not_treated_as_
    # a_crash, which saw the *previous* test's crash file leak through.
    _last_crash_path = None


def record_handled_exception(detail: str) -> Optional[Path]:
    """Write a traceback that was *caught* -- so it never reaches the
    hooks above -- to the same crash file.

    The GUI's background worker catches every exception so it can show
    the user a message instead of dying, which is correct behaviour and
    should stay. The cost was that an unexpected error inside a worker
    (an AttributeError in a converter, a driver raising something
    undocumented) left the user with a one-line message and left support
    with nothing at all. Recording it here puts the stack in the same
    place a real crash would, without changing what the user sees.
    """
    return _write_crash_file(detail)
