"""FIT export, Strava upload actions, and config reload.

``ExportUploadMixin`` is mixed into ``KayakFitGUI`` (see ``gui.main_gui``). It
defines no ``__init__`` and holds no state of its own; every ``self`` attribute
it uses is created on the main window. Those shared attributes/methods are
declared in ``gui._mixin_base.GuiSharedState``, which this mixin inherits under
``TYPE_CHECKING`` only (so mypy resolves them) — there is no runtime base class.
"""

import contextlib
import webbrowser
from typing import TYPE_CHECKING, Any

from app import secret_store
from app.config import get_bool
from app.events import UiEvent

STRAVA_UPLOAD_URL = "https://www.strava.com/upload/select"

# See gui/_mixin_base.py: type-checking-only shared surface for the composed GUI.
if TYPE_CHECKING:
    from ._mixin_base import GuiSharedState as _MixinBase
else:
    _MixinBase = object


class ExportUploadMixin(_MixinBase):
    """Export/upload worker wiring, Strava helpers, and config reload."""

    def _strava_connected(self) -> bool:
        return secret_store.has_complete_credentials(self.cfg)

    def _open_strava_activity(self) -> None:
        if self.export.strava_activity_id:
            webbrowser.open(
                f"https://www.strava.com/activities/{self.export.strava_activity_id}"
            )

    def _open_strava_upload(self) -> None:
        """Open Strava's manual activity upload page in the default browser."""
        webbrowser.open(STRAVA_UPLOAD_URL)

    def _export_args(self, path: str, upload: bool) -> dict[str, Any]:
        """Build the argument dict for the export/upload worker."""
        return {
            "file_path": path,
            "access_token": self.cfg.get("strava_access_token", ""),
            "refresh_token": self.cfg.get("strava_refresh_token", ""),
            "client_id": self.cfg.get("strava_client_id", ""),
            "client_secret": self.cfg.get("strava_client_secret", ""),
            "strava_upload": "yes" if upload else "no",
        }

    def _set_export_buttons(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for btn in (self.upload_btn, self.savefit_btn, self.discard_btn):
            with contextlib.suppress(Exception):
                btn.configure(state=state)
        if enabled and not self.recording.active:
            self.workouts_btn.configure(state="normal")
        elif not enabled:
            self.workouts_btn.configure(state="disabled")

    def _start_export(
        self, path: str | None, upload: bool, announce: bool = False
    ) -> None:
        """Convert (and optionally upload) a workout file without a file picker."""
        if not path:
            return
        if self.export.running or (self.worker_mgr and self.worker_mgr.is_running()):
            self.log_message("A task is already running; please wait.\n")
            return
        if upload and not self._strava_connected():
            self.log_message(
                "Strava API is not connected; opening Strava's manual upload page.\n"
            )
            self._open_strava_upload()
            return

        prefix = "Auto-export: " if announce else ""
        self.log_message(
            prefix
            + (
                "Uploading workout to Strava...\n"
                if upload
                else "Saving workout as FIT...\n"
            )
        )
        self.strava_btn.pack_forget()
        # Only mark the export as running once the worker actually started, so
        # a refused start can never leave _export_running stuck True.
        if self.worker_mgr is not None and self.worker_mgr.start_worker(
            worker_type="export_upload", args=self._export_args(path, upload)
        ):
            self.export.running = True
            self._set_export_buttons(enabled=False)

    def _auto_export(self, attempts: int = 20) -> None:
        """After a workout, always produce a FIT (and auto-upload if configured).

        The workout worker thread can still be tearing down when this first
        fires; retry until it has exited so the export is never silently
        dropped.
        """
        if not self.recording.csv_path or self.recording.points <= 0:
            return
        if self.recording.incomplete:
            self.log_message(
                "Automatic export skipped because recording stopped after a storage "
                "failure; review the retained partial workout first.\n"
            )
            return
        if self.worker_mgr is not None and self.worker_mgr.is_running():
            if attempts > 0:
                self.root.after(500, lambda: self._auto_export(attempts - 1))
            else:
                self.log_message(
                    "Auto-export skipped (a task is still running); "
                    "use the Workouts window to export it.\n"
                )
            return
        auto_upload = get_bool(self.cfg, "auto_upload", default=False)
        self._start_export(
            self.recording.csv_path,
            upload=auto_upload and self._strava_connected(),
            announce=True,
        )

    def _reload_cfg(self) -> None:
        """Reload the config from disk, keeping in-memory UI preferences.

        The pace-distance choice is only persisted on exit, so a reload (after
        saving settings or a token refresh) must not silently revert it.
        """
        self.cfg = self.config_mgr.load_config()
        self.cfg["pace_distance"] = self._pace_distance

    def _on_tokens_updated(self, result: UiEvent) -> None:
        self._reload_cfg()
        self._init_chips()
