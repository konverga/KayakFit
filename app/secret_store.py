"""Complete Strava authorization in the operating system's credential vault.

Stores one all-or-nothing authorization bundle in the platform's vault:

    - macOS:    Keychain
    - Windows:  Credential Locker
    - Linux:    Secret Service (if available)

Backed by the required ``keyring`` package. If no usable backend is present,
``available()`` returns False and the caller keeps the secrets in the config
file instead.

All credentials are kept in a **single** vault entry (a small JSON blob) rather
than one entry per key. On macOS an unsigned app is prompted for the login
password once per keychain item accessed, so a single entry means a single
prompt at startup. The result is cached for the lifetime of the process, so
repeated config loads never re-prompt.
"""

import contextlib
import json
from collections.abc import Mapping
from typing import Any

import keyring
from keyring.backends.fail import Keyring as _FailKeyring

SERVICE = "KayakFit"

# Single vault entry holding the complete authorization as JSON (one prompt).
_BLOB_KEY = "strava_secrets"

# A refreshable connection is one atomic record. Keeping client ID or expiry
# only in config.yml would create a misleading partial connection if that file
# is deleted while Keychain survives.
CREDENTIAL_KEYS = (
    "strava_client_id",
    "strava_client_secret",
    "strava_access_token",
    "strava_refresh_token",
    "strava_expires_at",
)

# Values omitted from plaintext config.yml when the vault is available.
SENSITIVE_KEYS = (
    "strava_client_secret",
    "strava_access_token",
    "strava_refresh_token",
)

# Process-lifetime cache so we hit the vault (and prompt) at most once per run.
# None means "not loaded yet".
_cache: dict[str, Any] | None = None


def has_complete_credentials(values: Mapping[str, Any]) -> bool:
    """Return whether values contain one complete, refreshable authorization."""
    text_keys = CREDENTIAL_KEYS[:-1]
    if any(
        not isinstance(values.get(key), str) or not values[key].strip()
        for key in text_keys
    ):
        return False
    expires_at = values.get("strava_expires_at")
    return isinstance(expires_at, int) and not isinstance(expires_at, bool) and expires_at > 0


def _normalize_credentials(values: Mapping[str, Any]) -> dict[str, Any]:
    """Return a built-in JSON-safe credential bundle, or an empty dictionary."""
    if not has_complete_credentials(values):
        return {}
    return {
        "strava_client_id": str(values["strava_client_id"]).strip(),
        "strava_client_secret": str(values["strava_client_secret"]).strip(),
        "strava_access_token": str(values["strava_access_token"]).strip(),
        "strava_refresh_token": str(values["strava_refresh_token"]).strip(),
        "strava_expires_at": int(values["strava_expires_at"]),
    }


def available() -> bool:
    """Return True if a usable OS credential backend is present."""
    try:
        backend = keyring.get_keyring()
        if isinstance(backend, _FailKeyring):
            return False
        return backend is not None
    except Exception:
        return False


def store_credentials(values: Mapping[str, Any]) -> bool:
    """Write the complete Strava authorization to one OS-vault entry.

    Args:
        values: Configuration mapping. Incomplete authorization deletes the
            entry rather than persisting a partial connection.

    Returns:
        True if the vault was used, False if it is unavailable (caller should
        then fall back to file storage).
    """
    global _cache
    if not available():
        return False
    credentials = _normalize_credentials(values)
    try:
        if credentials:
            keyring.set_password(SERVICE, _BLOB_KEY, json.dumps(credentials))
        else:
            if keyring.get_password(SERVICE, _BLOB_KEY) is not None:
                keyring.delete_password(SERVICE, _BLOB_KEY)
        _cache = dict(credentials)
        return True
    except Exception:
        return False


def load_credentials() -> dict[str, Any]:
    """Read one complete authorization from the vault.

    Incomplete or malformed entries are rejected and deleted.
    """
    global _cache
    if _cache is not None:
        return dict(_cache)
    if not available():
        _cache = {}
        return {}

    try:
        raw = keyring.get_password(SERVICE, _BLOB_KEY)
    except Exception:
        raw = None

    credentials: dict[str, Any] = {}
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                credentials = _normalize_credentials(parsed)
        except ValueError:
            credentials = {}

    if raw and not credentials:
        with contextlib.suppress(Exception):
            keyring.delete_password(SERVICE, _BLOB_KEY)

    _cache = dict(credentials)
    return dict(credentials)
