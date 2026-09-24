# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for TG DataBridge -- Connecting Legacy Data to the Future.

Produces a one-directory build matching the layout the shipped package
already uses: "TG DataBridge.exe" beside an
"_internal" folder holding Python, Qt and the database drivers.

Build it from the Source Code folder:

    pyinstaller packaging/tg_databridge.spec --noconfirm

Two things here are load-bearing and should not be "simplified":

* `console=False`. The app is a GUI; a console window would flash up on
  every launch. This is also exactly why tgdatabridge/utils/crash.py installs
  sys.excepthook, threading.excepthook and faulthandler -- with no stdout
  or stderr, an unhandled exception would otherwise leave no trace at all.

* The `datas` entries put tgdatabridge/assets and tgdatabridge/gui/style.qss at the same
  *relative* paths inside _internal that they occupy in the source tree.
  Both are loaded by path arithmetic off `__file__`
  (main_window._ASSETS_DIR, main._load_stylesheet), so moving them breaks
  the icon, the watermark and the entire stylesheet -- silently, since
  both loads are written to tolerate a missing file.
"""
import os

from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

# PyInstaller resolves every relative path in a spec against the *spec
# file's* directory, not the directory you ran it from -- so a bare
# "main.py" here is looked for in packaging/ and the build dies with
# "script 'packaging/main.py' not found". SPECPATH is injected by
# PyInstaller; everything below is anchored to the project root through it.
PROJECT_ROOT = os.path.abspath(os.path.join(SPECPATH, os.pardir))


def project(*parts):
    return os.path.join(PROJECT_ROOT, *parts)

# Every database driver is imported lazily, inside the connector's own
# connect() -- deliberately, so a missing driver only affects the engine
# that needs it. PyInstaller's static analysis cannot see through that, so
# each one has to be named here or the frozen build fails at "Connect"
# with ModuleNotFoundError instead of at import time.
hidden = [
    "mysql.connector",
    "mysql.connector.plugins.mysql_native_password",
    "mysql.connector.plugins.caching_sha2_password",
    "psycopg",
    "psycopg_binary",
    "oracledb",
    "pyodbc",
    "ibm_db",
    "pymongo",
    "bson",
    "gridfs",
    "openpyxl",
    # SSH tunnel to a database with no direct route (tgdatabridge/db/ssh_tunnel.py).
    # Imported lazily inside the tunnel backend for the same reason as the
    # drivers above, so it needs naming here too. The three cryptography
    # modules are ones asyncssh reaches that nothing else in this app does,
    # which is enough for PyInstaller's analysis to miss them.
    "asyncssh",
    "cryptography.hazmat.primitives.ciphers.aead",
    "cryptography.hazmat.primitives.kdf.pbkdf2",
    "cryptography.hazmat.primitives.poly1305",
]
hidden += collect_submodules("psycopg")
hidden += collect_submodules("tgdatabridge")
hidden += collect_submodules("asyncssh")

a = Analysis(
    [project("main.py")],
    pathex=[PROJECT_ROOT],
    binaries=[],
    datas=[
        (project("tgdatabridge", "assets"), "tgdatabridge/assets"),
        (project("tgdatabridge", "gui", "style.qss"), "tgdatabridge/gui"),
    ],
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Qt ships far more than this app uses; excluding the heavy optional
    # modules keeps the build to a sane size without touching anything
    # the GUI actually imports (Widgets, Gui, Core).
    excludes=[
        "PySide6.QtWebEngineCore",
        "PySide6.QtWebEngineWidgets",
        "PySide6.Qt3DCore",
        "PySide6.QtMultimedia",
        "PySide6.QtQuick",
        "PySide6.QtQml",
        "tkinter",
        "matplotlib",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="TG DataBridge",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # see the module docstring -- crash.py depends on this
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=project("tgdatabridge", "assets", "app_icon.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="App",
)
