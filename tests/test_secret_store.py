"""Tests for atomic Strava credential-vault persistence."""

import json
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

from app import secret_store
from gui.config_manager import ConfigManager


class _FakeKeyring:
    def __init__(self) -> None:
        self.value: str | None = None

    def get_password(self, _service: str, _key: str) -> str | None:
        return self.value

    def set_password(self, _service: str, _key: str, value: str) -> None:
        self.value = value

    def delete_password(self, _service: str, _key: str) -> None:
        self.value = None


def _install_fake_keyring(monkeypatch) -> _FakeKeyring:
    fake = _FakeKeyring()
    monkeypatch.setattr(secret_store, "keyring", fake)
    monkeypatch.setattr(secret_store, "available", lambda: True)
    monkeypatch.setattr(secret_store, "_cache", None)
    return fake


def _credentials() -> dict[str, object]:
    return {
        "strava_client_id": "12345",
        "strava_client_secret": "client-secret",
        "strava_access_token": "access-token",
        "strava_refresh_token": "refresh-token",
        "strava_expires_at": 2_000_000_000,
    }


def test_complete_credentials_round_trip_through_vault(monkeypatch) -> None:
    fake = _install_fake_keyring(monkeypatch)

    assert secret_store.store_credentials(_credentials()) is True
    assert fake.value is not None

    monkeypatch.setattr(secret_store, "_cache", None)
    loaded = secret_store.load_credentials()

    assert loaded == _credentials()
    assert secret_store.has_complete_credentials(loaded)


def test_incomplete_vault_entry_is_deleted_not_migrated(monkeypatch) -> None:
    fake = _install_fake_keyring(monkeypatch)
    fake.value = json.dumps({
        "strava_client_secret": "client-secret",
        "strava_access_token": "access-token",
        "strava_refresh_token": "refresh-token",
    })

    assert secret_store.load_credentials() == {}
    assert fake.value is None


def test_complete_connection_survives_config_folder_deletion(monkeypatch) -> None:
    _install_fake_keyring(monkeypatch)

    with tempfile.TemporaryDirectory() as tmp, patch.object(Path, "home", return_value=Path(tmp)):
        manager = ConfigManager()
        cfg = dict(manager.DEFAULTS)
        cfg.update(_credentials())
        manager.save_config(cfg)

        shutil.rmtree(manager.config_folder)
        monkeypatch.setattr(secret_store, "_cache", None)

        restored = ConfigManager().load_config()

    assert secret_store.has_complete_credentials(restored)
    assert restored["strava_client_id"] == "12345"
    assert restored["strava_expires_at"] == 2_000_000_000
