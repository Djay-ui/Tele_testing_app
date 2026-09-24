"""Small, local persisted application settings: the optional log-shipping
endpoint (ENTERPRISE_READINESS.md section 3, item 3) and the optional
shared-storage path for a team deployment (section 5, item 4).

settings.json always lives under tgdatabridge.utils.app_storage.local_app_data_dir()
-- the real per-machine directory -- rather than app_data_dir(), which is
what everything *else* in app_storage.py uses. This is deliberate, not an
oversight: shared_storage_path is itself a setting stored in this file, so
this file can't be one of the things that setting redirects, or a machine
would need to already know the shared location before it could read the
file that tells it the shared location. Every user who wants to join a
shared deployment points their own local settings.json at it via the
Settings… dialog -- the same "small local pointer to a shared location"
model file-sync clients use.

Follows app_storage.py's base_dir=None test-injection seam and
tolerant-of-corruption loading convention (a missing or corrupted
settings.json is treated as "use the defaults", never a crash).
"""
from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

_SETTINGS_FILE_NAME = "settings.json"

VALID_LOG_LEVELS = ("info", "warning", "error")


@dataclass
class AppSettings:
    # Central log shipping (an HTTP endpoint to POST every log line to)
    # used to live here. It was removed: logs are written to the local
    # machine only. Old settings.json files may still carry the three
    # log_shipping_* keys -- load_settings drops unknown keys, so they are
    # ignored rather than causing an error, and disappear on the next save.
    #
    # ENTERPRISE_READINESS.md section 5, item 4: "Multi-user/shared
    # deployment". Empty (the default) means "use the ordinary per-machine
    # %APPDATA%\TGDataBridge\ / ~/.tg_databridge/ directory" exactly as
    # before. A non-empty path (typically a mapped network drive or UNC
    # path) redirects connection profiles, conversion history, migration
    # checkpoints, logs, and metrics there instead -- see
    # app_storage.app_data_dir()'s own docstring for how that redirect
    # works. Deliberately simple: last-write-wins, no file locking or
    # conflict resolution -- matches this tool's existing one-analyst-at-
    # a-time usage pattern, just with the state shared across whichever
    # machine that analyst is using it from.
    shared_storage_path: str = ""


def _settings_path(base_dir: Optional[Path] = None) -> Path:
    from tgdatabridge.utils import app_storage
    # Always the real per-machine location -- see this module's own
    # docstring on why settings.json can't itself live at the location one
    # of its own settings (shared_storage_path) might redirect everything
    # else to.
    return app_storage.local_app_data_dir(base_dir) / _SETTINGS_FILE_NAME


def load_settings(base_dir: Optional[Path] = None) -> AppSettings:
    """Defaults (log shipping off) if nothing has ever been saved, or if
    the file on disk is missing/corrupted/from an incompatible future
    version -- never raises."""
    path = _settings_path(base_dir)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return AppSettings()
    if not isinstance(raw, dict):
        return AppSettings()

    known_fields = {f.name for f in dataclasses.fields(AppSettings)}
    filtered = {k: v for k, v in raw.items() if k in known_fields}
    try:
        settings = AppSettings(**filtered)
    except TypeError:
        return AppSettings()

    return settings


def save_settings(settings: AppSettings, base_dir: Optional[Path] = None) -> None:
    path = _settings_path(base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dataclasses.asdict(settings), indent=2), encoding="utf-8")
