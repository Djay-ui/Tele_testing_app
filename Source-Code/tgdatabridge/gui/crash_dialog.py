"""The user-facing half of crash reporting -- ENTERPRISE_READINESS.md, C2.

``tgdatabridge.utils.crash`` deliberately imports no Qt so the same handlers can
serve the GUI, the headless CLI and a test process. This module is the
Qt-only piece: it turns a captured crash into something the person
sitting in front of the application can act on, and hands them the file a
support engineer will ask for.

Kept small and separate on purpose. Anything that runs while the process
is already in an unknown state should do as little as possible, and a
crash reporter that itself needs a working application is no reporter at
all -- so every step here is guarded, and failing to show the dialog
still leaves the crash file and the log line behind.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from tgdatabridge import version


def _open_in_file_manager(path: Path) -> None:
    """Reveal `path`'s directory in the OS file manager. Qt's
    QDesktopServices handles the per-platform differences (explorer.exe,
    Finder, xdg-open) without this module having to know about them."""
    try:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
    except Exception:  # noqa: BLE001
        pass


def show_crash_dialog(summary: str, crash_path: Optional[Path]) -> None:
    """Notify callback for ``crash.install(notify=...)``.

    Deliberately reports rather than reassures: the process may be in an
    inconsistent state after an unhandled exception, so this does not
    claim the application has recovered or invite the user to carry on as
    though nothing happened. It tells them what to send to support, and
    offers to open the folder containing it.
    """
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox
        if QApplication.instance() is None:
            # No event loop (crash during startup, or a headless import) --
            # the file and the log line are the whole report in that case.
            return

        box = QMessageBox()
        box.setIcon(QMessageBox.Critical)
        box.setWindowTitle(f"{version.PRODUCT_TM} — unexpected error")
        box.setText("The application hit an unexpected error.")
        if crash_path is not None:
            box.setInformativeText(
                "Details have been written to a log file. Please send that file "
                "to support along with a note of what you were doing.\n\n"
                "Any migration already in progress may not have finished. Check "
                "the target database before assuming it completed.\n\n"
                f"{crash_path}"
            )
            open_button = box.addButton("Open log folder", QMessageBox.ActionRole)
        else:
            # The crash file could not be written -- say so plainly rather
            # than pointing the user at a path that does not exist.
            box.setInformativeText(
                "Details could not be written to a log file (the application "
                "data folder may not be writable). The error was:\n\n"
                f"{summary}"
            )
            open_button = None
        explain_button = _add_explain_button(box)
        box.addButton("Close", QMessageBox.RejectRole)
        box.setDetailedText(summary)
        box.exec()

        clicked = box.clickedButton()
        if open_button is not None and clicked is open_button:
            _open_in_file_manager(crash_path.parent)
        elif explain_button is not None and clicked is explain_button:
            _show_ai_explanation(summary)
    except Exception:  # noqa: BLE001
        # Never let the reporter become the thing that crashes.
        pass


def _add_explain_button(box) -> Optional[object]:
    """The optional "Explain with AI" button -- present only when AI
    features and the error-diagnosis feature specifically are turned on
    (tgdatabridge.utils.ai_settings), so a plain crash dialog is exactly
    what it always was for anyone who hasn't opted in. Loading
    ai_settings/QMessageBox here (rather than importing at module level)
    keeps this module's only hard Qt dependency where it already was --
    inside this one function -- and means a problem reading AI settings
    can never be the reason a crash dialog itself fails to show."""
    try:
        from PySide6.QtWidgets import QMessageBox
        from tgdatabridge.utils import ai_settings
        settings = ai_settings.load_ai_settings()
        if not (settings.enabled and settings.feature_error_diagnostics):
            return None
        return box.addButton("Explain with AI", QMessageBox.ActionRole)
    except Exception:  # noqa: BLE001
        return None


def _build_ai_explanation_text(summary: str) -> str:
    """The plain-text body for the "AI Explanation" follow-up dialog --
    split out from _show_ai_explanation (which only adds the Qt box
    around this) so it can be tested without ever constructing or
    exec()ing a real QMessageBox. Every failure mode (AI off,
    unreachable, malformed reply) comes back as a plain sentence rather
    than raising -- this runs after an unhandled exception already
    interrupted the user once; raising a second one from the reporter
    itself would be exactly the wrong way to compound that."""
    from tgdatabridge.ai.ai_client import AiClient, AiError
    from tgdatabridge.ai.error_diagnostics import explain_error
    from tgdatabridge.utils import ai_settings

    config = ai_settings.to_ai_config(ai_settings.load_ai_settings())
    try:
        diagnosis = explain_error(AiClient(config), summary, context=f"{version.PRODUCT_TM} crash")
    except AiError as exc:
        return f"Could not get an AI explanation: {exc}"

    lines = [diagnosis.explanation or "(no explanation returned)"]
    if diagnosis.suggested_fixes:
        lines.append("")
        lines.append("Suggested fixes:")
        lines.extend(f"  {i + 1}. {fix}" for i, fix in enumerate(diagnosis.suggested_fixes))
    return "\n".join(lines)


def _show_ai_explanation(summary: str) -> None:
    """Best-effort: build the explanation text and show it in a follow-up
    dialog. Wrapped in a bare except for the same reason every other
    function in this module is -- see the module docstring."""
    try:
        from PySide6.QtWidgets import QMessageBox
        text = _build_ai_explanation_text(summary)

        box = QMessageBox()
        box.setIcon(QMessageBox.Information)
        box.setWindowTitle("AI Explanation")
        box.setText(text)
        box.exec()
    except Exception:  # noqa: BLE001
        pass
