"""Tests for GUI export/upload action routing."""

from unittest.mock import patch

from gui.controllers import ExportController
from gui.export_upload import STRAVA_UPLOAD_URL, ExportUploadMixin


class _DisconnectedGui(ExportUploadMixin):
    def __init__(self) -> None:
        self.export = ExportController()
        self.worker_mgr = None
        self.messages: list[str] = []

    def _strava_connected(self) -> bool:
        return False

    def log_message(self, message: str) -> None:
        self.messages.append(message)


def test_disconnected_upload_opens_strava_manual_upload_page() -> None:
    gui = _DisconnectedGui()

    with patch("gui.export_upload.webbrowser.open") as open_browser:
        gui._start_export("workout.fit", upload=True)

    open_browser.assert_called_once_with(STRAVA_UPLOAD_URL)
    assert gui.messages == [
        "Strava API is not connected; opening Strava's manual upload page.\n"
    ]
