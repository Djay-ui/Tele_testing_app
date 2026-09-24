"""
Small always-visible status widgets: a colored-dot activity indicator for
the status bar, and connection "badges" that show what source/target the
tool is currently pointed at (including when nothing is connected yet, so
that state is never invisible/ambiguous to the user).
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtWidgets import QHBoxLayout, QLabel, QWidget

_GRAY = "#9aa5b1"    # idle / not connected
_GREEN = "#2ea44f"   # running / connected / success
_RED = "#c62828"     # failed


class StatusIndicator(QWidget):
    """A colored dot + short text, meant to live in the status bar so the
    current running/succeeded/failed state is always visible at a glance."""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 0, 10, 0)
        layout.setSpacing(6)

        self._dot = QLabel("●")  # ●
        self._text = QLabel("Idle")
        self._text.setStyleSheet("font-weight: 600;")
        layout.addWidget(self._dot)
        layout.addWidget(self._text)

        self.set_idle()

    def _set(self, color: str, text: str) -> None:
        self._dot.setStyleSheet(f"color: {color}; font-size: 15px;")
        self._text.setText(text)
        self._text.setStyleSheet(f"font-weight: 600; color: {color};")

    def set_idle(self) -> None:
        self._set(_GRAY, "Idle")

    def set_running(self, message: str) -> None:
        self._set(_GREEN, f"Running: {message}")

    def set_success(self, message: str) -> None:
        self._set(_GREEN, f"Done: {message}")

    def set_failed(self, message: str) -> None:
        self._set(_RED, f"Failed: {message}")


class ConnectionBadge(QWidget):
    """Shows one connection's state (source or target): a colored dot plus
    engine/host/database/schema once connected, or a clear "Not connected"
    otherwise — so this is never a blank, invisible gap in the toolbar."""

    def __init__(self, label: str, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 3, 8, 3)
        layout.setSpacing(6)

        self.setStyleSheet(
            "ConnectionBadge { background: #ffffff; border: 1px solid #c3ddf1; border-radius: 5px; }"
        )

        self._dot = QLabel("●")
        self._label = QLabel(f"{label}:")
        self._label.setStyleSheet("font-weight: 600; color: #1c3a52;")
        self._detail = QLabel("not connected")
        self._detail.setStyleSheet("color: #5a7080;")

        layout.addWidget(self._dot)
        layout.addWidget(self._label)
        layout.addWidget(self._detail)

        self.set_disconnected()

    def set_disconnected(self) -> None:
        self._dot.setStyleSheet(f"color: {_GRAY}; font-size: 13px;")
        self._detail.setText("not connected")
        self._detail.setStyleSheet("color: #5a7080;")

    def set_connected(self, engine: str, host: str, database: str, schema: Optional[str]) -> None:
        self._dot.setStyleSheet(f"color: {_GREEN}; font-size: 13px;")
        schema_part = f"  ·  schema: {schema}" if schema else ""
        self._detail.setText(f"{engine} — {host}/{database}{schema_part}")
        self._detail.setStyleSheet("color: #1c3a52;")
