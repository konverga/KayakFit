"""Strava activity uploading.

Uploads kayaking activities to Strava using direct REST calls (via
``app.strava_api``): CSV→FIT conversion before upload, the multipart upload,
the asynchronous processing poll (cancellable early through an optional stop
event), and proactive token refresh via the stored ``expires_at``.
"""

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import strava_api
from .logger import Logger
from .workout_paths import FIT_FILENAME, WORKOUT_DIRECTORY_PREFIX

# Refresh the access token this many seconds before it actually expires.
TOKEN_REFRESH_MARGIN = 120
# Max seconds to wait for Strava to finish processing an upload.
UPLOAD_WAIT_TIMEOUT = 90
# How often to poll the upload status while waiting for processing.
UPLOAD_POLL_INTERVAL = 2.0


class StravaUploader:
    """Uploads activities to Strava via the ``app.strava_api`` REST layer."""

    def __init__(
        self,
        access_token: str,
        refresh_token: str,
        client_id: str,
        client_secret: str,
        config: dict[str, Any],
        log_callback: Callable[[str], None] | None = None,
        expires_at: int = 0,
        stop_event: Any | None = None,
    ) -> None:
        """Initialize Strava uploader.

        Args:
            access_token: Strava access token.
            refresh_token: Strava refresh token.
            client_id: Strava API client ID.
            client_secret: Strava API client secret.
            config: App configuration.
            log_callback: Optional callback function for logging.
            expires_at: Unix time (s) when the access token expires (0 if unknown).
            stop_event: Optional ``threading.Event``-like object (only ``.is_set()``
                is used). When set, the upload-processing poll stops early instead
                of waiting out the full timeout; ``None`` disables the check.
        """
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.client_id = client_id
        self.client_secret = client_secret
        self.expires_at = int(expires_at or 0)
        self.config = config
        self.stop_event = stop_event

        Logger.setup(
            log_level=self.config.get("log_level", "INFO"), log_callback=log_callback
        )
        self.logger = Logger.get_logger(name=__name__)

    def _ensure_fresh_token(self) -> bool:
        """Refresh the access token if it is missing or about to expire.

        Returns:
            True if a usable access token is available, False otherwise.
        """
        token_valid = (
            self.access_token
            and self.expires_at
            and time.time() < self.expires_at - TOKEN_REFRESH_MARGIN
        )
        if token_valid:
            return True

        if not self.refresh_token:
            return bool(self.access_token)

        try:
            self.logger.info(msg="Refreshing Strava access token...")
            info = strava_api.refresh_access_token(
                client_id=self.client_id,
                client_secret=self.client_secret,
                refresh_token=self.refresh_token,
            )
            self.access_token = info["access_token"]
            self.refresh_token = info["refresh_token"]
            self.expires_at = int(info["expires_at"])
            return True
        except Exception as e:
            self.logger.error(msg=f"Token refresh failed: {e}")
            return bool(
                self.access_token
                and (not self.expires_at or time.time() < self.expires_at)
            )

    def upload_file(
        self,
        file_path: str,
        name: str | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        """Upload a CSV or FIT file to Strava.

        CSV files are converted to FIT first.

        Args:
            file_path: Path to the CSV or FIT file.
            name: Optional activity name.
            description: Optional activity description.

        Returns:
            Dictionary with 'success' and 'message' (and 'activity_id' on success).
        """
        from .export_fit import FitExporter

        file_path_obj = Path(file_path)
        if not file_path_obj.exists():
            return {"success": False, "message": f"File not found: {file_path_obj}"}

        if file_path_obj.suffix.lower() == ".csv":
            self.logger.info(msg=f"Converting CSV to FIT: {file_path_obj.name}")
            convert_result = FitExporter.convert_csv_to_fit(str(file_path_obj))
            if not convert_result["success"]:
                return {
                    "success": False,
                    "message": convert_result.get("message", "Conversion failed"),
                }
            file_path_obj = Path(convert_result["file_path"])

        if not self._ensure_fresh_token():
            return {
                "success": False,
                "message": "Strava token expired and refresh failed. Please reconnect.",
            }

        try:
            external_id = (
                file_path_obj.parent.name
                if (
                    file_path_obj.name == FIT_FILENAME
                    and file_path_obj.parent.name.startswith(WORKOUT_DIRECTORY_PREFIX)
                )
                else file_path_obj.stem
            )
            with open(file_path_obj, "rb") as f:
                upload = strava_api.upload_activity(
                    access_token=self.access_token,
                    file_obj=f,
                    data_type="fit",
                    name=name,
                    description=description,
                    # Stable id lets Strava reject accidental re-uploads as duplicates.
                    external_id=external_id,
                )

            upload_id = upload.get("id") or upload.get("id_str")
            if not upload_id:
                raise strava_api.StravaApiError(
                    "Strava upload response did not include an upload id."
                )

            # Poll until Strava finishes processing (raises ActivityUploadError
            # on a reported error, UploadTimeoutError if the poll window elapses).
            activity_id = self._wait_for_upload(upload_id)

            try:
                # Read the created activity back to confirm its id (mirrors the
                # old Client.get_activity read-back).
                activity = strava_api.get_activity(self.access_token, activity_id)
                activity_id = activity.get("id", activity_id)
            except strava_api.ActivityUploadError:
                raise  # genuine rejection - handled below
            except Exception as e:
                # Polling already assigned an activity id, but the final
                # read-back failed - e.g. a token without activity:read scope,
                # or Strava eventual consistency. The activity exists, so treat
                # this as a successful upload.
                if not activity_id:
                    raise
                self.logger.warning(
                    msg=f"Activity {activity_id} created; read-back failed: {e}"
                )

            return {
                "success": True,
                "activity_id": activity_id,
                "message": f"Upload successful! Activity ID: {activity_id}",
            }

        except strava_api.ActivityUploadError as e:
            return {"success": False, "message": f"Strava rejected the upload: {e}"}
        except strava_api.UploadTimeoutError as e:
            # Poll window elapsed or a stop was requested mid-poll; either way
            # the upload was accepted and finishes processing server-side.
            return {
                "success": True,
                "pending": True,
                "activity_id": None,
                "message": str(e),
            }
        except Exception as e:
            return {"success": False, "message": f"Upload error: {e}"}

    def _wait_for_upload(self, upload_id: Any) -> Any:
        """Poll an upload until Strava finishes processing it.

        Args:
            upload_id: The id Strava returned from the initial upload POST.

        Returns:
            The created ``activity_id`` once processing succeeds.

        Raises:
            strava_api.ActivityUploadError: Strava reported a processing error.
            strava_api.UploadTimeoutError: processing did not finish within
                ``UPLOAD_WAIT_TIMEOUT`` seconds, or ``stop_event`` was set while
                waiting (the upload keeps processing on Strava's side either way).
        """
        deadline = time.time() + UPLOAD_WAIT_TIMEOUT
        while True:
            if self.stop_event is not None and self.stop_event.is_set():
                raise strava_api.UploadTimeoutError(
                    "Stop requested; upload accepted and still processing on "
                    "Strava. It should appear shortly."
                )
            status = strava_api.get_upload(self.access_token, upload_id)
            error = status.get("error")
            if error:
                raise strava_api.ActivityUploadError(str(error))
            activity_id = status.get("activity_id")
            if activity_id:
                return activity_id
            if time.time() >= deadline:
                raise strava_api.UploadTimeoutError(
                    "Upload accepted; still processing on Strava. "
                    "It should appear shortly."
                )
            time.sleep(UPLOAD_POLL_INTERVAL)

    def get_updated_tokens(self) -> tuple[str, str, int]:
        """Return current (access_token, refresh_token, expires_at)."""
        return self.access_token, self.refresh_token, self.expires_at
