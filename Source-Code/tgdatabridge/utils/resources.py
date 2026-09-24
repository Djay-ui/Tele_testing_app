"""Finding this package's data files -- the icons, the watermark logo and
the stylesheet -- in every layout the app actually runs in.

Why this is not just ``Path(__file__).parent``
----------------------------------------------
Three layouts have to work:

1. **From source.** ``tgdatabridge/assets/`` sits next to this file.
   ``__file__`` is a real absolute path and the naive answer is correct.

2. **A frozen build made from this source.** PyInstaller puts the data
   files at the same *relative* path inside ``_internal`` that they occupy
   in the source tree, and sets each module's ``__file__`` under
   ``sys._MEIPASS``. The naive answer is still correct.

3. **A frozen build whose ``_internal`` predates the rebrand.** This is the
   one that matters. ``_internal`` is 208 MB of Qt and database drivers; it
   is not re-sent with every build, and the copy already on the user's
   machine was produced when this package was called ``tgsct``. Its data
   files therefore live at ``_internal/tgsct/assets`` while the renamed
   code looks under ``_internal/tgdatabridge/assets``.

Case 3 fails *silently*: both call sites are written to tolerate a missing
file, so the application starts perfectly well with no window icon, no
dashboard watermark and no stylesheet at all -- which looks like a broken
build and is very hard to attribute to a rename.

So the root is resolved by looking for a directory that actually contains
``assets``, trying the current package name first and the pre-rebrand one
second. First match wins; nothing is ever guessed.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import List, Optional

#: The package directory name in the source tree, and the one a frozen
#: build made from it uses inside _internal.
_PACKAGE_DIR_NAME = "tgdatabridge"

#: What that directory was called before the rebrand. Kept because an
#: existing install's _internal still uses it -- see the module docstring.
_LEGACY_PACKAGE_DIR_NAME = "tgsct"

#: The subdirectory whose presence proves a candidate is the right root.
#: assets/ ships in every layout; picking a marker rather than trusting
#: the path means a wrong candidate is rejected instead of silently
#: producing missing-file behaviour later.
_MARKER = "assets"

_cached_root: Optional[Path] = None


def _candidates() -> List[Path]:
    """Where to look, best first.

    ``sys._MEIPASS`` is set only in a PyInstaller build; for a one-dir
    build it is the ``_internal`` directory. When it is set it goes first,
    ahead of the ``__file__``-derived answer, for two reasons: in a frozen
    build ``__file__`` is derived from _MEIPASS anyway so nothing is lost,
    and a frozen module's ``__file__`` can be a *relative* path in some
    layouts, which would otherwise resolve against the working directory
    and point somewhere arbitrary.
    """
    here = Path(__file__).resolve().parent.parent      # .../<pkg>/utils -> .../<pkg>
    found: List[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        base = Path(meipass)
        found += [base / _PACKAGE_DIR_NAME, base / _LEGACY_PACKAGE_DIR_NAME, base]
    found.append(here)
    # Same tree, old name -- a source checkout that still has the
    # pre-rebrand package directory sitting beside the new one.
    found.append(here.parent / _LEGACY_PACKAGE_DIR_NAME)
    return found


def package_root() -> Path:
    """The directory holding this package's data files.

    Falls back to the source-tree answer when no candidate has an
    ``assets`` directory -- callers all tolerate a missing file, and
    returning something plausible keeps the failure to "no icon" rather
    than an exception during startup.
    """
    global _cached_root
    if _cached_root is not None:
        return _cached_root
    candidates = _candidates()
    for candidate in candidates:
        try:
            if (candidate / _MARKER).is_dir():
                _cached_root = candidate
                return candidate
        except OSError:
            continue
    _cached_root = candidates[0]
    return _cached_root


def assets_dir() -> Path:
    """Where logo_256.png, app_icon.ico and watermark_logo.png live."""
    return package_root() / _MARKER


def stylesheet_path() -> Path:
    """gui/style.qss -- the entire application stylesheet."""
    return package_root() / "gui" / "style.qss"


def describe() -> str:
    """One line for the startup log, so a build that cannot find its own
    assets says so instead of just looking wrong."""
    root = package_root()
    ok = (root / _MARKER).is_dir()
    return (f"resources: {root}" if ok
            else f"resources: NOT FOUND (looked in {[str(c) for c in _candidates()]}) "
                 f"-- the window icon, watermark and stylesheet will be missing")


def reset_cache() -> None:
    """Tests only: forget the resolved root."""
    global _cached_root
    _cached_root = None


__all__ = ["package_root", "assets_dir", "stylesheet_path", "describe",
           "reset_cache"]
