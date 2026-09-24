"""A faint, click-through Teleglobal watermark (logo + "Teleglobal
International Pvt Ltd" wordmark) for the center of the main window's
dashboard.

Two pieces:
  - `build_dashboard_watermark_pixmap` composes the source logo with a
    "Teleglobal International Pvt Ltd" wordmark underneath on one canvas,
    then fades that composite down to a low-but-legible opacity. Unlike the
    tool's original text-only wordmark treatment, the logo itself is kept in
    its own original colors rather than being flattened to a single tint --
    see the function's own comment for why only the wordmark text (not the
    logo) still gets a solid navy recolor pass. The logo is sized to a
    physical ~10cm square based on the screen's actual DPI.
  - `DashboardWatermark` is the QWidget that paints that pixmap centered
    (offset a further ~5cm to the right) over whatever it's stretched
    across, and never intercepts clicks -- it's decoration, not a real UI
    element.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QWidget

# Used only for the wordmark text (the logo below keeps its own original
# colors -- see build_dashboard_watermark_pixmap's docstring).
_TEXT_TINT = QColor(0, 0, 128)
# Full-color logo washes out more easily than the old flat navy silhouette
# did at the same opacity, so this sits noticeably higher than the previous
# 0.1125 text-only-watermark value -- "low but more visible" was the ask
# when the logo switched from a navy silhouette to the real full-color mark.
_OPACITY = 0.22
_WORDMARK_TEXT = "Teleglobal International Pvt Ltd"
_TARGET_LOGO_SIZE_CM = 10.0
_HORIZONTAL_OFFSET_CM = 5.0  # shifts the whole watermark to the right of center
_FALLBACK_DPI = 96.0


def _cm_to_px(cm: float) -> int:
    screen = QApplication.primaryScreen()
    dpi = screen.logicalDotsPerInch() if screen else _FALLBACK_DPI
    if not dpi or dpi <= 0:
        dpi = _FALLBACK_DPI
    inches = cm / 2.54
    return max(1, round(inches * dpi))


def _wordmark_font(logo_width_px: int, max_width_px: int) -> QFont:
    """A bold font sized so the wordmark's width roughly matches the logo's
    width above it (scaled down if the text would run wider than that, left
    alone otherwise -- a long string like "Teleglobal International Pvt
    Ltd" at a font size purely proportional to the logo can otherwise end
    up far wider than the mark itself)."""
    font = QFont()
    font.setBold(True)
    font.setPointSizeF(max(8.0, logo_width_px * 0.09))
    metrics = QFontMetrics(font)
    text_width = metrics.horizontalAdvance(_WORDMARK_TEXT)
    if text_width > max_width_px:
        scale = max_width_px / text_width
        font.setPointSizeF(max(8.0, font.pointSizeF() * scale))
    return font


def build_dashboard_watermark_pixmap(assets_dir: Path) -> QPixmap:
    """Load the Teleglobal logo, stack the "Teleglobal International Pvt
    Ltd" wordmark underneath it, and fade the whole composite down to a low
    opacity. Unlike the tool's original watermark (which flattened the logo
    itself to a solid navy silhouette, the same tint as the text), the logo
    here keeps its own original colors -- only the wordmark *text* gets a
    solid navy fill, drawn separately and composited on top of the
    full-color logo before the shared fade pass. Returns a null QPixmap if
    the source asset is missing, so callers can skip drawing it entirely
    rather than crash."""
    source_path = assets_dir / "watermark_logo.png"
    source = QPixmap(str(source_path))
    if source.isNull():
        return QPixmap()

    logo_size_px = _cm_to_px(_TARGET_LOGO_SIZE_CM)
    logo = source.scaled(
        logo_size_px, logo_size_px,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )

    padding = max(4, round(logo.width() * 0.04))
    font = _wordmark_font(logo.width(), round(logo.width() * 1.15))
    metrics = QFontMetrics(font)
    text_width = metrics.horizontalAdvance(_WORDMARK_TEXT)
    text_height = metrics.height()

    canvas_width = max(logo.width(), text_width)
    canvas_height = logo.height() + padding + text_height

    # Composite the full-color logo and the (still solid-navy, un-faded)
    # wordmark text onto one canvas, then fade that single composite as one
    # unit in the next step. Fading them together (rather than as two
    # separately-faded layers stacked on top of each other) is what
    # guarantees the logo and the text end up at exactly the same
    # visibility -- two separate low-opacity layers don't sum to the same
    # opacity as one composite faded once.
    composite = QPixmap(canvas_width, canvas_height)
    composite.fill(Qt.GlobalColor.transparent)
    painter = QPainter(composite)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    painter.drawPixmap((canvas_width - logo.width()) // 2, 0, logo)
    painter.setFont(font)
    painter.setPen(_TEXT_TINT)
    text_rect = QRect(0, logo.height() + padding, canvas_width, text_height)
    painter.drawText(text_rect, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter, _WORDMARK_TEXT)
    painter.end()

    # Fade the whole composite (full-color logo + navy text, in their
    # original colors) down to a low, uniform opacity -- QPainter.setOpacity
    # scales alpha correctly without needing a manual per-pixel pass.
    faded = QPixmap(composite.size())
    faded.fill(Qt.GlobalColor.transparent)
    painter = QPainter(faded)
    painter.setOpacity(_OPACITY)
    painter.drawPixmap(0, 0, composite)
    painter.end()

    return faded


class DashboardWatermark(QWidget):
    """Paints a pixmap centered (offset ~5cm to the right) and never
    intercepts mouse input -- meant to be stretched to cover the whole
    central dashboard area and raised above every other pane, purely as
    decoration."""

    def __init__(self, pixmap: QPixmap, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._pixmap = pixmap
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override signature
        if self._pixmap.isNull():
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        x = (self.width() - self._pixmap.width()) // 2 + _cm_to_px(_HORIZONTAL_OFFSET_CM)
        y = (self.height() - self._pixmap.height()) // 2
        painter.drawPixmap(x, y, self._pixmap)
        painter.end()
