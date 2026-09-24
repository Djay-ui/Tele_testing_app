"""Local, on-disk persistence for the AI-assisted features' configuration
-- follows tgdatabridge.utils.settings.py's shape exactly (a small
dataclass, JSON on disk under app_storage.local_app_data_dir(), tolerant of
a missing/corrupted file, a `base_dir` test-injection seam) with one
addition: the API key is never written into ai_settings.json alongside
everything else.

Why the API key gets its own file
----------------------------------
Every other secret this tool ever handles -- a database password, an SSH
key passphrase, a TLS client key password -- is simply never persisted at
all; the user re-enters it each time (see ConnectionProfile's and
TlsConfig's own docstrings in app_storage.py / tls_config.py). An AI
provider's API key can't follow that same rule and still be usable: it is
configured once for the whole installation and used repeatedly across
unrelated operations (an error dialog days later, a data quality review
after some future migration), not re-entered per connection the way a
database password is.

So it is persisted, but not in plain text next to `provider`, `model` and
the rest: it's encrypted at rest with a key generated on first use and
stored in its own file (`ai_secret.key`) alongside it. This is a real
improvement over plain text -- glancing at ai_settings.json, or a support
engineer being sent that one file, does not expose it -- but it is not
equivalent to an OS credential store (Windows Credential Manager, macOS
Keychain): anyone with read access to this tool's whole app-data directory
can decrypt it, because the key to do so lives right there next to it.
That is the honest limit of a same-machine, no-new-dependency secret store;
if `cryptography` (already a dependency of asyncssh -- see
requirements.txt) is ever unavailable for some reason, this module does
not fall back to writing the key in plain text: it simply doesn't persist
it, and the user re-enters it next time AI Settings is opened.
"""
from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from tgdatabridge.ai.ai_config import AiConfig

_SETTINGS_FILE_NAME = "ai_settings.json"
_SECRET_KEY_FILE_NAME = "ai_secret.key"
_API_KEY_FILE_NAME = "ai_api_key.enc"


@dataclass
class AiSettings:
    """Everything about the AI configuration except the API key itself --
    see this module's docstring for why that's split out. Mirrors
    tgdatabridge.ai.ai_config.AiConfig field-for-field (minus api_key);
    to_ai_config() below combines the two back into one AiConfig for
    tgdatabridge/ai/ai_client.py to use."""
    enabled: bool = False
    provider: str = "claude"
    base_url: str = ""
    model: str = ""
    azure_api_version: str = "2024-06-01"
    timeout_seconds: float = 60.0
    feature_schema_mapping: bool = True
    feature_nl_config: bool = True
    feature_error_diagnostics: bool = True
    feature_data_quality: bool = True


def _settings_path(base_dir: Optional[Path] = None) -> Path:
    from tgdatabridge.utils import app_storage
    return app_storage.local_app_data_dir(base_dir) / _SETTINGS_FILE_NAME


def load_ai_settings(base_dir: Optional[Path] = None) -> AiSettings:
    """Defaults (AI off) if nothing has ever been saved, or if the file on
    disk is missing/corrupted/from an incompatible future version -- never
    raises, matching every other loader in app_storage.py/settings.py."""
    path = _settings_path(base_dir)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return AiSettings()
    if not isinstance(raw, dict):
        return AiSettings()

    known_fields = {f.name for f in dataclasses.fields(AiSettings)}
    filtered = {k: v for k, v in raw.items() if k in known_fields}
    try:
        return AiSettings(**filtered)
    except TypeError:
        return AiSettings()


def save_ai_settings(settings: AiSettings, base_dir: Optional[Path] = None) -> None:
    path = _settings_path(base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dataclasses.asdict(settings), indent=2), encoding="utf-8")


# ------------------------------------------------------------- the API key


