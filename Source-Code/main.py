"""
TG DataBridge — Connecting Legacy Data to the Future.

Entry point. Run with:  python main.py

The product name is not written here; it comes from tgdatabridge.version,
which is the one file that spells it.
"""
import sys

from PySide6.QtWidgets import QApplication

from tgdatabridge import version
from tgdatabridge.gui.crash_dialog import show_crash_dialog
from tgdatabridge.gui.main_window import MainWindow
from tgdatabridge.utils import crash, resources


def _load_stylesheet() -> str:
    # Resolved through utils.resources, not by path arithmetic off
    # __file__: a frozen build whose _internal predates the rebrand keeps
    # its data files under the old package directory name, and a missing
    # stylesheet fails silently (the app starts, looking unstyled).
    try:
        return resources.stylesheet_path().read_text(encoding="utf-8")
    except OSError:
        return ""


def main() -> int:
    # Installed before anything else can fail. The packaged build sets
    # console=False (packaging/tg_databridge.spec), so without these
    # handlers an unhandled exception writes its traceback to a stream
    # that does not exist -- no dialog, no log line, nothing for a
    # support engineer to work from. See tgdatabridge/utils/crash.py.
    crash.install(notify=show_crash_dialog)

    app = QApplication(sys.argv)
    # Qt uses the application name for taskbar grouping and as the default
    # QSettings path, so it gets the plain product name -- not the title bar
    # string, which also carries the tagline and the build number.
    app.setApplicationName(version.PRODUCT)
    app.setApplicationDisplayName(version.PRODUCT_TM)
    app.setOrganizationName(version.VENDOR)
    app.setApplicationVersion(version.BUILD)
    app.setStyleSheet(_load_stylesheet())
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