def _get_or_create_fernet(base_dir: Optional[Path]):
    """A cryptography.fernet.Fernet built from a key generated on first
    use and reused thereafter, or None if the `cryptography` package
    isn't available (see the module docstring: this is the signal to not
    persist the API key at all, not a reason to fall back to plain
    text)."""
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        return None

    from tgdatabridge.utils import app_storage
    key_path = app_storage.local_app_data_dir(base_dir) / _SECRET_KEY_FILE_NAME
    try:
        if key_path.exists():
            key_bytes = key_path.read_bytes()
        else:
            key_bytes = Fernet.generate_key()
            key_path.write_bytes(key_bytes)
            try:
                # Best-effort -- POSIX only; Windows ACLs aren't touched
                # by chmod, but this file is already under the user's own
                # %APPDATA%, which Windows already restricts to that user
                # by default.
                key_path.chmod(0o600)
            except OSError:
                pass
        return Fernet(key_bytes)
    except (OSError, ValueError):
        return None


def _api_key_path(base_dir: Optional[Path]) -> Path:
    from tgdatabridge.utils import app_storage
    return app_storage.local_app_data_dir(base_dir) / _API_KEY_FILE_NAME


def load_api_key(base_dir: Optional[Path] = None) -> str:
    """The stored API key, decrypted -- "" if none was ever saved, if the
    secret-key file is missing/unreadable, or if decryption fails for any
    reason (a key file from a different machine, corruption, `cryptography`
    unavailable). Never raises: a missing/unusable API key should read
    exactly like "not configured yet", the same tolerance every other
    loader in this tool gives a missing or corrupted file."""
    path = _api_key_path(base_dir)
    if not path.exists():
        return ""
    fernet = _get_or_create_fernet(base_dir)
    if fernet is None:
        return ""
    try:
        token = path.read_bytes()
        return fernet.decrypt(token).decode("utf-8")
    except Exception:  # noqa: BLE001 -- InvalidToken, OSError, UnicodeDecodeError, etc.
        return ""


def save_api_key(api_key: str, base_dir: Optional[Path] = None) -> None:
    """Encrypt and persist `api_key`; an empty string clears whatever was
    stored (deletes the file, rather than encrypting an empty value) --
    the same "blank means unset" convention AppSettings.shared_storage_path
    uses. Silently does nothing if `cryptography` is unavailable, per the
    module docstring's "don't fall back to plain text" rule -- the caller
    (AI Settings dialog) is expected to warn the user in that case rather
    than assume the key was saved."""
    path = _api_key_path(base_dir)
    if not api_key:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return
    fernet = _get_or_create_fernet(base_dir)
    if fernet is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(fernet.encrypt(api_key.encode("utf-8")))
    except Exception:  # noqa: BLE001 -- encryption/IO failing must not crash the caller
        # (e.g. a version mismatch between the `cryptography` build this
        # was compiled against and the one actually bundled at runtime --
        # see this module's own docstring). The key simply isn't saved;
        # the user re-enters it next time, same as `cryptography` being
        # unavailable entirely.
        pass


def api_key_is_stored(base_dir: Optional[Path] = None) -> bool:
    """Whether *something* is stored -- used by the AI Settings dialog to
    show "an API key is already saved" without ever displaying or
    re-decrypting it just to check."""
    return _api_key_path(base_dir).exists()


def to_ai_config(settings: AiSettings, base_dir: Optional[Path] = None) -> AiConfig:
    """Combine the persisted, non-secret `settings` with the separately
    stored API key into one AiConfig ready for AiClient. This is the one
    place the two are recombined -- every other reader/writer in this
    module touches only one or the other."""
    return AiConfig(
        enabled=settings.enabled,
        provider=settings.provider,
        api_key=load_api_key(base_dir),
        base_url=settings.base_url,
        model=settings.model,
        azure_api_version=settings.azure_api_version,
        timeout_seconds=settings.timeout_seconds,
        feature_schema_mapping=settings.feature_schema_mapping,
        feature_nl_config=settings.feature_nl_config,
        feature_error_diagnostics=settings.feature_error_diagnostics,
        feature_data_quality=settings.feature_data_quality,
    )


__all__ = [
    "AiSettings", "load_ai_settings", "save_ai_settings",
    "load_api_key", "save_api_key", "api_key_is_stored", "to_ai_config",
]
